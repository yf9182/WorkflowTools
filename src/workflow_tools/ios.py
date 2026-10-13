from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import stat
import subprocess
import zipfile

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import pkcs12, Encoding, PublicFormat
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from .auth import Apple
from .common import Failure, atomic_bytes, canonical, digest, file_sha, lock, private_read, read_json, require, run, save, secure_dir, stamp
from .config import source_binding


def decode_profile(raw):
    # openssl verifies the CMS signature. Xcode/codesign subsequently verifies distribution trust.
    r = run(["openssl", "cms", "-verify", "-inform", "DER", "-noverify"], stdin=raw)
    require(r.returncode == 0, "PROFILE_INVALID", "描述文件 CMS 签名无效")
    try:
        return plistlib.loads(r.stdout)
    except Exception:
        raise Failure("PROFILE_INVALID", "描述文件内容无效") from None


def validate_profile(data, team, bundle, certificate_sha1=None):
    e = data.get("Entitlements", {})
    expiration = data.get("ExpirationDate")
    require(isinstance(expiration, dt.datetime), "PROFILE_INVALID", "描述文件缺少到期时间")
    require(expiration.replace(tzinfo=dt.timezone.utc) > dt.datetime.now(dt.timezone.utc),
            "AUTH_EXPIRED", "App Store 描述文件已到期", bundle_id=bundle)
    require(team in data.get("TeamIdentifier", []) and e.get("com.apple.developer.team-identifier", team) == team,
            "TARGET_MISMATCH", "描述文件团队不匹配", bundle_id=bundle)
    app_id = e.get("application-identifier", "")
    require(app_id.endswith("." + bundle) and app_id.split(".", 1)[0] in data.get("ApplicationIdentifierPrefix", []),
            "TARGET_MISMATCH", "描述文件 Bundle ID 不匹配", bundle_id=bundle)
    require(e.get("get-task-allow") is False and not data.get("ProvisionedDevices")
            and not data.get("ProvisionsAllDevices"), "PROFILE_NOT_APP_STORE", "需要 App Store 分发描述文件，不能使用开发或 Ad Hoc 文件", bundle_id=bundle)
    require(isinstance(data.get("UUID"), str) and re.fullmatch(r"[0-9A-Fa-f-]{36}", data["UUID"]),
            "PROFILE_INVALID", "描述文件 UUID 无效")
    if certificate_sha1:
        allowed = [hashlib.sha1(bytes(x)).hexdigest().upper() for x in data.get("DeveloperCertificates", [])]
        require(certificate_sha1 in allowed, "TARGET_MISMATCH", "描述文件未包含所提供的 Distribution 证书", bundle_id=bundle)
    return {"uuid": data["UUID"], "bundle_id": bundle, "expires_at": expiration.isoformat(), "type": "app-store"}


def signing_metadata(project, vault):
    ios = project.section("ios")
    signing = ios.get("signing", {})
    raw = vault.read(signing["p12_ref"], "pkcs12")
    password = vault.text(signing["password_ref"], "password")
    try:
        key, cert, _ = pkcs12.load_key_and_certificates(raw, password.encode())
    except Exception:
        raise Failure("P12_INVALID", "无法读取 Distribution .p12，请检查文件与密码") from None
    require(key is not None and cert is not None, "P12_INVALID", ".p12 必须包含证书及私钥")
    require(key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo) ==
            cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo), "P12_INVALID", "证书与私钥不匹配")
    require(cert.not_valid_before_utc <= dt.datetime.now(dt.timezone.utc) < cert.not_valid_after_utc,
            "AUTH_EXPIRED", "Distribution 证书未生效或已到期")
    names = [x.value for x in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
    teams = [x.value for x in cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)]
    require(ios["team_id"] in teams and any(x.startswith(("Apple Distribution:", "iPhone Distribution:")) for x in names),
            "TARGET_MISMATCH", "需要目标团队的 Apple Distribution 证书")
    fingerprint = cert.fingerprint(hashes.SHA1()).hex().upper()
    profiles = {}
    for bundle, ref in signing.get("profiles", {}).items():
        profiles[bundle] = validate_profile(decode_profile(vault.read(ref, "provisioning_profile")), ios["team_id"], bundle, fingerprint)
    require(ios["bundle_id"] in profiles, "AUTH_MISSING", "缺少主 App 的 App Store 描述文件")
    return {"certificate_sha1": fingerprint, "certificate_expires_at": cert.not_valid_after_utc.isoformat(), "profiles": profiles}


def native_helper(home):
    source = Path(__file__).parent / "assets/native/keychain.swift"
    path = Path(home) / "cache/native" / file_sha(source) / "workflow-keychain"
    require(not path.is_symlink(), "UNSAFE_PATH", "签名助手不能是符号链接")
    if not path.is_file():
        with lock(Path(home) / "locks/native-build.lock"):
            if not path.is_file():
                secure_dir(path.parent)
                r = run(["xcrun", "swiftc", "-module-cache-path", str(path.parent / "swift-cache"), str(source), "-o", str(path)], timeout=180)
                require(r.returncode == 0, "SIGNING_HELPER_UNAVAILABLE", "无法编译本机签名助手，请检查 Xcode 工具链")
                os.chmod(path, 0o700)
    return path


def helper(home, payload):
    r = run([str(native_helper(home))], stdin=canonical(payload), timeout=60)
    try:
        result = json.loads(r.stdout)
    except Exception:
        result = {}
    require(r.returncode == 0 and result.get("status") == "ready", "KEYCHAIN_UNAVAILABLE",
            "受控钥匙串操作失败；不会弹出登录或密码窗口", os_status=result.get("os_status"))
    return result


def cleanup_signing_session(home, directory):
    journal = read_json(Path(directory) / "signing-session.json")
    if not journal:
        return
    keychain = Path(journal["keychain"])
    require(keychain.parent == Path(directory), "UNSAFE_PATH", "签名恢复路径无效")
    helper(home, {"action": "remove", "keychain": str(keychain)})
    for entry in journal.get("installed_profiles", []):
        path = Path(entry["path"])
        expected = Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles"
        require(path.parent == expected and re.fullmatch(r"[0-9A-Fa-f-]{36}\.mobileprovision", path.name),
                "UNSAFE_PATH", "描述文件恢复路径无效")
        if path.exists():
            require(not path.is_symlink() and file_sha(path) == entry["sha256"], "SIGNING_CLEANUP_CONFLICT", "安装的描述文件被其他进程修改，未删除")
            path.unlink()
    (Path(directory) / "signing-session.json").unlink()


def inspect_ipa(path, expected, certificate_sha1=None, verify_signature=False, scratch=None):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "IPA_INVALID", "IPA 必须是普通文件")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            require(sum(x.file_size for x in entries) < 4 * 1024**3, "IPA_INVALID", "IPA 解压大小超过限制")
            for entry in entries:
                p = Path(entry.filename)
                require(not p.is_absolute() and ".." not in p.parts and not stat.S_ISLNK(entry.external_attr >> 16),
                        "IPA_INVALID", "IPA 包含不安全路径或符号链接")
            mains = [x.filename for x in entries if re.fullmatch(r"Payload/[^/]+\.app/Info.plist", x.filename)]
            require(len(mains) == 1, "IPA_INVALID", "IPA 主 App 不唯一")
            app_path = mains[0].rsplit("/", 1)[0]
            info = plistlib.loads(archive.read(mains[0]))
            actual = {"bundle_id": info.get("CFBundleIdentifier"), "version": str(info.get("CFBundleShortVersionString", "")),
                      "build_number": str(info.get("CFBundleVersion", ""))}
            require(all(str(actual.get(k)) == str(expected[k]) for k in actual), "TARGET_MISMATCH", "实际 IPA 身份与目标不匹配")
            bundles = {}
            for entry in entries:
                if entry.filename == mains[0] or re.fullmatch(r"Payload/[^/]+\.app/PlugIns/[^/]+\.appex/Info.plist", entry.filename):
                    prefix = entry.filename.rsplit("/", 1)[0]
                    bundle_info = plistlib.loads(archive.read(entry.filename))
                    bundle = bundle_info["CFBundleIdentifier"]
                    profile = decode_profile(archive.read(prefix + "/embedded.mobileprovision"))
                    bundles[bundle] = validate_profile(profile, expected["team_id"], bundle, certificate_sha1)
            if verify_signature:
                require(scratch is not None, "IPA_INVALID", "缺少签名校验临时目录")
                destination = secure_dir(scratch)
                archive.extractall(destination)
                r = run(["codesign", "--verify", "--deep", "--strict", str(destination / app_path)], timeout=120)
                require(r.returncode == 0, "IPA_SIGNATURE_INVALID", "IPA 的实际签名校验失败")
            return {**actual, "team_id": expected["team_id"], "profiles": bundles, "ipa_sha256": file_sha(path)}
    except Failure:
        raise
    except Exception:
        raise Failure("IPA_INVALID", "IPA 缺少有效身份、签名或描述文件") from None


def artifact(project, artifact_id):
    require(isinstance(artifact_id, str) and re.fullmatch(r"[0-9a-f]{64}", artifact_id), "ARTIFACT_INVALID", "制品编号无效")
    d = read_json(project.artifacts / (artifact_id + ".json"))
    require(d is not None and d.get("artifact_id") == artifact_id and d.get("project_root") == str(project.root),
            "ARTIFACT_NOT_FOUND", "本项目没有这个 IPA 制品")
    path = Path(d["ipa"])
    require(path.resolve().is_relative_to(project.artifacts.resolve()) and not path.is_symlink(), "UNSAFE_PATH", "IPA 超出项目制品目录")
    require(file_sha(path) == d["ipa_sha256"], "IPA_CHANGED", "IPA 已改变，不能继续上传")
    ios = project.section("ios")
    require(all(str(d[k]) == str(ios[k]) for k in ("bundle_id", "team_id", "app_store_id")),
            "TARGET_MISMATCH", "IPA 制品与当前项目身份不匹配")
    return d


def _json_output(raw):
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except Exception:
        raise Failure("XCODE_INVALID_RESPONSE", "Xcode 返回了无效构建配置") from None


def _project_args(project, root=None):
    ios = project.section("ios")
    chosen = ios.get("workspace") or ios.get("project")
    original = project.resolve(chosen, exists=True)
    location = (Path(root) / original.relative_to(project.root)) if root else original
    return ["-workspace" if ios.get("workspace") else "-project", str(location), "-scheme", ios["scheme"], "-configuration", "Release"]


def build(runtime, params, job_id, checkpoint):
    project, vault = runtime.project, runtime.vault
    runtime.enforce("ios_build")
    ios = project.section("ios")
    checkpoint("waiting_for_signing_lock")
    with lock(project.home / "locks/macos-signing.lock", timeout=4 * 3600):
        sweep = vault.sweep()
        require(sweep["status"] == "ready", "AUTH_CLEANUP_FAILED", "存在无法安全清理的签名残留")
        signing = signing_metadata(project, vault)
        items = {"distribution": (ios["signing"]["p12_ref"], "pkcs12", ".p12")}
        items.update({"profile" + str(i): (ref, "provisioning_profile", ".mobileprovision")
                      for i, ref in enumerate(ios["signing"]["profiles"].values())})
        with vault.lease(items) as files:
            directory = files["distribution"].parent
            journal_path = directory / "signing-session.json"
            journal = {"keychain": str(directory / "signing.keychain-db"), "installed_profiles": []}
            save(journal_path, journal)
            try:
                checkpoint("preparing_signing")
                helper(project.home, {"action": "create", "keychain": journal["keychain"],
                    "keychain_password": secrets.token_hex(32), "p12": str(files["distribution"]),
                    "p12_password": vault.text(ios["signing"]["password_ref"], "password")})
                profile_root = secure_dir(Path.home() / "Library/Developer/Xcode/UserData/Provisioning Profiles")
                for i, (bundle, meta) in enumerate(signing["profiles"].items()):
                    raw = files["profile" + str(i)].read_bytes()
                    destination = profile_root / (meta["uuid"] + ".mobileprovision")
                    if destination.exists():
                        require(not destination.is_symlink() and destination.read_bytes() == raw,
                                "PROFILE_CONFLICT", "本机已有同 UUID 的不同描述文件")
                    else:
                        # Journal first, so SIGKILL cleanup is recoverable.
                        journal["installed_profiles"].append({"path": str(destination), "sha256": hashlib.sha256(raw).hexdigest()})
                        save(journal_path, journal)
                        atomic_bytes(destination, raw)
                work = secure_dir(project.artifacts / ("build-" + job_id))
                checkout = work / "checkout"
                before = source_binding(project)
                require(not params.get("source_binding") or params["source_binding"] == before,
                        "SOURCE_CHANGED", "源码在任务排队后发生变化，请重新构建")
                if checkout.exists():
                    shutil.rmtree(checkout)
                shutil.copytree(project.root, checkout, ignore=shutil.ignore_patterns(
                    ".git", ".build", ".workflow-tools", ".agents", ".codex", ".venv", "node_modules", "__pycache__",
                    ".env", ".dev.vars", "*.p8", "*.p12", "*.key", "*.keychain*"))
                require(before == source_binding(project), "SOURCE_CHANGED", "复制源码期间发生变化")
                log = work / "build.log"
                r = run(["xcodebuild", *_project_args(project, checkout), "-showBuildSettings", "-json"], timeout=180, log=log)
                require(r.returncode == 0, "XCODE_SETTINGS_FAILED", "无法读取 Release 构建设置")
                settings = _json_output(r.stdout)
                mapping = {(x["buildSettings"].get("PROJECT_FILE_PATH", ""), x["target"]): x["buildSettings"] for x in settings}
                app_settings = [x["buildSettings"] for x in settings if x["buildSettings"].get("PRODUCT_BUNDLE_IDENTIFIER") == ios["bundle_id"]]
                require(len(app_settings) == 1, "TARGET_MISMATCH", "共享 scheme 中主 App 不唯一")
                version = str(params.get("version") or app_settings[0].get("MARKETING_VERSION", ""))
                number = str(params.get("build_number") or app_settings[0].get("CURRENT_PROJECT_VERSION", ""))
                require(re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version) and re.fullmatch(r"[1-9]\d*", number),
                        "VERSION_INVALID", "版本号需要数字版本；构建号需要正整数")
                configured_bundles = set()
                # Modify only the ephemeral copy. plistlib writes an Xcode-readable XML property list.
                for pbx in checkout.rglob("project.pbxproj"):
                    result = run(["plutil", "-convert", "json", "-o", "-", str(pbx)])
                    require(result.returncode == 0, "XCODE_PROJECT_INVALID", "无法解析工程属性列表")
                    document = _json_output(result.stdout)
                    objects = document["objects"]
                    for obj in objects.values():
                        if obj.get("isa") != "PBXNativeTarget" or obj.get("productType") not in (
                            "com.apple.product-type.application", "com.apple.product-type.app-extension", "com.apple.product-type.application.watchapp2",
                            "com.apple.product-type.watchkit2-extension", "com.apple.product-type.application.on-demand-install-capable"):
                            continue
                        resolved = mapping.get((str(pbx.parent), obj.get("name")), {})
                        if not resolved:
                            continue  # another App/sample target outside the requested scheme
                        for config_id in objects[obj["buildConfigurationList"]]["buildConfigurations"]:
                            cfg = objects[config_id]["buildSettings"]
                            bundle = resolved.get("PRODUCT_BUNDLE_IDENTIFIER") or cfg.get("PRODUCT_BUNDLE_IDENTIFIER", "")
                            require(bundle in signing["profiles"], "AUTH_MISSING", "签名 target 缺少描述文件", bundle_id=bundle)
                            configured_bundles.add(bundle)
                            cfg.update(CODE_SIGN_STYLE="Manual", DEVELOPMENT_TEAM=ios["team_id"],
                                CODE_SIGN_IDENTITY=signing["certificate_sha1"],
                                PROVISIONING_PROFILE_SPECIFIER=signing["profiles"][bundle]["uuid"],
                                MARKETING_VERSION=version, CURRENT_PROJECT_VERSION=number,
                                OTHER_CODE_SIGN_FLAGS="--keychain " + journal["keychain"].replace(" ", "\\ "))
                    pbx.write_bytes(plistlib.dumps(document))
                require(ios["bundle_id"] in configured_bundles, "TARGET_MISMATCH", "未找到主 App 签名 target")
                checkpoint("archiving", version=version, build_number=number)
                archive = work / "app.xcarchive"
                r = run(["xcodebuild", *_project_args(project, checkout), "-destination", "generic/platform=iOS",
                    "-derivedDataPath", str(work / "DerivedData"), "-archivePath", str(archive), "archive"],
                    timeout=3 * 3600, secret_values=vault.known_secrets, log=log)
                require(r.returncode == 0 and archive.is_dir(), "ARCHIVE_FAILED", "Xcode 归档失败，请查看脱敏构建日志", log=str(log))
                options = {"method": "app-store-connect", "teamID": ios["team_id"], "signingStyle": "manual",
                    "signingCertificate": signing["certificate_sha1"], "provisioningProfiles": {k: v["uuid"] for k, v in signing["profiles"].items()},
                    "manageAppVersionAndBuildNumber": False, "uploadSymbols": False}
                export_plist = work / "ExportOptions.plist"
                atomic_bytes(export_plist, plistlib.dumps(options))
                checkpoint("exporting")
                r = run(["xcodebuild", "-exportArchive", "-archivePath", str(archive), "-exportPath", str(work / "export"),
                    "-exportOptionsPlist", str(export_plist)], timeout=3600, secret_values=vault.known_secrets, log=log)
                ipas = list((work / "export").glob("*.ipa"))
                require(r.returncode == 0 and len(ipas) == 1, "EXPORT_FAILED", "IPA 导出失败或产物不唯一", log=str(log))
                checkpoint("verifying_ipa")
                identity = {"bundle_id": ios["bundle_id"], "team_id": ios["team_id"], "version": version, "build_number": number}
                actual = inspect_ipa(ipas[0], identity, signing["certificate_sha1"], True, work / "ipa-check")
                require(set(actual["profiles"]) == configured_bundles, "TARGET_MISMATCH", "IPA 的 App/扩展集合与签名 target 不一致")
                artifact_id = digest({"ipa": actual["ipa_sha256"], "source": before, "project": str(project.root)})
                metadata = {**actual, "artifact_id": artifact_id, "project_root": str(project.root), "app_store_id": str(ios["app_store_id"]),
                    "ipa": str(ipas[0]), "source_binding": before, "certificate_sha1": signing["certificate_sha1"], "created_at": stamp(), "log": str(log)}
                save(project.artifacts / (artifact_id + ".json"), metadata)
                return {"status": "succeeded", "artifact_id": artifact_id, "artifacts": [metadata], "blockers": []}
            finally:
                cleanup_signing_session(project.home, directory)


def status(runtime, version=None, build_number=None):
    runtime.enforce("ios_status")
    apple = Apple(runtime.project, runtime.vault, runtime.http)
    apple.identity()
    builds = apple.builds(version, build_number)
    return {"status": "ready", "builds": [{"build_id": x["id"], "build_number": x.get("attributes", {}).get("version"),
        "processing_state": x.get("attributes", {}).get("processingState"), "expired": x.get("attributes", {}).get("expired"),
        "uploaded_at": x.get("attributes", {}).get("uploadedDate")} for x in builds], "blockers": [], "artifacts": []}


def upload(runtime, params, job_id, checkpoint):
    runtime.enforce("ios_upload")
    project, vault = runtime.project, runtime.vault
    candidate = artifact(project, params["artifact_id"])
    apple = Apple(project, vault, runtime.http)
    apple.identity()
    slot_key = digest({k: candidate[k] for k in ("app_store_id", "version", "build_number")})
    slot_path = project.home / "state/uploads" / (slot_key + ".json")
    with lock(project.home / "locks" / ("upload-" + slot_key + ".lock"), timeout=30):
        slot = read_json(slot_path)
        builds = apple.builds(candidate["version"], candidate["build_number"])
        require(len(builds) <= 1, "UPLOAD_UNKNOWN", "远端同版本构建不唯一")
        if slot:
            require(slot.get("ipa_sha256") == candidate["ipa_sha256"] and slot.get("bundle_id") == candidate["bundle_id"],
                    "UPLOAD_CONFLICT", "该 App/版本/构建号已保留给另一 IPA")
        else:
            require(not builds, "UPLOAD_CONFLICT", "远端已有同版本构建，缺少本工具的 IPA 绑定记录；不重复上传")
        if not slot:
            items = {"asc": (project.section("ios")["apple"]["p8_ref"], "apple_p8", ".p8")}
            with vault.lease(items) as files:
                check = inspect_ipa(candidate["ipa"], candidate, candidate["certificate_sha1"], True,
                                    project.artifacts / ("upload-check-" + job_id))
                require(check["ipa_sha256"] == candidate["ipa_sha256"], "IPA_CHANGED", "IPA 已改变")
                a = project.section("ios")["apple"]
                base_args = ["xcrun", "altool", "-f", candidate["ipa"], "--api-key", a["key_id"],
                             "--api-issuer", a["issuer_id"], "--p8-file-path", str(files["asc"]), "--output-format", "json"]
                checkpoint("validating_upload")
                r = run(base_args + ["--validate-app"], timeout=1800, secret_values=vault.known_secrets)
                require(r.returncode == 0, "IPA_REJECTED", "Apple IPA 验证失败，未执行上传")
                slot = {k: candidate[k] for k in ("app_store_id", "bundle_id", "team_id", "version", "build_number", "ipa_sha256")}
                slot.update(status="intent", created_at=stamp(), job_id=job_id, project_root=str(project.root))
                save(slot_path, slot)  # durable intent BEFORE the external mutation
                checkpoint("uploading", upload_intent=str(slot_path))
                try:
                    r = run(base_args + ["--upload-app"], timeout=3600, secret_values=vault.known_secrets)
                except (subprocess.TimeoutExpired, OSError):
                    raise Failure("UPLOAD_UNKNOWN", "上传结果未知；恢复仅查询远端，不重新上传") from None
                text = (r.stdout + r.stderr).decode("utf-8", "replace")
                delivery = re.search(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", text)
                try:
                    response = json.loads(r.stdout)
                except Exception:
                    response = {}
                delivery_id = response.get("upload-results", {}).get("delivery-uuid") if isinstance(response.get("upload-results"), dict) else None
                delivery_id = delivery_id or (delivery.group(0) if delivery else None)
                require(r.returncode == 0 and delivery_id, "UPLOAD_UNKNOWN", "未获得可确认的 Delivery UUID；恢复仅查询远端")
                slot.update(status="uploaded", delivery_uuid=delivery_id, uploaded_at=stamp())
                save(slot_path, slot)
        checkpoint("reading_processing_state")
        builds = apple.builds(candidate["version"], candidate["build_number"])
        if not builds:
            require(slot["status"] == "uploaded", "UPLOAD_UNKNOWN", "存在未确认上传意图，且远端尚未出现构建；禁止重新上传")
            return {"status": "succeeded", "uploaded": True, "processing_state": "not_visible_yet",
                    "delivery_uuid": slot.get("delivery_uuid"), "artifacts": [], "blockers": []}
        require(len(builds) == 1, "UPLOAD_UNKNOWN", "远端构建不唯一")
        b = builds[0]
        slot.update(status="remote_visible", build_id=b["id"], processing_state=b["attributes"].get("processingState"), confirmed_at=stamp())
        save(slot_path, slot)
        require(not b["attributes"].get("expired") and slot["processing_state"] not in ("FAILED", "INVALID"),
                "APPLE_PROCESSING_FAILED", "Apple 构建处理失败或构建已过期")
        return {"status": "succeeded", "uploaded": True, "build_id": b["id"], "processing_state": slot["processing_state"],
                "delivery_uuid": slot.get("delivery_uuid"), "artifacts": [], "blockers": []}
