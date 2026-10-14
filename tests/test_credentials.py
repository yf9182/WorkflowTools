import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import pytest
from workflow_tools.common import Failure, atomic_bytes, canonical, private_read, read_json, save
from workflow_tools.credentials import Vault, encrypt, decrypt


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.calls = 0
    def put_object(self, Bucket, Key, Body, **kwargs):
        self.calls += 1
        if Key in self.objects and kwargs.get("IfNoneMatch") == "*":
            raise RuntimeError("precondition")
        self.objects[Key] = Body
    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


def test_roundtrip_and_immutable_version(project, vault, apple_key):
    assert vault.read("team/asc", "apple_p8") == apple_key
    catalog = read_json(project.home / "vault/catalog.json")
    obj = private_read(project.home / "vault" / Vault.object_key("team/asc", 1), project.home)
    assert apple_key not in obj and b"PRIVATE KEY" not in obj
    source = project.home / "incoming/apple/key.p8"
    next_version = vault.import_file("team/asc", "apple_p8", source)
    assert next_version["version"] == 2
    with pytest.raises(Failure, match="更高"):
        vault.import_file("team/asc", "apple_p8", source, 1)
    assert vault.read("team/asc") == apple_key  # configured immutable version stays v1


@pytest.mark.parametrize("mutation", ["ref", "version", "kind", "ciphertext", "nonce", "key"])
def test_aead_rejects_swaps_and_tampering(mutation):
    key = os.urandom(32)
    obj = encrypt(key, b"test-value", "team/item", 1, "api_token")
    if mutation == "key":
        key = os.urandom(32)
    elif mutation in ("ciphertext", "nonce"):
        obj[mutation] = ("A" if obj[mutation][0] != "A" else "B") + obj[mutation][1:]
    else:
        obj[mutation] = {"ref": "other/item", "version": 2, "kind": "password"}[mutation]
    with pytest.raises(Failure):
        decrypt(key, obj, "team/item", 1, "api_token")


def test_cloud_upload_reconciles_and_never_uploads_plaintext(project, vault, apple_key):
    s3 = MemoryS3()
    cloud = Vault(project.home, vault.refs, "r2", s3=s3)
    assert cloud.upload("team/asc")["verified"]
    assert cloud.upload("team/asc")["verified"]
    assert s3.calls == 2  # conditional PUT is read back on its second precondition failure
    assert cloud.read("team/asc") == apple_key
    assert all(apple_key not in x for x in s3.objects.values())
    key = Vault.object_key("team/asc", 1)
    s3.objects[key] = b'{"ref":"wrong"}'
    with pytest.raises(Failure):
        Vault(project.home, vault.refs, "r2", s3=s3).read("team/asc")


def test_runtime_never_falls_back_to_local(project, vault):
    with pytest.raises(Failure):
        Vault(project.home, vault.refs, "r2", s3=MemoryS3()).read("team/asc")


@pytest.mark.parametrize("ref", ["../secret", "/secret", "a/../b", "a//b", "a/./b"])
def test_path_traversal_rejected(vault, ref):
    with pytest.raises(Failure):
        vault.read(ref)


def test_unregistered_and_wrong_kind(vault):
    with pytest.raises(Failure) as exc:
        vault.read("other/key")
    assert exc.value.code == "AUTH_REF_UNREGISTERED"
    with pytest.raises(Failure) as exc:
        vault.read("team/asc", "api_token")
    assert exc.value.code == "AUTH_BINDING_MISMATCH"


def test_private_file_permissions_symlink_and_ancestor(tmp_path):
    source = tmp_path / "value"
    source.write_text("never-print-this")
    source.chmod(0o644)
    with pytest.raises(Failure) as exc:
        private_read(source)
    assert exc.value.code == "AUTH_PERMISSIONS"
    source.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(Failure):
        private_read(link)
    directory = tmp_path / "dir"
    directory.mkdir()
    (directory / "v").symlink_to(source)
    with pytest.raises(Failure):
        private_read(directory / "v", directory)


def test_lease_keeps_active_secrets_and_cleans_after_use(project, vault):
    with vault.lease({"asc": ("team/asc", "apple_p8", ".p8")}) as files:
        path = files["asc"]
        assert path.stat().st_mode & 0o777 == 0o600
        assert vault.sweep()["active"] == 1
        assert path.exists()
    assert not path.exists()


def test_cleanup_failure_is_visible_and_keeps_recovery(project, vault, monkeypatch):
    import workflow_tools.credentials as module
    monkeypatch.setattr(module.shutil, "rmtree", lambda _: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(Failure) as exc:
        with vault.lease({"asc": ("team/asc", "apple_p8", ".p8")}):
            pass
    assert exc.value.code == "AUTH_CLEANUP_FAILED"
    assert list((project.home / "run/credentials").glob("lease-*/asc.p8"))


def test_synthetic_sigkill_keeps_child_lease_until_child_exits(project, vault):
    marker = project.home / "child-pid.txt"
    child_code = "import os,time;from pathlib import Path;Path(" + repr(str(marker)) + ").write_text(str(os.getpid()));time.sleep(30)"
    code = "from workflow_tools.credentials import Vault;from workflow_tools.common import run;import sys,json;" \
        + "v=Vault(" + repr(str(project.home)) + "," + repr(vault.refs) + ",provider='local');" \
        + "\nwith v.lease({'asc':('team/asc','apple_p8','.p8')}): run([sys.executable,'-c'," + repr(child_code) + "],timeout=40)"
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        child = int(marker.read_text())
        parent.kill(); parent.wait(timeout=5)
        assert vault.sweep()["active"] == 1
        assert list((project.home / "run/credentials").glob("lease-*/asc.p8"))
        os.kill(child, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = vault.sweep()
            if not result["active"]:
                break
            time.sleep(0.05)
        assert result["removed"] == 1
        assert not list((project.home / "run/credentials").glob("lease-*"))
    finally:
        if parent.poll() is None:
            parent.kill(); parent.wait()
        if child:
            try: os.kill(child, signal.SIGKILL)
            except ProcessLookupError: pass


def test_backup_restore_and_wrong_password_leave_no_connection(project, vault, tmp_path, apple_key):
    password = tmp_path / "password"
    atomic_bytes(password, b"synthetic-backup-password")
    out = tmp_path / "backup.enc"
    vault.backup(out, password)
    assert apple_key not in out.read_bytes()
    restored = Vault(tmp_path / "new-machine", vault.refs, "local")
    assert restored.restore(out, password)["status"] == "ready"
    assert restored.read("team/asc") == apple_key
    with pytest.raises(Failure):
        restored.restore(out, password)
    wrong = tmp_path / "wrong"
    atomic_bytes(wrong, b"different-backup-password")
    bad = Vault(tmp_path / "bad-machine")
    with pytest.raises(Failure):
        bad.restore(out, wrong)
    assert not bad.bootstrap_path.exists()


def test_import_api_bytes_encrypts_without_plaintext_staging(project, vault, tmp_path):
    raw = b"synthetic-profile-content"
    result = vault.import_bytes("apple/profile-test", "provisioning_profile", raw)
    assert result["version"] == 1
    encrypted = next((project.home / "vault/workflow-tools/v1/objects").rglob("1.json")).read_bytes()
    assert raw not in encrypted
    assert vault.read("apple/profile-test", "provisioning_profile") == raw
