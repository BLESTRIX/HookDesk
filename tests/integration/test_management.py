import sys

import pytest
from sqlalchemy import func, select

from app import manage
from app.core.errors import AppError
from app.models import Membership, Organization, Shipment, User, WebhookSource
from app.services.management import add_member, create_shipment

pytestmark = pytest.mark.integration


def test_seed_is_idempotent_and_never_prints_password(db, settings, monkeypatch, capsys):
    password = "OnlyForThisSeedTest123!"
    monkeypatch.setenv("DEMO_PASSWORD", password)
    manage.seed(db, settings)
    first_password_hash = db.scalar(
        select(User.password_hash).where(User.email == "admin@example.com")
    )
    manage.seed(db, settings)
    assert db.scalar(select(func.count()).select_from(Organization)) == 2
    assert db.scalar(select(func.count()).select_from(User)) == 4
    assert db.scalar(select(func.count()).select_from(WebhookSource)) == 2
    assert db.scalar(select(func.count()).select_from(Shipment)) == 10
    assert (
        db.scalar(select(User.password_hash).where(User.email == "admin@example.com"))
        == first_password_hash
    )
    assert password not in capsys.readouterr().out


def test_membership_and_shipment_management_is_authorized(tenant, db, password_hash):
    principal, source, _, _ = tenant()
    viewer, foreign_source, _, _ = tenant("viewer@example.com", "VIEWER")
    with pytest.raises(AppError) as exc:
        create_shipment(db, viewer, source.id, "SHIP-NEW")
    assert exc.value.status == 403
    with pytest.raises(AppError) as exc:
        create_shipment(db, principal, foreign_source.id, "SHIP-NEW")
    assert exc.value.status == 404
    db.rollback()
    shipment = create_shipment(db, principal, source.id, "SHIP-NEW")
    assert create_shipment(db, principal, source.id, "SHIP-NEW").id == shipment.id
    member = add_member(db, principal, "viewer@example.com", "OPERATOR")
    assert member.organization_id == principal.organization_id
    assert (
        db.scalar(
            select(Membership.role).where(
                Membership.organization_id == viewer.organization_id,
                Membership.user_id == viewer.user_id,
            )
        )
        == "VIEWER"
    )
    with pytest.raises(AppError) as exc:
        add_member(db, principal, principal.email, "VIEWER")
    assert exc.value.code == "last_admin"


def test_fault_cli_refuses_disabled_demo(tenant, factory, settings, monkeypatch):
    principal, source, _, _ = tenant()
    monkeypatch.setattr(
        manage, "get_settings", lambda: settings.model_copy(update={"demo_enabled": False})
    )
    monkeypatch.setattr(manage, "session_factory", lambda: factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app.manage",
            "fault",
            "--organization",
            principal.organization_id,
            "--actor-email",
            principal.email,
            "--source",
            source.id,
            "--mode",
            "crash",
        ],
    )
    with pytest.raises(SystemExit, match="demo_disabled"):
        manage.main()
