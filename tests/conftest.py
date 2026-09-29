import pytest
from fastapi.testclient import TestClient

from callguard.app import create_app
from callguard.config import Settings
from helpers import BASE, CELL, TOKEN, TWILIO_NUM, FakeClassifier


@pytest.fixture
def make(tmp_path):
    clients = []

    def _make(verdict, mode="enforce", delay=0):
        s = Settings(twilio_auth_token=TOKEN, twilio_number=TWILIO_NUM, my_cell=CELL,
                     public_base_url=BASE, admin_token="admin", owner_name="Sam",
                     mode=mode, db_path=str(tmp_path / "calls.db"), classify_timeout_s=0.5)
        fake = FakeClassifier(verdict, delay)
        # Entering the client keeps one event loop across requests, like uvicorn does,
        # so the classification task started in /screen survives until /decide.
        client = TestClient(create_app(s, fake)).__enter__()
        clients.append(client)
        return client, fake

    yield _make
    for c in clients:
        c.__exit__(None, None, None)
