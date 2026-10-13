from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shutil
import sys
import tomlkit
from . import __version__
from .common import Failure, atomic_bytes, file_sha, read_json, require, save, secure_dir
from .config import TEMPLATE


def install(root, home=None):
    root = Path(root).expanduser().resolve()
    require(root.is_dir(), "PROJECT_NOT_FOUND", "项目目录不存在")
    package = Path(__file__).parent
    config_path = root / ".workflow-tools/project.json"
    manifest_path = root / ".workflow-tools/installation.json"
    old = read_json(manifest_path, {"files": {}})
    manifest = {"schema_version": 1, "version": __version__, "files": {}}
    preserved = []
    if not config_path.exists():
        template = copy.deepcopy(TEMPLATE)
        template["project_id"] = re.sub(r"[^a-zA-Z0-9_-]", "-", root.name).strip("-")[:64] or "project"
        save(config_path, template)
    for source in sorted((package / "assets/skills").rglob("*")):
        if not source.is_file():
            continue
        relative = ".agents/skills/" + str(source.relative_to(package / "assets/skills"))
        dest = root / relative
        require(dest.resolve().is_relative_to(root) and not dest.is_symlink(), "UNSAFE_PATH", "Skill 安装路径超出项目目录")
        source_hash = file_sha(source)
        if dest.exists() and file_sha(dest) not in (source_hash, old.get("files", {}).get(relative)):
            preserved.append(relative)
            if relative in old.get("files", {}):
                manifest["files"][relative] = old["files"][relative]
            continue
        atomic_bytes(dest, source.read_bytes(), mode=0o644)
        manifest["files"][relative] = source_hash
    require(manifest["files"] or preserved, "INSTALL_INCOMPLETE", "安装包缺少 Skills 资源")
    mcp_path = root / ".codex/config.toml"
    require(not mcp_path.is_symlink(), "UNSAFE_PATH", "MCP 配置不能是符号链接")
    try:
        document = tomlkit.parse(mcp_path.read_text()) if mcp_path.exists() else tomlkit.document()
        servers = document.setdefault("mcp_servers", tomlkit.table())
        existing = servers.get("workflow_tools", {})
        require(not existing or "workflow_tools.cli" in existing.get("args", []), "INSTALL_CONFLICT", "已有同名 MCP 服务不属于 WorkflowTools，未覆盖")
        server = servers.setdefault("workflow_tools", tomlkit.table())
        server["command"] = sys.executable
        args = ["-m", "workflow_tools.cli", "--project", str(root)]
        if home:
            args += ["--home", str(Path(home).expanduser().absolute())]
        server["args"] = args + ["mcp"]
        server.setdefault("startup_timeout_sec", 30)
        server.setdefault("tool_timeout_sec", 120)
        atomic_bytes(mcp_path, tomlkit.dumps(document).encode(), mode=0o600)
    except Failure:
        raise
    except Exception:
        raise Failure("INSTALL_CONFLICT", "现有 Codex 配置无效；未替换配置文件") from None
    save(manifest_path, manifest)
    import shlex
    launcher = "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -m workflow_tools.cli --project " + shlex.quote(str(root))
    if home:
        launcher += " --home " + shlex.quote(str(Path(home).expanduser().absolute()))
    launcher += ' "$@"\n'
    atomic_bytes(root / ".workflow-tools/run", launcher.encode(), mode=0o700)
    ignore_path = root / ".gitignore"
    require(not ignore_path.is_symlink(), "UNSAFE_PATH", ".gitignore 不能是符号链接")
    current = ignore_path.read_text() if ignore_path.exists() else ""
    block = "\n# WorkflowTools: authorization belongs outside this repository\n.workflow-tools/output/\n.workflow-tools/installation.json\n*.p8\n*.p12\n*.keychain*\n.env\n.dev.vars\n"
    if "# WorkflowTools: authorization belongs outside this repository" not in current:
        atomic_bytes(ignore_path, (current.rstrip() + "\n" + block).encode(), mode=0o644)
    return {"status": "ready", "project": str(root), "version": __version__, "skills": 4,
            "config": str(config_path), "mcp_config": str(mcp_path), "preserved_files": preserved,
            "message": "填写项目配置并运行 doctor；项目级 MCP 需要宿主信任项目并重新加载连接"}


def setup_wrangler(home):
    from .common import lock, run
    home = Path(home).expanduser().absolute()
    npm, node = shutil.which("npm"), shutil.which("node")
    require(npm and node, "TOOL_MISSING", "需要 Node.js 22+ 和 npm")
    result = run([node, "--version"])
    require(int(result.stdout.decode().strip().lstrip("v").split(".")[0]) >= 22, "TOOL_VERSION", "需要 Node.js 22 或更高版本")
    destination = home / "tooling/node"
    source = Path(__file__).parent / "assets/tooling"
    with lock(home / "locks/wrangler-install.lock"):
        secure_dir(destination)
        for name in ("package.json", "package-lock.json"):
            require((source / name).is_file(), "INSTALL_INCOMPLETE", "安装包缺少锁定的 Wrangler 依赖")
            atomic_bytes(destination / name, (source / name).read_bytes())
        r = run([npm, "ci", "--prefix", str(destination), "--cache", str(home / "cache/npm"),
                 "--no-audit", "--no-fund", "--ignore-scripts"], timeout=600)
        require(r.returncode == 0, "TOOL_INSTALL_FAILED", "Wrangler 安装失败，未触发交互式登录")
        path = destination / "node_modules/wrangler/bin/wrangler.js"
        require(path.is_file(), "TOOL_INSTALL_FAILED", "Wrangler 安装不完整")
    return {"status": "ready", "tool": "wrangler", "path": str(path)}
