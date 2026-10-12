import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = tempfile.mkdtemp(prefix="3dm-test-")
os.environ.update(DATA_DIR=DATA, SEED="1", AMS_MQTT="0", BAMBU_SCHEDULER="0", BACKUP_SCHEDULER="0", CATALOG_FETCH="0")
sys.path.insert(0, str(ROOT / "app"))


@pytest.fixture(scope="session")
def appmod():
    import app as appmod  # noqa: WPS433 – Import erst nach dem Setzen der Umgebung
    return appmod


@pytest.fixture()
def client(appmod):
    return appmod.app.test_client()
