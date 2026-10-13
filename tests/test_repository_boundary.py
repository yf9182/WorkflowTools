import pytest
from workflow_tools.common import Failure, atomic_bytes
from workflow_tools.credentials import Vault


def test_authorization_home_inside_repository_refused_before_creation(project):
    with pytest.raises(Failure) as exc:
        Vault(project.root / 'credentials').initialize()
    assert exc.value.code == 'AUTH_LOCATION'
    assert not (project.root / 'credentials').exists()


def test_authorization_backup_inside_repository_refused(project,vault):
    password = project.home / 'incoming/backup-password.txt'
    atomic_bytes(password,b'synthetic-backup-password')
    with pytest.raises(Failure) as exc:
        vault.backup(project.root / 'backup.enc',password)
    assert exc.value.code == 'AUTH_LOCATION'
    assert not (project.root / 'backup.enc').exists()
