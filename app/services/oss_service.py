from __future__ import annotations

from dataclasses import dataclass
from io import BufferedIOBase
import logging
from pathlib import Path
import shutil
from tempfile import SpooledTemporaryFile
from urllib.parse import urlparse
from uuid import uuid4

import oss2
from fastapi import HTTPException, UploadFile, status

from app.core.config import settings

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
SPOOL_MAX_MEMORY_SIZE = 8 * 1024 * 1024
ALLOWED_UPLOADS = {
    "image": {
        "extensions": {".jpg", ".jpeg", ".png", ".webp"},
        "content_types": {"image/jpeg", "image/png", "image/webp"},
        "default_extension": ".jpg",
    },
    "video": {
        "extensions": {".mp4", ".mov", ".m4v", ".webm"},
        "content_types": {"video/mp4", "video/quicktime", "video/webm", "video/x-m4v"},
        "default_extension": ".mp4",
    },
}


@dataclass
class UploadResult:
    url: str
    filename: str


def _create_bucket_client() -> oss2.Bucket:
    auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
    return oss2.Bucket(auth, settings.oss_endpoint, settings.oss_bucket_name)


def _missing_oss_fields() -> list[str]:
    missing = []
    if not settings.oss_access_key_id or "你的AccessKey ID" in settings.oss_access_key_id:
        missing.append("OSS_ACCESS_KEY_ID")
    if not settings.oss_access_key_secret or "你的AccessKey Secret" in settings.oss_access_key_secret:
        missing.append("OSS_ACCESS_KEY_SECRET")
    if not settings.oss_endpoint:
        missing.append("OSS_ENDPOINT")
    if not settings.oss_bucket_name or "你的Bucket名称" in settings.oss_bucket_name:
        missing.append("OSS_BUCKET_NAME")
    return missing


def _build_local_media_url(request_base_url: str | None, media_type: str, user_id: str, filename: str) -> str:
    prefix = settings.local_media_url_prefix.strip("/")
    path = f"{prefix}/{media_type}/{user_id}/{filename}" if prefix else f"{media_type}/{user_id}/{filename}"
    if request_base_url:
        return f"{request_base_url.rstrip('/')}/{path}"
    return f"/{path}"


def _build_oss_key(media_type: str, user_id: str, filename: str) -> str:
    prefix = settings.oss_upload_prefix.strip("/")
    return "/".join(part for part in (prefix, media_type, user_id, filename) if part)


def _normalize_content_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _validate_media_type(media_type: str) -> dict:
    config = ALLOWED_UPLOADS.get(media_type)
    if not config:
        raise HTTPException(status_code=400, detail="unsupported media type")
    return config


def _validate_upload_metadata(file: UploadFile, media_type: str) -> str:
    config = _validate_media_type(media_type)
    ext = Path(file.filename or "").suffix.lower() or config["default_extension"]
    if ext not in config["extensions"]:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"unsupported {media_type} extension")

    content_type = _normalize_content_type(file.content_type)
    if content_type and content_type != "application/octet-stream" and content_type not in config["content_types"]:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"unsupported {media_type} content type")
    return ext


def _looks_like_image(content: bytes) -> bool:
    return (
        content.startswith(b"\xff\xd8\xff")
        or content.startswith(b"\x89PNG\r\n\x1a\n")
        or (content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP")
    )


def _looks_like_video(content: bytes) -> bool:
    return (
        len(content) >= 12
        and content[4:8] == b"ftyp"
    ) or content.startswith(b"\x1a\x45\xdf\xa3")


def _validate_magic_bytes(content: bytes, media_type: str) -> None:
    if media_type == "image" and _looks_like_image(content):
        return
    if media_type == "video" and _looks_like_video(content):
        return
    raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"invalid {media_type} file content")


async def _spool_upload(file: UploadFile, media_type: str, max_bytes: int) -> SpooledTemporaryFile:
    spooled = SpooledTemporaryFile(max_size=SPOOL_MAX_MEMORY_SIZE, mode="w+b")
    total = 0
    sample = bytearray()

    while True:
        chunk = await file.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            spooled.close()
            raise HTTPException(status_code=413, detail=f"{media_type} file too large")
        if len(sample) < 512:
            sample.extend(chunk[: 512 - len(sample)])
        spooled.write(chunk)

    if total <= 0:
        spooled.close()
        raise HTTPException(status_code=400, detail="empty upload file")

    try:
        _validate_magic_bytes(bytes(sample), media_type)
    except HTTPException:
        spooled.close()
        raise
    spooled.seek(0)
    return spooled


def _save_local_media(content: BufferedIOBase, media_type: str, user_id: str, filename: str, request_base_url: str | None) -> UploadResult:
    target_dir = settings.resolved_local_media_dir / media_type / user_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    with target_path.open("wb") as target_file:
        shutil.copyfileobj(content, target_file)
    logger.info("Local media saved: media_type=%s user_id=%s path=%s", media_type, user_id, target_path)
    return UploadResult(
        url=_build_local_media_url(request_base_url, media_type, user_id, filename),
        filename=filename,
    )


async def upload_media(file: UploadFile, media_type: str, user_id: str, request_base_url: str | None = None) -> UploadResult:
    ext = _validate_upload_metadata(file, media_type)
    max_mb = settings.max_image_size_mb if media_type == "image" else settings.max_video_size_mb
    content = await _spool_upload(file, media_type, max_mb * 1024 * 1024)

    filename = f"{uuid4().hex}{ext}"
    missing = _missing_oss_fields()
    if missing:
        logger.warning("OSS is not configured; saving media locally. Missing: %s", ", ".join(missing))
        try:
            return _save_local_media(content, media_type, user_id, filename, request_base_url)
        finally:
            content.close()

    key = _build_oss_key(media_type, user_id, filename)
    logger.info(
        "Starting OSS upload: media_type=%s user_id=%s key=%s bucket=%s endpoint=%s",
        media_type,
        user_id,
        key,
        settings.oss_bucket_name,
        settings.oss_endpoint,
    )

    bucket = _create_bucket_client()
    try:
        bucket.put_object(key, content)
    except Exception as exc:
        logger.exception(
            "OSS upload failed: media_type=%s user_id=%s key=%s bucket=%s endpoint=%s",
            media_type,
            user_id,
            key,
            settings.oss_bucket_name,
            settings.oss_endpoint,
        )
        raise HTTPException(status_code=502, detail=f"Aliyun OSS upload failed: {exc}") from exc
    finally:
        content.close()

    if settings.oss_public_base_url:
        base = settings.oss_public_base_url.rstrip("/")
        url = f"{base}/{key}"
    else:
        endpoint_host = settings.oss_endpoint.removeprefix("https://").removeprefix("http://")
        url = f"https://{settings.oss_bucket_name}.{endpoint_host}/{key}"
    logger.info("OSS upload succeeded: key=%s url=%s", key, url)
    return UploadResult(url=url, filename=filename)


def _extract_oss_key_from_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    endpoint_host = settings.oss_endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    bucket_host = f"{settings.oss_bucket_name}.{endpoint_host}"
    public_host = urlparse(settings.oss_public_base_url).netloc if settings.oss_public_base_url else ""
    if parsed.netloc not in {bucket_host, public_host}:
        return ""
    return parsed.path.lstrip("/")


def sign_read_url(url: str) -> str:
    missing = _missing_oss_fields()
    if missing or not url:
        return url

    key = _extract_oss_key_from_url(url)
    if not key:
        return url

    try:
        bucket = _create_bucket_client()
        return bucket.sign_url("GET", key, settings.oss_sign_expire_seconds)
    except Exception:
        logger.exception("OSS sign url failed for key=%s", key)
        return url
