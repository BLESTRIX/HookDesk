import hmac
from dataclasses import dataclass
from datetime import timedelta

from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import digest, new_token, password_hasher, utcnow
from app.models import AuditEvent, LoginBucket, Membership, Organization, Session, User
from app.schemas import LoginInput, RegisterInput

# Verify an Argon2 hash on unknown-account paths too.
DUMMY_HASH = password_hasher.hash("not-a-real-account-password")


@dataclass(frozen=True)
class Principal:
    organization_id: str
    user_id: str
    role: str
    email: str
    session_id: str
    csrf_hash: str


def require_role(principal: Principal, *roles: str):
    if principal.role not in roles:
        raise AppError(403, "permission_denied", "Your role does not permit this action")


def create_session(db, user, org_id, settings):
    token, csrf = new_token(), new_token()
    db.add(
        Session(
            organization_id=org_id,
            user_id=user.id,
            token_hash=digest(token),
            csrf_hash=digest(csrf),
            expires_at=utcnow() + timedelta(hours=settings.session_hours),
        )
    )
    return token, csrf


def register(db: DBSession, data: RegisterInput, settings: Settings):
    user = User(email=str(data.email).lower(), password_hash=password_hasher.hash(data.password))
    org = Organization(name=data.organization_name)
    try:
        db.add_all([user, org])
        db.flush()
        db.add(Membership(user_id=user.id, organization_id=org.id, role="ADMIN"))
        db.flush()
        token, csrf = create_session(db, user, org.id, settings)
        db.add(
            AuditEvent(
                organization_id=org.id,
                actor_id=user.id,
                action="organization.registered",
                resource_id=org.id,
                details={},
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "email_exists", "This email is already registered") from None
    return token, csrf


def rate_limit(db, key: str, settings: Settings):
    now = utcnow()
    key = digest(key)
    db.execute(
        insert(LoginBucket)
        .values(key=key, count=0, window_start=now)
        .on_conflict_do_nothing(index_elements=[LoginBucket.key])
    )
    bucket = db.scalar(select(LoginBucket).where(LoginBucket.key == key).with_for_update())
    if (now - bucket.window_start).total_seconds() >= settings.login_window_seconds:
        bucket.count, bucket.window_start = 0, now
    bucket.count += 1
    limited = bucket.count > settings.login_limit
    db.commit()  # Invalid logins must consume their allowance too.
    if limited:
        raise AppError(429, "login_rate_limited", "Too many attempts; try again later")


def login(db, data: LoginInput, settings, client_ip):
    # Independent IP and account limits prevent simple account/IP switching.
    rate_limit(db, f"ip:{client_ip}", settings)
    rate_limit(db, f"email:{str(data.email).lower()}", settings)
    user = db.scalar(select(User).where(User.email == str(data.email).lower()))
    try:
        valid = password_hasher.verify(user.password_hash if user else DUMMY_HASH, data.password)
    except (VerificationError, InvalidHashError):
        valid = False
    if not user or not valid:
        raise AppError(401, "invalid_credentials", "Invalid email or password")
    memberships = list(
        db.scalars(
            select(Membership)
            .where(Membership.user_id == user.id)
            .order_by(Membership.organization_id)
        )
    )
    member = (
        next((m for m in memberships if m.organization_id == data.organization_id), None)
        if data.organization_id
        else next(iter(memberships), None)
    )
    if not member:
        raise AppError(403, "no_membership", "No membership for the selected organization")
    if password_hasher.check_needs_rehash(user.password_hash):
        user.password_hash = password_hasher.hash(data.password)
    token, csrf = create_session(db, user, member.organization_id, settings)
    db.commit()
    return token, csrf


def authenticate(db, token: str | None) -> Principal:
    if not token:
        raise AppError(401, "authentication_required", "Please sign in")
    row = db.execute(
        select(Session, User, Membership)
        .join(User, User.id == Session.user_id)
        .join(
            Membership,
            (Membership.user_id == Session.user_id)
            & (Membership.organization_id == Session.organization_id),
        )
        .where(Session.token_hash == digest(token), Session.expires_at > utcnow())
    ).first()
    if not row:
        raise AppError(401, "session_expired", "Your session has expired; sign in again")
    session, user, membership = row
    return Principal(
        session.organization_id, user.id, membership.role, user.email, session.id, session.csrf_hash
    )


def verify_csrf(principal, supplied):
    if not supplied or not hmac.compare_digest(digest(supplied), principal.csrf_hash):
        raise AppError(
            403, "csrf_failed", "The security token is missing or invalid; reload the page"
        )


def logout(db, principal):
    session = db.get(Session, principal.session_id)
    if session:
        db.delete(session)
    db.commit()
