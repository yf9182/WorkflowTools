"""Local, project-bound stdio MCP; the model never receives credential material."""
from __future__ import annotations

from typing import Any, Literal
from .common import Failure, redact
from .runtime import Runtime


def create_server(root, home=None):
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations
    server = MCPServer("WorkflowTools", version="0.1.0",
        instructions="Tools operate only on the configured project. Run workflow_doctor for the capability you need. "
        "Build, upload, deploy and collection return durable job_id values: poll workflow_job_status until completion. "
        "Never request secret values. Resume unknown external outcomes only through workflow_job_resume, which reconciles first.")

    def call(method, *args, **kwargs):
        runtime = None
        try:
            runtime = Runtime(root, home)
            result = getattr(runtime, method)(*args, **kwargs)
            return redact(result, runtime.vault.known_secrets)
        except Failure as e:
            return redact(e.result(), runtime.vault.known_secrets if runtime else ())
        except Exception:
            return {"status": "failed", "artifacts": [], "blockers": [{"code": "INTERNAL_ERROR", "message": "工具执行失败；敏感异常内容已隐藏"}]}

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
    local_write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    external = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

    @server.tool(annotations=read, structured_output=True)
    def workflow_doctor(capability: str | None = None, target: str | None = None,
                        environment: str | None = None, remote: bool = False) -> dict[str, Any]:
        """Check only needed capability dependencies; remote=True verifies service identity/access using APIs."""
        return call("doctor", capability, target, environment, remote)

    @server.tool(annotations=local_write, structured_output=True)
    def ios_build(version: str | None = None, build_number: str | None = None) -> dict[str, Any]:
        """Queue a Distribution-signed IPA build in a temporary source copy; does not upload or edit the project."""
        return call("ios_build", version, build_number)

    @server.tool(annotations=external, structured_output=True)
    def ios_profiles_prepare(bundle_ids: list[str]) -> dict[str, Any]:
        """Create or reuse App Store profiles for explicit existing Bundle IDs via the team's ASC API key, then encrypt, store, and register them for this project. This creates Apple-side profile records and updates project credential references."""
        return call("ios_profiles_prepare", bundle_ids)

    @server.tool(annotations=external, structured_output=True)
    def ios_upload_testflight(artifact_id: str) -> dict[str, Any]:
        """Queue one TestFlight upload of an existing verified IPA; binds App/version/build and reconciles interrupted uploads."""
        return call("ios_upload_testflight", artifact_id)

    @server.tool(annotations=read, structured_output=True)
    def ios_testflight_status(version: str | None = None, build_number: str | None = None) -> dict[str, Any]:
        """Read ASC build processing status; does not submit, release or invite testers."""
        return call("ios_testflight_status", version, build_number)

    @server.tool(annotations=local_write, structured_output=True)
    def analytics_fetch(sources: list[Literal["ga4", "asc_sales", "apple_analytics"]] | None = None,
                        start_date: str | None = None, end_date: str | None = None, refresh: bool = False) -> dict[str, Any]:
        """Queue API-only data collection; sources fail independently and results carry actual date coverage."""
        return call("analytics_fetch", sources, start_date, end_date, refresh)

    @server.tool(annotations=local_write, structured_output=True)
    def analytics_report(dataset_id: str) -> dict[str, Any]:
        """Analyze a saved dataset by app version; emit JSON/Markdown without summing users or inventing rates/profit."""
        return call("analytics_report", dataset_id)

    @server.tool(annotations=external, structured_output=True)
    def cloudflare_deploy(target: str, environment: str | None = None) -> dict[str, Any]:
        """Queue a configured Workers, Workers assets or Pages deployment; records intent and verifies the remote deployment."""
        return call("cloudflare_deploy", target, environment)

    @server.tool(annotations=read, structured_output=True)
    def cloudflare_status(target: str, environment: str | None = None) -> dict[str, Any]:
        """Read a configured account/target/environment's deployments and optional healthcheck."""
        return call("cloudflare_status", target, environment)

    @server.tool(annotations=local_write, structured_output=True)
    def workflow_job_status(job_id: str) -> dict[str, Any]:
        """Read durable progress and detect an exited worker; returned blockers explain files or permissions needed."""
        return call("job_status", job_id)

    @server.tool(annotations=external, structured_output=True)
    def workflow_job_resume(job_id: str) -> dict[str, Any]:
        """Resume an interrupted/blocked job. Known external intents are reconciled before any new write; never blind-retry."""
        return call("job_resume", job_id)

    return server
