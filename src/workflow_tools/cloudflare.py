from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib
import urllib.parse

from .common import Failure, digest, lock, read_json, require, run, save, secure_dir, stamp
from .config import source_binding

API = "https://api.cloudflare.com/client/v4"


def read_wrangler(path):
    try:
        text = Path(path).read_text()
        if Path(path).suffix == ".toml":
            return tomllib.loads(text)
        # JSONC comments and trailing commas; keep complete quoted strings intact.
        text = re.sub(r'("(?:\\.|[^"\\])*")|(/\*[\s\S]*?\*/|//[^\r\n]*)',
                      lambda m: m.group(1) or "", text)
        text = re.sub(r'("(?:\\.|[^"\\])*")|(,\s*(?=[}\]]))', lambda m: m.group(1) or "", text)
        value = json.loads(text)
        require(isinstance(value, dict), "CONFIG_INVALID", "Wrangler 配置需要对象")
        return value
    except Failure:
        raise
    except Exception:
        raise Failure("CONFIG_INVALID", "Wrangler JSON/JSONC/TOML 配置无效") from None


def target_config(project, target):
    cf = project.section("cloudflare")
    require(re.fullmatch(r"[0-9a-f]{32}", cf.get("account_id", "")), "CONFIG_MISSING", "请填写 Cloudflare Account ID")
    require(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", target.get("name", "")), "CONFIG_MISSING", "请填写部署目标 name")
    directory = project.resolve(target.get("directory", "."), exists=True, directory=True)
    build = target.get("build_command", [])
    require(isinstance(build, list) and all(isinstance(x, str) for x in build), "CONFIG_INVALID", "构建命令需要参数数组")
    if target["kind"] == "pages":
        project.resolve(target.get("output_directory", ""))
        require(target.get("branch"), "CONFIG_MISSING", "Pages 部署需要明确 branch")
    else:
        path = project.resolve(target.get("wrangler_config", ""), exists=True)
        config = read_wrangler(path)
        env_name = target.get("wrangler_environment", "")
        overlay = config.get("env", {}).get(env_name, {}) if env_name else {}
        require(not env_name or env_name in config.get("env", {}), "CONFIG_MISSING", "Wrangler 未定义这个环境")
        account = overlay.get("account_id", config.get("account_id"))
        require(not account or account == cf["account_id"], "TARGET_MISMATCH", "Wrangler account_id 与目标账户不一致")
        name = overlay.get("name") or (config.get("name", "") + ("-" + env_name if env_name else ""))
        require(name == target["name"], "TARGET_MISMATCH", "Wrangler 解析后的 Worker 名称与部署目标不一致")
        resolved = {**config, **overlay}
        require(not resolved.get("migrations") and not resolved.get("containers") and not resolved.get("unsafe"),
                "UNSUPPORTED_DEPLOY_ACTION", "首版不执行数据库/DO 迁移、容器开通或 unsafe 配置")
        for group, identifier in (("d1_databases", "database_id"), ("r2_buckets", "bucket_name"), ("kv_namespaces", "id")):
            bindings = resolved.get(group, [])
            require(isinstance(bindings, list) and all(isinstance(b, dict) and b.get(identifier) for b in bindings),
                    "RESOURCE_NOT_CONFIGURED", "绑定需要既有资源标识；不自动新建存储服务", resource_type=group)
        if target["kind"] == "workers-assets":
            require(overlay.get("assets", config.get("assets")), "CONFIG_MISSING", "Workers 前端需要 assets 配置")
    return directory


def wrangler(home):
    found = shutil.which("wrangler")
    if found:
        return [found]
    path = Path(home) / "tooling/node/node_modules/wrangler/bin/wrangler.js"
    require(path.is_file() and shutil.which("node"), "TOOL_MISSING", "请先运行 workflow-tools setup --wrangler")
    return [shutil.which("node"), str(path)]


class Cloudflare:
    def __init__(self, runtime):
        self.runtime = runtime
        self.project = runtime.project
        self.config = self.project.section("cloudflare")
        self.token = runtime.vault.text(self.config["api_token_ref"], "api_token")
        self.prefix = f"/accounts/{self.config['account_id']}"

    def request(self, path, method="GET", payload=None):
        require(path.startswith(self.prefix + "/") and not urllib.parse.urlsplit(path).netloc, "UNSAFE_URL", "Cloudflare API 超出目标账户")
        data = self.runtime.http.json(API + path, method=method, payload=payload, headers={"Authorization": "Bearer " + self.token})
        require(data.get("success") is True, "API_REJECTED", "Cloudflare API 操作失败，错误正文已隐藏")
        return data.get("result")

    def deployments(self, target):
        name = target["name"]
        if target["kind"] == "pages":
            result = self.request(self.prefix + f"/pages/projects/{name}/deployments?per_page=100")
            return result or []
        result = self.request(self.prefix + f"/workers/scripts/{name}/deployments")
        return result.get("deployments", []) if isinstance(result, dict) else result or []

    def tagged_active_version(self, target, slot_key, deployments):
        if not deployments:
            return None
        versions = deployments[0].get("versions", [])
        for v in versions:
            identifier = v.get("version_id", "")
            if v.get("percentage") != 100 or not re.fullmatch(r"[0-9a-fA-F-]{36}", identifier):
                continue
            row = self.request(self.prefix + f"/workers/scripts/{target['name']}/versions/{identifier}")
            if row.get("annotations", {}).get("workers/message") == "workflow-tools:" + slot_key:
                return identifier
        return None

    def ensure_pages(self, target, slot, slot_path):
        path = self.prefix + "/pages/projects/" + target["name"]
        try:
            current = self.request(path)
            require(current.get("name") == target["name"], "TARGET_MISMATCH", "Pages 项目名称不匹配")
            desired = target.get("pages_environment", "production" if target["environment"] in ("production", "prod") else "preview")
            require((current.get("production_branch") == target["branch"]) == (desired == "production"),
                    "TARGET_MISMATCH", "Pages branch 与预期生产/预览环境不匹配")
            return
        except Failure as e:
            if e.code != "REMOTE_NOT_FOUND":
                raise
        require(not slot.get("creation_intent"), "DEPLOY_UNKNOWN", "Pages 创建意图尚未确认，不重复创建")
        desired = target.get("pages_environment", "production" if target["environment"] in ("production", "prod") else "preview")
        production_branch = target.get("production_branch") or (target["branch"] if desired == "production" else None)
        require(production_branch, "CONFIG_MISSING", "首次建立预览项目需要明确 production_branch")
        slot["creation_intent"] = stamp()
        save(slot_path, slot)
        try:
            self.request(self.prefix + "/pages/projects", "POST", {"name": target["name"],
                "production_branch": production_branch, "build_config": {"destination_dir": target["output_directory"]}})
        except Failure:
            pass  # query the exact target before interpreting an interrupted POST
        try:
            current = self.request(path)
            require(current.get("name") == target["name"], "TARGET_MISMATCH", "Pages 项目名称不匹配")
        except Failure:
            raise Failure("DEPLOY_UNKNOWN", "Pages 创建结果未确认；恢复时只回读目标") from None
        slot["creation_confirmed"] = stamp()
        save(slot_path, slot)


def clean_environment():
    # The build step never receives our deployment token, or another account's shell token.
    return {k: v for k, v in os.environ.items() if not k.startswith(("CLOUDFLARE_", "CF_", "AWS_"))} | {"CI": "true", "WRANGLER_SEND_METRICS": "false"}


def health(runtime, target):
    config = target.get("healthcheck")
    if not config:
        return {"status": "unverified", "reason": "healthcheck_not_configured"}
    require(isinstance(config, dict) and isinstance(config.get("url"), str), "CONFIG_INVALID", "健康检查需要 HTTPS URL")
    try:
        raw, headers = runtime.http.request(config["url"])
        require(headers.get("_workflow_status", 200) == config.get("expected_status", 200), "HEALTHCHECK_FAILED", "健康检查状态码不匹配")
        if "contains" in config:
            require(config["contains"].encode() in raw, "HEALTHCHECK_FAILED", "健康检查响应内容不匹配")
        return {"status": "ready", "url": config["url"]}
    except Failure as e:
        return {"status": "failed", "reason": e.code}


def remote_view(deployments, target):
    if target["kind"] == "pages":
        return [{"deployment_id": x.get("id"), "url": x.get("url"), "environment": x.get("environment"),
                 "stage": x.get("latest_stage", {}).get("status"), "created_at": x.get("created_on")} for x in deployments]
    return [{"deployment_id": x.get("id"), "versions": [{"version_id": v.get("version_id"), "percentage": v.get("percentage")} for v in x.get("versions", [])],
             "created_at": x.get("created_on")} for x in deployments]


def status(runtime, target_name, environment=None):
    runtime.enforce("cloudflare_status", target_name, environment)
    target = runtime.project.target(target_name, environment)
    deployments = Cloudflare(runtime).deployments(target)
    return {"status": "ready", "target": target_name, "environment": target["environment"],
            "deployments": remote_view(deployments, target), "healthcheck": health(runtime, target), "artifacts": [], "blockers": []}


def deploy(runtime, params, job_id, checkpoint):
    project, vault = runtime.project, runtime.vault
    target = project.target(params["target"], params.get("environment"))
    runtime.enforce("cloudflare_deploy", params["target"], params.get("environment"))
    directory = target_config(project, target)
    cf = Cloudflare(runtime)
    operation_target = {k: v for k, v in target.items() if k not in ("healthcheck", "environments", "default_environment")}
    slot_key = digest({"account": cf.config["account_id"], "target": operation_target, "source": params["source_binding"]})
    slot_path = project.home / "state/deployments" / (slot_key + ".json")
    resource = digest({"account": cf.config["account_id"], "kind": target["kind"], "name": target["name"]})
    with lock(project.home / "locks" / ("deploy-" + resource + ".lock"), timeout=30):
        slot = read_json(slot_path, {"schema_version": 1, "slot_key": slot_key})
        if not slot.get("deployment_intent"):
            require(params["source_binding"] == source_binding(project), "SOURCE_CHANGED", "部署源文件发生变化，请重新创建任务")
            work = secure_dir(project.artifacts / ("deploy-" + job_id))
            log = work / "deploy.log"
            checkpoint("building_frontend_or_backend")
            if target.get("build_command"):
                r = run(target["build_command"], cwd=directory, env=clean_environment(), timeout=3600, log=log)
                require(r.returncode == 0, "BUILD_FAILED", "部署前构建失败", log=str(log))
            command = wrangler(project.home)
            if target["kind"] == "pages":
                output = project.resolve(target["output_directory"], exists=True, directory=True)
                cf.ensure_pages(target, slot, slot_path)
                args = ["pages", "deploy", str(output), "--project-name", target["name"], "--branch", target["branch"],
                        "--commit-message", "workflow-tools:" + slot_key]
            else:
                args = ["deploy", "--config", str(project.resolve(target["wrangler_config"])), "--name", target["name"],
                        "--no-autoconfig", "--message", "workflow-tools:" + slot_key]
                if target.get("wrangler_environment"):
                    args += ["--env", target["wrangler_environment"]]
                dry = run(command + args + ["--dry-run", "--outdir", str(work / "bundle")],
                          cwd=directory, env=clean_environment(), timeout=1800, log=log)
                require(dry.returncode == 0, "BUILD_FAILED", "Worker 打包验证失败", log=str(log))
            require(params["source_binding"] == source_binding(project), "SOURCE_CHANGED", "构建期间源文件发生变化，未发布代码")
            slot.update(deployment_intent=stamp(), project_root=str(project.root), job_id=job_id, status="intent")
            save(slot_path, slot)
            env = clean_environment() | {"CLOUDFLARE_API_TOKEN": cf.token, "CLOUDFLARE_ACCOUNT_ID": cf.config["account_id"]}
            checkpoint("deploying", deployment_intent=str(slot_path))
            try:
                r = run(command + args, cwd=directory, env=env, timeout=1800, secret_values=vault.known_secrets, log=log)
            except (subprocess.TimeoutExpired, OSError):
                raise Failure("DEPLOY_UNKNOWN", "部署结果未知；恢复时仅回读远端，不重新部署") from None
            text = (r.stdout + r.stderr).decode("utf-8", "replace")
            if target["kind"] == "pages":
                urls = re.findall(r"https://[a-z0-9-]+\." + re.escape(target["name"]) + r"\.pages\.dev", text)
                slot["expected_url"] = urls[-1] if urls else None
            else:
                match = re.search(r"(?:Current Version ID|Version ID):\s*([0-9a-fA-F-]{36})", text)
                slot["expected_version_id"] = match.group(1) if match else None
            slot["command_succeeded"] = r.returncode == 0
            slot["status"] = "command_finished"
            save(slot_path, slot)
            require(r.returncode == 0, "DEPLOY_UNKNOWN", "部署命令失败或结果未知，请回读远端")
        checkpoint("reading_deployment_state")
        deployments = cf.deployments(target)
        if target["kind"] == "pages":
            matches = [x for x in deployments if slot.get("expected_url") and x.get("url") == slot["expected_url"]]
            if not matches:
                matches = [x for x in deployments if x.get("deployment_trigger", {}).get("metadata", {}).get("commit_message") == "workflow-tools:" + slot_key]
            desired = target.get("pages_environment", "production" if target["environment"] in ("production", "prod") else "preview")
            matches = [x for x in matches if x.get("environment") == desired
                       and x.get("deployment_trigger", {}).get("metadata", {}).get("branch") == target["branch"]]
        else:
            if not slot.get("expected_version_id"):
                slot["expected_version_id"] = cf.tagged_active_version(target, slot_key, deployments)
                if slot["expected_version_id"]:
                    save(slot_path, slot)
            matches = [x for x in deployments[:1] if slot.get("expected_version_id") and any(
                v.get("version_id") == slot["expected_version_id"] and v.get("percentage") == 100 for v in x.get("versions", []))]
        require(len(matches) == 1, "DEPLOY_UNKNOWN", "尚未回读到与本次部署绑定的结果，不重复部署")
        if target["kind"] == "pages":
            require(matches[0].get("latest_stage", {}).get("status") == "success", "DEPLOY_UNKNOWN", "Pages 部署尚未成功完成；恢复仅回读处理结果")
        check = health(runtime, target)
        slot.update(status="confirmed", deployment_id=matches[0]["id"], confirmed_at=stamp(), healthcheck=check)
        save(slot_path, slot)
        require(check["status"] != "failed", "HEALTHCHECK_FAILED", "部署已确认，但健康检查失败；恢复仅重验健康检查")
        return {"status": "succeeded", "target": params["target"], "environment": target["environment"],
                "deployment": remote_view(matches, target)[0], "healthcheck": check,
                "artifacts": [{"type": "deployment_record", "path": str(slot_path)}], "blockers": []}
