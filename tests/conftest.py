import copy
import subprocess
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
from workflow_tools.common import atomic_bytes, save
from workflow_tools.config import TEMPLATE, Project
from workflow_tools.credentials import Vault


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project with spaces"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    config = copy.deepcopy(TEMPLATE)
    config["project_id"] = "test-app"
    config["credential_provider"] = "local"
    config["ios"].update(enabled=True, bundle_id="com.example.app", team_id="ABCDE12345", app_store_id="123456789")
    config["ios"]["apple"].update(key_id="ABCDEFGHIJ", issuer_id="11111111-1111-1111-1111-111111111111", vendor_number="123456", p8_ref="team/asc")
    save(root / ".workflow-tools/project.json", config)
    return Project(root, tmp_path / "authorization")


@pytest.fixture
def apple_key():
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


@pytest.fixture
def vault(project, apple_key):
    vault = Vault(project.home, provider="local")
    vault.initialize()
    source = project.home / "incoming/apple/key.p8"
    atomic_bytes(source, apple_key)
    result = vault.import_file("team/asc", "apple_p8", source)
    data = copy.deepcopy(project.data)
    data["credential_refs"].update(result["credential_ref"])
    save(project.path, data)
    return Vault(project.home, data["credential_refs"], provider="local")
