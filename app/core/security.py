from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from app.core.config import settings
from app.services.auth_service import get_user_by_token


@dataclass(frozen=True)
class CurrentUser:
    uid: str
    email: str = ""
    display_name: str = "Dev User"


def get_current_user(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    x_user_email: str | None = Header(default=""),
    x_user_name: str | None = Header(default=""),
) -> CurrentUser:
    if settings.dev_trust_x_user_headers and x_user_id:
        return CurrentUser(
            uid=x_user_id,
            email=x_user_email or "",
            display_name=x_user_name or x_user_id,
        )

    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        user = get_user_by_token(token)
        if user:
            return CurrentUser(
                uid=user.get("uid", ""),
                email=user.get("email", ""),
                display_name=user.get("display_name", user.get("uid", "")),
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    if settings.dev_allow_anon_auth:
        return CurrentUser(uid="dev-user", email="dev@example.com", display_name="Dev User")

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing Authorization: Bearer <token>",
    )


AuthUser = Depends(get_current_user)
