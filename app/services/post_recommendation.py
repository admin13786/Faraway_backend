from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.models.common import now_utc
from app.services.store import store

POST_AI_PROFILES_COLLECTION = "post_ai_profiles"
USER_INTEREST_PROFILES_COLLECTION = "user_interest_profiles"
RECOMMENDATION_EVENTS_COLLECTION = "recommendation_events"
RECOMMENDATION_TRACES_COLLECTION = "recommendation_traces"

DESTINATION_ALIASES = {
    "大理": ["大理", "洱海", "喜洲", "双廊", "沙溪"],
    "杭州": ["杭州", "西湖", "灵隐寺", "千岛湖"],
    "京都": ["京都", "清水寺", "岚山", "祇园"],
    "东京": ["东京", "新宿", "涩谷", "银座"],
    "厦门": ["厦门", "鼓浪屿", "曾厝垵", "环岛路"],
    "西藏": ["西藏", "拉萨", "林芝", "纳木错"],
    "新疆": ["新疆", "伊犁", "赛里木湖", "喀纳斯"],
    "冰岛": ["冰岛", "雷克雅未克", "极光"],
    "云南": ["云南", "丽江", "香格里拉", "泸沽湖"],
}

THEME_KEYWORDS = {
    "拍照": ["拍照", "摄影", "出片", "打卡", "写真"],
    "慢旅行": ["慢旅行", "不赶", "轻松", "松弛", "散步", "citywalk", "Citywalk"],
    "美食": ["美食", "小吃", "餐厅", "探店", "咖啡"],
    "自然风光": ["自然", "风光", "海边", "湖", "山", "草原", "日出", "日落"],
    "人文历史": ["人文", "历史", "博物馆", "古城", "寺", "建筑"],
    "避坑": ["避坑", "不要去", "踩雷", "注意", "后悔"],
    "省钱": ["省钱", "预算", "人均", "便宜", "免费"],
    "自驾": ["自驾", "租车", "公路", "路线"],
    "徒步": ["徒步", "爬山", "户外", "路线"],
    "特种兵": ["特种兵", "赶路", "暴走", "一天很多点"],
}

USEFULNESS_MARKERS = ["day", "Day", "路线", "预算", "人均", "交通", "门票", "酒店", "住宿", "避坑", "时间", "适合"]
COMMERCIAL_MARKERS = ["加微信", "私信", "团购", "优惠码", "推广", "合作", "下单", "链接", "VX", "v信"]
CLICKBAIT_MARKERS = ["千万别", "后悔死", "全网最", "姐妹们快", "绝了", "封神", "不看亏"]
EVENT_WEIGHTS = {
    "view": 0.2,
    "click": 1.0,
    "like": 3.0,
    "favorite": 5.0,
    "comment": 4.0,
    "share": 4.0,
    "quick_skip": -2.0,
    "dislike": -5.0,
    "report": -10.0,
}


def _has_valid_llm_config() -> bool:
    return bool(settings.dashscope_api_key and "API Key" not in settings.dashscope_api_key)


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def _chat_json(prompt: str, *, temperature: float = 0.1, timeout: int = 35) -> dict:
    url = settings.dashscope_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.dashscope_model,
        "messages": [
            {
                "role": "system",
                "content": "You are a travel content quality rater. Return strict JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    response = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return _extract_json(content)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return round(max(low, min(high, value)), 4)


def _content_text(item: dict) -> str:
    values = [
        item.get("title", ""),
        item.get("summary", ""),
        item.get("content", ""),
        item.get("destination", ""),
        item.get("location", ""),
        " ".join(str(tag) for tag in item.get("tags", []) if tag),
    ]
    return " ".join(str(value) for value in values if value).strip()


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    return {
        token
        for token in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", lowered)
        if len(token) >= 2
    }


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    common = set(left) & set(right)
    dot = sum(left[key] * right[key] for key in common)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _text_counter(text: str) -> Counter[str]:
    counter: Counter[str] = Counter()
    lowered = text.lower()
    for token in _tokens(lowered):
        counter[token] += 1
    for destination, aliases in DESTINATION_ALIASES.items():
        if any(alias.lower() in lowered for alias in aliases):
            counter[destination.lower()] += 3
    for theme, keywords in THEME_KEYWORDS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            counter[theme.lower()] += 3
    return counter


def _extract_destinations(text: str, item: dict) -> list[str]:
    destinations: list[str] = []
    for key in ("destination", "location"):
        value = str(item.get(key, "")).strip()
        if value:
            destinations.append(value)
    lowered = text.lower()
    for destination, aliases in DESTINATION_ALIASES.items():
        if any(alias.lower() in lowered for alias in aliases):
            destinations.append(destination)
    return list(dict.fromkeys(destinations))[:8]


def _extract_themes(text: str, item: dict) -> list[str]:
    themes: list[str] = []
    for tag in item.get("tags", []):
        if tag:
            themes.append(str(tag))
    lowered = text.lower()
    for theme, keywords in THEME_KEYWORDS.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            themes.append(theme)
    return list(dict.fromkeys(themes))[:12]


def _freshness_score(item: dict) -> float:
    raw = item.get("createdAt") or item.get("updatedAt")
    if not raw:
        return 0.35
    try:
        created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_days = max((now_utc() - created).total_seconds() / 86400, 0)
    except ValueError:
        return 0.35
    return _clamp(1 / (1 + age_days / 14))


def _engagement_score(item: dict) -> float:
    likes = int(item.get("likeCount", 0) or 0)
    favorites = int(item.get("favoriteCount", 0) or 0)
    comments = int(item.get("commentCount", 0) or 0)
    shares = int(item.get("shareCount", 0) or 0)
    views = int(item.get("viewCount", 0) or 0)
    raw = likes * 1.0 + favorites * 2.0 + comments * 2.5 + shares * 2.0 + views * 0.15
    return _clamp(math.log1p(raw) / 8)


def _list_value(data: dict, key: str) -> list[str]:
    value = data.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _score_value(data: dict, key: str, default: float) -> float:
    try:
        return _clamp(float(data.get(key, default)))
    except (TypeError, ValueError):
        return _clamp(default)


def _llm_content_profile(target_type: str, item: dict) -> dict | None:
    if not _has_valid_llm_config():
        return None
    title = str(item.get("title", ""))
    content = str(item.get("content", "") or item.get("summary", ""))
    destination = str(item.get("destination", "") or item.get("location", ""))
    tags = [str(tag) for tag in item.get("tags", []) if tag]
    prompt = f"""
Analyze this travel content for a Xiaohongshu-style travel feed recommender.
Return strict JSON. Scores must be numbers from 0 to 1.

Content type: {target_type}
Title: {title}
Destination/location: {destination}
Tags: {tags}
Body:
{content[:3500]}

JSON schema:
{{
  "destinations": ["string"],
  "themes": ["string"],
  "travelStyle": "general | relaxed | outdoor | food | culture | photo",
  "budgetLevel": "low | medium | high | unknown",
  "suitableUsers": ["string"],
  "titleScore": 0.0,
  "contentQualityScore": 0.0,
  "usefulnessScore": 0.0,
  "authenticityScore": 0.0,
  "commercialRiskScore": 0.0,
  "clickbaitRiskScore": 0.0,
  "overallAiQualityScore": 0.0,
  "summary": "one short Chinese sentence explaining the content signal"
}}
"""
    try:
        data = _chat_json(prompt, timeout=8)
    except Exception:
        return None

    destinations = _list_value(data, "destinations") or _extract_destinations(_content_text(item), item)
    themes = _list_value(data, "themes") or _extract_themes(_content_text(item), item)
    profile = {
        "id": f"{target_type}:{item['id']}",
        "targetType": target_type,
        "targetId": item["id"],
        "destinations": destinations[:8],
        "themes": themes[:12],
        "travelStyle": str(data.get("travelStyle") or "general"),
        "budgetLevel": str(data.get("budgetLevel") or "unknown"),
        "suitableUsers": _list_value(data, "suitableUsers")[:8] or ["旅行兴趣用户"],
        "titleScore": _score_value(data, "titleScore", 0.5),
        "contentQualityScore": _score_value(data, "contentQualityScore", 0.5),
        "usefulnessScore": _score_value(data, "usefulnessScore", 0.5),
        "authenticityScore": _score_value(data, "authenticityScore", 0.5),
        "commercialRiskScore": _score_value(data, "commercialRiskScore", 0.0),
        "clickbaitRiskScore": _score_value(data, "clickbaitRiskScore", 0.0),
        "overallAiQualityScore": _score_value(data, "overallAiQualityScore", 0.5),
        "summary": str(data.get("summary") or "")[:160],
        "analysisMode": "llm",
        "model": settings.dashscope_model,
        "sourceUpdatedAt": item.get("updatedAt") or item.get("createdAt") or "",
        "updatedAt": now_utc().isoformat(),
    }
    return profile


def _quick_content_profile(target_type: str, item: dict) -> dict:
    text = _content_text(item)
    title = str(item.get("title", "")).strip()
    content = str(item.get("content", "") or item.get("summary", "")).strip()
    usefulness_hits = sum(1 for marker in USEFULNESS_MARKERS if marker in content or marker in title)
    commercial_hits = sum(1 for marker in COMMERCIAL_MARKERS if marker.lower() in text.lower())
    clickbait_hits = sum(1 for marker in CLICKBAIT_MARKERS if marker in title)
    destinations = _extract_destinations(text, item)
    themes = _extract_themes(text, item)
    usefulness_score = _clamp(0.28 + min(usefulness_hits, 8) * 0.09 + (0.12 if destinations else 0))
    commercial_risk = _clamp(commercial_hits * 0.18)
    clickbait_risk = _clamp(clickbait_hits * 0.2)
    overall = _clamp(
        0.45
        + min(len(content), 700) / 1600
        + usefulness_score * 0.2
        - commercial_risk * 0.2
        - clickbait_risk * 0.15
    )
    return {
        "id": f"{target_type}:{item['id']}",
        "targetType": target_type,
        "targetId": item["id"],
        "destinations": destinations,
        "themes": themes,
        "travelStyle": "relaxed" if "慢旅行" in themes else ("outdoor" if "徒步" in themes or "自驾" in themes else "general"),
        "budgetLevel": "medium",
        "suitableUsers": ["旅行兴趣用户"],
        "titleScore": _clamp(0.45 + min(len(title), 32) / 80 + (0.08 if destinations else 0) - clickbait_hits * 0.12),
        "contentQualityScore": _clamp(0.35 + min(len(content), 700) / 1000 + min(usefulness_hits, 6) * 0.05),
        "usefulnessScore": usefulness_score,
        "authenticityScore": _clamp(0.55 + (0.12 if re.search(r"\d+", content) else 0) + (0.08 if "我" in content else 0) - commercial_hits * 0.12),
        "commercialRiskScore": commercial_risk,
        "clickbaitRiskScore": clickbait_risk,
        "overallAiQualityScore": overall,
        "summary": " / ".join([*(destinations[:2]), *(themes[:3])]) or title[:40],
        "analysisMode": "quick_heuristic",
        "model": "local-heuristic-v1",
        "sourceUpdatedAt": item.get("updatedAt") or item.get("createdAt") or "",
        "updatedAt": now_utc().isoformat(),
    }


def analyze_content(target_type: str, item: dict) -> dict:
    llm_profile = _llm_content_profile(target_type, item)
    if llm_profile:
        return store.upsert(POST_AI_PROFILES_COLLECTION, llm_profile["id"], llm_profile)

    text = _content_text(item)
    title = str(item.get("title", "")).strip()
    content = str(item.get("content", "") or item.get("summary", "")).strip()
    title_len = len(title)
    content_len = len(content)
    usefulness_hits = sum(1 for marker in USEFULNESS_MARKERS if marker in content or marker in title)
    commercial_hits = sum(1 for marker in COMMERCIAL_MARKERS if marker.lower() in text.lower())
    clickbait_hits = sum(1 for marker in CLICKBAIT_MARKERS if marker in title)

    destinations = _extract_destinations(text, item)
    themes = _extract_themes(text, item)
    title_score = _clamp(0.45 + min(title_len, 32) / 80 + (0.08 if destinations else 0) - clickbait_hits * 0.12)
    quality_score = _clamp(0.35 + min(content_len, 700) / 1000 + min(usefulness_hits, 6) * 0.05)
    usefulness_score = _clamp(0.28 + min(usefulness_hits, 8) * 0.09 + (0.12 if destinations else 0))
    authenticity_score = _clamp(0.55 + (0.12 if re.search(r"\d+", content) else 0) + (0.08 if "我" in content else 0) - commercial_hits * 0.12)
    commercial_risk = _clamp(commercial_hits * 0.18)
    clickbait_risk = _clamp(clickbait_hits * 0.2)
    overall = _clamp(
        title_score * 0.15
        + quality_score * 0.28
        + usefulness_score * 0.26
        + authenticity_score * 0.21
        - commercial_risk * 0.2
        - clickbait_risk * 0.15
        + 0.1
    )

    profile = {
        "id": f"{target_type}:{item['id']}",
        "targetType": target_type,
        "targetId": item["id"],
        "destinations": destinations,
        "themes": themes,
        "travelStyle": "relaxed" if "慢旅行" in themes else ("outdoor" if "徒步" in themes or "自驾" in themes else "general"),
        "budgetLevel": "medium",
        "suitableUsers": ["旅行兴趣用户"],
        "titleScore": title_score,
        "contentQualityScore": quality_score,
        "usefulnessScore": usefulness_score,
        "authenticityScore": authenticity_score,
        "commercialRiskScore": commercial_risk,
        "clickbaitRiskScore": clickbait_risk,
        "overallAiQualityScore": overall,
        "summary": " / ".join([*(destinations[:2]), *(themes[:3])]) or title[:40],
        "analysisMode": "local_heuristic",
        "model": "local-heuristic-v1",
        "sourceUpdatedAt": item.get("updatedAt") or item.get("createdAt") or "",
        "updatedAt": now_utc().isoformat(),
    }
    return store.upsert(POST_AI_PROFILES_COLLECTION, profile["id"], profile)


def ensure_content_profile(target_type: str, item: dict) -> dict:
    profile_id = f"{target_type}:{item['id']}"
    profile = store.get(POST_AI_PROFILES_COLLECTION, profile_id)
    source_updated = item.get("updatedAt") or item.get("createdAt") or ""
    if profile and str(profile.get("sourceUpdatedAt", "")) == str(source_updated):
        return profile
    profile = _quick_content_profile(target_type, item)
    profile["sourceUpdatedAt"] = source_updated
    return store.upsert(POST_AI_PROFILES_COLLECTION, profile_id, profile)


def save_recommendation_event(
    user_id: str,
    event_type: str,
    target_type: str,
    target_id: str,
    *,
    trace_id: str = "",
    metadata: dict | None = None,
) -> dict:
    now = now_utc().isoformat()
    return store.create(
        RECOMMENDATION_EVENTS_COLLECTION,
        {
            "userId": user_id,
            "eventType": event_type,
            "targetType": target_type,
            "targetId": target_id,
            "traceId": trace_id,
            "metadata": metadata or {},
            "createdAt": now,
            "updatedAt": now,
        },
    )


def build_user_interest_profile(user_id: str | None) -> dict:
    if not user_id:
        return {
            "id": "anonymous",
            "userId": "",
            "favoriteDestinations": [],
            "favoriteThemes": [],
            "negativeThemes": [],
            "shortTermInterests": [],
            "longTermInterests": [],
            "authorAffinity": [],
            "profileText": "",
            "updatedAt": now_utc().isoformat(),
        }

    destination_scores: defaultdict[str, float] = defaultdict(float)
    theme_scores: defaultdict[str, float] = defaultdict(float)
    negative_scores: defaultdict[str, float] = defaultdict(float)
    author_scores: defaultdict[str, float] = defaultdict(float)

    def absorb(target_type: str, target_id: str, weight: float) -> None:
        collection = "fe_strategies" if target_type == "strategy" else "fe_posts"
        item = store.get(collection, target_id)
        if not item or item.get("status", "published") != "published":
            return
        profile = ensure_content_profile(target_type, item)
        author_id = str(item.get("author", {}).get("id", ""))
        if author_id:
            author_scores[author_id] += weight
        for destination in profile.get("destinations", []):
            destination_scores[str(destination)] += weight
        for theme in profile.get("themes", []):
            if weight >= 0:
                theme_scores[str(theme)] += weight
            else:
                negative_scores[str(theme)] += abs(weight)

    for interaction in store.list("fe_interactions"):
        if interaction.get("userId") != user_id:
            continue
        kind = str(interaction.get("kind", ""))
        target_id = str(interaction.get("targetId", ""))
        if not target_id:
            continue
        if kind.startswith("strategy_"):
            event_name = kind.removeprefix("strategy_")
            absorb("strategy", target_id, EVENT_WEIGHTS.get(event_name, 1.0))
        elif kind.startswith("post_"):
            event_name = kind.removeprefix("post_")
            absorb("post", target_id, EVENT_WEIGHTS.get(event_name, 1.0))

    for event in store.list(RECOMMENDATION_EVENTS_COLLECTION):
        if event.get("userId") != user_id:
            continue
        if event.get("eventType") in {"like", "favorite"} and event.get("metadata", {}).get("enabled") is False:
            continue
        weight = EVENT_WEIGHTS.get(str(event.get("eventType", "")), 0.0)
        if not weight:
            continue
        absorb(str(event.get("targetType", "")), str(event.get("targetId", "")), weight)

    favorite_destinations = [item for item, _ in Counter(destination_scores).most_common(12)]
    favorite_themes = [item for item, _ in Counter(theme_scores).most_common(16)]
    negative_themes = [item for item, _ in Counter(negative_scores).most_common(10)]
    author_affinity = [item for item, _ in Counter(author_scores).most_common(10)]
    profile_text = " ".join(favorite_destinations + favorite_themes)
    payload = {
        "id": user_id,
        "userId": user_id,
        "favoriteDestinations": favorite_destinations,
        "favoriteThemes": favorite_themes,
        "negativeThemes": negative_themes,
        "shortTermInterests": favorite_destinations[:5] + favorite_themes[:5],
        "longTermInterests": favorite_themes[:8],
        "authorAffinity": author_affinity,
        "profileText": profile_text,
        "updatedAt": now_utc().isoformat(),
    }
    return store.upsert(USER_INTEREST_PROFILES_COLLECTION, user_id, payload)


def _list_content_candidates() -> list[dict]:
    items: list[dict] = []
    for item in store.list("fe_strategies"):
        if item.get("status", "published") == "published":
            items.append({"targetType": "strategy", "contentType": "strategy", "item": item})
    for item in store.list("fe_posts"):
        if item.get("status", "published") == "published":
            items.append({"targetType": "post", "contentType": item.get("type", "vlog"), "item": item})
    return items


def _candidate_features(candidate: dict, user_profile: dict, recent_target_id: str = "") -> dict:
    item = candidate["item"]
    target_type = candidate["targetType"]
    ai_profile = ensure_content_profile(target_type, item)
    user_terms = set(str(value).lower() for value in user_profile.get("favoriteDestinations", []) + user_profile.get("favoriteThemes", []))
    negative_terms = set(str(value).lower() for value in user_profile.get("negativeThemes", []))
    post_terms = set(str(value).lower() for value in ai_profile.get("destinations", []) + ai_profile.get("themes", []))
    interest_match = len(user_terms & post_terms) / max(len(user_terms), 1) if user_terms else 0.0
    semantic_similarity = _cosine(_text_counter(user_profile.get("profileText", "")), _text_counter(_content_text(item))) if user_terms else 0.0
    negative_hit = len(negative_terms & post_terms) / max(len(negative_terms), 1) if negative_terms else 0.0
    author_id = str(item.get("author", {}).get("id", ""))
    author_score = 0.8 if author_id and author_id in user_profile.get("authorAffinity", []) else 0.0
    similar_recent = 0.0
    if recent_target_id and str(item.get("id")) != str(recent_target_id):
        recent_item = store.get("fe_strategies", recent_target_id) or store.get("fe_posts", recent_target_id)
        if recent_item:
            similar_recent = _cosine(_text_counter(_content_text(recent_item)), _text_counter(_content_text(item)))

    return {
        "interestMatch": _clamp(interest_match),
        "semanticSimilarity": _clamp(semantic_similarity),
        "aiContentQuality": float(ai_profile.get("overallAiQualityScore", 0.5)),
        "usefulnessScore": float(ai_profile.get("usefulnessScore", 0.5)),
        "freshnessScore": _freshness_score(item),
        "engagementScore": _engagement_score(item),
        "authorQualityScore": _clamp(author_score),
        "similarRecentScore": _clamp(similar_recent),
        "commercialRisk": float(ai_profile.get("commercialRiskScore", 0.0)),
        "clickbaitRisk": float(ai_profile.get("clickbaitRiskScore", 0.0)),
        "negativeFeedbackRate": _clamp(negative_hit),
        "destinations": ai_profile.get("destinations", []),
        "themes": ai_profile.get("themes", []),
    }


def _score_candidate(features: dict) -> float:
    return round(
        features["interestMatch"] * 0.22
        + features["semanticSimilarity"] * 0.14
        + features["aiContentQuality"] * 0.18
        + features["usefulnessScore"] * 0.12
        + features["engagementScore"] * 0.13
        + features["freshnessScore"] * 0.11
        + features["authorQualityScore"] * 0.05
        + features["similarRecentScore"] * 0.05
        - features["commercialRisk"] * 0.12
        - features["clickbaitRisk"] * 0.08
        - features["negativeFeedbackRate"] * 0.18,
        5,
    )


def _diversity_rerank(scored: list[dict]) -> list[dict]:
    result: list[dict] = []
    destination_counts: Counter[str] = Counter()
    author_counts: Counter[str] = Counter()
    theme_counts: Counter[str] = Counter()
    pool = list(scored)
    while pool:
        best_index = 0
        best_value = float("-inf")
        for index, candidate in enumerate(pool):
            item = candidate["item"]
            features = candidate["features"]
            destinations = [str(value) for value in features.get("destinations", [])]
            themes = [str(value) for value in features.get("themes", [])]
            author_id = str(item.get("author", {}).get("id", ""))
            penalty = 0.0
            penalty += sum(destination_counts[destination] * 0.035 for destination in destinations[:2])
            penalty += sum(theme_counts[theme] * 0.02 for theme in themes[:3])
            if author_id:
                penalty += author_counts[author_id] * 0.04
            value = candidate["score"] - penalty
            if value > best_value:
                best_value = value
                best_index = index
        selected = pool.pop(best_index)
        result.append(selected)
        selected_item = selected["item"]
        selected_features = selected["features"]
        for destination in selected_features.get("destinations", [])[:2]:
            destination_counts[str(destination)] += 1
        for theme in selected_features.get("themes", [])[:3]:
            theme_counts[str(theme)] += 1
        author_id = str(selected_item.get("author", {}).get("id", ""))
        if author_id:
            author_counts[author_id] += 1
    return result


def recommend_feed(user_id: str | None, *, page: int = 1, page_size: int = 10, recent_target_id: str = "") -> dict:
    started = datetime.now(timezone.utc)
    user_profile = build_user_interest_profile(user_id)
    candidates = _list_content_candidates()
    scored: list[dict] = []
    for candidate in candidates:
        features = _candidate_features(candidate, user_profile, recent_target_id)
        scored.append(
            {
                **candidate,
                "features": features,
                "score": _score_candidate(features),
                "recallRoutes": _matched_routes(features),
            }
        )
    scored.sort(key=lambda value: value["score"], reverse=True)
    ranked = _diversity_rerank(scored)
    total = len(ranked)
    page = max(page, 1)
    page_size = max(min(page_size, 100), 1)
    start = (page - 1) * page_size
    page_items = ranked[start:start + page_size]
    trace = store.create(
        RECOMMENDATION_TRACES_COLLECTION,
        {
            "userId": user_id or "",
            "queryIntent": {
                "mode": "home_feed",
                "recentTargetId": recent_target_id,
                "profile": {
                    "favoriteDestinations": user_profile.get("favoriteDestinations", [])[:8],
                    "favoriteThemes": user_profile.get("favoriteThemes", [])[:8],
                },
            },
            "recallResults": {
                "candidateCount": len(candidates),
                "routes": ["interest", "destination", "semantic", "hot", "fresh", "author"],
            },
            "rankOutput": [
                {
                    "targetType": item["targetType"],
                    "targetId": item["item"].get("id"),
                    "score": item["score"],
                    "features": item["features"],
                    "recallRoutes": item["recallRoutes"],
                }
                for item in ranked[:50]
            ],
            "finalItems": [
                {
                    "targetType": item["targetType"],
                    "targetId": item["item"].get("id"),
                    "score": item["score"],
                }
                for item in page_items
            ],
            "latencyMs": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            "model": "local-heuristic-v1",
            "createdAt": now_utc().isoformat(),
        },
    )
    return {
        "page": page,
        "pageSize": page_size,
        "total": total,
        "traceId": trace["id"],
        "items": [
            {
                **item["item"],
                "contentType": item["contentType"],
                "targetType": item["targetType"],
                "recommendationScore": item["score"],
                "recommendationReason": _reason_for(item),
                "recommendationTraceId": trace["id"],
                "recommendationFeatures": item["features"],
            }
            for item in page_items
        ],
    }


def _matched_routes(features: dict) -> list[str]:
    routes: list[str] = []
    if features["interestMatch"] > 0:
        routes.append("interest")
    if features["semanticSimilarity"] > 0:
        routes.append("semantic")
    if features["engagementScore"] > 0.4:
        routes.append("hot")
    if features["freshnessScore"] > 0.55:
        routes.append("fresh")
    if features["authorQualityScore"] > 0:
        routes.append("author")
    if not routes:
        routes.append("quality")
    return routes


def _reason_for(candidate: dict) -> str:
    features = candidate["features"]
    themes = [str(value) for value in features.get("themes", [])[:2]]
    destinations = [str(value) for value in features.get("destinations", [])[:1]]
    parts = []
    if destinations:
        parts.append(f"目的地匹配：{destinations[0]}")
    if themes:
        parts.append(f"主题相关：{'、'.join(themes)}")
    if features["aiContentQuality"] >= 0.75:
        parts.append("内容质量较高")
    if features["engagementScore"] >= 0.5:
        parts.append("近期互动表现不错")
    return "；".join(parts[:3]) or "综合质量和新鲜度较好"


def get_recommendation_trace(trace_id: str) -> dict | None:
    return store.get(RECOMMENDATION_TRACES_COLLECTION, trace_id)
