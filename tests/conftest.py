import json
import os
from datetime import UTC, datetime

import pytest
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.core.config import get_settings
from app.core.security import password_hasher, sign
from app.db.base import Base
from app.models import Membership, Organization, Shipment, User
from app.schemas import SourceInput
from app.services.auth import Principal
from app.services.sources import create_source


@pytest.fixture(scope="session")
def settings():
    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    os.environ["DEMO_ENABLED"] = "true"
    os.environ["ENVIRONMENT"] = "test"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ["LOGIN_LIMIT"] = "1000"
    if os.getenv("TEST_DATABASE_URL"):
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
def engine(settings):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "Set TEST_DATABASE_URL to a dedicated PostgreSQL test database; see scripts/test.py"
        )
    assert make_url(url).database.startswith("hookdesk_test"), (
        "Refusing to clear a non-test database"
    )
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url, pool_size=20, connect_args={"connect_timeout": 5})
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine):
    with engine.begin() as conn:
        names = ", ".join(f'"{t}"' for t in Base.metadata.tables)
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def db(factory):
    with factory() as session:
        yield session


@pytest.fixture(scope="session")
def password_hash():
    return password_hasher.hash("TestPassword123!")


@pytest.fixture
def tenant(db, settings, password_hash):
    def make(email="admin@example.com", role="ADMIN", name="Test Shop"):
        org, user = Organization(name=name), User(email=email, password_hash=password_hash)
        db.add_all([org, user])
        db.flush()
        db.add(Membership(organization_id=org.id, user_id=user.id, role=role))
        db.commit()
        principal = Principal(org.id, user.id, role, user.email, "", "")
        source, secret = create_source(
            db,
            Principal(org.id, user.id, "ADMIN", user.email, "", ""),
            SourceInput(name="Courier"),
            settings,
        )
        shipment = Shipment(organization_id=org.id, source_id=source.id, external_id="SHIP-1001")
        db.add(shipment)
        db.commit()
        return principal, source, secret, shipment

    return make


@pytest.fixture
def client(factory, settings):
    from app.db.session import get_db
    from app.main import app

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def login_client(client):
    def login(email="admin@example.com"):
        csrf = client.get("/api/v1/auth/csrf").json()["csrf_token"]
        result = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "TestPassword123!"},
            headers={"X-CSRF-Token": csrf},
        )
        assert result.status_code == 200, result.text
        return {"X-CSRF-Token": result.json()["csrf_token"]}

    return login


@pytest.fixture
def body():
    def make(event_id="evt_1", sequence=1, status="shipped", shipment_id="SHIP-1001"):
        return json.dumps(
            {
                "event_id": event_id,
                "event_type": "shipment.status_changed",
                "occurred_at": "2026-10-16T03:00:00Z",
                "data": {
                    "shipment_id": shipment_id,
                    "sequence": sequence,
                    "status": status,
                },
            },
            separators=(",", ":"),
        ).encode()

    return make


def headers(secret, body, *, offset=0):
    timestamp = str(int(datetime.now(UTC).timestamp()) + offset)
    return {
        "Content-Type": "application/json",
        "X-HookDesk-Timestamp": timestamp,
        "X-HookDesk-Signature": sign(secret, timestamp, body),
    }
