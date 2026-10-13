"""Atomic state and kernel locks adapted from CalorieSnapshot ops helpers."""
from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time

LOCK_FDS = contextvars.ContextVar("workflow_lock_fds", default=())


class Failure(Exception):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def result(self):
        return {"status": "blocked", "blockers": [{"code": self.code,
                "message": self.message, **self.details}], "artifacts": []}


def require(ok, code, message, **details):
    if not ok:
        raise Failure(code, message, **details)


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def secure_dir(path):
    path = Path(path).expanduser().absolute()
    # /tmp may be a system symlink; canonicalize its parent before enforcing our subtree.
    require(not path.is_symlink(), "UNSAFE_PATH", "受限目录不能是符号链接")
    for ancestor in path.parents:
        require(not ancestor.is_symlink() or str(ancestor) in ("/tmp", "/var"),
                "UNSAFE_PATH", "受限目录不能经过用户符号链接")
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    require(path.is_dir(), "UNSAFE_PATH", "受限路径不是目录")
    os.chmod(path, 0o700)
    return path.resolve()


def atomic_bytes(path, data, mode=0o600):
    path = Path(path)
    secure_dir(path.parent)
    require(not path.is_symlink(), "UNSAFE_PATH", "不能覆盖符号链接")
    import secrets
    temporary = path.parent / ("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        temporary.unlink(missing_ok=True)


def save(path, value):
    atomic_bytes(path, canonical(value) + b"\n")


def read_json(path, default=None):
    path = Path(path)
    require(not path.is_symlink(), "UNSAFE_PATH", "状态文件不能是符号链接")
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_bytes())
        require(isinstance(value, dict), "STATE_CORRUPT", "状态文件必须是 JSON 对象")
        return value
    except (ValueError, OSError):
        raise Failure("STATE_CORRUPT", "状态文件损坏或不可读") from None


@contextlib.contextmanager
def lock(path, timeout=30):
    """Never unlink a lock inode: concurrent processes must lock the same file."""
    path = Path(path)
    secure_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Failure("RESOURCE_BUSY", "资源正由另一任务使用，请查询任务状态")
                time.sleep(0.05)
        token = LOCK_FDS.set(LOCK_FDS.get() + (fd,))
        try:
            yield fd
        finally:
            LOCK_FDS.reset(token)
    finally:
        os.close(fd)


def private_read(path, root=None):
    path = Path(path).expanduser().absolute()
    if root:
        root = Path(root).resolve()
        require(path.is_relative_to(root), "UNSAFE_PATH", "授权文件超出受限目录")
        current = path
        while current != root:
            require(not current.is_symlink(), "UNSAFE_PATH", "授权路径不能经过符号链接")
            current = current.parent
    require(not path.is_symlink(), "UNSAFE_PATH", "授权文件不能是符号链接")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Failure("AUTH_MISSING", "缺少授权文件", file=str(path)) from None
    except OSError:
        raise Failure("AUTH_UNREADABLE", "授权文件不可读取", file=str(path)) from None
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(),
                "UNSAFE_PATH", "授权文件必须是当前用户拥有的普通文件")
        require(not info.st_mode & 0o077, "AUTH_PERMISSIONS", "授权文件权限必须为 0600", file=str(path))
        require(info.st_size <= 16 * 1024 * 1024, "AUTH_INVALID", "授权文件超出大小限制")
        return f.read()


SECRET_PATTERNS = [
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
    r"(?i)(?:Bearer\s+)[A-Za-z0-9._~+/-]+",
    r"\bcfast_[A-Za-z0-9]+\b",
    r"(?i)(?:access_token|client_secret|password|authorization|api_token)[\"']?\s*[:=]\s*[\"']?[^\s\"',}]+",
]


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in sorted((s for s in secrets if isinstance(s, str) and len(s) >= 4), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    for pattern in SECRET_PATTERNS:
        value = re.sub(pattern, "[REDACTED]", value, flags=re.S)
    return value


def run(argv, *, cwd=None, env=None, stdin=None, timeout=60, secret_values=(), log=None):
    """Capture raw output privately. Only sanitized output may be persisted."""
    require(isinstance(argv, list) and all(isinstance(x, str) for x in argv),
            "CONFIG_INVALID", "命令必须使用参数数组")
    child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                             pass_fds=LOCK_FDS.get())
    try:
        stdout, stderr = child.communicate(stdin, timeout=timeout)
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.communicate(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
        raise
    if log:
        # Append only sanitized text, never the command environment or stdin.
        old = Path(log).read_bytes() if Path(log).exists() else b""
        clean = redact((stdout + stderr).decode("utf-8", "replace"), secret_values)
        atomic_bytes(log, (old + clean.encode())[-4 * 1024 * 1024:])
    return subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
