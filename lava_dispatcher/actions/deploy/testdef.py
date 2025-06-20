# Copyright (C) 2014 Linaro Limited
#
# Author: Neil Williams <neil.williams@linaro.org>
#
# SPDX-License-Identifier: GPL-2.0-or-later
from __future__ import annotations

import hashlib
import os
import re
import shutil
from typing import TYPE_CHECKING

from lava_common.constants import DEFAULT_TESTDEF_NAME_CLASS, DISPATCHER_DOWNLOAD_DIR
from lava_common.decorators import nottest
from lava_common.exceptions import InfrastructureError, JobError, LAVABug, TestError
from lava_common.yaml import yaml_safe_dump, yaml_safe_load
from lava_dispatcher.action import Action, Pipeline
from lava_dispatcher.utils.compression import untar_file
from lava_dispatcher.utils.vcs import GitHelper

if TYPE_CHECKING:
    from lava_dispatcher.job import Job


@nottest
def identify_test_definitions(test_info, namespace):
    """
    Iterates through the job parameters to identify all the test definitions,
    including those involved in repeat actions.
    """
    # All test definitions are deployed in each deployment - TestDefinitionAction needs to only run relevant ones.
    test_list = []
    for test in test_info.get(namespace, []):
        if "definitions" in test["parameters"]:
            testdefs = test["parameters"]["definitions"]
            needs_delay = test["class"].needs_character_delay(test["parameters"])
            for testdef in testdefs:
                testdef["needs_character_delay"] = needs_delay
            test_list.append(testdefs)
    return test_list


@nottest
def get_test_action_namespaces(parameters=None):
    """Iterates through the job parameters to identify all the test action
    namespaces."""
    test_namespaces = []
    for action in parameters["actions"]:
        if "test" in action:
            if action["test"].get("namespace"):
                test_namespaces.append(action["test"]["namespace"])
    repeat_list = [
        action["repeat"] for action in parameters["actions"] if "repeat" in action
    ]
    if repeat_list:
        test_namespaces.extend(
            [
                action["test"]["namespace"]
                for action in repeat_list[0]["actions"]
                if "test" in action and action["test"].get("namespace")
            ]
        )
    return test_namespaces


# pylint:disable=too-many-public-methods,too-many-instance-attributes,too-many-locals,too-many-branches


class RepoAction(Action):
    name = "repo-action"
    description = "apply tests to the test image"
    summary = "repo base class"

    def __init__(self, job: Job):
        super().__init__(job)
        self.vcs = None
        self.runner = None
        self.uuid = None

    @classmethod
    def select(cls, repo_type):
        candidates = cls.__subclasses__()
        willing = [c for c in candidates if c.accepts(repo_type)]

        if not willing:
            raise JobError(
                "No testdef_repo handler is available for the given repository type"
                " '%s'." % repo_type
            )

        # higher priority first
        willing.sort(key=lambda x: x.priority, reverse=True)
        return willing[0]

    def validate(self):
        if "test_name" not in self.parameters:
            self.errors = "Unable to determine test_name"
            return
        if not isinstance(self, InlineRepoAction) and not isinstance(
            self, UrlRepoAction
        ):
            if self.vcs is None:
                raise LAVABug(
                    "RepoAction validate called super without setting the vcs"
                )
            if not os.path.exists(self.vcs.binary):
                self.errors = "%s is not installed on the dispatcher." % self.vcs.binary
        super().validate()

        # FIXME: unused
        # list of levels involved in the repo actions for this overlay
        uuid_list = self.get_namespace_data(
            action="repo-action", label="repo-action", key="uuid-list"
        )
        if uuid_list:
            if self.uuid not in uuid_list:
                uuid_list.append(self.uuid)
        else:
            uuid_list = [self.uuid]
        self.set_namespace_data(
            action="repo-action", label="repo-action", key="uuid-list", value=uuid_list
        )

    def run(self, connection, max_end_time):
        """
        The base class run() currently needs to run after the mount operation, i.e. as part of run() so that
        the path can be correctly set when writing the overlay.
        Better approach will be to create the entire overlay without mounting and then
        unpack an overlay.tgz after mounting.
        """
        connection = super().run(connection, max_end_time)

        location = self.get_namespace_data(
            action="test", label="shared", key="location"
        )
        lava_test_results_dir = self.get_namespace_data(
            action="test", label="results", key="lava_test_results_dir"
        )
        if not lava_test_results_dir:
            raise LAVABug("Unable to identify top level test shell directory")
        self.logger.debug("Using %s at stage %s", lava_test_results_dir, self.stage)
        if not location:
            raise LAVABug("Missing lava overlay location")
        if not os.path.exists(location):
            raise LAVABug("Overlay location does not exist")

        # runner_path is the path to read and execute from to run the tests after boot
        lava_test_results_dir = self.get_constant("lava_test_results_dir", "posix")
        args = self.parameters
        runner_path = os.path.join(
            lava_test_results_dir % self.job.job_id,
            str(self.stage),
            "tests",
            args["test_name"],
        )
        self.set_namespace_data(
            action="uuid", label="runner_path", key=args["test_name"], value=runner_path
        )
        # the location written into the lava-test-runner.conf (needs a line ending)
        self.runner = "%s\n" % runner_path

        overlay_base = self.get_namespace_data(
            action="test", label="test-definition", key="overlay_dir"
        )
        overlay_path = os.path.join(
            overlay_base, str(self.stage), "tests", args["test_name"]
        )
        self.set_namespace_data(
            action="uuid",
            label="overlay_path",
            key=args["test_name"],
            value=overlay_path,
        )
        self.set_namespace_data(
            action="test",
            label=self.uuid,
            key="repository",
            value=self.parameters["repository"],
        )
        self.set_namespace_data(
            action="test", label=self.uuid, key="path", value=self.parameters["path"]
        )
        revision = self.parameters.get("revision")
        if revision:
            self.set_namespace_data(
                action="test", label=self.uuid, key="revision", value=revision
            )

        return connection

    def store_testdef(self, testdef, vcs_name, commit_id=None):
        """
        Allows subclasses to pass in the parsed testdef after the repository has been obtained
        and the specified YAML file can be read.
        The Connection uses the data in the test dict to retrieve the supplied parse pattern
        and the fixup dictionary, if specified.
        The Connection stores raw results in the same test dict.
        The main TestAction can then process the results.
        """
        val = {
            "os": testdef["metadata"].get("os", ""),
            "devices": testdef["metadata"].get("devices", ""),
            "environment": testdef["metadata"].get("environment", ""),
            "branch_vcs": vcs_name,
            "project_name": testdef["metadata"]["name"],
        }

        if commit_id is not None:
            val["commit_id"] = commit_id
            self.set_namespace_data(
                action="test", label=self.uuid, key="commit-id", value=str(commit_id)
            )

        self.set_namespace_data(
            action="test", label=self.uuid, key="testdef_metadata", value=val
        )
        if "parse" in testdef:
            pattern = testdef["parse"].get("pattern", "")
            fixup = testdef["parse"].get("fixupdict", "")
            ret = {"testdef_pattern": {"pattern": pattern, "fixupdict": fixup}}
        else:
            ret = None
        self.set_namespace_data(
            action="test", label=self.uuid, key="testdef_pattern", value=ret
        )
        self.logger.debug("uuid=%s testdef=%s", self.uuid, ret)


class GitRepoAction(RepoAction):
    """
    Each repo action is for a single repository,
    tests using multiple repositories get multiple
    actions.
    """

    priority = 1
    name = "git-repo-action"
    description = "apply git repository of tests to the test image"
    summary = "clone git test repo"

    def validate(self):
        if "repository" not in self.parameters:
            self.errors = "Git repository not specified in job definition"
        if "path" not in self.parameters:
            self.errors = "Path to YAML file not specified in the job definition"
        if not self.valid:
            return
        self.vcs = GitHelper(self.parameters["repository"])
        super().validate()

    @classmethod
    def accepts(cls, repo_type):
        return repo_type == "git"

    def run(self, connection, max_end_time):
        """
        Clones the git repo into a directory name constructed from the mount_path,
        lava-$hostname prefix, tests, $index_$test_name elements. e.g.
        /tmp/tmp.234Ga213/lava-kvm01/tests/3_smoke-tests-basic
        Also updates some basic metadata about the test definition.
        """
        # use the base class to populate the runner_path and overlay_path data into the context
        connection = super().run(connection, max_end_time)

        # NOTE: the runner_path dir must remain empty until after the VCS clone, so let the VCS clone create the final dir
        runner_path = self.get_namespace_data(
            action="uuid", label="overlay_path", key=self.parameters["test_name"]
        )

        if os.path.exists(runner_path) and os.listdir(runner_path) == []:
            raise LAVABug(
                "Directory already exists and is not empty - duplicate Action?"
            )

        # Clear the data
        if os.path.exists(runner_path):
            shutil.rmtree(runner_path)

        self.logger.info("Fetching tests from %s", self.parameters["repository"])

        # Get the branch if specified.
        branch = self.parameters.get("branch")

        # Set shallow to False if revision is specified.
        # Otherwise default to True if not specified as a parameter.
        revision = self.parameters.get("revision")
        shallow = False
        if not revision:
            shallow = self.parameters.get("shallow", True)

        commit_id = self.vcs.clone(
            runner_path,
            shallow=shallow,
            revision=revision,
            branch=branch,
            history=self.parameters.get("history", True),
        )
        if commit_id is None:
            raise InfrastructureError(
                "Unable to get test definition from %s (%s)"
                % (self.vcs.binary, self.parameters)
            )
        self.results = {
            "commit": commit_id,
            "repository": self.parameters["repository"],
            "path": self.parameters["path"],
        }

        # now read the YAML to create a testdef dict to retrieve metadata
        yaml_file = os.path.join(runner_path, self.parameters["path"])
        self.logger.debug("Tests stored (tmp) in %s", yaml_file)
        try:
            with open(yaml_file) as test_file:
                testdef = yaml_safe_load(test_file)
        except OSError as exc:
            raise JobError(
                "Unable to open test definition '%s': %s"
                % (self.parameters["path"], str(exc))
            )

        # set testdef metadata in base class
        self.store_testdef(testdef, "git", commit_id)

        return connection


class InlineRepoAction(RepoAction):
    priority = 1
    name = "inline-repo-action"
    description = "apply inline test definition to the test image"
    summary = "extract inline test definition"

    def validate(self):
        if "repository" not in self.parameters:
            self.errors = "Inline definition not specified in job definition"
        if not isinstance(self.parameters["repository"], dict):
            self.errors = "Invalid inline definition in job definition"
        if not self.valid:
            return
        super().validate()

    @classmethod
    def accepts(cls, repo_type):
        return repo_type == "inline"

    def run(self, connection, max_end_time):
        """
        Extract the inlined test definition and dump it onto the target image
        """
        # use the base class to populate the runner_path and overlay_path data into the context
        connection = super().run(connection, max_end_time)

        # NOTE: the runner_path dir must remain empty until after the VCS clone, so let the VCS clone create the final dir
        runner_path = self.get_namespace_data(
            action="uuid", label="overlay_path", key=self.parameters["test_name"]
        )

        # Grab the inline test definition
        testdef = self.parameters["repository"]
        sha1 = hashlib.sha1()  # nosec - not used for cryptography

        # Dump the test definition and compute the sha1
        yaml_file = os.path.join(runner_path, self.parameters["path"])
        yaml_dirname = os.path.dirname(yaml_file)
        if yaml_dirname != "":
            os.makedirs(os.path.join(runner_path, yaml_dirname))
        with open(yaml_file, "w") as test_file:
            data = yaml_safe_dump(testdef)
            sha1.update(data.encode("utf-8"))
            test_file.write(data)

        # set testdef metadata in base class
        self.store_testdef(self.parameters["repository"], "inline")
        return connection


class UrlRepoAction(RepoAction):
    priority = 1
    name = "url-repo-action"
    description = "apply a single test file to the test image"
    summary = "download file test"

    def __init__(self, job: Job):
        super().__init__(job)
        self.tmpdir = None  # FIXME: needs to be a /mntpoint/lava-%hostname/ directory.
        self.testdef = None

    @classmethod
    def accepts(cls, repo_type):
        return repo_type == "url"

    def validate(self):
        if "repository" not in self.parameters:
            self.errors = "Url repository not specified in job definition"
        if "path" not in self.parameters:
            self.errors = "Path to YAML file not specified in the job definition"
        super().validate()

    def populate(self, parameters):
        # Import the module here to avoid cyclic import.
        from lava_dispatcher.actions.deploy.download import DownloaderAction

        # Add 'url' as an alias to 'repository'. DownloaderAction requires an
        # 'url' key.
        params = dict(**self.parameters, url=self.parameters["repository"])

        self.download_dir = self.mkdtemp()
        self.action_key = "url_repo"
        self.internal_pipeline = Pipeline(parent=self, job=self.job, parameters=params)
        self.internal_pipeline.add_action(
            DownloaderAction(
                self.job, self.action_key, self.download_dir, params=params
            )
        )

    def run(self, connection, max_end_time):
        """Download the provided test definition file into tmpdir."""
        super().run(connection, max_end_time)
        runner_path = self.get_namespace_data(
            action="uuid", label="overlay_path", key=self.parameters["test_name"]
        )

        fname = self.get_namespace_data(
            action="download-action", label=self.action_key, key="file"
        )
        self.logger.debug("Runner path : %s", runner_path)
        if os.path.exists(runner_path) and os.listdir(runner_path) == []:
            raise LAVABug(
                "Directory already exists and is not empty - duplicate Action?"
            )

        self.logger.info("Untar tests from file %s to directory %s", fname, runner_path)
        untar_file(fname, runner_path)

        # now read the YAML to create a testdef dict to retrieve metadata
        yaml_file = os.path.join(runner_path, self.parameters["path"])
        self.logger.debug("Tests stored (tmp) in %s", yaml_file)

        try:
            with open(yaml_file) as test_file:
                testdef = yaml_safe_load(test_file)
        except OSError as exc:
            raise JobError(
                "Unable to open test definition '%s': %s"
                % (self.parameters["path"], str(exc))
            )
        # set testdef metadata in base class
        self.store_testdef(testdef, "url")

        return connection


@nottest
class TestDefinitionAction(Action):
    name = "test-definition"
    description = "load test definitions into image"
    summary = "loading test definitions"

    def __init__(self, job: Job):
        """
        The TestDefinitionAction installs each test definition into
        the overlay. It does not execute the scripts in the test
        definition, that is the job of the TestAction class.
        One TestDefinitionAction handles all test definitions for
        the current job.
        In addition, a TestOverlayAction is added to the pipeline
        to handle parts of the overlay which are test definition dependent.
        """
        super().__init__(job)
        self.test_list = None
        self.stages = 0
        self.run_levels = {}

    def populate(self, parameters):
        """
        Each time a test definition is processed by a handler, a new set of
        overlay files are needed, based on that test definition. Basic overlay
        files are created by TestOverlayAction. More complex scripts like the
        install:deps script and the main run script have custom Actions.
        """
        index = []
        self.pipeline = Pipeline(parent=self, job=self.job, parameters=parameters)
        self.test_list = identify_test_definitions(
            self.job.test_info, parameters["namespace"]
        )
        if self.test_list:
            self.set_namespace_data(
                action=self.name,
                label=self.name,
                key="test_list",
                value=self.test_list,
                parameters=parameters,
            )
        for testdefs in self.test_list:
            for testdef in testdefs:
                # namespace support allows only running the install steps for the relevant
                # deployment as the next deployment could be a different OS.
                handler = RepoAction.select(testdef["from"])(self.job)

                # set the full set of job YAML parameters for this handler as handler parameters.
                handler.job = self.job
                handler.parameters = testdef
                # store the correct test_name before appending to the local index
                handler.parameters["test_name"] = "%s_%s" % (
                    len(index),
                    handler.parameters["name"],
                )
                self.pipeline.add_action(handler)
                # a genuinely unique ID based on the *database* JobID and
                # pipeline level for reproducibility and tracking -
                # {DB-JobID}_{PipelineLevel}, e.g. 15432.0_3.5.4
                handler.uuid = "%s_%s" % (self.job.job_id, handler.level)
                handler.stage = self.stages
                self.run_levels[testdef["name"]] = self.stages

                # copy details into the overlay, one per handler but the same class each time.
                overlay = TestOverlayAction(self.job)
                overlay.job = self.job
                overlay.parameters = testdef
                overlay.parameters["test_name"] = handler.parameters["test_name"]
                overlay.test_uuid = handler.uuid

                # add install handler - uses job parameters
                installer = TestInstallAction(self.job)
                installer.job = self.job
                installer.parameters = testdef
                installer.parameters["test_name"] = handler.parameters["test_name"]
                installer.test_uuid = handler.uuid

                # add runsh handler - uses job parameters
                runsh = TestRunnerAction(self.job)
                runsh.job = self.job
                runsh.parameters = testdef
                runsh.parameters["test_name"] = handler.parameters["test_name"]
                runsh.test_uuid = handler.uuid

                index.append(handler.parameters["name"])

                # add overlay handlers to the pipeline
                self.pipeline.add_action(overlay)
                self.pipeline.add_action(installer)
                self.pipeline.add_action(runsh)
                self.set_namespace_data(
                    action="test-definition",
                    label="test-definition",
                    key="testdef_index",
                    value=index,
                    parameters=parameters,
                )
            self.stages += 1

    def validate(self):
        """
        TestDefinitionAction is part of the overlay and therefore part of the deployment -
        the internal pipeline then looks inside the job definition for details of the tests to deploy.
        Jobs with no test actions defined (empty test_list) are explicitly allowed.
        """
        if not self.job:
            self.errors = "missing job object"
            return
        if "actions" not in self.job.parameters:
            self.errors = "No actions defined in job parameters"
            return
        if not self.test_list:
            return

        exp = re.compile(DEFAULT_TESTDEF_NAME_CLASS)
        for testdefs in self.test_list:
            for testdef in testdefs:
                if "parameters" in testdef:  # optional
                    if not isinstance(testdef["parameters"], dict):
                        self.errors = "Invalid test definition parameters"
                if "from" not in testdef:
                    self.errors = "missing 'from' field in test definition %s" % testdef
                if "name" not in testdef:
                    self.errors = "missing 'name' field in test definition %s" % testdef
                else:
                    res = exp.match(testdef["name"])
                    if not res:
                        self.errors = (
                            "Invalid characters found in test definition name: %s"
                            % testdef["name"]
                        )
        super().validate()
        for testdefs in self.test_list:
            for testdef in testdefs:
                try:
                    RepoAction.select(testdef["from"])(self.job)
                except JobError as exc:
                    self.errors = str(exc)

    def run(self, connection, max_end_time):
        """
        Creates the list of test definitions for this Test

        :param connection: Connection object, if any.
        :param max_end_time: remaining time before block timeout.
        :return: the received Connection.
        """
        location = self.get_namespace_data(
            action="test", label="shared", key="location"
        )
        lava_test_results_dir = self.get_namespace_data(
            action="test", label="results", key="lava_test_results_dir"
        )
        if not location:
            raise LAVABug("Missing lava overlay location")
        if not os.path.exists(location):
            raise LAVABug("Unable to find overlay location")
        self.logger.info("Loading test definitions")

        # overlay_path is the location of the files before boot
        overlay_base = os.path.abspath("%s/%s" % (location, lava_test_results_dir))
        self.set_namespace_data(
            action="test",
            label="test-definition",
            key="overlay_dir",
            value=overlay_base,
        )

        connection = super().run(connection, max_end_time)

        self.logger.info("Creating lava-test-runner.conf files")
        for stage in range(self.stages):
            path = "%s/%s" % (overlay_base, stage)
            self.logger.debug(
                "Using lava-test-runner path: %s for stage %d", path, stage
            )
            with open(
                "%s/%s/lava-test-runner.conf" % (overlay_base, stage), "a"
            ) as runner_conf:
                for handler in self.pipeline.actions:
                    if isinstance(handler, RepoAction) and handler.stage == stage:
                        self.logger.debug("- %s", handler.parameters["test_name"])
                        runner_conf.write(handler.runner)

        return connection


@nottest
class TestOverlayAction(Action):
    name = "test-overlay"
    description = "overlay test support files onto image"
    summary = "applying LAVA test overlay"

    def __init__(self, job: Job):
        """
        TestOverlayAction is a simple helper to do the same routine boilerplate
        for every RepoAction, tweaking the data for the specific parameters of
        the RepoAction. It also defines the handle_parameters call which other
        custom overlay actions may require.

        Each test definition handler has a separate overlay added to the pipeline,
        so the overlay has access to the same parameters as the handler and is
        always executed immediately after the relevant handler.
        """
        super().__init__(job)
        self.test_uuid = None  # Match the overlay to the handler

    def validate(self):
        super().validate()
        if "path" not in self.parameters:
            self.errors = "Missing path in parameters"

    def handle_parameters(self, testdef):
        def raise_if_not_dict(data, key):
            if not isinstance(data[key], dict):
                raise TestError(
                    "Test definition item '%s' should be a dictionary" % key
                )

        ret_val = ["###default parameters from test definition###\n"]
        if "params" in testdef:
            raise_if_not_dict(testdef, "params")
            for def_param_name, def_param_value in list(testdef["params"].items()):
                if def_param_value is None:
                    def_param_value = ""
                ret_val.append("%s='%s'\n" % (def_param_name, def_param_value))
        if "parameters" in testdef:
            raise_if_not_dict(testdef, "parameters")
            for def_param_name, def_param_value in list(testdef["parameters"].items()):
                if def_param_value is None:
                    def_param_value = ""
                ret_val.append("%s='%s'\n" % (def_param_name, def_param_value))
        ret_val.append("######\n")
        # inject the parameters that were set in job submission.
        ret_val.append("###test parameters from job submission###\n")
        if "parameters" in self.parameters:
            raise_if_not_dict(self.parameters, "parameters")
            # turn a string into a local variable.
            for param_name, param_value in list(self.parameters["parameters"].items()):
                if param_value is None:
                    param_value = ""
                ret_val.append("%s='%s'\n" % (param_name, param_value))
                self.logger.debug("%s='%s'", param_name, param_value)
        if "params" in self.parameters:
            raise_if_not_dict(self.parameters, "params")
            # turn a string into a local variable.
            for param_name, param_value in list(self.parameters["params"].items()):
                if param_value is None:
                    param_value = ""
                ret_val.append("%s='%s'\n" % (param_name, param_value))
                self.logger.debug("%s='%s'", param_name, param_value)
        ret_val.append("######\n")
        return ret_val

    def run(self, connection, max_end_time):
        connection = super().run(connection, max_end_time)
        runner_path = self.get_namespace_data(
            action="uuid", label="overlay_path", key=self.parameters["test_name"]
        )

        # now read the YAML to create a testdef dict to retrieve metadata
        yaml_file = os.path.join(runner_path, self.parameters["path"])
        try:
            with open(yaml_file) as test_file:
                testdef = yaml_safe_load(test_file)
        except OSError as exc:
            raise JobError(
                "Unable to open test definition '%s': %s"
                % (self.parameters["path"], str(exc))
            )

        # FIXME: change lava-test-runner to accept a variable instead of duplicating the YAML?
        with open("%s/testdef.yaml" % runner_path, "w") as run_file:
            yaml_safe_dump(testdef, run_file)

        # write out the UUID of each test definition.
        # FIXME: is this necessary any longer?
        with open("%s/uuid" % runner_path, "w") as uuid:
            uuid.write(self.test_uuid)

        # FIXME: does this match old-world test-shell & is it needed?
        with open("%s/testdef_metadata" % runner_path, "w") as metadata:
            content = self.get_namespace_data(
                action="test", label=self.test_uuid, key="testdef_metadata"
            )
            metadata.write(yaml_safe_dump(content))

        # Need actions for the run.sh script (calling parameter support in base class)
        # and install script (also calling parameter support here.)
        # this run then only does the incidental files.

        self.results = {
            "uuid": self.test_uuid,
            "name": self.parameters["name"],
            "path": self.parameters["path"],
            "from": self.parameters["from"],
        }
        if self.parameters["from"] != "inline":
            self.results["repository"] = self.parameters["repository"]
        return connection


@nottest
class TestInstallAction(TestOverlayAction):
    name = "test-install-overlay"
    description = "overlay dependency installation support files onto image"
    summary = "applying LAVA test install scripts"

    def __init__(self, job: Job):
        """
        This Action will need a run check that the file does not exist
        and then it will create it.
        The parameter action will need a run check that the file does
        exist and then it will append to it.
        TestOverlayAction will then add TestInstallAction to an
        internal pipeline followed by TestParameterAction then
        run the pipeline at the start of the TestOverlayAction
        run step.
        """
        super().__init__(job)
        self.test_uuid = None  # Match the overlay to the handler
        self.skip_list = [
            "keys",
            "sources",
            "deps",
            "steps",
            "git-repos",
            "all",
        ]  # keep 'all' as the last item
        self.skip_options = []
        self.param_keys = ["url", "destination", "branch"]

    def validate(self):
        if "skip_install" in self.parameters:
            if set(self.parameters["skip_install"]) - set(self.skip_list):
                self.errors = "Unrecognised skip_install value"
            if "all" in self.parameters["skip_install"]:
                self.skip_options = self.skip_list[:-1]  # without last item
            else:
                self.skip_options = self.parameters["skip_install"]
        super().validate()

    def _lookup_params(self, lookup_key, variable, testdef):
        # lookup_key 'branch'
        # variable ODP_BRANCH which has a value in the parameters of "master"
        ret = variable
        if not variable or not lookup_key or not testdef:
            return None
        if not isinstance(testdef, dict) or not isinstance(lookup_key, str):
            return None
        if lookup_key not in self.param_keys:
            return variable
        # prioritise the value in the testdef
        if "params" in testdef:
            if variable in testdef["params"]:
                self.logger.info(
                    "Substituting test definition parameter '%s' with value '%s'.",
                    variable,
                    self.parameters["parameters"][variable],
                )
                ret = testdef["params"][variable]
        # now override with a value from the job, if any
        if "parameters" in self.parameters:
            if variable in self.parameters["parameters"]:
                self.logger.info(
                    "Overriding job parameter '%s' with value '%s'.",
                    variable,
                    self.parameters["parameters"][variable],
                )
                ret = self.parameters["parameters"][variable]
        return ret

    def install_git_repos(self, testdef, runner_path):
        repos = testdef["install"].get("git-repos", [])
        for repo in repos:
            commit_id = None
            if isinstance(repo, str):
                # tests should expect git clone https://path/dir/repo.git to create ./repo/
                subdir = repo.replace(
                    ".git", "", len(repo) - 1
                )  # drop .git from the end, if present
                dest_path = os.path.join(runner_path, os.path.basename(subdir))
                commit_id = GitHelper(repo).clone(dest_path)
            elif isinstance(repo, dict):
                # TODO: We use 'skip_by_default' to check if this
                # specific repository should be skipped. The value
                # for 'skip_by_default' comes from job parameters.
                url = repo.get("url", "")
                url = self._lookup_params("url", url, testdef)
                branch = repo.get("branch")
                branch = self._lookup_params("branch", branch, testdef)
                if not url:
                    raise TestError(
                        "Invalid git-repos dictionary in install definition."
                    )
                subdir = url.replace(
                    ".git", "", len(url) - 1
                )  # drop .git from the end, if present
                destination = repo.get("destination", os.path.basename(subdir))
                destination = self._lookup_params("destination", destination, testdef)
                if destination:
                    dest_path = os.path.join(runner_path, destination)
                    if os.path.abspath(runner_path) != os.path.dirname(dest_path):
                        raise JobError(
                            "Destination path is unacceptable %s" % destination
                        )
                    if os.path.exists(dest_path):
                        raise TestError(
                            "Cannot mix string and url forms for the same repository."
                        )
                    commit_id = GitHelper(url).clone(dest_path, branch=branch)
            else:
                raise TestError("Unrecognised git-repos block.")
            if commit_id is None:
                raise JobError("Unable to clone %s" % str(repo))

    def run(self, connection, max_end_time):
        connection = super().run(connection, max_end_time)
        runner_path = self.get_namespace_data(
            action="uuid", label="overlay_path", key=self.parameters["test_name"]
        )

        # now read the YAML to create a testdef dict to retrieve metadata
        yaml_file = os.path.join(runner_path, self.parameters["path"])
        try:
            with open(yaml_file) as test_file:
                testdef = yaml_safe_load(test_file)
        except OSError as exc:
            raise JobError(
                "Unable to open test definition '%s': %s"
                % (self.parameters["path"], str(exc))
            )

        if "install" not in testdef:
            self.results = {"skipped %s" % self.name: self.test_uuid}
            return

        filename = "%s/install.sh" % runner_path
        content = self.handle_parameters(testdef)

        # TODO: once the migration is complete, design a better way to do skip_install support.
        with open(filename, "w") as install_file:
            for line in content:
                install_file.write(line)
            if "keys" not in self.skip_options:
                sources = testdef["install"].get("keys", [])
                for src in sources:
                    install_file.write("lava-add-keys %s" % src)
                    install_file.write("\n")

            if "sources" not in self.skip_options:
                sources = testdef["install"].get("sources", [])
                for src in sources:
                    install_file.write("lava-add-sources %s" % src)
                    install_file.write("\n")

            if "deps" not in self.skip_options:
                # generic dependencies - must be named the same across all distros
                # supported by the testdef
                deps = testdef["install"].get("deps", [])

                # distro-specific dependencies
                if (
                    "deployment_data" in self.parameters
                    and "distro" in self.parameters["deployment_data"]
                ):
                    deps = deps + testdef["install"].get(
                        "deps-" + self.parameters["deployment_data"]["distro"], []
                    )

                if deps:
                    install_file.write("lava-install-packages ")
                    for dep in deps:
                        install_file.write("%s " % dep)
                    install_file.write("\n")

            if "steps" not in self.skip_options:
                steps = testdef["install"].get("steps", [])
                if steps:
                    # Allow install steps to use the git-repo directly
                    # fake up the directory as it will be after the overlay is applied
                    # os.path.join refuses if the directory does not exist on the dispatcher
                    base = len(DISPATCHER_DOWNLOAD_DIR.split("/")) + 2
                    # skip job_id/action-tmpdir/ as well
                    install_dir = "/" + "/".join(runner_path.split("/")[base:])
                    install_file.write("cd %s\n" % install_dir)
                    install_file.write("pwd\n")
                    for cmd in steps:
                        install_file.write("%s\n" % cmd)

            if "git-repos" not in self.skip_options:
                self.install_git_repos(testdef, runner_path)

        self.results = {"uuid": self.test_uuid}
        return connection


class TestRunnerAction(TestOverlayAction):
    # This name is used to tally the submitted definitions
    # to the definitions which actually reported results.
    # avoid changing the self.name of this class.
    name = "test-runscript-overlay"
    description = "overlay run script onto image"
    summary = "applying LAVA test run script"

    def __init__(self, job: Job):
        super().__init__(job)
        self.testdef_levels = (
            {}
        )  # allow looking up the testname from the level of this action

    def validate(self):
        super().validate()
        testdef_index = self.get_namespace_data(
            action="test-definition", label="test-definition", key="testdef_index"
        )
        if not testdef_index:
            self.errors = "Unable to identify test definition index"
            return
        if len(testdef_index) != len(set(testdef_index)):
            self.errors = "Test definition names need to be unique."
        # convert from testdef_index {0: 'smoke-tests', 1: 'singlenode-advanced'}
        # to self.testdef_levels {'1.3.4.1': '0_smoke-tests', ...}
        for count, name in enumerate(testdef_index):
            if self.parameters["name"] == name:
                self.testdef_levels[self.level] = "%s_%s" % (count, name)
        if not self.testdef_levels:
            self.errors = "Unable to identify test definition names"
        current = self.get_namespace_data(
            action=self.name, label=self.name, key="testdef_levels"
        )
        if current:
            current.update(self.testdef_levels)
        else:
            current = self.testdef_levels
        self.set_namespace_data(
            action=self.name, label=self.name, key="testdef_levels", value=current
        )

    def run(self, connection, max_end_time):
        connection = super().run(connection, max_end_time)
        runner_path = self.get_namespace_data(
            action="uuid", label="overlay_path", key=self.parameters["test_name"]
        )

        # now read the YAML to create a testdef dict to retrieve metadata
        yaml_file = os.path.join(runner_path, self.parameters["path"])
        try:
            with open(yaml_file) as test_file:
                testdef = yaml_safe_load(test_file)
        except OSError as exc:
            raise JobError(
                "Unable to open test definition '%s': %s"
                % (self.parameters["path"], str(exc))
            )

        self.logger.debug("runner path: %s test_uuid %s", runner_path, self.test_uuid)
        filename = "%s/run.sh" % runner_path
        content = self.handle_parameters(testdef)

        # the 'lava' testdef name is reserved
        if self.parameters["name"] == "lava":
            raise TestError('The "lava" test definition name is reserved.')

        lava_signal = self.parameters.get("lava-signal", "stdout")

        testdef_levels = self.get_namespace_data(
            action=self.name, label=self.name, key="testdef_levels"
        )
        with open(filename, "a") as runsh:
            for line in content:
                runsh.write(line)
            runsh.write("set -e\n")
            runsh.write("set -x\n")
            # use the testdef_index value for the testrun name to handle repeats at source
            runsh.write("export TESTRUN_ID=%s\n" % testdef_levels[self.level])
            runsh.write(
                "cd %s\n"
                % self.get_namespace_data(
                    action="uuid", label="runner_path", key=self.parameters["test_name"]
                )
            )
            runsh.write("UUID=`cat uuid`\n")
            runsh.write("set +x\n")
            needs_delay = self.parameters.get("needs_character_delay")
            delay = self.job.device.get("character_delays", {}).get("test", 0)
            if needs_delay and delay:
                self.logger.debug(
                    f"A delay of {delay} milliseconds will be used for sending result signals"
                )
                delay = float(delay) / 1000
                runsh.write(f"export CHARACTER_DELAY={delay}\n")
                runsh.write(f"sleep {delay}\n")
            if lava_signal == "kmsg":
                runsh.write("export KMSG=true\n")
                runsh.write(
                    'echo "<0><LAVA_SIGNAL_STARTRUN $TESTRUN_ID $UUID>" > /dev/kmsg\n'
                )
            else:
                runsh.write('echo "<LAVA_SIGNAL_STARTRUN $TESTRUN_ID $UUID>"\n')
            runsh.write("set -x\n")
            steps = testdef.get("run", {}).get("steps", [])
            if steps is not None:
                for cmd in [step for step in steps if step is not None]:
                    if "--cmd" in cmd or "--shell" in cmd:
                        cmd = re.sub(r"\$(\d+)\b", r"\\$\1", cmd)
                    runsh.write("%s\n" % cmd)
            runsh.write("set +x\n")
            if needs_delay and delay:
                runsh.write(f"sleep {delay}\n")
            if lava_signal == "kmsg":
                runsh.write("unset KMSG\n")
                runsh.write(
                    'echo "<0><LAVA_SIGNAL_ENDRUN $TESTRUN_ID $UUID>" > /dev/kmsg\n'
                )
            else:
                runsh.write('echo "<LAVA_SIGNAL_ENDRUN $TESTRUN_ID $UUID>"\n')

        self.results = {
            "uuid": self.test_uuid,
            "filename": filename,
            "name": self.parameters["name"],
            "path": self.parameters["path"],
            "from": self.parameters["from"],
        }
        if self.parameters["from"] != "inline":
            self.results["repository"] = self.parameters["repository"]
        return connection
