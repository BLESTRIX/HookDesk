"""Host-administrator commands. Secrets are read from environment or secure input."""

import argparse
import getpass
import os

from dotenv import load_dotenv
from pydantic import EmailStr, TypeAdapter
from sqlalchemy import delete, select, text

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import password_hasher
from app.db.session import session_factory
from app.models import AuditEvent, DemoFault, Membership, Organization, User, WebhookSource
from app.repositories.common import owned
from app.schemas import SourceInput
from app.services.auth import Principal, require_role
from app.services.management import add_member, create_shipment
from app.services.sources import create_source


def principal_for(db, org_id, actor_email):
    row = db.execute(
        select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(User.email == actor_email.lower(), Membership.organization_id == org_id)
    ).first()
    if not row:
        raise AppError(403, "no_admin", "Actor must be an admin of this organization")
    user, member = row
    principal = Principal(org_id, user.id, member.role, user.email, "", "")
    require_role(principal, "ADMIN")
    return principal


def seed(db, settings):
    if not settings.demo_enabled or settings.environment == "production":
        raise AppError(
            403, "demo_disabled", "Set DEMO_ENABLED=true in development to seed demo accounts"
        )
    password = os.getenv("DEMO_PASSWORD") or getpass.getpass(
        "Password for demo accounts (12+ characters): "
    )
    if not 12 <= len(password) <= 128:
        raise AppError(422, "invalid_password", "Use a password of 12–128 characters")
    # Serialize concurrent seed invocations; each service may commit, so take a session lock.
    lock_connection = db.get_bind().connect()
    lock_connection.execute(text("SELECT pg_advisory_lock(9832101)"))
    try:
        for name, email in [
            ("Northstar Shop", "admin@example.com"),
            ("Second Shop", "other@example.com"),
        ]:
            user = db.scalar(select(User).where(User.email == email))
            if user:
                member = db.scalar(
                    select(Membership).where(
                        Membership.user_id == user.id, Membership.role == "ADMIN"
                    )
                )
                org = db.get(Organization, member.organization_id)
            else:
                org, user = (
                    Organization(name=name),
                    User(email=email, password_hash=password_hasher.hash(password)),
                )
                db.add_all([org, user])
                db.flush()
                db.add(Membership(organization_id=org.id, user_id=user.id, role="ADMIN"))
                db.commit()
            principal = Principal(org.id, user.id, "ADMIN", email, "", "")
            if email == "admin@example.com":
                for member_email, role in [
                    ("operator@example.com", "OPERATOR"),
                    ("viewer@example.com", "VIEWER"),
                ]:
                    add_member(db, principal, member_email, role, password)
            source = db.scalar(
                select(WebhookSource).where(
                    WebhookSource.organization_id == org.id, WebhookSource.name == "Demo Courier"
                )
            )
            if not source:
                source, _ = create_source(db, principal, SourceInput(name="Demo Courier"), settings)
                # Do not print signing secrets; rotate through the UI for the one-time display.
            for number in range(1001, 1006):
                create_shipment(db, principal, source.id, f"SHIP-{number}")
            print(f"{name}: organization={org.id} source={source.id} endpoint={source.public_id}")
    finally:
        lock_connection.execute(text("SELECT pg_advisory_unlock(9832101)"))
        lock_connection.close()
    print("Demo accounts are ready. Rotate a source secret in the UI before using the simulator.")


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("seed")
    for command in ("member", "shipment", "fault"):
        cmd = commands.add_parser(command)
        cmd.add_argument("--organization", required=True)
        cmd.add_argument("--actor-email", required=True)
        if command == "member":
            cmd.add_argument("--email", required=True)
            cmd.add_argument("--role", choices=["ADMIN", "OPERATOR", "VIEWER"], required=True)
        else:
            cmd.add_argument("--source", required=True)
        if command == "shipment":
            cmd.add_argument("--external-id", required=True)
        if command == "fault":
            cmd.add_argument("--mode", choices=["timeout", "crash", "off"], required=True)
            cmd.add_argument("--remaining", type=int, default=100)
    args = parser.parse_args()
    settings = get_settings()
    try:
        with session_factory()() as db:
            if args.command == "seed":
                seed(db, settings)
                return
            principal = principal_for(db, args.organization, args.actor_email)
            if args.command == "member":
                email = str(TypeAdapter(EmailStr).validate_python(args.email))
                existing = db.scalar(select(User).where(User.email == email.lower()))
                password = (
                    None
                    if existing
                    else os.getenv("MEMBER_PASSWORD") or getpass.getpass("New user's password: ")
                )
                member = add_member(db, principal, email, args.role, password)
                print(f"Membership saved: {member.id} ({member.role})")
            elif args.command == "shipment":
                shipment = create_shipment(db, principal, args.source, args.external_id)
                print(f"Shipment ready: {shipment.id}")
            elif args.command == "fault":
                if not settings.demo_enabled or settings.environment == "production":
                    raise AppError(403, "demo_disabled", "Failure injection is disabled")
                if not 0 <= args.remaining <= 10000:
                    raise AppError(422, "invalid_fault_count", "Choose 0–10000 failures")
                source = owned(db, WebhookSource, args.source, principal, lock=True)
                db.execute(
                    delete(DemoFault).where(
                        DemoFault.source_id == source.id,
                        DemoFault.organization_id == principal.organization_id,
                    )
                )
                if args.mode != "off":
                    db.add(
                        DemoFault(
                            organization_id=principal.organization_id,
                            source_id=source.id,
                            mode=args.mode,
                            remaining=args.remaining,
                        )
                    )
                db.add(
                    AuditEvent(
                        organization_id=principal.organization_id,
                        actor_id=principal.user_id,
                        action="demo.fault_configured",
                        resource_id=source.id,
                        details={"mode": args.mode, "remaining": args.remaining},
                    )
                )
                db.commit()
                print(f"Development fault: {args.mode}")
    except AppError as exc:
        raise SystemExit(f"{exc.code}: {exc.message}") from None


if __name__ == "__main__":
    main()
