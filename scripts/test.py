"""Provision an isolated local test database, then run pytest. Never clears the app database."""

import os
import subprocess
import sys

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from sqlalchemy.engine import make_url

load_dotenv()
url = make_url(os.environ.get("TEST_DATABASE_URL") or os.environ["DATABASE_URL"])
if not os.environ.get("TEST_DATABASE_URL"):
    url = url.set(database="hookdesk_test")
assert url.database.startswith("hookdesk_test"), "Use a database named hookdesk_test*"
admin_url = url.set(drivername="postgresql", database="postgres").render_as_string(
    hide_password=False
)
with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as conn:
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (url.database,)).fetchone():
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(url.database)))
os.environ["TEST_DATABASE_URL"] = url.render_as_string(hide_password=False)
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]]))
