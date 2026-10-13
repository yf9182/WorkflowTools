from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from . import __version__
from .common import Failure, private_read, redact, require
from .credentials import KINDS, Vault


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("USAGE_ERROR", "命令参数无效，请查看 --help")


def parser():
    p = Parser(prog="workflow-tools")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--project", default=".", help="项目根目录；操作命令前传入")
    p.add_argument("--home", default=os.environ.get("WORKFLOW_TOOLS_HOME", "~/.config/workflow-tools"), help="仓库外授权与状态目录")
    subs = p.add_subparsers(dest="command", required=True)
    install = subs.add_parser("install", help="安装项目 Skills 和 MCP 登记")
    install.add_argument("--project", dest="install_project")
    setup = subs.add_parser("setup", help="准备锁定版本的工具链")
    setup.add_argument("--wrangler", action="store_true", required=True)
    doctor = subs.add_parser("doctor", help="按能力列出门禁和授权缺项")
    doctor.add_argument("--capability")
    doctor.add_argument("--target")
    doctor.add_argument("--environment")
    doctor.add_argument("--remote", action="store_true")
    subs.add_parser("mcp", help="启动项目绑定的 stdio MCP")
    credentials = subs.add_parser("credentials", help="集中凭据管理；不输出明文")
    cs = credentials.add_subparsers(dest="action", required=True)
    cs.add_parser("init"); cs.add_parser("sweep")
    imp = cs.add_parser("import")
    imp.add_argument("--ref", required=True); imp.add_argument("--kind", required=True, choices=sorted(KINDS))
    imp.add_argument("--file", required=True); imp.add_argument("--credential-version", type=int)
    legacy = cs.add_parser("import-legacy")
    legacy.add_argument("--source", required=True); legacy.add_argument("--prefix", default="legacy")
    upload = cs.add_parser("upload")
    upload.add_argument("--ref", required=True)
    check = cs.add_parser("check")
    check.add_argument("--ref", required=True); check.add_argument("--provider", choices=("r2", "local"), default="r2")
    backup = cs.add_parser("backup")
    backup.add_argument("--out", required=True); backup.add_argument("--password-file", required=True)
    restore = cs.add_parser("restore")
    restore.add_argument("--file", required=True); restore.add_argument("--password-file", required=True)
    ios = subs.add_parser("ios")
    ios_sub = ios.add_subparsers(dest="action", required=True)
    build = ios_sub.add_parser("build")
    build.add_argument("--version", dest="app_version"); build.add_argument("--build-number")
    upload = ios_sub.add_parser("upload")
    upload.add_argument("--artifact-id", required=True)
    status = ios_sub.add_parser("status")
    status.add_argument("--version", dest="app_version"); status.add_argument("--build-number")
    analytics = subs.add_parser("analytics")
    analytics_sub = analytics.add_subparsers(dest="action", required=True)
    fetch = analytics_sub.add_parser("fetch")
    fetch.add_argument("--sources", nargs="+", choices=("ga4", "asc_sales", "apple_analytics"))
    fetch.add_argument("--start-date"); fetch.add_argument("--end-date")
    fetch.add_argument("--refresh", action="store_true")
    report = analytics_sub.add_parser("report")
    report.add_argument("--dataset-id", required=True)
    cloud = subs.add_parser("cloudflare")
    cloud_sub = cloud.add_subparsers(dest="action", required=True)
    for name in ("deploy", "status"):
        sub = cloud_sub.add_parser(name)
        sub.add_argument("--target", required=True); sub.add_argument("--environment")
    job = subs.add_parser("job")
    job_sub = job.add_subparsers(dest="action", required=True)
    for name in ("status", "resume"):
        sub = job_sub.add_parser(name)
        sub.add_argument("job_id")
    worker = subs.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("job_id"); worker.add_argument("generation", type=int)
    return p


def legacy_import(vault, source, prefix):
    from .common import read_json
    from .credentials import reference
    reference(prefix)
    source = Path(source).expanduser().resolve()
    try:
        data = json.loads(private_read(source / "config.yaml", source))
    except Failure:
        raise
    except Exception:
        raise Failure("AUTH_INVALID", "旧运营配置必须是 JSON 格式的 config.yaml") from None
    vault.initialize()
    results = []
    for key, field, suffix, kind in (("apple", "private_key_file", "apple-p8", "apple_p8"),
                                    ("google", "service_account_file", "google-service-account", "google_service_account")):
        path = Path(data.get(key, {}).get(field, "")).expanduser().absolute()
        require(path.is_relative_to(source), "UNSAFE_PATH", "旧配置授权文件超出原授权目录")
        private_read(path, source)
        results.append(vault.import_file(prefix + "/" + suffix, kind, path))
    return {"status": "ready", "imported": results,
        "identity": {k: data.get("app", {}).get(k) for k in ("bundle_id", "team_id", "app_store_id")},
        "apple_identifiers": {k: data.get("apple", {}).get(k) for k in ("key_id", "issuer_id", "vendor_number")},
        "ga4_property_id": data.get("google", {}).get("property_id"), "source_modified": False}


def main(argv=None):
    vault = runtime = None
    args = None
    try:
        args = parser().parse_args(argv)
        home = Path(args.home).expanduser().absolute()
        if args.command == "install":
            from .install import install
            result = install(args.install_project or args.project, home)
        elif args.command == "setup":
            from .install import setup_wrangler
            result = setup_wrangler(home)
        elif args.command == "credentials":
            vault = Vault(home)
            if args.action == "init":
                result = vault.initialize()
            elif args.action == "sweep":
                result = vault.sweep()
            elif args.action == "import":
                result = vault.import_file(args.ref, args.kind, args.file, args.credential_version)
            elif args.action == "import-legacy":
                result = legacy_import(vault, args.source, args.prefix)
            elif args.action == "upload":
                result = vault.upload(args.ref)
            elif args.action == "check":
                from .common import read_json
                catalog = read_json(home / "vault/catalog.json", {})
                refs = catalog.get("credentials", {})
                vault = Vault(home, refs, args.provider)
                vault.read(args.ref)
                result = {"status": "ready", "ref": args.ref, "version": refs[args.ref]["version"]}
            elif args.action == "backup":
                result = vault.backup(args.out, args.password_file)
            else:
                result = vault.restore(args.file, args.password_file)
        elif args.command == "mcp":
            from .mcp_server import create_server
            create_server(args.project, home).run(transport="stdio")
            return 0
        else:
            from .runtime import Runtime
            runtime = Runtime(args.project, home)
            vault = runtime.vault
            if args.command == "doctor":
                result = runtime.doctor(args.capability, args.target, args.environment, args.remote)
            elif args.command == "ios":
                result = runtime.ios_upload_testflight(args.artifact_id) if args.action == "upload" else \
                    runtime.ios_build(args.app_version, args.build_number) if args.action == "build" else \
                    runtime.ios_testflight_status(args.app_version, args.build_number)
            elif args.command == "analytics":
                result = runtime.analytics_fetch(args.sources, args.start_date, args.end_date, args.refresh) if args.action == "fetch" else runtime.analytics_report(args.dataset_id)
            elif args.command == "cloudflare":
                result = runtime.cloudflare_deploy(args.target, args.environment) if args.action == "deploy" else runtime.cloudflare_status(args.target, args.environment)
            elif args.command == "job":
                result = runtime.job_status(args.job_id) if args.action == "status" else runtime.job_resume(args.job_id)
            else:
                result = runtime.jobs.execute(args.job_id, args.generation, runtime)
        result = redact(result, vault.known_secrets if vault else ())
    except Failure as e:
        result = redact(e.result(), vault.known_secrets if vault else ())
    except (KeyboardInterrupt, BrokenPipeError):
        return 130
    except Exception:
        result = {"status": "failed", "artifacts": [], "blockers": [{"code": "INTERNAL_ERROR", "message": "执行失败；原始异常可能含凭据，已隐藏"}]}
    print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr if args and args.command == "mcp" else sys.stdout)
    return 2 if result.get("status") in ("blocked", "failed", "unknown", "interrupted", "not_configured") else 0


if __name__ == "__main__":
    raise SystemExit(main())
