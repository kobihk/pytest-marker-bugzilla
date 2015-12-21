# -*- coding: utf-8 -*-
import ConfigParser
import bugzilla
import inspect
import os
import pytest
import re
from distutils.version import LooseVersion
from functools import wraps
"""This plugin integrates pytest with bugzilla

It allows the tester to mark a test with a bug id. The test will then be skipped until the bug
status is no longer NEW, ON_DEV, or ASSIGNED.

You must set the url either at the command line or in bugzilla.cfg.

An update to this plugin brings new possibilities. You can now add multiple bugs to one test:

    @pytest.mark.bugzilla(1234, 2345, "3456")
    def test_something():
        pass

In order to skip the test, all of the specified bugs must lead to skipping. Even just one unskipped
means that the test will not be skipped.

You can also add "conditional guards", which will xfail or skip the test when condition is met:

    @pytest.mark.bugzilla(1234, skip_when=lambda bug: bug.status == "POST")
    or
    @pytest.mark.bugzilla(567, xfail_when=lambda bug, version: bug.fixed_in > version)

The guard is a function, it will receive max. 2 parameters. It depends what parameters you specify.
The parameters are `bug` which points to a specific BZ bug, and `version` which is tested product`s
version. Order or presence does not matter.

Additionally to the original parameters to this marker you can use --bugzilla-looseversion-fields.
It accepts a string of field names separated with comma. Fields specified will have their getter
function overriden that they return LooseVersion instead of string enabling easy comparison in
condition guards or inside tests.

    --bugzilla-looseversion-fields=fixed_in,target_release

Authors:
    Eric L. Sammons
    Milan Falešník
"""
_bugs_pool = {}  # Cache bugs for greater speed


def loose_func_gen(attr):
    def version_getter(self):
        return LooseVersion(re.sub(r"^[^0-9]+", "", getattr(self, attr)))
    version_getter.__name__ = attr
    return loose_func_gen


def kwargify(f):
    """Convert function having only positional args to a function taking dictionary"""
    @wraps(f)
    def wrapped(**kwargs):
        args = []
        for arg in inspect.getargspec(f).args:
            if arg not in kwargs:
                raise TypeError("Required parameter {} not found in the context!".format(arg))
            args.append(kwargs[arg])
        return f(*args)
    return wrapped


class BugWrapper(object):
    def __init__(self, bug, loose):
        self._bug = bug
        # We need to generate looseversions for simple comparison of the version params
        for loose_version_param in loose:
            setattr(self, loose_version_param, property(loose_func_gen(loose_version_param)))

    def __getattr__(self, attr):
        """Relay the query to the bug object if we did not override."""
        return getattr(self._bug, attr)


class BugzillaBugs(object):
    def __init__(self, bugzilla, loose, *bug_ids):
        self.bugzilla = bugzilla
        self.bug_ids = bug_ids
        self.loose = loose

    @property
    def bugs_gen(self):
        for bug_id in self.bug_ids:
            bug = BugWrapper(self.bugzilla.getbug(bug_id), self.loose)
            if bug_id not in _bugs_pool:
                _bugs_pool[bug_id] = bug
            yield _bugs_pool[bug_id]

    def bug(self, id):
        """Returns Bugzilla's Bug object for given ID"""
        id = int(id)
        if id in _bugs_pool:
            return _bugs_pool[id]
        for bug_id in self.bug_ids:
            if id == bug_id:
                bug = BugWrapper(self.bugzilla.getbug(bug_id), self.loose)
                _bugs_pool[bug_id] = bug
                return bug
        else:
            raise ValueError("Could not find bug with id {}".format(id))


class BugzillaHooks(object):
    def __init__(self, config, bugzilla, loose, version="0"):
        self.config = config
        self.bugzilla = bugzilla
        self.version = version
        self.loose = loose

    def pytest_runtest_setup(self, item):
        """
        Run test setup.
        :param item: test being run.
        """
        if "bugzilla" not in item.keywords:
            return
        bugs = item.funcargs["bugs"]
        will_skip = True
        skippers = []
        for bug in bugs.bugs_gen:
            if bug.status not in {"NEW", "ASSIGNED", "ON_DEV"}:
                will_skip = False
            else:
                skippers.append(bug)
        url = "{0}?id=".format(
            self.bugzilla.url.replace("xmlrpc.cgi", "show_bug.cgi"),
        )

        if will_skip:
            pytest.skip(
                "Skipping this test because all of these assigned bugs:\n{}".format(
                    "\n".join(
                        [
                            "{} {}{}".format(
                                bug.status, url, bug.id
                            )
                            for bug
                            in skippers])))

        marker = item.get_marker('bugzilla')
        xfail = kwargify(marker.kwargs.get("xfail_when", lambda: False))
        skip = kwargify(marker.kwargs.get("skip_when", lambda: False))
        if skip:
            self.evaluate_skip(skip, bugs)
        if xfail:
            xfailed = self.evaluate_xfail(xfail, bugs)
            if xfailed:
                item.add_marker(
                    pytest.mark.xfail(
                        reason="xfailing due to bugs: {}".format(
                            ", ".join(
                                map(
                                    lambda bug: "{}{}".format(url, str(bug.id)), xfailed)))))

    def evaluate_skip(self, skip, bugs):
        for bug in bugs.bugs_gen:
            context = {"bug": bug, "version": LooseVersion(self.version)}
            if skip(**context):
                pytest.skip("Skipped due to a given condition!")

    def evaluate_xfail(self, xfail, bugs):
        results = []
        for bug in bugs.bugs_gen:
            context = {"bug": bug, "version": LooseVersion(self.version)}
            if xfail(**context):
                results.append(bug)
        return results

    def pytest_collection_modifyitems(self, session, config, items):
        reporter = config.pluginmanager.getplugin("terminalreporter")
        reporter.write("Checking for bugzilla-related tests\n", bold=True)
        cache = {}
        for i, item in enumerate(filter(lambda i: i.get_marker("bugzilla") is not None, items)):
            marker = item.get_marker('bugzilla')
            bugs = tuple(sorted(set(map(int, marker.args))))  # (O_O) for caching
            if bugs not in cache:
                reporter.write(".")
                cache[bugs] = BugzillaBugs(self.bugzilla, self.loose, *bugs)
            item.funcargs["bugs"] = cache[bugs]
        reporter.write("\nChecking for bugzilla-related tests has finished\n", bold=True)
        reporter.write("{} bug marker sets found.\n".format(len(cache)), bold=True)


def pytest_addoption(parser):
    """
    Add a options section to py.test --help for bugzilla integration.
    Parse configuration file, bugzilla.cfg and / or the command line options
    passed.

    :param parser: Command line options.
    """
    group = parser.getgroup('Bugzilla integration')
    group.addoption('--bugzilla',
                    action='store_true',
                    default=False,
                    dest='bugzilla',
                    help='Enable Bugzilla support.')

    config = ConfigParser.ConfigParser()
    if os.path.exists('bugzilla.cfg'):
        config.read('bugzilla.cfg')
    else:
        return

    group.addoption('--bugzilla-url',
                    action='store',
                    dest='bugzilla_url',
                    default=config.get('DEFAULT', 'bugzilla_url'),
                    metavar='url',
                    help='Overrides the xmlrpc url for bugzilla found in bugzilla.cfg.')
    group.addoption('--bugzilla-user',
                    action='store',
                    dest='bugzilla_username',
                    default=config.get('DEFAULT', 'bugzilla_username'),
                    metavar='username',
                    help='Overrides the bugzilla username in bugzilla.cfg.')
    group.addoption('--bugzilla-password',
                    action='store',
                    dest='bugzilla_password',
                    default=config.get('DEFAULT', 'bugzilla_password'),
                    metavar='password',
                    help='Overrides the bugzilla password in bugzilla.cfg.')
    group.addoption('--bugzilla-project-version',
                    action='store',
                    dest='bugzilla_version',
                    default=config.get('DEFAULT', 'bugzilla_version'),
                    metavar='version',
                    help='Overrides the project version in bugzilla.cfg.')
    group.addoption('--bugzilla-looseversion-fields',
                    action='store',
                    dest='bugzilla_loose',
                    default=config.get('DEFAULT', 'bugzilla_loose'),
                    metavar='loose',
                    help='Overrides the project loose in bugzilla.cfg.')


def pytest_configure(config):
    """
    If bugzilla is neabled, setup a session
    with bugzilla_url.

    :param config: configuration object
    """
    config.addinivalue_line("markers", """bugzilla(*bug_ids, **guards): Bugzilla integration""")
    if config.getvalue("bugzilla") and all([config.getvalue('bugzilla_url'),
                                            config.getvalue('bugzilla_username'),
                                            config.getvalue('bugzilla_password'),
                                            config.getvalue('bugzilla_version'),
                                            config.getvalue('bugzilla_loose')]):
        url = config.getvalue('bugzilla_url')
        user = config.getvalue('bugzilla_username')
        password = config.getvalue('bugzilla_password')
        version = config.getvalue('bugzilla_version')
        loose = [x.strip() for x in config.getvalue('bugzilla_loose').strip().split(",")]
        if len(loose) == 1 and len(loose[0]) == 0:
            loose = []

        bz = bugzilla.Bugzilla(url=url, user=user, password=password)

        my = BugzillaHooks(config, bz, loose, version)
        assert config.pluginmanager.register(my, "bugzilla_helper")
