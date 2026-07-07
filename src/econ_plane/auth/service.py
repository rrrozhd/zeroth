from datetime import datetime, timedelta, timezone

from jose import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from econ_plane.auth.models import Role, User
from econ_plane.auth.schemas import LoginRequest, UserClaims
from econ_plane.config import settings


def issue_token(payload: LoginRequest, db: Session) -> str:
    user = db.execute(select(User).where(User.subject == payload.sub)).scalar_one_or_none()
    if user is None:
        user = User(subject=payload.sub, email=payload.email)
        db.add(user)

    role_objects: list[Role] = []
    for role_name in payload.roles:
        role = db.execute(select(Role).where(Role.name == role_name)).scalar_one_or_none()
        if role is not None:
            role_objects.append(role)
    user.roles = role_objects
    db.commit()

    exp = datetime.now(tz=timezone.utc) + timedelta(hours=12)
    claims = {
        "sub": payload.sub,
        "email": payload.email,
        "roles": payload.roles,
        "iss": "econ-plane",
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> UserClaims:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    return UserClaims(**payload)
