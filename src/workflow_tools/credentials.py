"""Encrypted credential objects. Secret bytes never form a tool result."""
from __future__ import annotations

import base64
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from .common import Failure, atomic_bytes, canonical, digest, lock, private_read, read_json, require, save, secure_dir, stamp


KINDS = {"apple_p8", "google_service_account", "pkcs12", "password", "provisioning_profile", "api_token"}
PREFIX = "workflow-tools/v1/"


def outside_repository(path):
    path = Path(path).expanduser().absolute()
    require(not any((p / ".git").exists() for p in (path, *path.parents)),
            "AUTH_LOCATION", "授权、连接和授权备份必须位于 Git 仓库外")
    return path


def reference(ref):
    require(isinstance(ref, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]{0,127}", ref)
            and all(x not in ("", ".", "..") for x in ref.split("/")),
            "AUTH_REF_INVALID", "凭据引用格式无效")
    return ref


def encode(data):
    return base64.b64encode(data).decode("ascii")


def decode(data):
    try:
        return base64.b64decode(data, validate=True)
    except Exception:
        raise Failure("AUTH_INVALID", "授权数据编码无效") from None


def encrypt(key, raw, ref, version, kind):
    reference(ref)
    require(kind in KINDS and isinstance(version, int) and version >= 1,
            "AUTH_INVALID", "凭据类型或版本无效")
    metadata = {"schema_version": 1, "ref": ref, "version": version, "kind": kind}
    nonce = secrets.token_bytes(12)
    return {**metadata, "nonce": encode(nonce),
            "ciphertext": encode(AESGCM(key).encrypt(nonce, raw, canonical(metadata)))}


def decrypt(key, obj, ref, version, kind):
    metadata = {"schema_version": 1, "ref": reference(ref), "version": version, "kind": kind}
    require(isinstance(obj, dict) and all(obj.get(k) == v for k, v in metadata.items()),
            "AUTH_BINDING_MISMATCH", "云端凭据的引用、类型或版本不匹配")
    try:
        return AESGCM(key).decrypt(decode(obj["nonce"]), decode(obj["ciphertext"]), canonical(metadata))
    except Exception:
        raise Failure("AUTH_INTEGRITY", "凭据完整性验证失败，请检查连接授权和凭据版本") from None


def validate_bytes(kind, raw):
    require(kind in KINDS and raw, "AUTH_INVALID", "凭据类型无效或文件为空")
    try:
        if kind == "apple_p8":
            key = load_pem_private_key(raw, password=None)
            require(isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1),
                    "AUTH_INVALID", "ASC 密钥必须是 P-256 私钥")
        elif kind == "google_service_account":
            d = json.loads(raw)
            require(d.get("type") == "service_account" and d.get("client_email") and d.get("private_key"),
                    "AUTH_INVALID", "需要 Google 服务账号 JSON")
            require(isinstance(load_pem_private_key(d["private_key"].encode(), password=None), rsa.RSAPrivateKey),
                    "AUTH_INVALID", "Google 服务账号需要 RSA 私钥")
        elif kind in ("password", "api_token"):
            text = raw.decode("utf-8").rstrip("\r\n")
            require(bool(text) and "\n" not in text and "\r" not in text,
                    "AUTH_INVALID", "密码或 token 文件必须只有一行")
            if kind == "api_token":
                require(re.fullmatch(r"[A-Za-z0-9_.~+/=-]+", text), "AUTH_INVALID", "API token 不能含空白或控制字符")
        elif kind == "pkcs12":
            require(raw.startswith(b"0"), "AUTH_INVALID", "需要 DER 编码的 PKCS#12 文件")
    except Failure:
        raise
    except Exception:
        raise Failure("AUTH_INVALID", "授权文件格式无效") from None


class Vault:
    def __init__(self, home, refs=None, provider="r2", s3=None):
        self.home = outside_repository(home)
        self.refs = refs or {}
        self.provider = provider
        self.bootstrap_path = self.home / "bootstrap/connection.json"
        self._s3_override = s3
        self._known = []
        self._cache = {}

    @property
    def known_secrets(self):
        return tuple(self._known)

    def remember(self, raw):
        try:
            text = raw.decode().rstrip("\r\n")
            self._known.append(text)
            if text.startswith("{"):
                obj = json.loads(text)
                for k in ("private_key", "access_token", "client_secret", "secret_access_key", "api_token"):
                    if obj.get(k):
                        self._known.append(obj[k])
        except (UnicodeError, ValueError):
            pass
        return raw

    def connection(self):
        try:
            d = json.loads(private_read(self.bootstrap_path, self.home))
            key = decode(d["encryption_key"])
            require(d.get("schema_version") == 1 and len(key) == 32,
                    "AUTH_INVALID", "连接授权格式无效")
            self._known.extend([d["encryption_key"], d.get("r2", {}).get("secret_access_key", "")])
            return d, key
        except Failure:
            raise
        except Exception:
            raise Failure("AUTH_INVALID", "连接授权格式无效") from None

    def initialize(self):
        secure_dir(self.home)
        with lock(self.home / "locks/vault.lock"):
            if self.bootstrap_path.exists():
                self.connection()
                return {"status": "ready", "created": False, "file": str(self.bootstrap_path)}
            save(self.bootstrap_path, {"schema_version": 1, "encryption_key": encode(secrets.token_bytes(32)),
                "r2": {"account_id": "", "bucket": "", "access_key_id": "", "secret_access_key": "", "jurisdiction": ""}})
            secure_dir(self.home / "incoming")
            for folder in ("apple/profiles", "google", "cloudflare"):
                secure_dir(self.home / "incoming" / folder)
            save(self.home / "admin/r2-writer.example.json", {"access_key_id": "", "secret_access_key": ""})
            save(self.home / "vault/catalog.json", {"schema_version": 1, "credentials": {}})
        return {"status": "ready", "created": True, "file": str(self.bootstrap_path),
                "message": "请在连接授权文件填入 R2 桶级只读访问凭据；不要贴入聊天"}

    def client(self, writer=False):
        if self._s3_override is not None:
            return self._s3_override, "test-vault"
        connection, _ = self.connection()
        c = connection.get("r2", {})
        if writer:
            try:
                c = {**c, **json.loads(private_read(self.home / "admin/r2-writer.json", self.home))}
            except (ValueError, TypeError):
                raise Failure("AUTH_INVALID", "R2 写授权格式无效") from None
        require(re.fullmatch(r"[0-9a-f]{32}", c.get("account_id", "")) is not None,
                "AUTH_MISSING", "请填写 Cloudflare Account ID")
        require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", c.get("bucket", "")) is not None,
                "AUTH_MISSING", "请填写私有 R2 桶名")
        require(c.get("access_key_id") and c.get("secret_access_key"), "AUTH_MISSING",
                "请提供 R2 桶级写授权" if writer else "请提供 R2 桶级只读授权")
        jurisdiction = c.get("jurisdiction", "")
        require(jurisdiction in ("", "eu", "us", "fedramp"), "AUTH_INVALID", "R2 jurisdiction 无效")
        suffix = ("." + jurisdiction) if jurisdiction else ""
        import boto3
        from botocore.config import Config
        self._known.append(c["secret_access_key"])
        client = boto3.client("s3", endpoint_url=f"https://{c['account_id']}{suffix}.r2.cloudflarestorage.com",
            aws_access_key_id=c["access_key_id"], aws_secret_access_key=c["secret_access_key"], region_name="auto",
            config=Config(signature_version="s3v4", connect_timeout=10, read_timeout=30,
                          retries={"max_attempts": 2, "mode": "standard"}))
        return client, c["bucket"]

    def metadata(self, ref, expected_kind=None):
        reference(ref)
        require(ref in self.refs, "AUTH_REF_UNREGISTERED", "项目没有登记这个凭据引用", ref=ref)
        m = self.refs[ref]
        require(isinstance(m, dict) and m.get("kind") in KINDS and isinstance(m.get("version"), int)
                and m["version"] >= 1, "CONFIG_INVALID", "凭据引用需要 kind 和正整数 version", ref=ref)
        require(expected_kind is None or m["kind"] == expected_kind, "AUTH_BINDING_MISMATCH", "凭据类型不匹配", ref=ref)
        return m

    @staticmethod
    def object_key(ref, version):
        return PREFIX + "objects/" + digest(reference(ref)) + "/" + str(version) + ".json"

    def read(self, ref, kind=None):
        m = self.metadata(ref, kind)
        cache_key = (ref, m["version"], m["kind"])
        if cache_key in self._cache:
            return self._cache[cache_key]
        _, key = self.connection()
        object_key = self.object_key(ref, m["version"])
        if self.provider == "local":
            raw = private_read(self.home / "vault" / object_key, self.home)
        else:
            client, bucket = self.client()
            try:
                body = client.get_object(Bucket=bucket, Key=object_key)["Body"]
                try:
                    raw = body.read(24 * 1024 * 1024)
                finally:
                    body.close()
            except Exception:
                raise Failure("AUTH_REMOTE_UNAVAILABLE", "无法读取云端凭据，请检查 R2 只读权限和凭据版本", ref=ref) from None
        try:
            obj = json.loads(raw)
        except Exception:
            raise Failure("AUTH_INTEGRITY", "加密凭据对象损坏", ref=ref) from None
        plain = decrypt(key, obj, ref, m["version"], m["kind"])
        validate_bytes(m["kind"], plain)
        self._cache[cache_key] = self.remember(plain)
        return plain

    def text(self, ref, kind):
        return self.read(ref, kind).decode().rstrip("\r\n")

    def import_file(self, ref, kind, source, version=None):
        reference(ref)
        outside_repository(source)
        raw = private_read(source)
        validate_bytes(kind, raw)
        _, key = self.connection()
        with lock(self.home / "locks/vault.lock"):
            catalog = read_json(self.home / "vault/catalog.json", {"schema_version": 1, "credentials": {}})
            old = catalog["credentials"].get(ref, {})
            chosen = version or old.get("version", 0) + 1
            require(isinstance(chosen, int) and chosen > old.get("version", 0),
                    "AUTH_VERSION_CONFLICT", "轮换必须使用更高的凭据版本")
            obj = encrypt(key, raw, ref, chosen, kind)
            path = self.home / "vault" / self.object_key(ref, chosen)
            require(not path.exists(), "AUTH_VERSION_CONFLICT", "该凭据版本已存在，不能覆盖")
            save(path, obj)
            catalog["credentials"][ref] = {"kind": kind, "version": chosen, "updated_at": stamp()}
            save(self.home / "vault/catalog.json", catalog)
        return {"status": "ready", "ref": ref, "kind": kind, "version": chosen,
                "credential_ref": {ref: {"kind": kind, "version": chosen}}}

    def upload(self, ref):
        reference(ref)
        catalog = read_json(self.home / "vault/catalog.json", {})
        require(ref in catalog.get("credentials", {}), "AUTH_MISSING", "本地尚未导入该凭据", ref=ref)
        m = catalog["credentials"][ref]
        _, key = self.connection()
        object_key = self.object_key(ref, m["version"])
        raw = private_read(self.home / "vault" / object_key, self.home)
        decrypt(key, json.loads(raw), ref, m["version"], m["kind"])
        client, bucket = self.client(writer=True)
        try:
            # Private objects only. Conditional creation makes immutable versions safe to retry.
            try:
                client.put_object(Bucket=bucket, Key=object_key, Body=raw,
                                  ContentType="application/json", IfNoneMatch="*")
            except Exception:
                pass  # Reconcile the exact object even if the PUT outcome is unknown.
            body = client.get_object(Bucket=bucket, Key=object_key)["Body"]
            try:
                actual = body.read(24 * 1024 * 1024)
            finally:
                body.close()
            require(actual == raw, "AUTH_VERSION_CONFLICT", "云端同版本对象不同或上传未确认")
        except Failure:
            raise
        except Exception:
            raise Failure("AUTH_REMOTE_UNAVAILABLE", "R2 上传结果未确认，请用同一凭据版本重试") from None
        return {"status": "ready", "ref": ref, "version": m["version"], "verified": True}

    @contextlib.contextmanager
    def lease(self, items):
        """Hold a kernel lock throughout a file lease; sweep only abandoned leases."""
        run_root = secure_dir(self.home / "run/credentials")
        directory = Path(tempfile.mkdtemp(prefix="lease-", dir=run_root))
        os.chmod(directory, 0o700)
        with lock(directory / "active.lock", timeout=0):
            try:
                files = {}
                for label, (ref, kind, suffix) in items.items():
                    require(re.fullmatch(r"[a-zA-Z0-9_-]+", label) and re.fullmatch(r"\.[a-zA-Z0-9]+", suffix),
                            "AUTH_INVALID", "临时授权文件名称无效")
                    files[label] = directory / (label + suffix)
                    atomic_bytes(files[label], self.read(ref, kind))
                yield files
            finally:
                if (directory / "signing-session.json").exists():
                    raise Failure("AUTH_CLEANUP_FAILED", "签名恢复记录仍在，保留受限目录供下次清理", directory=str(directory))
                try:
                    shutil.rmtree(directory)
                except OSError:
                    raise Failure("AUTH_CLEANUP_FAILED", "授权临时文件清理失败，相关任务需检查", directory=str(directory)) from None

    def sweep(self):
        root = self.home / "run/credentials"
        removed, active, refused = 0, 0, 0
        if not root.exists():
            return {"status": "ready", "removed": 0, "active": 0}
        for directory in root.glob("lease-*"):
            if directory.is_symlink() or not directory.is_dir():
                refused += 1
                continue
            try:
                with lock(directory / "active.lock", timeout=0):
                    if (directory / "signing-session.json").exists():
                        from .ios import cleanup_signing_session
                        cleanup_signing_session(self.home, directory)
                    shutil.rmtree(directory)
                    removed += 1
            except Failure as e:
                if e.code == "RESOURCE_BUSY":
                    active += 1
                else:
                    refused += 1
            except OSError:
                refused += 1
        return {"status": "blocked" if refused else "ready", "removed": removed, "active": active, "refused": refused}

    def backup(self, output, password_file):
        password = private_read(password_file).rstrip(b"\r\n")
        require(len(password) >= 12, "AUTH_INVALID", "备份口令至少 12 字节")
        connection, _ = self.connection()
        catalog = read_json(self.home / "vault/catalog.json", {"schema_version": 1, "credentials": {}})
        objects = {}
        for ref, m in catalog["credentials"].items():
            k = self.object_key(ref, m["version"])
            objects[k] = json.loads(private_read(self.home / "vault" / k, self.home))
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        key = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password)
        raw = canonical({"connection": connection, "catalog": catalog, "objects": objects})
        obj = {"schema_version": 1, "salt": encode(salt), "nonce": encode(nonce),
               "ciphertext": encode(AESGCM(key).encrypt(nonce, raw, b"workflow-tools-backup-v1"))}
        output = outside_repository(output)
        require(not output.exists(), "AUTH_VERSION_CONFLICT", "备份文件已存在，不能覆盖")
        atomic_bytes(output, canonical(obj))
        return {"status": "ready", "file": str(output), "credential_count": len(objects)}

    def restore(self, source, password_file):
        require(not self.bootstrap_path.exists(), "AUTH_VERSION_CONFLICT", "目标已有连接授权，不能覆盖")
        try:
            obj = json.loads(private_read(source))
            require(obj.get("schema_version") == 1, "AUTH_INVALID", "备份版本无效")
            key = Scrypt(salt=decode(obj["salt"]), length=32, n=2**14, r=8, p=1).derive(private_read(password_file).rstrip(b"\r\n"))
            data = json.loads(AESGCM(key).decrypt(decode(obj["nonce"]), decode(obj["ciphertext"]), b"workflow-tools-backup-v1"))
            master = decode(data["connection"]["encryption_key"])
            require(len(master) == 32, "AUTH_INVALID", "备份连接授权无效")
            # No archive extraction: derive every destination from validated references.
            for ref, m in data["catalog"]["credentials"].items():
                decrypt(master, data["objects"][self.object_key(ref, m["version"])], ref, m["version"], m["kind"])
        except Failure:
            raise
        except Exception:
            raise Failure("AUTH_INTEGRITY", "备份损坏或口令错误；未写入授权文件") from None
        with lock(self.home / "locks/vault.lock"):
            require(not self.bootstrap_path.exists(), "AUTH_VERSION_CONFLICT", "目标已有连接授权")
            for ref, m in data["catalog"]["credentials"].items():
                k = self.object_key(ref, m["version"])
                save(self.home / "vault" / k, data["objects"][k])
            save(self.home / "vault/catalog.json", data["catalog"])
            save(self.bootstrap_path, data["connection"])
        return {"status": "ready", "credential_count": len(data["objects"])}
