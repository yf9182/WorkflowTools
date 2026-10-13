"""Capability gates: unrelated credentials and services never block one another."""
from __future__ import annotations

import re
import shutil
import sys
from .common import Failure, require

CAPABILITIES = ("ios_build", "ios_upload_testflight", "ios_testflight_status", "analytics_ga4", "analytics_asc_sales",
                "analytics_apple_analytics", "cloudflare_deploy", "cloudflare_status")
ALIASES = {"ios_upload": "ios_upload_testflight", "ios_status": "ios_testflight_status"}


class Gates:
    def __init__(self, runtime):
        self.runtime, self.project, self.vault = runtime, runtime.project, runtime.vault

    def check(self, capability, target=None, environment=None, remote=False):
        capability = ALIASES.get(capability, capability)
        require(capability in CAPABILITIES, "CAPABILITY_UNKNOWN", "能力名称无效")
        checks, blockers = [], []

        def check(name, fn):
            try:
                fn()
                checks.append({"name": name, "status": "ready"})
            except Failure as e:
                issue = {"check": name, "code": e.code, "message": e.message, **e.details}
                blockers.append(issue)
                checks.append({"name": name, "status": "blocked", "blocker": issue})
            except Exception:
                issue = {"check": name, "code": "CONFIG_INVALID", "message": "配置格式无效，未显示敏感内容"}
                blockers.append(issue)
                checks.append({"name": name, "status": "blocked", "blocker": issue})

        def field(name, value, pattern=None):
            def validate():
                require(isinstance(value, str) and value and (not pattern or re.fullmatch(pattern, value)),
                        "CONFIG_MISSING", "请填写有效配置字段", field=name)
            check(name, validate)

        def credential(name, ref, kind):
            def validate():
                require(isinstance(ref, str) and ref, "CONFIG_MISSING", "请填写凭据引用", field=name)
                self.vault.read(ref, kind)
            check(name, validate)

        def tool(name):
            check(name, lambda: require(shutil.which(name), "TOOL_MISSING", "缺少本机工具", tool=name))

        ios = self.project.section("ios")
        analytics = self.project.section("analytics")
        cf = self.project.section("cloudflare")
        enabled = ios.get("enabled", False) if capability.startswith("ios_") else \
            capability.removeprefix("analytics_") in analytics.get("sources", []) if capability.startswith("analytics_") else bool(cf.get("targets"))
        if not enabled:
            return {"capability": capability, "status": "disabled", "checks": [], "blockers": [
                {"code": "CAPABILITY_DISABLED", "message": "项目未启用此能力"}], "verification": "local"}

        if capability.startswith("ios_") or capability.startswith("analytics_"):
            field("ios.bundle_id", ios.get("bundle_id"), r"[A-Za-z0-9][A-Za-z0-9.-]+")
        if capability.startswith("ios_"):
            field("ios.team_id", ios.get("team_id"), r"[A-Z0-9]{10}")
            field("ios.app_store_id", str(ios.get("app_store_id", "")), r"\d+")

        if capability == "ios_build":
            check("macos", lambda: require(sys.platform == "darwin", "PLATFORM_UNSUPPORTED", "iOS 签名构建需要 macOS"))
            tool("xcodebuild"); tool("xcrun"); tool("openssl"); tool("codesign")
            field("ios.scheme", ios.get("scheme"))
            check("ios.project", lambda: self.project.resolve(ios.get("workspace") or ios.get("project"), exists=True))
            signing = ios.get("signing", {})
            credential("ios.signing.p12_ref", signing.get("p12_ref"), "pkcs12")
            credential("ios.signing.password_ref", signing.get("password_ref"), "password")
            profiles = signing.get("profiles", {})
            check("ios.signing.profiles", lambda: require(isinstance(profiles, dict) and ios.get("bundle_id") in profiles,
                "CONFIG_MISSING", "请按 App/扩展的 Bundle ID 配置描述文件引用"))
            if isinstance(profiles, dict):
                for bundle, ref in profiles.items():
                    credential("profile:" + bundle, ref, "provisioning_profile")
            if not blockers:
                from .ios import signing_metadata
                check("distribution_signing", lambda: signing_metadata(self.project, self.vault))

        uses_apple = capability in ("ios_upload_testflight", "ios_testflight_status", "analytics_asc_sales", "analytics_apple_analytics")
        if uses_apple:
            a = ios.get("apple", {})
            field("ios.app_store_id", str(ios.get("app_store_id", "")), r"\d+")
            field("ios.apple.key_id", a.get("key_id"), r"[A-Za-z0-9]{10}")
            field("ios.apple.issuer_id", a.get("issuer_id"), r"[0-9a-fA-F-]{36}")
            credential("ios.apple.p8_ref", a.get("p8_ref"), "apple_p8")
            if capability == "analytics_asc_sales":
                field("ios.apple.vendor_number", str(a.get("vendor_number", "")), r"\d+")
            if capability == "ios_upload_testflight":
                check("macos", lambda: require(sys.platform == "darwin", "PLATFORM_UNSUPPORTED", "altool 上传需要 macOS"))
                tool("xcrun"); tool("codesign"); tool("openssl")
            if remote and not blockers:
                from .auth import Apple
                check("asc_remote_identity", lambda: Apple(self.project, self.vault, self.runtime.http).identity())

        if capability == "analytics_ga4":
            g = analytics.get("google", {})
            field("analytics.google.property_id", str(g.get("property_id", "")), r"\d+")
            credential("analytics.google.service_account_ref", g.get("service_account_ref"), "google_service_account")
            if remote and not blockers:
                from .auth import Google
                check("ga4_remote_stream", lambda: Google(self.project, self.vault, self.runtime.http).stream())

        if capability.startswith("cloudflare_"):
            field("cloudflare.account_id", cf.get("account_id"), r"[0-9a-f]{32}")
            credential("cloudflare.api_token_ref", cf.get("api_token_ref"), "api_token")
            from .cloudflare import Cloudflare, target_config, wrangler
            if capability == "cloudflare_deploy":
                check("wrangler", lambda: wrangler(self.project.home))
            selected = [target] if target else list(cf.get("targets", {}))
            for name in selected:
                check("target:" + name, lambda name=name: target_config(self.project, self.project.target(name, environment)))
                if remote and not blockers:
                    check("remote:" + name, lambda name=name: Cloudflare(self.runtime).deployments(self.project.target(name, environment)))

        return {"capability": capability, "status": "blocked" if blockers else "ready", "checks": checks,
                "blockers": blockers, "verification": "remote" if remote else "local",
                "remote_access": "checked" if remote and not blockers else "unverified"}

    def doctor(self, capability=None, target=None, environment=None, remote=False):
        names = [capability] if capability else CAPABILITIES
        results = [self.check(n, target, environment, remote) for n in names]
        enabled = [r for r in results if r["status"] != "disabled"]
        return {"status": "blocked" if any(r["status"] == "blocked" for r in enabled) else "ready" if enabled else "not_configured",
                "project_id": self.project.id, "capabilities": results,
                "blockers": [b for r in enabled for b in r["blockers"]], "artifacts": [], "authorization_home": str(self.project.home)}
