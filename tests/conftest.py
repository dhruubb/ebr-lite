import os

os.environ.setdefault("EBR_BCRYPT_ROUNDS", "4")   # fast hashing in tests only

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import clock, db, seed  # noqa: E402
from app.main import app  # noqa: E402

from .helpers import Api  # noqa: E402


@pytest.fixture
def client(tmp_path):
    """A fresh, freshly-seeded database file for every test."""
    db.configure(f"sqlite:///{tmp_path / 'test.db'}")
    seed.run(verbose=False)
    clock.reset()
    with TestClient(app) as c:
        yield c
    clock.reset()


@pytest.fixture
def api(client):
    return Api(client)
