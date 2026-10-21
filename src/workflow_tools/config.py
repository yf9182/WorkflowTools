from __future__ import annotations

import json
import os
from pathlib import Path
import re
from .common import Failure, canonical, digest, private_read, read_json, require


TEMPLATE = {
    "schema_version": 1,
    "project_id": "replace-with-project-id",
    "credential_provider": "r2",
    "credential_refs": {},
    "ios": {"enabled": False, "project": "", "workspace": "", "scheme": "",
            "bundle_id": "", "team_id": "", "app_store_id": "", "source_paths": [],
            "apple": {"key_id": "", "issuer_id": "", "vendor_number": "", "p8_ref": ""},
            "signing": {"p12_ref": "", "password_ref": "", "profiles": {}}},
    "analytics": {"sources": [], "window_days": 28, "sample_floor": 30,
                  "google": {"property_id": "", "service_account_ref": ""},
                  "product_ids": [], "funnels": []},
    "cloudflare": {"account_id": "", "api_token_ref": "", "targets": {}}
}


class Project:
    def __init__(self, root, home=None):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / ".workflow-tools/project.json"
        self.home = Path(home or os.environ.get("WORKFLOW_TOOLS_HOME", "~/.config/workflow-tools")).expanduser().absolute()
        self.data = read_json(self.path)
        require(self.data is not None, "PROJECT_NOT_INSTALLED", "项目尚未安装 WorkflowTools", file=str(self.path))
        require(self.data.get("schema_version") == 1, "CONFIG_INVALID", "项目配置版本必须为 1")
        def no_inline_secrets(value, parent=""):
            if isinstance(value, dict):
                for key, item in value.items():
                    forbidden = re.search(r"(?i)(^|[_-])(private[_-]?key|password|passwd|client[_-]?secret|access[_-]?token|api[_-]?token|api[_-]?key|secret[_-]?access[_-]?key|encryption[_-]?key)([_-]|$)", key)
                    require(parent in ("credential_refs", "profiles") or key.endswith("_ref") or not forbidden,
                        "INLINE_SECRET_REFUSED", "项目配置只能包含凭据引用，不能包含秘密值")
                    no_inline_secrets(item, key)
            elif isinstance(value, list):
                for item in value:
                    no_inline_secrets(item)
        no_inline_secrets(self.data)
        self.id = self.data.get("project_id", "")
        require(isinstance(self.id, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", self.id),
                "CONFIG_INVALID", "project_id 格式无效")
        require(self.id != "replace-with-project-id", "CONFIG_MISSING", "请填写 project_id")
        require(self.data.get("credential_provider", "r2") in ("r2", "local"), "CONFIG_INVALID", "未知凭据提供者")
        self._cloudflare_default_account_id = self._load_cloudflare_default()
        binding_config = self.data
        if self._cloudflare_default_account_id:
            binding_config = dict(self.data)
            cloudflare = dict(binding_config.get("cloudflare", {}))
            cloudflare["account_id"] = self._cloudflare_default_account_id
            binding_config["cloudflare"] = cloudflare
        self.binding = digest({"root": str(self.root), "config": binding_config})
        self.state = self.home / "state/projects" / (self.id + "-" + digest(str(self.root))[:12])
        self.artifacts = self.state / "artifacts"

    def _load_cloudflare_default(self):
        cloudflare = self.data.get("cloudflare", {})
        if not isinstance(cloudflare, dict) or cloudflare.get("account_id"):
            return ""
        path = self.home / "bootstrap/connection.json"
        try:
            raw = private_read(path, self.home)
        except Failure as exc:
            if exc.code == "AUTH_MISSING":
                return ""
            raise
        try:
            connection = json.loads(raw)
            require(isinstance(connection, dict), "AUTH_INVALID", "本机连接授权格式无效")
            account_id = connection.get("cloudflare", {}).get("account_id", "")
        except Failure:
            raise
        except Exception:
            raise Failure("AUTH_INVALID", "本机连接授权格式无效") from None
        require(not account_id or (isinstance(account_id, str)
                and re.fullmatch(r"[0-9a-f]{32}", account_id)),
                "AUTH_INVALID", "本机 Cloudflare Account ID 格式无效")
        return account_id

    def resolve(self, value, *, exists=False, directory=False):
        require(isinstance(value, str) and value and not Path(value).is_absolute(),
                "CONFIG_INVALID", "项目路径必须是非空相对路径")
        path = (self.root / value).resolve()
        require(path.is_relative_to(self.root), "UNSAFE_PATH", "项目路径超出项目目录")
        if exists:
            require(path.is_dir() if directory else path.exists(), "CONFIG_MISSING", "项目文件或目录不存在", file=value)
        return path

    def section(self, name):
        value = self.data.get(name, {})
        require(isinstance(value, dict), "CONFIG_INVALID", "配置段必须是对象", field=name)
        if name == "cloudflare" and self._cloudflare_default_account_id and not value.get("account_id"):
            return {**value, "account_id": self._cloudflare_default_account_id}
        return value

    def target(self, name, environment):
        cf = self.section("cloudflare")
        targets = cf.get("targets", {})
        require(name in targets, "CONFIG_MISSING", "未配置部署目标", target=name)
        base = targets[name]
        require(isinstance(base, dict), "CONFIG_INVALID", "部署目标必须是对象")
        environments = base.get("environments", {})
        chosen = environment or base.get("default_environment")
        require(chosen and chosen in environments, "CONFIG_MISSING", "请指定已配置的部署环境", target=name)
        require(isinstance(environments[chosen], dict), "CONFIG_INVALID", "部署环境必须是对象")
        target = {**base, **environments[chosen], "environment": chosen}
        require(target.get("kind") in ("workers", "workers-assets", "pages"), "CONFIG_INVALID", "不支持的部署类型")
        return target


def source_binding(project):
    """Bind artifacts to source bytes, including uncommitted changes; never edit the checkout."""
    import subprocess
    r = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"],
                       cwd=project.root, capture_output=True)
    require(r.returncode == 0, "CONFIG_INVALID", "构建项目需要 Git 源码记录")
    from .common import file_sha
    paths = sorted(set(x.decode() for x in r.stdout.split(b"\0") if x))
    records = []
    for relative in paths:
        if relative.startswith((".workflow-tools/", ".agents/skills/", ".codex/")):
            continue
        path = project.resolve(relative)
        if path.is_file():
            records.append((relative, file_sha(path)))
    return digest(records)
