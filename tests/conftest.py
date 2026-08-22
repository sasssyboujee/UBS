import os

# Never hit the challenge host from the test suite.
os.environ.setdefault("TOOLBOX_SKIP_WARMUP", "1")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client
