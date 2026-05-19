from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from secrets import token_urlsafe
from threading import RLock
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.security import AuthUser, CurrentUser
from app.models.ai import GenerateStrategyRequest
from app.models.common import now_utc
from app.services.ai_match.search_engine import (
    build_match_intent,
    build_query_plan,
    build_realtime_candidate,
    build_recruitment_candidate,
    normalize_destination,
    get_match_trace,
    public_ai_match,
    rank_candidates,
    reflect_candidate_quality,
    save_match_feedback,
    save_match_trace,
)
from app.services.ai_service import generate_safe_meeting_place, generate_strategy
from app.services.auth_service import (
    authenticate_password_user,
    authenticate_phone_user,
    create_sms_code,
    get_auth_account_by_uid,
    get_user_by_token,
    issue_token,
    login_wechat,
    register_password_user,
    revoke_token,
    upsert_user,
    verify_sms_code,
)
from app.services.oss_service import sign_read_url, upload_media
from app.services.post_recommendation import (
    analyze_content,
    get_recommendation_trace,
    recommend_feed,
    save_recommendation_event,
)
from app.services.store import store

router = APIRouter(prefix="/api", tags=["frontend-api"])

MATCH_RECRUITMENTS_COLLECTION = "fe_match_recruitments"
MATCH_APPLICATIONS_COLLECTION = "fe_match_applications"
MATCH_NOTIFICATIONS_COLLECTION = "fe_notifications"
LEGACY_MATCH_REQUESTS_COLLECTION = "fe_match_requests"
REALTIME_MATCH_REQUESTS_COLLECTION = "fe_realtime_match_requests"
REALTIME_MATCH_CANDIDATES_COLLECTION = "fe_realtime_match_candidates"
REALTIME_MATCH_PAIRS_COLLECTION = "fe_realtime_match_pairs"
MATCH_DATE_WINDOW_DAYS = 3
MATCH_STAY_WINDOW_DAYS = 2
REALTIME_MATCH_DECISION_MINUTES = 120
REALTIME_MATCH_MEET_HOUR = 10
REALTIME_MATCH_TIMEZONE = timezone(timedelta(hours=8))
REALTIME_MATCH_ACTIVE_STATUSES = {"pending", "matched_waiting_decision", "matched_accepted"}
REALTIME_MATCH_TERMINAL_STATUSES = {"failed", "cancelled", "finished"}
MATCH_OPERATION_LOCK = RLock()
REALTIME_MATCH_PREFERENCE_TAGS = [
    "特种兵",
    "慢旅行",
    "拍照",
    "美食",
    "自然风光",
    "人文历史",
    "早起",
    "夜景",
]


def ok(data: Any = None, message: str = "ok") -> dict:
    return {"code": 0, "message": message, "data": data if data is not None else {}}


def paginate(items: list[dict], page: int, page_size: int) -> dict:
    page = max(page, 1)
    page_size = max(min(page_size, 100), 1)
    start = (page - 1) * page_size
    return {
        "page": page,
        "pageSize": page_size,
        "total": len(items),
        "list": items[start:start + page_size],
    }


def normalize_text(value: str) -> str:
    return value.strip().lower()


def parse_date_value(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def author_from_user(user: CurrentUser) -> dict:
    profile = store.get("users", user.uid) or {}
    return {
        "id": user.uid,
        "nickname": profile.get("display_name") or user.display_name,
        "avatar": profile.get("photo_url", ""),
    }


def user_model(raw: dict) -> dict:
    return {
        "id": raw.get("uid") or raw.get("id"),
        "nickname": raw.get("display_name", ""),
        "avatar": raw.get("photo_url", ""),
        "bio": raw.get("bio", ""),
        "gender": raw.get("gender", "unknown"),
        "createdAt": raw.get("created_at"),
        "updatedAt": raw.get("updated_at"),
    }


def user_info(raw: dict) -> dict:
    return {
        "id": raw.get("uid") or raw.get("id"),
        "nickname": raw.get("display_name", ""),
        "avatar": raw.get("photo_url", ""),
    }


def is_published(item: dict) -> bool:
    return item.get("status", "published") == "published"


def is_owner(item: dict, user: CurrentUser) -> bool:
    return item.get("author", {}).get("id") == user.uid


def interaction_id(user_id: str, kind: str, target_id: str) -> str:
    return f"{user_id}:{kind}:{target_id}"


def interaction_exists(user_id: str, kind: str, target_id: str) -> bool:
    return store.get("fe_interactions", interaction_id(user_id, kind, target_id)) is not None


def strategy_model(raw: dict, user: CurrentUser | None = None) -> dict:
    strategy_id = raw["id"]
    image_list = raw.get("imageList", [])
    signed_image_list = []
    for item in image_list:
        if isinstance(item, dict):
            next_item = dict(item)
            next_item["rawUrl"] = next_item.get("url", "")
            next_item["url"] = sign_read_url(next_item.get("url", ""))
            signed_image_list.append(next_item)
        elif isinstance(item, str):
            signed_image_list.append(sign_read_url(item))
        else:
            signed_image_list.append(item)
    result = {
        "id": strategy_id,
        "title": raw.get("title", ""),
        "summary": raw.get("summary", ""),
        "content": raw.get("content", ""),
        "destination": raw.get("destination", ""),
        "days": raw.get("days", 0),
        "rawCoverUrl": raw.get("coverUrl", ""),
        "coverUrl": sign_read_url(raw.get("coverUrl", "")),
        "imageList": signed_image_list,
        "tags": raw.get("tags", []),
        "author": raw.get("author", {}),
        "likeCount": raw.get("likeCount", 0),
        "favoriteCount": raw.get("favoriteCount", 0),
        "viewCount": raw.get("viewCount", 0),
        "commentCount": raw.get("commentCount", 0),
        "shareCount": raw.get("shareCount", 0),
        "status": raw.get("status", "published"),
        "createdAt": raw.get("createdAt"),
        "updatedAt": raw.get("updatedAt"),
    }
    if user:
        result["isLiked"] = interaction_exists(user.uid, "strategy_like", strategy_id)
        result["isFavorited"] = interaction_exists(user.uid, "strategy_favorite", strategy_id)
    return result


def post_model(raw: dict, user: CurrentUser | None = None) -> dict:
    post_id = raw["id"]
    media_list = []
    for item in raw.get("mediaList", []):
        if isinstance(item, dict):
            next_item = dict(item)
            next_item["rawUrl"] = next_item.get("url", "")
            next_item["url"] = sign_read_url(next_item.get("url", ""))
            media_list.append(next_item)
        else:
            media_list.append(item)
    result = {
        "id": post_id,
        "type": raw.get("type", "vlog"),
        "title": raw.get("title", ""),
        "content": raw.get("content", ""),
        "location": raw.get("location", ""),
        "rawCoverUrl": raw.get("coverUrl", ""),
        "coverUrl": sign_read_url(raw.get("coverUrl", "")),
        "mediaList": media_list,
        "tags": raw.get("tags", []),
        "author": raw.get("author", {}),
        "likeCount": raw.get("likeCount", 0),
        "favoriteCount": raw.get("favoriteCount", 0),
        "commentCount": raw.get("commentCount", 0),
        "shareCount": raw.get("shareCount", 0),
        "status": raw.get("status", "published"),
        "createdAt": raw.get("createdAt"),
        "updatedAt": raw.get("updatedAt"),
    }
    if user:
        result["isLiked"] = interaction_exists(user.uid, "post_like", post_id)
        result["isFavorited"] = interaction_exists(user.uid, "post_favorite", post_id)
    return result


def optional_user_from_authorization(authorization: str | None) -> CurrentUser | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    user = get_user_by_token(authorization.removeprefix("Bearer ").strip())
    if not user:
        return None
    return CurrentUser(
        uid=user.get("uid", ""),
        email=user.get("email", ""),
        display_name=user.get("display_name", user.get("uid", "")),
    )


def recommendation_content_model(raw: dict, user: CurrentUser | None = None) -> dict:
    target_type = raw.get("targetType")
    if target_type == "strategy":
        result = {"contentType": "strategy", **strategy_model(raw, user)}
    else:
        result = {"contentType": raw.get("contentType") or raw.get("type", "vlog"), **post_model(raw, user)}
    for key in ("recommendationScore", "recommendationReason", "recommendationTraceId", "recommendationFeatures"):
        if key in raw:
            result[key] = raw[key]
    return result


def get_content_item(collection: str, item_id: str, not_found_detail: str) -> dict:
    item = store.get(collection, item_id)
    if not item:
        raise HTTPException(status_code=404, detail=not_found_detail)
    return item


def ensure_strategy_visible(item: dict, current_user: CurrentUser) -> dict:
    if is_published(item) or is_owner(item, current_user):
        return item
    raise HTTPException(status_code=404, detail="strategy not found")


def ensure_post_visible(item: dict, current_user: CurrentUser) -> dict:
    if is_published(item) or is_owner(item, current_user):
        return item
    raise HTTPException(status_code=404, detail="post not found")


def ensure_content_owner(item: dict, current_user: CurrentUser, detail: str = "not allowed to operate this content") -> None:
    if not is_owner(item, current_user):
        raise HTTPException(status_code=403, detail=detail)


def delete_content_references(target_type: str, target_id: str) -> None:
    for comment in store.list("fe_comments"):
        if comment.get("targetType") == target_type and comment.get("targetId") == target_id:
            store.delete("fe_comments", comment["id"])
    kind_prefix = f"{target_type}_"
    for interaction in store.list("fe_interactions"):
        if interaction.get("targetId") == target_id and str(interaction.get("kind", "")).startswith(kind_prefix):
            store.delete("fe_interactions", interaction["id"])


def toggle_interaction(user_id: str, collection: str, target_id: str, kind: str, count_field: str) -> tuple[bool, int]:
    item = store.get(collection, target_id)
    if not item:
        raise HTTPException(status_code=404, detail="content not found")

    iid = interaction_id(user_id, kind, target_id)
    if store.get("fe_interactions", iid):
        store.delete("fe_interactions", iid)
        new_count = max(int(item.get(count_field, 0)) - 1, 0)
        enabled = False
    else:
        now = now_utc().isoformat()
        store.upsert(
            "fe_interactions",
            iid,
            {
                "id": iid,
                "userId": user_id,
                "kind": kind,
                "targetId": target_id,
                "createdAt": now,
                "updatedAt": now,
            },
        )
        new_count = int(item.get(count_field, 0)) + 1
        enabled = True

    store.update(collection, target_id, {count_field: new_count, "updatedAt": now_utc().isoformat()})
    target_type = "strategy" if kind.startswith("strategy_") else "post"
    event_type = "like" if kind.endswith("_like") else "favorite"
    save_recommendation_event(user_id, event_type, target_type, target_id, metadata={"enabled": enabled})
    return enabled, new_count


def comment_model(raw: dict) -> dict:
    return {
        "id": raw["id"],
        "userId": raw.get("userId", ""),
        "nickname": raw.get("nickname", ""),
        "avatar": raw.get("avatar", ""),
        "content": raw.get("content", ""),
        "createdAt": raw.get("createdAt"),
    }


def list_comments(target_type: str, target_id: str) -> list[dict]:
    items = [
        comment_model(item)
        for item in store.list("fe_comments")
        if item.get("targetType") == target_type and item.get("targetId") == target_id
    ]
    items.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return items


def create_comment(collection: str, target_type: str, target_id: str, content: str, current_user: CurrentUser) -> dict:
    item = get_content_item(collection, target_id, "content not found")
    text = content.strip()
    if not text:
        raise HTTPException(status_code=400, detail="comment content is required")
    author = author_from_user(current_user)
    now = now_utc().isoformat()
    comment = store.create(
        "fe_comments",
        {
            "targetType": target_type,
            "targetId": target_id,
            "userId": current_user.uid,
            "nickname": author["nickname"],
            "avatar": author["avatar"],
            "content": text,
            "createdAt": now,
            "updatedAt": now,
        },
    )
    store.update(
        collection,
        target_id,
        {
            "commentCount": int(item.get("commentCount", 0)) + 1,
            "updatedAt": now,
        },
    )
    save_recommendation_event(current_user.uid, "comment", target_type, target_id)
    return comment_model(comment)


def share_content(collection: str, target_id: str) -> int:
    item = get_content_item(collection, target_id, "content not found")
    share_count = int(item.get("shareCount", 0)) + 1
    store.update(collection, target_id, {"shareCount": share_count, "updatedAt": now_utc().isoformat()})
    return share_count


def list_interaction_contents(current_user: CurrentUser, kind_suffix: str, type_filter: str, page: int, page_size: int) -> dict:
    items: list[dict] = []
    interactions = [
        item
        for item in store.list("fe_interactions")
        if item.get("userId") == current_user.uid and item.get("kind", "").endswith(kind_suffix)
    ]
    interactions.sort(key=lambda item: item.get("createdAt") or "", reverse=True)

    for interaction in interactions:
        kind = interaction.get("kind", "")
        target_id = interaction.get("targetId", "")
        if kind.startswith("strategy_"):
            if type_filter not in ("all", "strategy"):
                continue
            target = store.get("fe_strategies", target_id)
            if target and is_published(target):
                items.append({"contentType": "strategy", **strategy_model(target, current_user)})
        elif kind.startswith("post_"):
            target = store.get("fe_posts", target_id)
            if not target or not is_published(target):
                continue
            content_type = target.get("type", "vlog")
            if type_filter not in ("all", content_type):
                continue
            items.append({"contentType": content_type, **post_model(target, current_user)})

    return paginate(items, page, page_size)


def build_strategy_content(overview: str, daily_plans: list[dict], tips: list[str]) -> str:
    lines = [overview.strip(), "", "行程安排"]
    for plan in daily_plans:
        lines.append(f"Day {plan.get('day', 0)}")
        activities = plan.get("activities", [])
        if activities:
            lines.append("活动：")
            lines.extend(f"- {activity}" for activity in activities)
        food = plan.get("food", [])
        if food:
            lines.append("美食：")
            lines.extend(f"- {item}" for item in food)
        accommodation = plan.get("accommodation", "").strip()
        if accommodation:
            lines.append(f"住宿：{accommodation}")
        lines.append("")
    if tips:
        lines.append("出行提示")
        lines.extend(f"- {tip}" for tip in tips)
    return "\n".join(line for line in lines if line is not None).strip()


def build_ai_strategy_draft(payload: "AiPayload", result: dict) -> dict:
    destination = result.get("destination") or payload.destination
    overview = result.get("overview", "")
    daily_plans = result.get("dailyPlans", [])
    tips = result.get("tips", [])
    title = f"{destination}{payload.days}天旅行攻略"
    summary = overview[:120].strip() or f"适合 {payload.days} 天出行的 {destination} 行程草案"
    tags = [destination, payload.groupType, payload.budget]
    deduped_tags: list[str] = []
    for tag in tags:
        value = (tag or "").strip()
        if value and value not in deduped_tags:
            deduped_tags.append(value)
    return {
        "title": title,
        "summary": summary,
        "content": build_strategy_content(overview, daily_plans, tips),
        "destination": destination,
        "days": payload.days,
        "coverUrl": "",
        "tags": deduped_tags,
        "status": "draft",
        "overview": overview,
        "dailyPlans": daily_plans,
        "tips": tips,
        "editable": True,
        "saved": False,
        "source": "ai",
    }


def issue_frontend_token(user: dict, login_type: str) -> dict:
    token = issue_token(
        upsert_user(
            uid=user["uid"],
            display_name=user.get("display_name", ""),
            email=user.get("email", ""),
            photo_url=user.get("photo_url", ""),
            bio=user.get("bio", ""),
        )
    )
    profile = store.get("users", user["uid"]) or user
    return {
        "token": token.access_token,
        "refreshToken": token_urlsafe(32),
        "loginType": login_type,
        "userInfo": user_info(profile),
    }


def recruit_status(raw: dict) -> str:
    return raw.get("status", "open")


def is_open_recruitment(raw: dict) -> bool:
    return recruit_status(raw) == "open"


def recommend_card(raw: dict, application_status: str) -> dict:
    card = {
        "recruitId": raw["id"],
        "publisherUserId": raw.get("publisherUserId", ""),
        "publisherNickname": raw.get("publisherNickname", ""),
        "publisherAvatar": raw.get("publisherAvatar", ""),
        "destination": raw.get("destination", ""),
        "startDate": raw.get("startDate", ""),
        "days": raw.get("days", 0),
        "applicationStatus": application_status,
    }
    if raw.get("aiMatch"):
        card.update(raw["aiMatch"])
    return card


def my_recruitment_list_item(raw: dict) -> dict:
    return {
        "id": raw["id"],
        "destination": raw.get("destination", ""),
        "startDate": raw.get("startDate", ""),
        "days": raw.get("days", 0),
        "status": recruit_status(raw),
        "applicationCount": raw.get("applicationCount", 0),
        "createdAt": raw.get("createdAt"),
    }


def recruitment_application_item(raw: dict) -> dict:
    return {
        "applicationId": raw["id"],
        "applicantUserId": raw.get("applicantUserId", ""),
        "applicantNickname": raw.get("applicantNickname", ""),
        "applicantAvatar": raw.get("applicantAvatar", ""),
        "destination": raw.get("destination", ""),
        "startDate": raw.get("startDate", ""),
        "days": raw.get("days", 0),
        "status": raw.get("status", "pending"),
        "createdAt": raw.get("createdAt"),
    }


def my_match_application_item(raw: dict) -> dict:
    return {
        "applicationId": raw["id"],
        "recruitId": raw.get("recruitId", ""),
        "publisherUserId": raw.get("publisherUserId", ""),
        "publisherNickname": raw.get("publisherNickname", ""),
        "publisherAvatar": raw.get("publisherAvatar", ""),
        "destination": raw.get("destination", ""),
        "startDate": raw.get("startDate", ""),
        "days": raw.get("days", 0),
        "status": raw.get("status", "pending"),
        "createdAt": raw.get("createdAt"),
    }


def draft_item(raw: dict, draft_type: str) -> dict:
    if draft_type == "strategy":
        title = raw.get("title") or f'{raw.get("destination", "未命名目的地")} 行程草稿'
        payload = {
            "title": raw.get("title", ""),
            "summary": raw.get("summary", ""),
            "content": raw.get("content", ""),
            "destination": raw.get("destination", ""),
            "days": raw.get("days", 1),
            "coverUrl": raw.get("coverUrl", ""),
            "imageList": raw.get("imageList", []),
            "tags": raw.get("tags", []),
        }
    else:
        title = raw.get("title") or f'{raw.get("location", "未命名地点")} 发布草稿'
        payload = {
            "type": raw.get("type", "vlog"),
            "title": raw.get("title", ""),
            "location": raw.get("location", ""),
            "content": raw.get("content", ""),
            "tags": raw.get("tags", []),
            "coverUrl": raw.get("coverUrl", ""),
            "mediaList": raw.get("mediaList", []),
        }

    return {
        "id": raw["id"],
        "draftType": draft_type,
        "title": title,
        "updatedAt": raw.get("updatedAt") or raw.get("createdAt"),
        "payload": payload,
    }


def notification_title(notification_type: str) -> tuple[str, str]:
    mapping = {
        "new_match_application": ("搭子申请", "你收到了一条新的搭子申请"),
        "match_application_approved": ("搭子结果", "你的搭子申请已通过"),
        "match_application_rejected": ("搭子结果", "你的搭子申请未通过"),
        "realtime_match_found": ("搭子匹配", "系统为你找到了一位新的候选搭子"),
        "realtime_match_confirmed": ("搭子确认", "你和对方已互相同意，可以准备见面了"),
    }
    return mapping.get(notification_type, ("系统通知", "你收到一条新的消息"))


def notification_item(raw: dict) -> dict:
    type_label, title = notification_title(raw.get("type", ""))
    return {
        "id": raw["id"],
        "type": type_label,
        "notificationType": raw.get("type", ""),
        "title": title,
        "data": raw.get("data", {}),
        "createdAt": raw.get("createdAt"),
        "read": raw.get("read", False),
    }


def legacy_match_request_model(raw: dict) -> dict:
    return {
        "id": raw["id"],
        "destination": raw.get("destination", ""),
        "startDate": raw.get("startDate", ""),
        "days": raw.get("days", 0),
        "budget": raw.get("budget", "中等"),
        "genderPreference": raw.get("genderPreference", "不限"),
        "remarks": raw.get("remarks", ""),
        "status": raw.get("status", "searching"),
        "createdAt": raw.get("createdAt"),
        "updatedAt": raw.get("updatedAt"),
    }


def get_recruitment_item(recruit_id: str) -> dict:
    item = store.get(MATCH_RECRUITMENTS_COLLECTION, recruit_id)
    if not item:
        raise HTTPException(status_code=404, detail="recruitment not found")
    return item


def get_application_item(application_id: str) -> dict:
    item = store.get(MATCH_APPLICATIONS_COLLECTION, application_id)
    if not item:
        raise HTTPException(status_code=404, detail="application not found")
    return item


def find_match_application(recruit_id: str, applicant_user_id: str) -> dict | None:
    for item in store.list(MATCH_APPLICATIONS_COLLECTION):
        if item.get("recruitId") == recruit_id and item.get("applicantUserId") == applicant_user_id:
            return item
    return None


def list_recruitment_applications(recruit_id: str) -> list[dict]:
    return [
        item
        for item in store.list(MATCH_APPLICATIONS_COLLECTION)
        if item.get("recruitId") == recruit_id
    ]


def list_recruitment_application_models(recruit_id: str) -> list[dict]:
    application_rows = list_recruitment_applications(recruit_id)
    application_rows.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    application_rows.sort(key=lambda item: 0 if item.get("status") == "pending" else 1)
    return [recruitment_application_item(item) for item in application_rows]


def create_match_notification(user_id: str, notification_type: str, data: dict) -> None:
    now = now_utc().isoformat()
    store.create(
        MATCH_NOTIFICATIONS_COLLECTION,
        {
            "userId": user_id,
            "type": notification_type,
            "data": data,
            "read": False,
            "createdAt": now,
            "updatedAt": now,
        },
    )


def is_hidden_recruitment(raw: dict) -> bool:
    return raw.get("source") == "realtime" and raw.get("visibility") == "hidden"


def normalize_preference_tags(tags: list[str]) -> list[str]:
    cleaned: list[str] = []
    for tag in tags:
        value = tag.strip()
        if not value:
            continue
        if value not in REALTIME_MATCH_PREFERENCE_TAGS:
            raise HTTPException(status_code=400, detail=f"unsupported preference tag: {value}")
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def extract_text_keywords(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", normalize_text(value))
        if len(token) >= 2
    }


def parse_datetime_value(value: str) -> datetime:
    return datetime.fromisoformat(value)


def build_match_deadline(travel_start_date: date) -> datetime:
    return datetime.combine(travel_start_date, time.min, tzinfo=REALTIME_MATCH_TIMEZONE) - timedelta(hours=48)


def build_realtime_match_deadline(travel_start_date: date) -> datetime:
    base_deadline = build_match_deadline(travel_start_date)
    now = now_utc()
    if base_deadline > now:
        return base_deadline
    return now + timedelta(hours=24)


def build_meet_time(travel_start_date: date) -> datetime:
    return datetime.combine(travel_start_date, time(REALTIME_MATCH_MEET_HOUR), tzinfo=REALTIME_MATCH_TIMEZONE)


def date_range_overlap_days(start_a: date, end_a: date, start_b: date, end_b: date) -> int:
    latest_start = max(start_a, start_b)
    earliest_end = min(end_a, end_b)
    if latest_start > earliest_end:
        return 0
    return (earliest_end - latest_start).days + 1


def save_realtime_request(raw: dict) -> dict:
    return store.upsert(REALTIME_MATCH_REQUESTS_COLLECTION, raw["id"], raw)


def save_realtime_candidate(raw: dict) -> dict:
    return store.upsert(REALTIME_MATCH_CANDIDATES_COLLECTION, raw["id"], raw)


def save_realtime_pair(raw: dict) -> dict:
    return store.upsert(REALTIME_MATCH_PAIRS_COLLECTION, raw["id"], raw)


def get_realtime_request(request_id: str) -> dict | None:
    return store.get(REALTIME_MATCH_REQUESTS_COLLECTION, request_id)


def get_realtime_candidate(candidate_id: str) -> dict | None:
    return store.get(REALTIME_MATCH_CANDIDATES_COLLECTION, candidate_id)


def get_realtime_pair(pair_id: str) -> dict | None:
    return store.get(REALTIME_MATCH_PAIRS_COLLECTION, pair_id)


def list_realtime_requests() -> list[dict]:
    return store.list(REALTIME_MATCH_REQUESTS_COLLECTION)


def list_realtime_candidates() -> list[dict]:
    return store.list(REALTIME_MATCH_CANDIDATES_COLLECTION)


def list_realtime_pairs() -> list[dict]:
    return store.list(REALTIME_MATCH_PAIRS_COLLECTION)


def get_hidden_recruitment_for_request(request_id: str) -> dict | None:
    for item in store.list(MATCH_RECRUITMENTS_COLLECTION):
        if is_hidden_recruitment(item) and item.get("requestId") == request_id:
            return item
    return None


def set_hidden_recruitment_status(request_id: str, status: str) -> None:
    recruitment = get_hidden_recruitment_for_request(request_id)
    if recruitment:
        recruitment["status"] = status
        recruitment["updatedAt"] = now_utc().isoformat()
        store.upsert(MATCH_RECRUITMENTS_COLLECTION, recruitment["id"], recruitment)


def get_user_latest_realtime_request(user_id: str) -> dict | None:
    items = [item for item in list_realtime_requests() if item.get("user_id") == user_id]
    if not items:
        return None
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items[0]


def get_user_active_realtime_request(user_id: str) -> dict | None:
    items = [
        item
        for item in list_realtime_requests()
        if item.get("user_id") == user_id and item.get("status") in REALTIME_MATCH_ACTIVE_STATUSES
    ]
    if not items:
        return None
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items[0]


def supersede_user_active_realtime_requests(user_id: str) -> None:
    now = now_utc().isoformat()
    for request in list_realtime_requests():
        if request.get("user_id") != user_id or request.get("status") not in REALTIME_MATCH_ACTIVE_STATUSES:
            continue
        request["status"] = "cancelled"
        request["updated_at"] = now
        save_realtime_request(request)
        set_hidden_recruitment_status(request["id"], "closed")

        candidate_id = request.get("current_candidate_id", "")
        if candidate_id:
            candidate = get_realtime_candidate(candidate_id)
            if candidate:
                candidate["status"] = "cancelled"
                candidate["my_decision"] = candidate.get("my_decision") or "cancelled"
                candidate["updated_at"] = now
                save_realtime_candidate(candidate)

                peer_candidate = get_realtime_candidate(candidate.get("peer_candidate_id", ""))
                if peer_candidate:
                    peer_candidate["status"] = "cancelled"
                    peer_candidate["peer_decision"] = "cancelled"
                    peer_candidate["updated_at"] = now
                    save_realtime_candidate(peer_candidate)

        pair_id = request.get("current_pair_id", "")
        if pair_id:
            pair = get_realtime_pair(pair_id)
            if pair and pair.get("status") == "active":
                pair["status"] = "cancelled"
                pair["updated_at"] = now
                save_realtime_pair(pair)


def get_realtime_request_or_404(request_id: str) -> dict:
    request = get_realtime_request(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="match request not found")
    return request


def get_realtime_candidate_or_404(candidate_id: str) -> dict:
    candidate = get_realtime_candidate(candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="match candidate not found")
    return candidate


def get_realtime_pair_or_404(pair_id: str) -> dict:
    pair = get_realtime_pair(pair_id)
    if not pair:
        raise HTTPException(status_code=404, detail="match pair not found")
    return pair


def build_match_summary(request: dict, peer_request: dict) -> str:
    shared_tags = [
        tag
        for tag in request.get("preference_tags", [])
        if tag in peer_request.get("preference_tags", [])
    ]
    if shared_tags:
        preview = "、".join(shared_tags[:2])
        return f"你们都偏好{preview}，旅行节奏更容易合拍"

    shared_keywords = sorted(
        extract_text_keywords(request.get("preference_text", ""))
        & extract_text_keywords(peer_request.get("preference_text", ""))
    )
    if shared_keywords:
        return f"你们都提到了“{shared_keywords[0]}”，这趟旅途可能很同频"

    return "你们的旅行地点和时间高度重合，适合先见面聊聊"


def build_realtime_candidate_response(candidate: dict) -> dict:
    response = {
        "candidate_id": candidate["id"],
        "peer_user_id": candidate.get("peer_user_id", ""),
        "peer_nickname": candidate.get("peer_nickname", ""),
        "peer_avatar": candidate.get("peer_avatar", ""),
        "meeting_place_text": candidate.get("meeting_place_text", ""),
        "match_summary": candidate.get("match_summary", ""),
        "decision_expires_at": candidate.get("decision_expires_at"),
        "my_decision": candidate.get("my_decision", "pending"),
        "peer_decision": candidate.get("peer_decision", "pending"),
    }
    if candidate.get("ai_match"):
        response.update(candidate["ai_match"])
    return response


def build_realtime_pair_response(pair: dict, current_user_id: str) -> dict:
    is_user_a = pair.get("user_a_id") == current_user_id
    return {
        "pair_id": pair["id"],
        "peer_user_id": pair.get("user_b_id", "") if is_user_a else pair.get("user_a_id", ""),
        "peer_nickname": pair.get("user_b_nickname", "") if is_user_a else pair.get("user_a_nickname", ""),
        "peer_avatar": pair.get("user_b_avatar", "") if is_user_a else pair.get("user_a_avatar", ""),
        "meet_time": pair.get("meet_time"),
        "meet_location_text": pair.get("meet_location_text", ""),
        "my_remark": pair.get("remark_a", "") if is_user_a else pair.get("remark_b", ""),
        "peer_remark": pair.get("remark_b", "") if is_user_a else pair.get("remark_a", ""),
        "my_rating": pair.get("rating_a") if is_user_a else pair.get("rating_b"),
        "peer_rating": pair.get("rating_b") if is_user_a else pair.get("rating_a"),
        "my_rating_comment": pair.get("rating_comment_a", "") if is_user_a else pair.get("rating_comment_b", ""),
        "rating_submitted": bool(pair.get("rating_a") if is_user_a else pair.get("rating_b")),
    }


def build_realtime_state_response(request: dict | None, current_user_id: str) -> dict:
    if not request:
        return {"active": False}

    candidate = (
        get_realtime_candidate(request.get("current_candidate_id", ""))
        if request.get("current_candidate_id")
        else None
    )
    pair = (
        get_realtime_pair(request.get("current_pair_id", ""))
        if request.get("current_pair_id")
        else None
    )
    status = request.get("status", "")
    return {
        "active": status in REALTIME_MATCH_ACTIVE_STATUSES,
        "request_id": request["id"],
        "status": status,
        "destination": request.get("destination", ""),
        "travel_start_date": request.get("travel_start_date", ""),
        "travel_end_date": request.get("travel_end_date", ""),
        "preference_tags": request.get("preference_tags", []),
        "preference_text": request.get("preference_text", ""),
        "match_deadline_at": request.get("match_deadline_at"),
        "candidate": build_realtime_candidate_response(candidate) if candidate and status == "matched_waiting_decision" else None,
        "pair": build_realtime_pair_response(pair, current_user_id) if pair and status == "matched_accepted" else None,
    }


def build_realtime_history_item(request: dict, current_user_id: str) -> dict:
    state = build_realtime_state_response(request, current_user_id)
    state["created_at"] = request.get("created_at", "")
    state["updated_at"] = request.get("updated_at", "")
    return state


def has_rejected_or_expired_history(request_id: str, peer_request_id: str) -> bool:
    pair_keys = {(request_id, peer_request_id), (peer_request_id, request_id)}
    for item in list_realtime_candidates():
        if (item.get("request_id"), item.get("peer_request_id")) not in pair_keys:
            continue
        if item.get("status") in {"rejected", "expired"}:
            return True
    return False


def reopen_request_or_fail(request_id: str) -> None:
    request = get_realtime_request(request_id)
    if not request or request.get("status") == "matched_accepted":
        return

    now = now_utc()
    deadline = parse_datetime_value(request["match_deadline_at"])
    request["current_candidate_id"] = ""
    request["updated_at"] = now.isoformat()
    if now >= deadline:
        request["status"] = "failed"
        set_hidden_recruitment_status(request_id, "closed")
    else:
        request["status"] = "pending"
        set_hidden_recruitment_status(request_id, "open")
    save_realtime_request(request)


def sync_realtime_match_state() -> None:
    now = now_utc()

    for pair in list_realtime_pairs():
        if pair.get("status") != "active":
            continue
        meet_time = parse_datetime_value(pair["meet_time"])
        end_of_day = datetime.combine(meet_time.date(), time.max, tzinfo=meet_time.tzinfo or timezone.utc)
        if now < end_of_day:
            continue
        pair["status"] = "finished"
        pair["updated_at"] = now.isoformat()
        save_realtime_pair(pair)
        for request_id in (pair.get("request_a_id", ""), pair.get("request_b_id", "")):
            request = get_realtime_request(request_id)
            if not request:
                continue
            request["status"] = "finished"
            request["updated_at"] = now.isoformat()
            save_realtime_request(request)
            set_hidden_recruitment_status(request_id, "closed")

    processed_candidate_ids: set[str] = set()
    for candidate in list_realtime_candidates():
        if candidate.get("id") in processed_candidate_ids:
            continue
        if candidate.get("status") != "pending_decision":
            continue
        decision_expires_at = parse_datetime_value(candidate["decision_expires_at"])
        if now < decision_expires_at:
            continue

        peer_candidate = get_realtime_candidate(candidate.get("peer_candidate_id", ""))
        group = [candidate]
        if peer_candidate:
            group.append(peer_candidate)

        for item in group:
            processed_candidate_ids.add(item["id"])
            item["status"] = "expired"
            item["my_decision"] = "expired"
            item["peer_decision"] = "expired"
            item["updated_at"] = now.isoformat()
            save_realtime_candidate(item)

        reopen_request_or_fail(candidate.get("request_id", ""))
        reopen_request_or_fail(candidate.get("peer_request_id", ""))

    for request in list_realtime_requests():
        if request.get("status") != "pending":
            continue
        if now < parse_datetime_value(request["match_deadline_at"]):
            continue
        request["status"] = "failed"
        request["updated_at"] = now.isoformat()
        save_realtime_request(request)
        set_hidden_recruitment_status(request["id"], "closed")


def build_hidden_recruitment_payload(payload: dict, request_id: str, current_user: CurrentUser) -> dict:
    author = author_from_user(current_user)
    now = now_utc().isoformat()
    return {
        "publisherUserId": current_user.uid,
        "publisherNickname": author["nickname"],
        "publisherAvatar": author["avatar"],
        "destination": payload.get("destination", ""),
        "travelStartDate": payload.get("travel_start_date", ""),
        "travelEndDate": payload.get("travel_end_date", ""),
        "preferenceTags": payload.get("preference_tags", []),
        "preferenceText": payload.get("preference_text", ""),
        "source": "realtime",
        "visibility": "hidden",
        "requestId": request_id,
        "status": "open",
        "applicationCount": 0,
        "createdAt": now,
        "updatedAt": now,
    }


def choose_best_peer_request(request: dict) -> dict | None:
    current_start = parse_date_value(request["travel_start_date"])
    current_end = parse_date_value(request["travel_end_date"])
    current_destination = normalize_destination(request.get("destination", ""))
    intent = request.get("ai_profile") or build_match_intent(
        request.get("destination", ""),
        request.get("preference_tags", []),
        request.get("preference_text", ""),
    )
    candidates: list[dict] = []

    for peer_request in list_realtime_requests():
        if peer_request["id"] == request["id"]:
            continue
        if peer_request.get("user_id") == request.get("user_id"):
            continue
        if peer_request.get("status") != "pending":
            continue
        if peer_request.get("current_candidate_id") or peer_request.get("current_pair_id"):
            continue
        if has_rejected_or_expired_history(request["id"], peer_request["id"]):
            continue
        if normalize_destination(peer_request.get("destination", "")) != current_destination:
            continue

        peer_start = parse_date_value(peer_request["travel_start_date"])
        peer_end = parse_date_value(peer_request["travel_end_date"])
        overlap_days = date_range_overlap_days(current_start, current_end, peer_start, peer_end)
        if overlap_days <= 0:
            continue

        candidates.append(build_realtime_candidate(request, peer_request, intent))

    if not candidates:
        return None

    ranked, meta = rank_candidates(intent, candidates, mode="realtime", user_id=request.get("user_id", ""))
    initial_reflection = reflect_candidate_quality(candidates, ranked, mode="realtime", intent=intent)
    meta["reflection"] = initial_reflection
    meta["queryPlan"] = build_query_plan(
        intent,
        "realtime",
        candidate_count=len(candidates),
        reflection=initial_reflection,
    )
    trace_id = save_match_trace(
        mode="realtime",
        user_id=request.get("user_id", ""),
        intent=intent,
        candidates=candidates,
        ranked=ranked,
        meta=meta,
    )
    best = ranked[0]
    return {
        "peer_request": best["raw"],
        "ai_match": {
            **public_ai_match(best, trace_id),
            "features": best.get("features", {}),
        },
    }


def create_realtime_candidate_pair(request: dict, peer_request: dict, ai_match: dict | None = None) -> None:
    summary_for_request = (ai_match or {}).get("aiMatchReason") or build_match_summary(request, peer_request)
    summary_for_peer = summary_for_request if ai_match else build_match_summary(peer_request, request)
    shared_tags = [
        tag
        for tag in request.get("preference_tags", [])
        if tag in peer_request.get("preference_tags", [])
    ]
    meeting_place_text = generate_safe_meeting_place(
        request.get("destination", ""),
        shared_tags,
        f"{request.get('preference_text', '')} {peer_request.get('preference_text', '')}".strip(),
    )
    now = now_utc()
    decision_expires_at = (now + timedelta(minutes=REALTIME_MATCH_DECISION_MINUTES)).isoformat()

    peer_profile = store.get("users", peer_request.get("user_id", "")) or {}
    request_profile = store.get("users", request.get("user_id", "")) or {}

    request_candidate = store.create(
        REALTIME_MATCH_CANDIDATES_COLLECTION,
        {
            "request_id": request["id"],
            "peer_request_id": peer_request["id"],
            "user_id": request.get("user_id", ""),
            "peer_user_id": peer_request.get("user_id", ""),
            "peer_avatar": peer_profile.get("photo_url", ""),
            "peer_nickname": peer_profile.get("display_name", peer_request.get("user_id", "")),
            "meeting_place_text": meeting_place_text,
            "match_summary": summary_for_request,
            "ai_match": ai_match or {},
            "my_decision": "pending",
            "peer_decision": "pending",
            "status": "pending_decision",
            "decision_expires_at": decision_expires_at,
            "peer_candidate_id": "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    peer_candidate = store.create(
        REALTIME_MATCH_CANDIDATES_COLLECTION,
        {
            "request_id": peer_request["id"],
            "peer_request_id": request["id"],
            "user_id": peer_request.get("user_id", ""),
            "peer_user_id": request.get("user_id", ""),
            "peer_avatar": request_profile.get("photo_url", ""),
            "peer_nickname": request_profile.get("display_name", request.get("user_id", "")),
            "meeting_place_text": meeting_place_text,
            "match_summary": summary_for_peer,
            "ai_match": ai_match or {},
            "my_decision": "pending",
            "peer_decision": "pending",
            "status": "pending_decision",
            "decision_expires_at": decision_expires_at,
            "peer_candidate_id": request_candidate["id"],
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    request_candidate["peer_candidate_id"] = peer_candidate["id"]
    save_realtime_candidate(request_candidate)

    request["status"] = "matched_waiting_decision"
    request["current_candidate_id"] = request_candidate["id"]
    request["updated_at"] = now.isoformat()
    save_realtime_request(request)
    set_hidden_recruitment_status(request["id"], "closed")

    peer_request["status"] = "matched_waiting_decision"
    peer_request["current_candidate_id"] = peer_candidate["id"]
    peer_request["updated_at"] = now.isoformat()
    save_realtime_request(peer_request)
    set_hidden_recruitment_status(peer_request["id"], "closed")

    create_match_notification(
        request.get("user_id", ""),
        "realtime_match_found",
        {"requestId": request["id"], "candidateId": request_candidate["id"]},
    )
    create_match_notification(
        peer_request.get("user_id", ""),
        "realtime_match_found",
        {"requestId": peer_request["id"], "candidateId": peer_candidate["id"]},
    )


def try_match_pending_request(request: dict) -> dict:
    with MATCH_OPERATION_LOCK:
        latest_request = get_realtime_request(request["id"]) or request
        if latest_request.get("status") != "pending":
            return latest_request
        if latest_request.get("current_candidate_id") or latest_request.get("current_pair_id"):
            return latest_request

        match_result = choose_best_peer_request(latest_request)
        if not match_result:
            return latest_request
        peer_request = match_result["peer_request"]

        latest_peer_request = get_realtime_request(peer_request["id"]) or peer_request
        if latest_peer_request.get("status") != "pending":
            return latest_request
        if latest_peer_request.get("current_candidate_id") or latest_peer_request.get("current_pair_id"):
            return latest_request

        create_realtime_candidate_pair(latest_request, latest_peer_request, match_result.get("ai_match"))
        return get_realtime_request(latest_request["id"]) or latest_request


class DemoLoginPayload(BaseModel):
    nickname: str = "远方旅客"
    avatar: str = ""


class PasswordLoginPayload(BaseModel):
    username: str
    password: str


class RegisterPayload(BaseModel):
    username: str
    password: str
    nickname: str = ""
    phone: str = ""
    smsCode: str = ""
    email: str = ""


class PhoneLoginPayload(BaseModel):
    phone: str
    smsCode: str


class SmsCodePayload(BaseModel):
    phone: str


class WeChatLoginPayload(BaseModel):
    code: str


class UserUpdatePayload(BaseModel):
    nickname: str | None = None
    bio: str | None = None
    avatar: str | None = None
    gender: str | None = None


class StrategyPayload(BaseModel):
    title: str
    summary: str = ""
    content: str
    destination: str
    days: int = Field(default=1, gt=0, le=60)
    coverUrl: str = ""
    imageList: list[dict] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: str = "published"


class PostPayload(BaseModel):
    type: str = "vlog"
    title: str
    location: str = ""
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    coverUrl: str = ""
    mediaList: list[dict] = Field(default_factory=list)
    status: str = "published"


class MatchInputPayload(BaseModel):
    destination: str = Field(min_length=1, max_length=100)
    startDate: date
    days: int = Field(gt=0, le=60)


class LegacyMatchPayload(MatchInputPayload):
    genderPreference: str = "不限"
    budget: str = "中等"
    remarks: str = ""


class RealtimeMatchPayload(BaseModel):
    destination: str = Field(min_length=1, max_length=100)
    travel_start_date: date
    travel_end_date: date
    preference_tags: list[str] = Field(default_factory=list)
    preference_text: str = ""
    replace_existing: bool = False


class RealtimeRemarkPayload(BaseModel):
    remark: str


class RealtimeRatingPayload(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = ""


class MatchFeedbackPayload(BaseModel):
    action: str = Field(min_length=1, max_length=50)
    targetId: str = ""
    traceId: str = ""
    mode: str = ""
    rating: int | None = Field(default=None, ge=1, le=5)
    extra: dict[str, Any] = Field(default_factory=dict)


class AiPayload(BaseModel):
    destination: str
    days: int = Field(default=3, gt=0, le=60)
    budget: str = "中等"
    hotelRequirement: str = "不限"
    allergies: str = "无"
    pace: str = "适中"
    groupType: str = "自由行"


class CommentPayload(BaseModel):
    content: str


class RecommendationEventPayload(BaseModel):
    eventType: str
    targetType: str
    targetId: str
    traceId: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/auth/demo-login")
def login_by_demo(payload: DemoLoginPayload | None = None) -> dict:
    if not settings.dev_allow_anon_auth:
        raise HTTPException(status_code=403, detail="demo login is disabled")
    payload = payload or DemoLoginPayload()
    user = {
        "uid": "demo-user",
        "display_name": payload.nickname,
        "email": "",
        "photo_url": payload.avatar,
        "bio": "探索未知的旅人",
    }
    return ok(issue_frontend_token(user, "demo"))


@router.post("/auth/register")
def register(payload: RegisterPayload) -> dict:
    if payload.phone:
        verify_sms_code(payload.phone, payload.smsCode)
    user = register_password_user(
        username=payload.username,
        password=payload.password,
        nickname=payload.nickname,
        phone=payload.phone,
        email=payload.email,
    )
    account = get_auth_account_by_uid(user["uid"]) or {}
    return ok(
        {
            "id": user["uid"],
            "username": account.get("username", payload.username),
            "phone": account.get("phone", ""),
            "nickname": user.get("display_name", ""),
            "avatar": user.get("photo_url", ""),
            "createdAt": user.get("created_at"),
        },
        message="registered",
    )


@router.post("/auth/password-login")
def login_by_password(payload: PasswordLoginPayload) -> dict:
    if not payload.username or not payload.password:
        raise HTTPException(status_code=400, detail="username and password are required")
    user = authenticate_password_user(payload.username, payload.password)
    return ok(issue_frontend_token(user, "password"))


@router.post("/auth/phone-login")
def login_by_phone(payload: PhoneLoginPayload) -> dict:
    if not payload.phone or not payload.smsCode:
        raise HTTPException(status_code=400, detail="phone and smsCode are required")
    user = authenticate_phone_user(payload.phone, payload.smsCode)
    return ok(issue_frontend_token(user, "phone"))


@router.post("/auth/send-sms-code")
def send_sms_code(payload: SmsCodePayload) -> dict:
    record = create_sms_code(payload.phone)
    data = {"phone": record["phone"], "sent": True}
    if settings.environment.lower() != "production":
        data["debugSmsCode"] = record["code"]
    return ok(data)


@router.post("/auth/wechat")
@router.post("/auth/wechat-login")
def login_by_wechat(payload: WeChatLoginPayload) -> dict:
    token = login_wechat(payload.code)
    return ok(
        {
            "token": token.access_token,
            "refreshToken": token_urlsafe(32),
            "loginType": "wechat",
            "userInfo": user_info(token.user),
        }
    )


@router.post("/auth/logout")
def logout(
    current_user: CurrentUser = AuthUser,
    authorization: str | None = Header(default=None),
) -> dict:
    if authorization and authorization.startswith("Bearer "):
        revoke_token(authorization.removeprefix("Bearer ").strip(), current_user.uid)
    return ok({"userId": current_user.uid})


@router.get("/user/profile")
def get_user_profile(current_user: CurrentUser = AuthUser) -> dict:
    profile = store.get("users", current_user.uid)
    if not profile:
        profile = upsert_user(uid=current_user.uid, display_name=current_user.display_name, email=current_user.email)
    return ok(user_model(profile))


@router.put("/user/profile")
def update_user_profile(payload: UserUpdatePayload, current_user: CurrentUser = AuthUser) -> dict:
    profile = store.get("users", current_user.uid) or upsert_user(uid=current_user.uid, display_name=current_user.display_name)
    updates = {
        "display_name": payload.nickname if payload.nickname is not None else profile.get("display_name", ""),
        "bio": payload.bio if payload.bio is not None else profile.get("bio", ""),
        "photo_url": payload.avatar if payload.avatar is not None else profile.get("photo_url", ""),
        "gender": payload.gender if payload.gender is not None else profile.get("gender", "unknown"),
        "updated_at": now_utc().isoformat(),
    }
    updated = store.update("users", current_user.uid, updates)
    account = get_auth_account_by_uid(current_user.uid)
    if account:
        store.update(
            "auth_accounts",
            current_user.uid,
            {
                "display_name": updated.get("display_name", account.get("display_name", "")),
                "updated_at": now_utc().isoformat(),
            },
        )
    return ok(user_model(updated))


@router.get("/user/stats")
def get_my_stats(current_user: CurrentUser = AuthUser) -> dict:
    strategies = [
        item
        for item in store.list("fe_strategies")
        if item.get("author", {}).get("id") == current_user.uid and is_published(item)
    ]
    posts = [
        item
        for item in store.list("fe_posts")
        if item.get("author", {}).get("id") == current_user.uid and is_published(item)
    ]
    likes = [
        item
        for item in store.list("fe_interactions")
        if item.get("userId") == current_user.uid and item.get("kind", "").endswith("_like")
    ]
    favorites = [
        item
        for item in store.list("fe_interactions")
        if item.get("userId") == current_user.uid and item.get("kind", "").endswith("_favorite")
    ]
    return ok(
        {
            "postCount": len(posts),
            "strategyCount": len(strategies),
            "favoriteCount": len(favorites),
            "likeCount": len(likes),
        }
    )


@router.get("/users/{user_id}")
def get_user_public_profile(user_id: str) -> dict:
    profile = store.get("users", user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="user not found")
    strategies = [
        item
        for item in store.list("fe_strategies")
        if item.get("author", {}).get("id") == user_id and is_published(item)
    ]
    posts = [
        item
        for item in store.list("fe_posts")
        if item.get("author", {}).get("id") == user_id and is_published(item)
    ]
    published_items = [*strategies, *posts]
    stats = {
        "posts": len(published_items),
        "favorites": sum(int(item.get("favoriteCount", 0) or 0) for item in published_items),
        "likes": sum(int(item.get("likeCount", 0) or 0) for item in published_items),
    }
    return ok({**user_model(profile), "stats": stats})


@router.get("/users/{user_id}/contents")
def get_user_published_content(user_id: str, type: str = "all", page: int = 1, pageSize: int = 10) -> dict:
    items: list[dict] = []
    if type in ("all", "strategy"):
        items += [
            {"contentType": "strategy", **strategy_model(item)}
            for item in store.list("fe_strategies")
            if item.get("author", {}).get("id") == user_id and is_published(item)
        ]
    if type in ("all", "vlog"):
        items += [
            {"contentType": item.get("type", "vlog"), **post_model(item)}
            for item in store.list("fe_posts")
            if item.get("author", {}).get("id") == user_id and is_published(item)
        ]
    items.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return ok(paginate(items, page, pageSize))


@router.get("/my/favorites")
def get_my_favorite_contents(type: str = "all", page: int = 1, pageSize: int = 10, current_user: CurrentUser = AuthUser) -> dict:
    return ok(list_interaction_contents(current_user, "_favorite", type, page, pageSize))


@router.get("/my/likes")
def get_my_like_contents(type: str = "all", page: int = 1, pageSize: int = 10, current_user: CurrentUser = AuthUser) -> dict:
    return ok(list_interaction_contents(current_user, "_like", type, page, pageSize))


@router.get("/my/drafts")
def get_my_drafts(current_user: CurrentUser = AuthUser) -> dict:
    items = [
        draft_item(item, "strategy")
        for item in store.list("fe_strategies")
        if item.get("author", {}).get("id") == current_user.uid and item.get("status") == "draft"
    ]
    items += [
        draft_item(item, "post")
        for item in store.list("fe_posts")
        if item.get("author", {}).get("id") == current_user.uid and item.get("status") == "draft"
    ]
    items.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    return ok({"list": items})


@router.get("/my/notifications")
def get_my_notifications(current_user: CurrentUser = AuthUser) -> dict:
    items = [
        notification_item(item)
        for item in store.list(MATCH_NOTIFICATIONS_COLLECTION)
        if item.get("userId") == current_user.uid
    ]
    items.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return ok({"list": items})


@router.post("/my/notifications/read-all")
def mark_all_notifications_read(current_user: CurrentUser = AuthUser) -> dict:
    now = now_utc().isoformat()
    count = 0
    for item in store.list(MATCH_NOTIFICATIONS_COLLECTION):
        if item.get("userId") != current_user.uid or item.get("read"):
            continue
        store.update(MATCH_NOTIFICATIONS_COLLECTION, item["id"], {"read": True, "updatedAt": now})
        count += 1
    return ok({"updated": count})


@router.post("/my/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, current_user: CurrentUser = AuthUser) -> dict:
    item = store.get(MATCH_NOTIFICATIONS_COLLECTION, notification_id)
    if not item or item.get("userId") != current_user.uid:
        raise HTTPException(status_code=404, detail="notification not found")
    updated = store.update(
        MATCH_NOTIFICATIONS_COLLECTION,
        notification_id,
        {"read": True, "updatedAt": now_utc().isoformat()},
    )
    return ok(notification_item(updated))


@router.delete("/my/drafts/{draft_id}")
def delete_my_draft(draft_id: str, current_user: CurrentUser = AuthUser) -> dict:
    for collection, draft_type in (("fe_strategies", "strategy"), ("fe_posts", "post")):
        item = store.get(collection, draft_id)
        if not item:
            continue
        if item.get("status") != "draft":
            raise HTTPException(status_code=400, detail="content is not a draft")
        ensure_content_owner(item, current_user)
        target_type = "strategy" if draft_type == "strategy" else "post"
        delete_content_references(target_type, draft_id)
        store.delete(collection, draft_id)
        return ok({"id": draft_id, "draftType": draft_type})
    raise HTTPException(status_code=404, detail="draft not found")


@router.get("/home/feed")
def get_home_feed(
    page: int = 1,
    pageSize: int = 10,
    recentTargetId: str = "",
    authorization: str | None = Header(default=None),
) -> dict:
    current_user = optional_user_from_authorization(authorization)
    result = recommend_feed(
        current_user.uid if current_user else None,
        page=page,
        page_size=pageSize,
        recent_target_id=recentTargetId,
    )
    return ok(
        {
            "page": result["page"],
            "pageSize": result["pageSize"],
            "total": result["total"],
            "traceId": result["traceId"],
            "list": [recommendation_content_model(item, current_user) for item in result["items"]],
        }
    )


@router.get("/home/recommend-blocks")
def get_home_recommend_blocks() -> dict:
    return ok(
        {
            "blocks": [
                {"title": "热门目的地", "type": "destination", "list": ["大理", "冰岛", "京都", "西藏"]},
                {"title": "精选主题", "type": "category", "list": ["人文历史", "自然风光", "美食地图", "摄影攻略"]},
            ]
        }
    )


@router.get("/recommendation/feed")
def get_recommendation_feed(
    page: int = 1,
    pageSize: int = 10,
    recentTargetId: str = "",
    current_user: CurrentUser = AuthUser,
) -> dict:
    result = recommend_feed(current_user.uid, page=page, page_size=pageSize, recent_target_id=recentTargetId)
    return ok(
        {
            "page": result["page"],
            "pageSize": result["pageSize"],
            "total": result["total"],
            "traceId": result["traceId"],
            "list": [recommendation_content_model(item, current_user) for item in result["items"]],
        }
    )


@router.post("/recommendation/events")
def create_recommendation_event(payload: RecommendationEventPayload, current_user: CurrentUser = AuthUser) -> dict:
    event_type = payload.eventType.strip()
    target_type = payload.targetType.strip()
    if event_type not in {"view", "click", "like", "favorite", "comment", "share", "quick_skip", "dislike", "report"}:
        raise HTTPException(status_code=400, detail="unsupported recommendation event type")
    if target_type not in {"strategy", "post"}:
        raise HTTPException(status_code=400, detail="unsupported recommendation target type")
    collection = "fe_strategies" if target_type == "strategy" else "fe_posts"
    get_content_item(collection, payload.targetId, "content not found")
    item = save_recommendation_event(
        current_user.uid,
        event_type,
        target_type,
        payload.targetId,
        trace_id=payload.traceId,
        metadata=payload.metadata,
    )
    return ok(item)


@router.get("/recommendation/traces/{trace_id}")
def get_recommendation_trace_detail(trace_id: str, current_user: CurrentUser = AuthUser) -> dict:
    trace = get_recommendation_trace(trace_id)
    if not trace or trace.get("userId") not in ("", current_user.uid):
        raise HTTPException(status_code=404, detail="recommendation trace not found")
    return ok(trace)


@router.post("/posts/{post_id}/ai-profile")
def analyze_post_ai_profile(post_id: str, current_user: CurrentUser = AuthUser) -> dict:
    item = get_content_item("fe_posts", post_id, "post not found")
    if not is_published(item):
        ensure_content_owner(item, current_user)
    return ok(analyze_content("post", item))


@router.post("/strategies/{strategy_id}/ai-profile")
def analyze_strategy_ai_profile(strategy_id: str, current_user: CurrentUser = AuthUser) -> dict:
    item = get_content_item("fe_strategies", strategy_id, "strategy not found")
    if not is_published(item):
        ensure_content_owner(item, current_user)
    return ok(analyze_content("strategy", item))


@router.get("/strategies")
def get_strategy_list(keyword: str = "", category: str = "", page: int = 1, pageSize: int = 10) -> dict:
    items = [strategy_model(item) for item in store.list("fe_strategies") if is_published(item)]
    if keyword:
        key = keyword.lower()
        items = [
            item
            for item in items
            if key in item["title"].lower() or key in item["content"].lower() or key in item["destination"].lower()
        ]
    if category and category != "全部":
        items = [item for item in items if category in item.get("tags", [])]
    items.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return ok(paginate(items, page, pageSize))


@router.post("/strategies")
def create_strategy(payload: StrategyPayload, current_user: CurrentUser = AuthUser) -> dict:
    now = now_utc().isoformat()
    item = payload.model_dump()
    item["status"] = "published"
    item.update(
        {
            "author": author_from_user(current_user),
            "likeCount": 0,
            "favoriteCount": 0,
            "viewCount": 0,
            "commentCount": 0,
            "shareCount": 0,
            "createdAt": now,
            "updatedAt": now,
        }
    )
    created = store.create("fe_strategies", item)
    analyze_content("strategy", created)
    return ok(strategy_model(created, current_user))


@router.post("/strategies/drafts")
def save_strategy_draft(payload: StrategyPayload, current_user: CurrentUser = AuthUser) -> dict:
    now = now_utc().isoformat()
    item = payload.model_dump()
    item["status"] = "draft"
    item.update(
        {
            "author": author_from_user(current_user),
            "likeCount": 0,
            "favoriteCount": 0,
            "viewCount": 0,
            "commentCount": 0,
            "shareCount": 0,
            "createdAt": now,
            "updatedAt": now,
        }
    )
    created = store.create("fe_strategies", item)
    return ok(strategy_model(created, current_user))


@router.put("/strategies/{strategy_id}")
def update_strategy(strategy_id: str, payload: StrategyPayload, current_user: CurrentUser = AuthUser) -> dict:
    existing = get_content_item("fe_strategies", strategy_id, "strategy not found")
    ensure_content_owner(existing, current_user)
    item = payload.model_dump()
    item["status"] = existing.get("status", "published")
    item.update(
        {
            "author": existing.get("author", author_from_user(current_user)),
            "likeCount": existing.get("likeCount", 0),
            "favoriteCount": existing.get("favoriteCount", 0),
            "viewCount": existing.get("viewCount", 0),
            "commentCount": existing.get("commentCount", 0),
            "shareCount": existing.get("shareCount", 0),
            "createdAt": existing.get("createdAt"),
            "updatedAt": now_utc().isoformat(),
        }
    )
    updated = store.upsert("fe_strategies", strategy_id, item)
    analyze_content("strategy", updated)
    return ok(strategy_model(updated, current_user))


@router.get("/strategies/{strategy_id}")
def get_strategy_detail(strategy_id: str, current_user: CurrentUser = AuthUser) -> dict:
    item = get_content_item("fe_strategies", strategy_id, "strategy not found")
    item = ensure_strategy_visible(item, current_user)
    item = store.update("fe_strategies", strategy_id, {"viewCount": int(item.get("viewCount", 0)) + 1})
    save_recommendation_event(current_user.uid, "view", "strategy", strategy_id)
    return ok(strategy_model(item, current_user))


@router.post("/strategies/{strategy_id}/like")
def toggle_strategy_like(strategy_id: str, current_user: CurrentUser = AuthUser) -> dict:
    is_liked, count = toggle_interaction(current_user.uid, "fe_strategies", strategy_id, "strategy_like", "likeCount")
    return ok({"isLiked": is_liked, "likeCount": count})


@router.post("/strategies/{strategy_id}/favorite")
def toggle_strategy_favorite(strategy_id: str, current_user: CurrentUser = AuthUser) -> dict:
    is_favorited, count = toggle_interaction(current_user.uid, "fe_strategies", strategy_id, "strategy_favorite", "favoriteCount")
    return ok({"isFavorited": is_favorited, "favoriteCount": count})


@router.get("/strategies/{strategy_id}/comments")
def get_strategy_comments(strategy_id: str) -> dict:
    get_content_item("fe_strategies", strategy_id, "strategy not found")
    return ok({"list": list_comments("strategy", strategy_id)})


@router.post("/strategies/{strategy_id}/comments")
def create_strategy_comment(strategy_id: str, payload: CommentPayload, current_user: CurrentUser = AuthUser) -> dict:
    return ok(create_comment("fe_strategies", "strategy", strategy_id, payload.content, current_user))


@router.post("/strategies/{strategy_id}/share")
def share_strategy(strategy_id: str, current_user: CurrentUser = AuthUser) -> dict:
    share_count = share_content("fe_strategies", strategy_id)
    save_recommendation_event(current_user.uid, "share", "strategy", strategy_id)
    return ok({"shareCount": share_count})


@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: str, current_user: CurrentUser = AuthUser) -> dict:
    item = get_content_item("fe_strategies", strategy_id, "strategy not found")
    ensure_content_owner(item, current_user)
    delete_content_references("strategy", strategy_id)
    store.delete("fe_strategies", strategy_id)
    return ok({"id": strategy_id})


@router.get("/posts")
def get_post_list(type: str = "all", keyword: str = "", page: int = 1, pageSize: int = 10) -> dict:
    items = [post_model(item) for item in store.list("fe_posts") if is_published(item)]
    if type in ("strategy", "vlog"):
        items = [item for item in items if item.get("type") == type]
    if keyword:
        key = keyword.lower()
        items = [
            item
            for item in items
            if key in item["title"].lower() or key in item["content"].lower() or key in item["location"].lower()
        ]
    items.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return ok(paginate(items, page, pageSize))


@router.post("/posts")
def create_post(payload: PostPayload, current_user: CurrentUser = AuthUser) -> dict:
    now = now_utc().isoformat()
    item = payload.model_dump()
    item.update(
        {
            "author": author_from_user(current_user),
            "likeCount": 0,
            "favoriteCount": 0,
            "commentCount": 0,
            "shareCount": 0,
            "createdAt": now,
            "updatedAt": now,
        }
    )
    created = store.create("fe_posts", item)
    analyze_content("post", created)
    return ok(post_model(created, current_user))


@router.post("/posts/drafts")
def save_post_draft(payload: PostPayload, current_user: CurrentUser = AuthUser) -> dict:
    now = now_utc().isoformat()
    item = payload.model_dump()
    item["status"] = "draft"
    item.update(
        {
            "author": author_from_user(current_user),
            "likeCount": 0,
            "favoriteCount": 0,
            "commentCount": 0,
            "shareCount": 0,
            "createdAt": now,
            "updatedAt": now,
        }
    )
    created = store.create("fe_posts", item)
    return ok(draft_item(created, "post"))


@router.put("/posts/{post_id}")
def update_post(post_id: str, payload: PostPayload, current_user: CurrentUser = AuthUser) -> dict:
    existing = get_content_item("fe_posts", post_id, "post not found")
    ensure_content_owner(existing, current_user)
    item = payload.model_dump()
    item["status"] = existing.get("status", "published")
    item.update(
        {
            "author": existing.get("author", author_from_user(current_user)),
            "likeCount": existing.get("likeCount", 0),
            "favoriteCount": existing.get("favoriteCount", 0),
            "commentCount": existing.get("commentCount", 0),
            "shareCount": existing.get("shareCount", 0),
            "createdAt": existing.get("createdAt"),
            "updatedAt": now_utc().isoformat(),
        }
    )
    updated = store.upsert("fe_posts", post_id, item)
    analyze_content("post", updated)
    return ok(post_model(updated, current_user))


@router.get("/posts/{post_id}")
def get_post_detail(post_id: str, current_user: CurrentUser = AuthUser) -> dict:
    item = get_content_item("fe_posts", post_id, "post not found")
    item = ensure_post_visible(item, current_user)
    save_recommendation_event(current_user.uid, "view", "post", post_id)
    return ok(post_model(item, current_user))


@router.get("/my/posts")
def get_my_posts(type: str = "all", page: int = 1, pageSize: int = 10, current_user: CurrentUser = AuthUser) -> dict:
    items = [
        post_model(item, current_user)
        for item in store.list("fe_posts")
        if item.get("author", {}).get("id") == current_user.uid and is_published(item)
    ]
    if type in ("strategy", "vlog"):
        items = [item for item in items if item.get("type") == type]
    items.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return ok(paginate(items, page, pageSize))


@router.post("/posts/{post_id}/like")
def toggle_post_like(post_id: str, current_user: CurrentUser = AuthUser) -> dict:
    is_liked, count = toggle_interaction(current_user.uid, "fe_posts", post_id, "post_like", "likeCount")
    return ok({"isLiked": is_liked, "likeCount": count})


@router.post("/posts/{post_id}/favorite")
def toggle_post_favorite(post_id: str, current_user: CurrentUser = AuthUser) -> dict:
    is_favorited, count = toggle_interaction(current_user.uid, "fe_posts", post_id, "post_favorite", "favoriteCount")
    return ok({"isFavorited": is_favorited, "favoriteCount": count})


@router.get("/posts/{post_id}/comments")
def get_post_comments(post_id: str) -> dict:
    get_content_item("fe_posts", post_id, "post not found")
    return ok({"list": list_comments("post", post_id)})


@router.post("/posts/{post_id}/comments")
def create_post_comment(post_id: str, payload: CommentPayload, current_user: CurrentUser = AuthUser) -> dict:
    return ok(create_comment("fe_posts", "post", post_id, payload.content, current_user))


@router.post("/posts/{post_id}/share")
def share_post(post_id: str, current_user: CurrentUser = AuthUser) -> dict:
    share_count = share_content("fe_posts", post_id)
    save_recommendation_event(current_user.uid, "share", "post", post_id)
    return ok({"shareCount": share_count})


@router.delete("/posts/{post_id}")
def delete_post(post_id: str, current_user: CurrentUser = AuthUser) -> dict:
    item = get_content_item("fe_posts", post_id, "post not found")
    ensure_content_owner(item, current_user)
    delete_content_references("post", post_id)
    store.delete("fe_posts", post_id)
    return ok({"id": post_id})


@router.get("/search")
def search_all(keyword: str = "", type: str = "all", page: int = 1, pageSize: int = 10) -> dict:
    key = keyword.lower()
    strategies = []
    vlogs = []
    if type in ("all", "strategy"):
        strategies = [
            strategy_model(item)
            for item in store.list("fe_strategies")
            if is_published(item)
            and (
                key in item.get("title", "").lower()
                or key in item.get("destination", "").lower()
                or key in item.get("content", "").lower()
            )
        ]
    if type in ("all", "vlog"):
        vlogs = [
            post_model(item)
            for item in store.list("fe_posts")
            if is_published(item)
            and (
                key in item.get("title", "").lower()
                or key in item.get("location", "").lower()
                or key in item.get("content", "").lower()
            )
        ]
    total = len(strategies) + len(vlogs)
    return ok(
        {
            "keyword": keyword,
            "type": type,
            "page": page,
            "pageSize": pageSize,
            "total": total,
            "strategyList": paginate(strategies, page, pageSize)["list"],
            "vlogList": paginate(vlogs, page, pageSize)["list"],
        }
    )


@router.get("/search/hot")
def get_hot_search_list() -> dict:
    now = now_utc().isoformat()
    return ok(
        {
            "list": [
                {"id": 1, "keyword": "大理", "type": "destination", "sort": 1, "status": "active", "createdAt": now, "updatedAt": now},
                {"id": 2, "keyword": "冰岛", "type": "destination", "sort": 2, "status": "active", "createdAt": now, "updatedAt": now},
                {"id": 3, "keyword": "京都", "type": "destination", "sort": 3, "status": "active", "createdAt": now, "updatedAt": now},
            ]
        }
    )


@router.post("/matches/recommend")
def recommend_match_recruitments(payload: MatchInputPayload, current_user: CurrentUser = AuthUser) -> dict:
    destination = normalize_destination(payload.destination)
    intent = build_match_intent(payload.destination, [], f"{payload.startDate.isoformat()} 出发，旅行 {payload.days} 天")
    candidates: list[dict] = []
    seen_recruit_ids: set[str] = set()
    for recruit in store.list(MATCH_RECRUITMENTS_COLLECTION):
        if recruit.get("publisherUserId") == current_user.uid:
            continue
        if is_hidden_recruitment(recruit):
            continue
        if not is_open_recruitment(recruit):
            continue
        if normalize_destination(recruit.get("destination", "")) != destination:
            continue
        start_gap = abs((parse_date_value(recruit.get("startDate", "")) - payload.startDate).days)
        if start_gap > MATCH_DATE_WINDOW_DAYS:
            continue
        stay_gap = abs(int(recruit.get("days", 0)) - payload.days)
        if stay_gap > MATCH_STAY_WINDOW_DAYS:
            continue
        existing_application = find_match_application(recruit["id"], current_user.uid)
        application_status = existing_application.get("status", "pending") if existing_application else "none"
        seen_recruit_ids.add(recruit["id"])
        candidates.append(
            build_recruitment_candidate(
                recruit,
                start_gap=start_gap,
                stay_gap=stay_gap,
                application_status=application_status,
                intent=intent,
            )
        )

    initial_reflection = reflect_candidate_quality(candidates, mode="recruitment")
    relaxed_added = 0
    if "expand_date_and_stay_windows_once" in initial_reflection.get("suggestedActions", []):
        for recruit in store.list(MATCH_RECRUITMENTS_COLLECTION):
            if recruit.get("id") in seen_recruit_ids:
                continue
            if recruit.get("publisherUserId") == current_user.uid:
                continue
            if is_hidden_recruitment(recruit):
                continue
            if not is_open_recruitment(recruit):
                continue
            if normalize_destination(recruit.get("destination", "")) != destination:
                continue
            start_gap = abs((parse_date_value(recruit.get("startDate", "")) - payload.startDate).days)
            if start_gap > MATCH_DATE_WINDOW_DAYS + 4:
                continue
            stay_gap = abs(int(recruit.get("days", 0)) - payload.days)
            if stay_gap > MATCH_STAY_WINDOW_DAYS + 2:
                continue
            existing_application = find_match_application(recruit["id"], current_user.uid)
            application_status = existing_application.get("status", "pending") if existing_application else "none"
            candidate = build_recruitment_candidate(
                recruit,
                start_gap=start_gap,
                stay_gap=stay_gap,
                application_status=application_status,
                intent=intent,
            )
            candidate["features"]["relaxedRecall"] = True
            candidate["features"]["relaxReason"] = "expanded_date_or_stay_window"
            candidate["features"]["retrievalRoutes"] = sorted(set(candidate["features"].get("retrievalRoutes", []) + ["agent_relaxation"]))
            candidates.append(candidate)
            relaxed_added += 1

    ranked, meta = rank_candidates(intent, candidates, mode="recruitment", user_id=current_user.uid)
    initial_reflection = reflect_candidate_quality(candidates, ranked, mode="recruitment", intent=intent)
    meta["reflection"] = initial_reflection
    meta["queryPlan"] = build_query_plan(
        intent,
        "recruitment",
        candidate_count=len(candidates),
        reflection=initial_reflection,
    )
    meta["initialReflection"] = initial_reflection
    meta["agentRelaxedAdded"] = relaxed_added
    trace_id = (
        save_match_trace(
            mode="recruitment",
            user_id=current_user.uid,
            intent=intent,
            candidates=candidates,
            ranked=ranked,
            meta=meta,
        )
        if ranked
        else ""
    )
    cards = []
    for item in ranked:
        raw = dict(item["raw"])
        raw["aiMatch"] = public_ai_match(item, trace_id)
        cards.append(recommend_card(raw, item.get("features", {}).get("applicationStatus", "none")))
    return ok({"list": cards, "total": len(cards)})


@router.get("/match/traces/{trace_id}")
def get_match_trace_detail(trace_id: str, current_user: CurrentUser = AuthUser) -> dict:
    trace = get_match_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="match trace not found")
    if trace.get("userId") != current_user.uid:
        raise HTTPException(status_code=403, detail="not allowed to access this trace")
    return ok(trace)


@router.post("/match/feedback")
def create_match_feedback(payload: MatchFeedbackPayload, current_user: CurrentUser = AuthUser) -> dict:
    allowed_actions = {
        "exposure",
        "click",
        "apply",
        "accept",
        "reject",
        "ignore",
        "timeout",
        "report",
        "block",
        "rate",
    }
    action = payload.action.strip().lower()
    if action not in allowed_actions:
        raise HTTPException(status_code=400, detail="unsupported feedback action")
    reward = (payload.rating - 3) / 2 if payload.rating is not None else None

    item = save_match_feedback(
        user_id=current_user.uid,
        action=action,
        target_id=payload.targetId,
        trace_id=payload.traceId,
        mode=payload.mode,
        extra={**payload.extra, **({"rating": payload.rating} if payload.rating is not None else {})},
        rating=payload.rating,
        reward=reward,
    )
    return ok({"id": item["id"], "action": item.get("action", action)})


@router.post("/matches/recruitments")
def create_match_recruitment(payload: MatchInputPayload, current_user: CurrentUser = AuthUser) -> dict:
    publisher = author_from_user(current_user)
    now = now_utc().isoformat()
    item = payload.model_dump(mode="json")
    item.update(
        {
            "publisherUserId": current_user.uid,
            "publisherNickname": publisher["nickname"],
            "publisherAvatar": publisher["avatar"],
            "status": "open",
            "applicationCount": 0,
            "createdAt": now,
            "updatedAt": now,
        }
    )
    created = store.create(MATCH_RECRUITMENTS_COLLECTION, item)
    return ok(
        {
            "id": created["id"],
            "publisherUserId": created.get("publisherUserId", ""),
            "destination": created.get("destination", ""),
            "startDate": created.get("startDate", ""),
            "days": created.get("days", 0),
            "status": recruit_status(created),
            "createdAt": created.get("createdAt"),
        }
    )


@router.post("/matches/recruitments/{recruit_id}/apply")
def apply_match_recruitment(recruit_id: str, current_user: CurrentUser = AuthUser) -> dict:
    with MATCH_OPERATION_LOCK:
        recruitment = get_recruitment_item(recruit_id)
        if is_hidden_recruitment(recruitment):
            raise HTTPException(status_code=404, detail="recruitment not found")
        if recruitment.get("publisherUserId") == current_user.uid:
            raise HTTPException(status_code=400, detail="cannot apply to your own recruitment")
        if not is_open_recruitment(recruitment):
            raise HTTPException(status_code=400, detail="recruitment is not open")
        if find_match_application(recruit_id, current_user.uid):
            raise HTTPException(status_code=409, detail="recruitment already applied")

        applicant = author_from_user(current_user)
        now = now_utc().isoformat()
        application = store.create(
            MATCH_APPLICATIONS_COLLECTION,
            {
                "recruitId": recruit_id,
                "publisherUserId": recruitment.get("publisherUserId", ""),
                "publisherNickname": recruitment.get("publisherNickname", ""),
                "publisherAvatar": recruitment.get("publisherAvatar", ""),
                "applicantUserId": current_user.uid,
                "applicantNickname": applicant["nickname"],
                "applicantAvatar": applicant["avatar"],
                "destination": recruitment.get("destination", ""),
                "startDate": recruitment.get("startDate", ""),
                "days": recruitment.get("days", 0),
                "status": "pending",
                "createdAt": now,
                "updatedAt": now,
            },
        )
        store.update(
            MATCH_RECRUITMENTS_COLLECTION,
            recruit_id,
            {
                "applicationCount": len(list_recruitment_applications(recruit_id)),
                "updatedAt": now,
            },
        )
        create_match_notification(
            recruitment.get("publisherUserId", ""),
            "new_match_application",
            {"recruitId": recruit_id, "applicationId": application["id"], "applicantUserId": current_user.uid},
        )
        save_match_feedback(
            user_id=current_user.uid,
            action="apply",
            target_id=recruit_id,
            mode="recruitment",
            extra={"applicationId": application["id"], "publisherUserId": recruitment.get("publisherUserId", "")},
        )
    return ok({"applicationId": application["id"], "status": application.get("status", "pending")})


@router.post("/match/realtime")
def create_realtime_match(
    payload: RealtimeMatchPayload,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = AuthUser,
) -> dict:
    sync_realtime_match_state()

    if payload.travel_end_date < payload.travel_start_date:
        raise HTTPException(status_code=400, detail="travel_end_date must be on or after travel_start_date")

    preference_tags = normalize_preference_tags(payload.preference_tags)
    preference_text = payload.preference_text.strip()
    if not preference_tags and not preference_text:
        raise HTTPException(status_code=400, detail="at least one preference tag or preference text is required")

    today = now_utc().astimezone(REALTIME_MATCH_TIMEZONE).date()
    if payload.travel_start_date < today:
        raise HTTPException(status_code=400, detail="travel_start_date cannot be in the past")
    match_deadline = build_realtime_match_deadline(payload.travel_start_date)

    now = now_utc().isoformat()
    active_request = get_user_active_realtime_request(current_user.uid)
    replaced_request_id = ""
    if active_request:
        replaced_request_id = active_request.get("id", "")
        # The mobile client asks the user before starting a new round. Treat a
        # repeated submit as a replacement as a backend safety net, so stale
        # app state cannot block the demo flow with a 409.
        supersede_user_active_realtime_requests(current_user.uid)

    item = payload.model_dump(mode="json", exclude={"replace_existing"})
    item["destination"] = item["destination"].strip()
    item["preference_tags"] = preference_tags
    item["preference_text"] = preference_text
    item["ai_profile"] = build_match_intent(item["destination"], preference_tags, preference_text)
    item.update(
        {
            "user_id": current_user.uid,
            "status": "pending",
            "match_deadline_at": match_deadline.isoformat(),
            "current_candidate_id": "",
            "current_pair_id": "",
            "recruitment_id": "",
            "created_at": now,
            "updated_at": now,
        }
    )
    created = store.create(REALTIME_MATCH_REQUESTS_COLLECTION, item)
    hidden_recruitment = store.create(
        MATCH_RECRUITMENTS_COLLECTION,
        build_hidden_recruitment_payload(created, created["id"], current_user),
    )
    created["recruitment_id"] = hidden_recruitment["id"]
    save_realtime_request(created)

    background_tasks.add_task(try_match_pending_request, created)
    return ok(
        {
            "request_id": created["id"],
            "status": created.get("status", "pending"),
            "match_deadline_at": created.get("match_deadline_at"),
            "replaced_request_id": replaced_request_id,
        }
    )


@router.get("/match/realtime/current")
def get_current_realtime_match(current_user: CurrentUser = AuthUser) -> dict:
    sync_realtime_match_state()

    request = get_user_active_realtime_request(current_user.uid) or get_user_latest_realtime_request(current_user.uid)
    if not request:
        return ok({"active": False})

    if request.get("status") == "pending":
        request = try_match_pending_request(request)

    return ok(build_realtime_state_response(request, current_user.uid))


@router.get("/match/realtime/history")
def get_realtime_match_history(current_user: CurrentUser = AuthUser) -> dict:
    sync_realtime_match_state()
    items = [
        build_realtime_history_item(item, current_user.uid)
        for item in list_realtime_requests()
        if item.get("user_id") == current_user.uid
    ]
    items.sort(key=lambda item: item.get("created_at") or item.get("updated_at") or "", reverse=True)
    return ok({"list": items})


@router.post("/match/realtime/candidate/{candidate_id}/accept")
def accept_realtime_candidate(candidate_id: str, current_user: CurrentUser = AuthUser) -> dict:
    sync_realtime_match_state()

    candidate = get_realtime_candidate_or_404(candidate_id)
    if candidate.get("user_id") != current_user.uid:
        raise HTTPException(status_code=403, detail="not allowed to operate this candidate")

    request = get_realtime_request_or_404(candidate.get("request_id", ""))
    if request.get("status") == "matched_accepted" and request.get("current_pair_id"):
        pair = get_realtime_pair_or_404(request["current_pair_id"])
        return ok({"status": "matched_accepted", "pair": build_realtime_pair_response(pair, current_user.uid)})
    if candidate.get("status") != "pending_decision":
        raise HTTPException(status_code=400, detail="candidate is not waiting for decision")

    peer_candidate = get_realtime_candidate_or_404(candidate.get("peer_candidate_id", ""))
    peer_request = get_realtime_request_or_404(candidate.get("peer_request_id", ""))

    if candidate.get("my_decision") == "accepted" and peer_candidate.get("my_decision") != "accepted":
        candidate["peer_decision"] = peer_candidate.get("my_decision", "pending")
        return ok({"status": "matched_waiting_decision", "candidate": build_realtime_candidate_response(candidate)})

    now = now_utc().isoformat()
    peer_already_accepted = peer_candidate.get("my_decision") == "accepted"

    candidate["my_decision"] = "accepted"
    candidate["peer_decision"] = "accepted" if peer_already_accepted else "pending"
    candidate["updated_at"] = now
    if peer_already_accepted:
        candidate["status"] = "accepted"
    save_realtime_candidate(candidate)
    save_match_feedback(
        user_id=current_user.uid,
        action="accept",
        target_id=candidate["id"],
        trace_id=candidate.get("ai_match", {}).get("aiMatchTraceId", ""),
        mode="realtime",
        extra={"peerUserId": candidate.get("peer_user_id", "")},
    )

    peer_candidate["peer_decision"] = "accepted"
    peer_candidate["updated_at"] = now
    if peer_already_accepted:
        peer_candidate["status"] = "accepted"
    save_realtime_candidate(peer_candidate)

    if not peer_already_accepted:
        return ok({"status": "matched_waiting_decision", "candidate": build_realtime_candidate_response(candidate)})

    meet_day = max(
        parse_date_value(request["travel_start_date"]),
        parse_date_value(peer_request["travel_start_date"]),
    )
    request_profile = store.get("users", request.get("user_id", "")) or {}
    peer_profile = store.get("users", peer_request.get("user_id", "")) or {}
    pair = store.create(
        REALTIME_MATCH_PAIRS_COLLECTION,
        {
            "request_a_id": request["id"],
            "request_b_id": peer_request["id"],
            "user_a_id": request.get("user_id", ""),
            "user_b_id": peer_request.get("user_id", ""),
            "user_a_nickname": request_profile.get("display_name", request.get("user_id", "")),
            "user_b_nickname": peer_profile.get("display_name", peer_request.get("user_id", "")),
            "user_a_avatar": request_profile.get("photo_url", ""),
            "user_b_avatar": peer_profile.get("photo_url", ""),
            "meet_time": build_meet_time(meet_day).isoformat(),
            "meet_location_text": candidate.get("meeting_place_text", ""),
            "remark_a": "",
            "remark_b": "",
            "rating_a": None,
            "rating_b": None,
            "rating_comment_a": "",
            "rating_comment_b": "",
            "rating_at_a": "",
            "rating_at_b": "",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
    )

    request["status"] = "matched_accepted"
    request["current_pair_id"] = pair["id"]
    request["updated_at"] = now
    save_realtime_request(request)
    set_hidden_recruitment_status(request["id"], "closed")

    peer_request["status"] = "matched_accepted"
    peer_request["current_pair_id"] = pair["id"]
    peer_request["updated_at"] = now
    save_realtime_request(peer_request)
    set_hidden_recruitment_status(peer_request["id"], "closed")

    create_match_notification(
        request.get("user_id", ""),
        "realtime_match_confirmed",
        {"requestId": request["id"], "pairId": pair["id"]},
    )
    create_match_notification(
        peer_request.get("user_id", ""),
        "realtime_match_confirmed",
        {"requestId": peer_request["id"], "pairId": pair["id"]},
    )
    return ok({"status": "matched_accepted", "pair": build_realtime_pair_response(pair, current_user.uid)})


@router.post("/match/realtime/candidate/{candidate_id}/reject")
def reject_realtime_candidate(candidate_id: str, current_user: CurrentUser = AuthUser) -> dict:
    sync_realtime_match_state()

    candidate = get_realtime_candidate_or_404(candidate_id)
    if candidate.get("user_id") != current_user.uid:
        raise HTTPException(status_code=403, detail="not allowed to operate this candidate")

    request = get_realtime_request_or_404(candidate.get("request_id", ""))
    if request.get("status") == "matched_accepted":
        raise HTTPException(status_code=400, detail="matched pair is already confirmed")
    if candidate.get("status") == "rejected":
        latest_request = get_realtime_request_or_404(candidate.get("request_id", ""))
        return ok({"status": latest_request.get("status", "pending")})
    if candidate.get("status") != "pending_decision":
        raise HTTPException(status_code=400, detail="candidate is not waiting for decision")

    peer_candidate = get_realtime_candidate_or_404(candidate.get("peer_candidate_id", ""))
    now = now_utc().isoformat()

    candidate["status"] = "rejected"
    candidate["my_decision"] = "rejected"
    candidate["peer_decision"] = peer_candidate.get("my_decision", "pending")
    candidate["updated_at"] = now
    save_realtime_candidate(candidate)
    save_match_feedback(
        user_id=current_user.uid,
        action="reject",
        target_id=candidate["id"],
        trace_id=candidate.get("ai_match", {}).get("aiMatchTraceId", ""),
        mode="realtime",
        extra={"peerUserId": candidate.get("peer_user_id", "")},
    )

    peer_candidate["status"] = "rejected"
    peer_candidate["peer_decision"] = "rejected"
    peer_candidate["updated_at"] = now
    save_realtime_candidate(peer_candidate)

    reopen_request_or_fail(candidate.get("request_id", ""))
    reopen_request_or_fail(candidate.get("peer_request_id", ""))

    latest_request = get_realtime_request_or_404(candidate.get("request_id", ""))
    return ok({"status": latest_request.get("status", "pending")})


@router.post("/match/realtime/pair/{pair_id}/remark")
def submit_realtime_pair_remark(
    pair_id: str,
    payload: RealtimeRemarkPayload,
    current_user: CurrentUser = AuthUser,
) -> dict:
    sync_realtime_match_state()

    pair = get_realtime_pair_or_404(pair_id)
    is_user_a = pair.get("user_a_id") == current_user.uid
    is_user_b = pair.get("user_b_id") == current_user.uid
    if not is_user_a and not is_user_b:
        raise HTTPException(status_code=403, detail="not allowed to access this pair")
    if pair.get("status") != "active":
        raise HTTPException(status_code=400, detail="pair is not active")

    remark = payload.remark.strip()
    if not remark:
        raise HTTPException(status_code=400, detail="remark is required")

    now = now_utc().isoformat()
    if is_user_a:
        if pair.get("remark_a", "").strip():
            raise HTTPException(status_code=409, detail="remark already submitted")
        pair["remark_a"] = remark
    else:
        if pair.get("remark_b", "").strip():
            raise HTTPException(status_code=409, detail="remark already submitted")
        pair["remark_b"] = remark
    pair["updated_at"] = now
    save_realtime_pair(pair)
    return ok(build_realtime_pair_response(pair, current_user.uid))


@router.post("/match/realtime/pair/{pair_id}/rate")
def rate_realtime_pair(
    pair_id: str,
    payload: RealtimeRatingPayload,
    current_user: CurrentUser = AuthUser,
) -> dict:
    sync_realtime_match_state()

    pair = get_realtime_pair_or_404(pair_id)
    is_user_a = pair.get("user_a_id") == current_user.uid
    is_user_b = pair.get("user_b_id") == current_user.uid
    if not is_user_a and not is_user_b:
        raise HTTPException(status_code=403, detail="not allowed to access this pair")
    if pair.get("status") not in {"active", "finished"}:
        raise HTTPException(status_code=400, detail="pair is not rateable")

    now = now_utc().isoformat()
    rating = int(payload.rating)
    comment = payload.comment.strip()[:200]
    reward = (rating - 3) / 2
    if is_user_a:
        pair["rating_a"] = rating
        pair["rating_comment_a"] = comment
        pair["rating_at_a"] = now
        peer_user_id = pair.get("user_b_id", "")
        peer_request_id = pair.get("request_b_id", "")
    else:
        pair["rating_b"] = rating
        pair["rating_comment_b"] = comment
        pair["rating_at_b"] = now
        peer_user_id = pair.get("user_a_id", "")
        peer_request_id = pair.get("request_a_id", "")

    peer_request = get_realtime_request(peer_request_id) or {}
    extra = {
        "pairId": pair_id,
        "peerUserId": peer_user_id,
        "destination": peer_request.get("destination", ""),
        "preferenceTags": peer_request.get("preference_tags", []),
        "preferenceText": peer_request.get("preference_text", ""),
        "rating": rating,
        "comment": comment,
    }
    save_match_feedback(
        user_id=current_user.uid,
        action="rate",
        target_id=pair_id,
        mode="realtime",
        extra=extra,
        rating=rating,
        reward=reward,
    )
    pair["updated_at"] = now
    save_realtime_pair(pair)
    return ok(build_realtime_pair_response(pair, current_user.uid))


@router.post("/match/realtime/{request_id}/cancel")
def cancel_realtime_match(request_id: str, current_user: CurrentUser = AuthUser) -> dict:
    sync_realtime_match_state()

    request = get_realtime_request_or_404(request_id)
    if request.get("user_id") != current_user.uid:
        raise HTTPException(status_code=403, detail="not allowed to cancel this request")
    if request.get("status") != "pending":
        raise HTTPException(status_code=400, detail="only pending request can be cancelled")

    request["status"] = "cancelled"
    request["updated_at"] = now_utc().isoformat()
    save_realtime_request(request)
    set_hidden_recruitment_status(request_id, "closed")
    return ok({"status": "cancelled"})


@router.post("/matches")
def submit_match_request(payload: LegacyMatchPayload, current_user: CurrentUser = AuthUser) -> dict:
    now = now_utc().isoformat()
    item = payload.model_dump(mode="json")
    item.update(
        {
            "userId": current_user.uid,
            "status": "searching",
            "createdAt": now,
            "updatedAt": now,
        }
    )
    created = store.create(LEGACY_MATCH_REQUESTS_COLLECTION, item)
    return ok(legacy_match_request_model(created))


@router.get("/matches")
def get_match_records(
    page: int = 1,
    pageSize: int = 10,
    status: str = "",
    current_user: CurrentUser = AuthUser,
) -> dict:
    items = [
        legacy_match_request_model(item)
        for item in store.list(LEGACY_MATCH_REQUESTS_COLLECTION)
        if item.get("userId") == current_user.uid and (not status or item.get("status") == status)
    ]
    items.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return ok(paginate(items, page, pageSize))


@router.get("/my/recruitments")
def get_my_recruitments(current_user: CurrentUser = AuthUser) -> dict:
    items = []
    for item in store.list(MATCH_RECRUITMENTS_COLLECTION):
        if item.get("publisherUserId") != current_user.uid or is_hidden_recruitment(item):
            continue
        recruitment = my_recruitment_list_item(item)
        recruitment["applications"] = list_recruitment_application_models(item["id"])
        items.append(recruitment)
    items.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return ok({"list": items})


@router.get("/my/recruitments/{recruit_id}")
def get_my_recruitment_detail(recruit_id: str, current_user: CurrentUser = AuthUser) -> dict:
    recruitment = get_recruitment_item(recruit_id)
    if is_hidden_recruitment(recruitment):
        raise HTTPException(status_code=404, detail="recruitment not found")
    if recruitment.get("publisherUserId") != current_user.uid:
        raise HTTPException(status_code=403, detail="not allowed to access this recruitment")
    return ok(
        {
            "id": recruitment["id"],
            "destination": recruitment.get("destination", ""),
            "startDate": recruitment.get("startDate", ""),
            "days": recruitment.get("days", 0),
            "status": recruit_status(recruitment),
            "applications": list_recruitment_application_models(recruit_id),
        }
    )


def update_match_application_status(recruit_id: str, application_id: str, new_status: str, current_user: CurrentUser) -> dict:
    recruitment = get_recruitment_item(recruit_id)
    if recruitment.get("publisherUserId") != current_user.uid:
        raise HTTPException(status_code=403, detail="not allowed to operate this recruitment")
    application = get_application_item(application_id)
    if application.get("recruitId") != recruit_id:
        raise HTTPException(status_code=400, detail="application does not belong to this recruitment")
    if application.get("status") != "pending":
        raise HTTPException(status_code=400, detail="application is already processed")
    updated = store.update(
        MATCH_APPLICATIONS_COLLECTION,
        application_id,
        {"status": new_status, "updatedAt": now_utc().isoformat()},
    )
    create_match_notification(
        application.get("applicantUserId", ""),
        f"match_application_{new_status}",
        {"recruitId": recruit_id, "applicationId": application_id, "status": new_status},
    )
    return {"applicationId": updated["id"], "status": updated.get("status", new_status)}


@router.post("/my/recruitments/{recruit_id}/applications/{application_id}/approve")
def approve_match_application(recruit_id: str, application_id: str, current_user: CurrentUser = AuthUser) -> dict:
    return ok(update_match_application_status(recruit_id, application_id, "approved", current_user))


@router.post("/my/recruitments/{recruit_id}/applications/{application_id}/reject")
def reject_match_application(recruit_id: str, application_id: str, current_user: CurrentUser = AuthUser) -> dict:
    return ok(update_match_application_status(recruit_id, application_id, "rejected", current_user))


@router.get("/my/match-applications")
def get_my_match_applications(current_user: CurrentUser = AuthUser) -> dict:
    items = [
        my_match_application_item(item)
        for item in store.list(MATCH_APPLICATIONS_COLLECTION)
        if item.get("applicantUserId") == current_user.uid
    ]
    items.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return ok({"list": items})


@router.get("/matches/{match_id}")
def get_match_detail(match_id: str, current_user: CurrentUser = AuthUser) -> dict:
    item = store.get(LEGACY_MATCH_REQUESTS_COLLECTION, match_id)
    if not item:
        raise HTTPException(status_code=404, detail="match record not found")
    if item.get("userId") != current_user.uid:
        raise HTTPException(status_code=403, detail="not allowed to access this match record")
    return ok(legacy_match_request_model(item))


@router.post("/ai/generate-strategy")
def generate_strategy_for_frontend(payload: AiPayload, current_user: CurrentUser = AuthUser) -> dict:
    result = generate_strategy(
        GenerateStrategyRequest(
            destination=payload.destination,
            days=payload.days,
            budget=payload.budget,
            hotel_req=payload.hotelRequirement,
            allergies=payload.allergies,
            pace=payload.pace,
            group_type=payload.groupType,
        )
    )
    data = result.model_dump()
    data["dailyPlans"] = data.pop("daily_plans")
    return ok(build_ai_strategy_draft(payload, data))


@router.post("/upload/image")
async def upload_image_for_frontend(request: Request, file: UploadFile = File(...), current_user: CurrentUser = AuthUser) -> dict:
    uploaded = await upload_media(file, "image", current_user.uid, str(request.base_url))
    return ok({"url": uploaded.url, "fileName": uploaded.filename})


@router.post("/upload/video")
async def upload_video_for_frontend(request: Request, file: UploadFile = File(...), current_user: CurrentUser = AuthUser) -> dict:
    uploaded = await upload_media(file, "video", current_user.uid, str(request.base_url))
    return ok({"url": uploaded.url, "fileName": uploaded.filename, "coverUrl": ""})
