import logging
import json
import os

from cwltool.errors import WorkflowException
from cwltool.process import get_feature, adjustFiles, UnsupportedRequirement, shortname

import arvados.collection

from .arvdocker import arv_docker_get_image
from . import done
from .runner import Runner

logger = logging.getLogger('arvados.cwl-runner')

class ArvadosContainer(object):
    """Submit and manage a Crunch job for executing a CWL CommandLineTool."""

    def __init__(self, runner):
        self.arvrunner = runner
        self.running = False

    def update_pipeline_component(self, r):
        pass

    def run(self, dry_run=False, pull_image=True, **kwargs):
        container_request = {
            "command": self.command_line,
            "owner_uuid": self.arvrunner.project_uuid,
            "name": self.name,
            "output_path": "/var/spool/cwl",
            "cwd": "/var/spool/cwl",
            "priority": 1,
            "state": "Committed"
        }
        runtime_constraints = {}
        mounts = {
            "/var/spool/cwl": {
                "kind": "tmp"
            }
        }

        for f in self.pathmapper.files():
            _, p = self.pathmapper.mapper(f)
            mounts[p] = {
                "kind": "collection",
                "portable_data_hash": p[6:]
            }

        if self.generatefiles:
            raise UnsupportedRequirement("Generate files not supported")

            vwd = arvados.collection.Collection(api_client=self.arvrunner.api_client)
            container_request["task.vwd"] = {}
            for t in self.generatefiles:
                if isinstance(self.generatefiles[t], dict):
                    src, rest = self.arvrunner.fs_access.get_collection(self.generatefiles[t]["path"].replace("$(task.keep)/", "keep:"))
                    vwd.copy(rest, t, source_collection=src)
                else:
                    with vwd.open(t, "w") as f:
                        f.write(self.generatefiles[t])
            vwd.save_new()
            # TODO
            # for t in self.generatefiles:
            #     container_request["task.vwd"][t] = "$(task.keep)/%s/%s" % (vwd.portable_data_hash(), t)

        container_request["environment"] = {"TMPDIR": "/tmp"}
        if self.environment:
            container_request["environment"].update(self.environment)

        if self.stdin:
            raise UnsupportedRequirement("Stdin redirection currently not suppported")

        if self.stdout:
            mounts["stdout"] = {"kind": "file",
                                "path": "/var/spool/cwl/%s" % (self.stdout)}

        (docker_req, docker_is_req) = get_feature(self, "DockerRequirement")
        if not docker_req:
            docker_req = {"dockerImageId": "arvados/jobs"}

        container_request["container_image"] = arv_docker_get_image(self.arvrunner.api,
                                                                     docker_req,
                                                                     pull_image,
                                                                     self.arvrunner.project_uuid)

        resources = self.builder.resources
        if resources is not None:
            runtime_constraints["vcpus"] = resources.get("cores", 1)
            runtime_constraints["ram"] = resources.get("ram") * 2**20
            #runtime_constraints["min_scratch_mb_per_node"] = resources.get("tmpdirSize", 0) + resources.get("outdirSize", 0)

        container_request["mounts"] = mounts
        container_request["runtime_constraints"] = runtime_constraints

        try:
            response = self.arvrunner.api.container_requests().create(
                body=container_request
            ).execute(num_retries=self.arvrunner.num_retries)

            self.arvrunner.jobs[response["container_uuid"]] = self

            logger.info("Container %s (%s) request state is %s", self.name, response["container_uuid"], response["state"])

            if response["state"] == "Final":
                self.done(response)
        except Exception as e:
            logger.error("Got error %s" % str(e))
            self.output_callback({}, "permanentFail")

    def done(self, record):
        try:
            if record["state"] == "Complete":
                processStatus = "success"
            else:
                processStatus = "permanentFail"

            try:
                outputs = {}
                if record["output"]:
                    outputs = done.done(self, record, "/tmp", "/var/spool/cwl", "/keep")
            except WorkflowException as e:
                logger.error("Error while collecting container outputs:\n%s", e, exc_info=(e if self.arvrunner.debug else False))
                processStatus = "permanentFail"
            except Exception as e:
                logger.exception("Got unknown exception while collecting job outputs:")
                processStatus = "permanentFail"

            self.output_callback(outputs, processStatus)
        finally:
            del self.arvrunner.jobs[record["uuid"]]


class RunnerContainer(Runner):
    """Submit and manage a container that runs arvados-cwl-runner."""

    def arvados_job_spec(self, dry_run=False, pull_image=True, **kwargs):
        """Create an Arvados job specification for this workflow.

        The returned dict can be used to create a job (i.e., passed as
        the +body+ argument to jobs().create()), or as a component in
        a pipeline template or pipeline instance.
        """

        workflowmapper = super(RunnerContainer, self).arvados_job_spec(dry_run=dry_run, pull_image=pull_image, **kwargs)

        with arvados.collection.Collection(api_client=self.arvrunner.api) as jobobj:
            with jobobj.open("cwl.input.json", "w") as f:
                json.dump(self.job_order, f, sort_keys=True, indent=4)
            jobobj.save_new(owner_uuid=self.arvrunner.project_uuid)

        workflowname = os.path.basename(self.tool.tool["id"])
        workflowpath = "/var/lib/cwl/workflow/%s" % workflowname
        workflowcollection = workflowmapper.mapper(self.tool.tool["id"])[1]
        workflowcollection = workflowcollection[:workflowcollection.index('/')]
        jobpath = "/var/lib/cwl/job/cwl.input.json"

        container_image = arv_docker_get_image(self.arvrunner.api,
                                               {"dockerImageId": "arvados/jobs"},
                                               pull_image,
                                               self.arvrunner.project_uuid)

        return {
            "command": ["arvados-cwl-runner", "--local", "--crunch2", workflowpath, jobpath],
            "owner_uuid": self.arvrunner.project_uuid,
            "name": self.name,
            "output_path": "/var/spool/cwl",
            "cwd": "/var/spool/cwl",
            "priority": 1,
            "state": "Committed",
            "container_image": container_image,
            "mounts": {
                "/var/lib/cwl/workflow": {
                    "kind": "collection",
                    "portable_data_hash": "%s" % workflowcollection
                },
                jobpath: {
                    "kind": "collection",
                    "portable_data_hash": "%s/cwl.input.json" % jobobj.portable_data_hash()
                },
                "stdout": {
                    "kind": "file",
                    "path": "/var/spool/cwl/cwl.output.json"
                },
                "/var/spool/cwl": {
                    "kind": "collection",
                    "writable": True
                }
            },
            "runtime_constraints": {
                "vcpus": 1,
                "ram": 1024*1024*256,
                "API": True
            }
        }

    def run(self, *args, **kwargs):
        job_spec = self.arvados_job_spec(*args, **kwargs)
        job_spec.setdefault("owner_uuid", self.arvrunner.project_uuid)

        response = self.arvrunner.api.container_requests().create(
            body=job_spec
        ).execute(num_retries=self.arvrunner.num_retries)

        self.uuid = response["uuid"]
        self.arvrunner.jobs[response["container_uuid"]] = self

        logger.info("Submitted container %s", response["uuid"])

        if response["state"] in ("Complete", "Failed", "Cancelled"):
            self.done(response)
