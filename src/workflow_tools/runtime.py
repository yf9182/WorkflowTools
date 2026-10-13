"""CLI and MCP share these operations, gates, identities and job records."""
from __future__ import annotations

from .auth import HTTP
from .common import Failure, require
from .config import Project, source_binding
from .credentials import Vault
from .gates import Gates
from .jobs import Jobs


class Runtime:
    def __init__(self, root, home=None, *, http=None, s3=None):
        self.project = Project(root, home)
        self.vault = Vault(self.project.home, self.project.data.get("credential_refs", {}),
                           self.project.data.get("credential_provider", "r2"), s3=s3)
        self.http = http or HTTP()
        self.gates = Gates(self)
        self.jobs = Jobs(self.project)

    def enforce(self, capability, target=None, environment=None):
        result = self.gates.check(capability, target, environment)
        if result["status"] != "ready":
            raise Failure("CAPABILITY_BLOCKED", "此操作的门禁未通过", capability=result["capability"], checks=result["blockers"])

    def doctor(self, capability=None, target=None, environment=None, remote=False):
        return self.gates.doctor(capability, target, environment, remote)

    def ios_build(self, version=None, build_number=None):
        return self.jobs.submit("ios_build", {"version": version, "build_number": build_number,
            "source_binding": source_binding(self.project)})

    def ios_upload_testflight(self, artifact_id):
        from .ios import artifact
        artifact(self.project, artifact_id)
        return self.jobs.submit("ios_upload_testflight", {"artifact_id": artifact_id})

    def ios_testflight_status(self, version=None, build_number=None):
        from .ios import status
        return status(self, version, build_number)

    def analytics_fetch(self, sources=None, start_date=None, end_date=None, refresh=False):
        from .analytics import dates
        start, end = dates(start_date, end_date, self.project.section("analytics").get("window_days", 28))
        import uuid
        return self.jobs.submit("analytics_fetch", {"sources": sources, "start_date": start.isoformat(), "end_date": end.isoformat(),
                                "refresh_id": uuid.uuid4().hex if refresh else None})

    def analytics_report(self, dataset_id):
        from .analytics import report
        return report(self, dataset_id)

    def cloudflare_deploy(self, target, environment=None):
        self.project.target(target, environment)
        return self.jobs.submit("cloudflare_deploy", {"target": target, "environment": environment,
                                "source_binding": source_binding(self.project)})

    def cloudflare_status(self, target, environment=None):
        from .cloudflare import status
        return status(self, target, environment)

    def job_status(self, job_id):
        return self.jobs.status(job_id)

    def job_resume(self, job_id):
        return self.jobs.resume(job_id)

    def execute(self, operation, params, job_id, checkpoint):
        if operation == "ios_build":
            from .ios import build
            return build(self, params, job_id, checkpoint)
        if operation == "ios_upload_testflight":
            from .ios import upload
            return upload(self, params, job_id, checkpoint)
        if operation == "analytics_fetch":
            from .analytics import fetch
            return fetch(self, params, job_id, checkpoint)
        if operation == "cloudflare_deploy":
            from .cloudflare import deploy
            return deploy(self, params, job_id, checkpoint)
        raise Failure("OPERATION_UNKNOWN", "任务操作类型无效")
