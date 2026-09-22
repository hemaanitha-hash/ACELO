import os
import sys
import uuid

import pytest

from cryptography.fernet import Fernet

import tempfile

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)

os.environ["APP_ENV"] = "development"
# Unit tests drive polling explicitly; real background pollers would sleep on
# timers and make the suite slow and non-deterministic.
os.environ["ACELO_BACKGROUND_POLLING"] = "0"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"
os.environ["SECRET_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Base, SessionLocal, engine  # noqa: E402
from models import Connection, Customer  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def customer(db_session):
    c = Customer(id=str(uuid.uuid4()), name="Test Customer")
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.fixture
def databricks_connection(db_session, customer):
    conn = Connection(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        platform="databricks",
        workspace="Test Lakehouse",
        endpoint="https://adb-test.azuredatabricks.net",
        auth_method="pat",
        auth_metadata='{"cluster_source_table": "src", "cluster_result_table": "dst"}',
        secret_encrypted=None,
        status="pending",
    )
    db_session.add(conn)
    db_session.commit()
    db_session.refresh(conn)
    return conn


@pytest.fixture
def fabric_connection(db_session, customer):
    conn = Connection(
        id=str(uuid.uuid4()),
        customer_id=customer.id,
        platform="fabric",
        workspace="Test Fabric Workspace",
        endpoint="https://api.fabric.microsoft.com",
        auth_method="service_principal",
        auth_metadata=(
            '{"tenant_id": "t", "client_id": "c", '
            '"cluster_source_table": "src", "cluster_result_table": "dst"}'
        ),
        secret_encrypted=None,
        status="pending",
    )
    db_session.add(conn)
    db_session.commit()
    db_session.refresh(conn)
    return conn
