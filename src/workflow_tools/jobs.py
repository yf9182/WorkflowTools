"""Durable background jobs; no daemon or scheduled task required."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid
from .common import Failure, digest, lock, read_json, redact, require, save, secure_dir, stamp


class Jobs:
    def __init__(self, project):
        self.project = project
        self.root = project.state / "jobs"

    def path(self, job_id):
        require(isinstance(job_id, str) and re.fullmatch(r"[0-9a-f]{32}", job_id), "JOB_INVALID", "任务编号无效")
        return self.root / (job_id + ".json")

    def read(self, job_id):
        record = read_json(self.path(job_id))
        require(record is not None and record.get("job_id") == job_id
                and record.get("project_root") == str(self.project.root), "JOB_NOT_FOUND", "本项目没有这个任务")
        return record

    def submit(self, operation, params, *, launch=True):
        key = digest({"operation": operation, "params": params, "project": self.project.binding})
        with lock(self.project.state / "locks/job-index.lock"):
            index_path = self.root / "index.json"
            index = read_json(index_path, {"jobs": {}})
            if key in index["jobs"]:
                return self.read(index["jobs"][key])
            job_id = uuid.uuid4().hex
            record = {"schema_version": 1, "job_id": job_id, "status": "queued", "operation": operation,
                "params": params, "project_root": str(self.project.root), "config_binding": self.project.binding,
                "generation": 1, "created_at": stamp(), "updated_at": stamp(), "artifacts": [], "blockers": []}
            save(self.path(job_id), record)
            index["jobs"][key] = job_id
            save(index_path, index)
        if launch:
            self.launch(record)
        return record

    def launch(self, record):
        try:
            subprocess.Popen([sys.executable, "-m", "workflow_tools.cli", "--project", str(self.project.root),
                "--home", str(self.project.home), "_worker", record["job_id"], str(record["generation"])],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True)
        except OSError:
            with lock(self.root / (record["job_id"] + ".meta.lock")):
                record.update(status="failed", blockers=[{"code": "JOB_LAUNCH_FAILED", "message": "后台进程无法启动"}])
                save(self.path(record["job_id"]), record)

    def status(self, job_id):
        record = self.read(job_id)
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(record["updated_at"])).total_seconds()
        if record["status"] == "running" or (record["status"] == "queued" and age > 30):
            try:
                with lock(self.root / (job_id + ".run.lock"), timeout=0):
                    with lock(self.root / (job_id + ".meta.lock")):
                        record = self.read(job_id)
                        if record["status"] in ("running", "queued"):
                            record.update(status="interrupted", updated_at=stamp(),
                                blockers=[{"code": "JOB_INTERRUPTED", "message": "执行进程已退出；恢复时会先核对远端意图"}])
                            save(self.path(job_id), record)
            except Failure as e:
                if e.code != "RESOURCE_BUSY":
                    raise
        return record

    def resume(self, job_id, *, launch=True):
        with lock(self.root / (job_id + ".run.lock"), timeout=0):
            with lock(self.root / (job_id + ".meta.lock")):
                record = self.read(job_id)
                require(record["config_binding"] == self.project.binding, "PROJECT_CHANGED", "项目配置已变化，请创建新任务；既有外部意图仍会核对")
                if record["status"] == "succeeded":
                    return record
                require(record["status"] != "queued", "RESOURCE_BUSY", "任务仍在排队")
                record.update(status="queued", generation=record["generation"] + 1, updated_at=stamp(), blockers=[])
                save(self.path(job_id), record)
        if launch:
            self.launch(record)
        return record

    def execute(self, job_id, generation, runtime):
        with lock(self.root / (job_id + ".run.lock"), timeout=0):
            with lock(self.root / (job_id + ".meta.lock")):
                record = self.read(job_id)
                if record["generation"] != generation or record["status"] != "queued":
                    return record
                require(record["config_binding"] == self.project.binding, "PROJECT_CHANGED", "任务配置已变化")
                record.update(status="running", pid=os.getpid(), updated_at=stamp(), step="preflight")
                save(self.path(job_id), record)

            def checkpoint(step, **extra):
                with lock(self.root / (job_id + ".meta.lock")):
                    current = self.read(job_id)
                    current.update(step=step, updated_at=stamp(), **redact(extra, runtime.vault.known_secrets))
                    save(self.path(job_id), current)

            def terminate(_signum, _frame):
                raise Failure("JOB_INTERRUPTED", "任务被中断，恢复前需要核对检查点")

            previous = signal.signal(signal.SIGTERM, terminate)
            try:
                result = runtime.execute(record["operation"], record["params"], job_id, checkpoint)
                result = redact(result, runtime.vault.known_secrets)
            except Failure as e:
                result = e.result()
                if e.code in ("OUTCOME_UNKNOWN", "UPLOAD_UNKNOWN", "DEPLOY_UNKNOWN"):
                    result["status"] = "unknown"
            except BaseException:
                # Never serialize traceback or third-party exception strings.
                result = {"status": "failed", "artifacts": [],
                          "blockers": [{"code": "INTERNAL_ERROR", "message": "执行失败；检查受限任务日志和环境"}]}
            finally:
                signal.signal(signal.SIGTERM, previous)
            checkpoint("complete", **result)
            return self.read(job_id)
