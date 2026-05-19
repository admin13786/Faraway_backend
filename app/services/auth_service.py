from __future__ import annotations

import hashlib
import hmac
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import jwt
from fastapi import HTTPException, status

from app.core.config import settings
from app.models.common import now_utc
from app.services.store import store

AUTH_ACCOUNTS_COLLECTION = "auth_accounts"
AUTH_IDENTITIES_COLLECTION = "auth_identities"
AUTH_SMS_CODES_COLLECTION = "auth_sms_codes"
AUTH_REVOKED_TOKENS_COLLECTION = "auth_revoked_tokens"


@dataclass
class TokenEnvelope:
    access_token: str
    user: dict


def normalize_username(username: str) -> str:
    return username.strip().lower()


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits


def validate_username(username: str) -> str:
    value = username.strip()
    if len(value) < 3 or len(value) > 32:
        raise HTTPException(status_code=400, detail="username must be 3-32 characters")
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise HTTPException(status_code=400, detail="username can only contain letters, numbers and underscore")
    return value


def validate_password(password: str) -> str:
    if len(password) < 6 or len(password) > 128:
        raise HTTPException(status_code=400, detail="password must be 6-128 characters")
    return password


def validate_phone(phone: str) -> str:
    digits = normalize_phone(phone)
    if digits and not 6 <= len(digits) <= 20:
        raise HTTPException(status_code=400, detail="phone format is invalid")
    return digits


def _identity_key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    iterations = settings.password_hash_iterations
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{iterations}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        iteration_text, salt_hex, digest_hex = encoded.split("$", 2)
        iterations = int(iteration_text)
        expected = bytes.fromhex(digest_hex)
        salt = bytes.fromhex(salt_hex)
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _get_identity(kind: str, value: str) -> dict | None:
    return store.get(AUTH_IDENTITIES_COLLECTION, _identity_key(kind, value))


def get_auth_account_by_uid(uid: str) -> dict | None:
    return store.get(AUTH_ACCOUNTS_COLLECTION, uid)


def get_auth_account_by_username(username: str) -> dict | None:
    normalized = normalize_username(username)
    identity = _get_identity("username", normalized)
    if not identity:
        return None
    return get_auth_account_by_uid(identity["uid"])


def get_auth_account_by_phone(phone: str) -> dict | None:
    normalized = validate_phone(phone)
    if not normalized:
        return None
    identity = _get_identity("phone", normalized)
    if not identity:
        return None
    return get_auth_account_by_uid(identity["uid"])


def upsert_user(
    uid: str,
    display_name: str = "",
    email: str = "",
    photo_url: str = "",
    bio: str = "",
    gender: str = "unknown",
) -> dict:
    existing = store.get("users", uid) or {}
    now = now_utc().isoformat()
    payload = {
        "uid": uid,
        "display_name": display_name or existing.get("display_name", uid),
        "email": email or existing.get("email", ""),
        "photo_url": photo_url if photo_url != "" else existing.get("photo_url", ""),
        "bio": bio if bio != "" else existing.get("bio", ""),
        "gender": gender or existing.get("gender", "unknown"),
        "created_at": existing.get("created_at", now),
        "updated_at": now,
    }
    store.upsert("users", uid, payload)
    return payload


def _create_account_identities(uid: str, username_key: str, phone: str) -> None:
    store.upsert(
        AUTH_IDENTITIES_COLLECTION,
        _identity_key("username", username_key),
        {"uid": uid, "kind": "username", "value": username_key},
    )
    if phone:
        store.upsert(
            AUTH_IDENTITIES_COLLECTION,
            _identity_key("phone", phone),
            {"uid": uid, "kind": "phone", "value": phone},
        )


def register_password_user(
    username: str,
    password: str,
    nickname: str = "",
    phone: str = "",
    email: str = "",
) -> dict:
    username = validate_username(username)
    validate_password(password)
    username_key = normalize_username(username)
    phone = validate_phone(phone)

    if _get_identity("username", username_key):
        raise HTTPException(status_code=409, detail="username already exists")
    if phone and _get_identity("phone", phone):
        raise HTTPException(status_code=409, detail="phone already exists")

    now = now_utc().isoformat()
    uid = uuid4().hex
    display_name = nickname.strip() or username
    account = {
        "uid": uid,
        "username": username,
        "username_key": username_key,
        "phone": phone,
        "email": email.strip(),
        "display_name": display_name,
        "password_hash": _hash_password(password),
        "login_types": ["password"] + (["phone"] if phone else []),
        "created_at": now,
        "updated_at": now,
        "status": "active",
    }
    store.upsert(AUTH_ACCOUNTS_COLLECTION, uid, account)
    _create_account_identities(uid, username_key, phone)
    return upsert_user(uid=uid, display_name=display_name, email=account["email"])


def authenticate_password_user(username: str, password: str) -> dict:
    validate_password(password)
    account = get_auth_account_by_username(username)
    if not account:
        raise HTTPException(status_code=401, detail="username or password is incorrect")
    if account.get("status") != "active":
        raise HTTPException(status_code=403, detail="account is disabled")
    if not _verify_password(password, account.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="username or password is incorrect")
    user = store.get("users", account["uid"]) or upsert_user(
        uid=account["uid"],
        display_name=account.get("display_name", account.get("username", "")),
        email=account.get("email", ""),
    )
    return user


def create_sms_code(phone: str) -> dict:
    normalized = validate_phone(phone)
    if not normalized:
        raise HTTPException(status_code=400, detail="phone is required")
    code = f"{random.randint(0, 999999):06d}"
    now = now_utc()
    payload = {
        "phone": normalized,
        "code": code,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=settings.sms_code_ttl_minutes)).isoformat(),
    }
    store.upsert(AUTH_SMS_CODES_COLLECTION, normalized, payload)
    return payload


def verify_sms_code(phone: str, sms_code: str) -> None:
    normalized = validate_phone(phone)
    record = store.get(AUTH_SMS_CODES_COLLECTION, normalized)
    if not record or record.get("code") != sms_code:
        raise HTTPException(status_code=401, detail="smsCode is incorrect")
    expires_at = record.get("expires_at")
    if not expires_at or now_utc().isoformat() > expires_at:
        raise HTTPException(status_code=401, detail="smsCode is expired")


def authenticate_phone_user(phone: str, sms_code: str) -> dict:
    verify_sms_code(phone, sms_code)
    account = get_auth_account_by_phone(phone)
    if not account:
        raise HTTPException(status_code=404, detail="phone is not registered")
    if account.get("status") != "active":
        raise HTTPException(status_code=403, detail="account is disabled")
    user = store.get("users", account["uid"]) or upsert_user(
        uid=account["uid"],
        display_name=account.get("display_name", account.get("username", "")),
        email=account.get("email", ""),
    )
    return user


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cleanup_expired_revoked_tokens() -> None:
    now = now_utc().isoformat()
    for item in store.list(AUTH_REVOKED_TOKENS_COLLECTION):
        expires_at = item.get("expires_at", "")
        if expires_at and expires_at < now:
            store.delete(AUTH_REVOKED_TOKENS_COLLECTION, item["id"])


def revoke_token(token: str, uid: str = "") -> None:
    if not token:
        return
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"], options={"verify_exp": False})
    except jwt.PyJWTError:
        return
    expires_at = payload.get("exp")
    if isinstance(expires_at, (int, float)):
        expires_at = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
    else:
        expires_at = (now_utc() + timedelta(hours=settings.auth_token_ttl_hours)).isoformat()
    token_id = _token_hash(token)
    store.upsert(
        AUTH_REVOKED_TOKENS_COLLECTION,
        token_id,
        {
            "id": token_id,
            "uid": uid or payload.get("sub", ""),
            "expires_at": expires_at,
            "created_at": now_utc().isoformat(),
        },
    )


def is_token_revoked(token: str) -> bool:
    _cleanup_expired_revoked_tokens()
    return store.get(AUTH_REVOKED_TOKENS_COLLECTION, _token_hash(token)) is not None


def issue_token(user: dict) -> TokenEnvelope:
    expires_at = now_utc() + timedelta(hours=settings.auth_token_ttl_hours)
    token = jwt.encode(
        {"sub": user["uid"], "exp": expires_at.timestamp()},
        settings.secret_key,
        algorithm="HS256",
    )
    return TokenEnvelope(access_token=token, user=user)


def get_user_by_token(token: str) -> dict | None:
    if is_token_revoked(token):
        return None
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    uid = payload.get("sub")
    if not uid:
        return None
    return store.get("users", uid)


def login_wechat(code: str) -> TokenEnvelope:
    if not settings.wechat_app_id or not settings.wechat_app_secret:
        raise HTTPException(status_code=503, detail="wechat login is not configured")
    if not code:
        raise HTTPException(status_code=400, detail="wechat code is required")

    try:
        token_resp = httpx.get(
            "https://api.weixin.qq.com/sns/oauth2/access_token",
            params={
                "appid": settings.wechat_app_id,
                "secret": settings.wechat_app_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        openid = token_data.get("openid")
        if not access_token or not openid:
            raise HTTPException(status_code=502, detail=f"wechat login failed: {token_data}")

        profile_resp = httpx.get(
            "https://api.weixin.qq.com/sns/userinfo",
            params={"access_token": access_token, "openid": openid, "lang": "zh_CN"},
            timeout=30,
        )
        profile_resp.raise_for_status()
        profile = profile_resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"wechat login failed: {exc}") from exc

    uid = f"wechat_{openid}"
    account = get_auth_account_by_uid(uid)
    display_name = profile.get("nickname", f"wx_{openid[-6:]}")
    user = upsert_user(
        uid=uid,
        display_name=display_name,
        photo_url=profile.get("headimgurl", ""),
    )

    now = now_utc().isoformat()
    store.upsert(
        AUTH_ACCOUNTS_COLLECTION,
        uid,
        {
            "uid": uid,
            "username": account.get("username", "") if account else "",
            "username_key": account.get("username_key", "") if account else "",
            "phone": account.get("phone", "") if account else "",
            "email": account.get("email", "") if account else "",
            "display_name": display_name,
            "password_hash": account.get("password_hash", "") if account else "",
            "login_types": sorted(set((account.get("login_types", []) if account else []) + ["wechat"])),
            "created_at": account.get("created_at", now) if account else now,
            "updated_at": now,
            "status": "active",
        },
    )
    return issue_token(user)
