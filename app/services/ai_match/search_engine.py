from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

import httpx

from app.core.config import settings
from app.models.common import now_utc
from app.services.store import store

AI_MATCH_TRACES_COLLECTION = "ai_match_traces"
AI_MATCH_FEEDBACK_COLLECTION = "ai_match_feedback"
MAX_LLM_RERANK_CANDIDATES = 20

DESTINATION_ALIASES = {
    "北京": ["北京", "北京市", "北平", "帝都", "故宫", "三里屯"],
    "上海": ["上海", "上海市", "魔都", "外滩", "武康路"],
    "广州": ["广州", "广州市", "羊城", "花城", "珠江新城"],
    "深圳": ["深圳", "深圳市", "鹏城", "南山", "福田"],
    "大理": ["大理", "大理古城", "洱海", "双廊", "喜洲"],
    "东京": ["东京", "日本东京", "新宿", "涩谷", "银座"],
    "京都": ["京都", "日本京都", "清水寺", "岚山", "祇园"],
    "大阪": ["大阪", "日本大阪", "心斋桥", "难波", "环球影城"],
    "杭州": ["杭州", "西湖", "灵隐寺", "千岛湖"],
    "厦门": ["厦门", "鼓浪屿", "曾厝垵", "环岛路"],
    "伊犁": ["伊犁", "新疆伊犁", "赛里木湖", "那拉提"],
}

STYLE_KEYWORDS = {
    "慢旅行": ["慢旅行", "轻松", "不赶", "松弛", "散步", "咖啡", "citywalk", "Citywalk"],
    "拍照": ["拍照", "摄影", "出片", "打卡", "写真"],
    "美食": ["美食", "小吃", "餐厅", "探店", "咖啡"],
    "自然风光": ["自然", "风光", "海边", "湖", "山", "草原", "日出", "日落"],
    "人文历史": ["人文", "历史", "博物馆", "古城", "寺", "建筑"],
    "夜景": ["夜景", "夜市", "晚上", "酒吧"],
    "早起": ["早起", "日出", "清晨"],
    "特种兵": ["特种兵", "赶路", "高强度", "暴走", "一天很多点"],
}

NEGATIVE_KEYWORDS = ["不想", "不要", "避开", "不太", "不能", "讨厌", "拒绝"]


def _has_valid_llm_config() -> bool:
    return bool(settings.dashscope_api_key and "你的阿里云百炼API Key" not in settings.dashscope_api_key)


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def _chat_json(prompt: str, *, temperature: float = 0.2, timeout: int = 30) -> dict:
    url = settings.dashscope_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.dashscope_model,
        "messages": [
            {"role": "system", "content": "你是旅行搭子推荐系统的一部分，只能输出严格 JSON。"},
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


def normalize_destination(destination: str) -> str:
    value = destination.strip()
    lowered = value.lower()
    for canonical, aliases in DESTINATION_ALIASES.items():
        if any(alias.lower() in lowered or lowered in alias.lower() for alias in aliases):
            return canonical
    return value


def destination_aliases(destination: str) -> list[str]:
    canonical = normalize_destination(destination)
    return DESTINATION_ALIASES.get(canonical, [canonical])


def extract_keywords(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", value.strip().lower())
        if len(token) >= 2
    }


def _keyword_bag(value: str) -> set[str]:
    text = value.lower()
    tokens = extract_keywords(text)
    for keywords in STYLE_KEYWORDS.values():
        for keyword in keywords:
            if keyword.lower() in text:
                tokens.add(keyword.lower())
    for canonical, aliases in DESTINATION_ALIASES.items():
        if canonical.lower() in text:
            tokens.add(canonical.lower())
        for alias in aliases:
            if alias.lower() in text:
                tokens.add(alias.lower())
    return tokens


def _expanded_terms(terms: set[str]) -> set[str]:
    expanded = set(terms)
    for style, keywords in STYLE_KEYWORDS.items():
        style_lower = style.lower()
        keyword_set = {keyword.lower() for keyword in keywords}
        if style_lower in expanded or expanded & keyword_set:
            expanded.add(style_lower)
            expanded.update(keyword_set)
    for canonical, aliases in DESTINATION_ALIASES.items():
        canonical_lower = canonical.lower()
        alias_set = {alias.lower() for alias in aliases}
        if canonical_lower in expanded or expanded & alias_set:
            expanded.add(canonical_lower)
            expanded.update(alias_set)
    return expanded


def _semantic_overlap_score(query_terms: set[str], document_terms: set[str], *, max_score: int) -> tuple[int, list[str]]:
    if not query_terms or not document_terms:
        return 0, []
    expanded_query = _expanded_terms(query_terms)
    expanded_document = _expanded_terms(document_terms)
    matched_terms = sorted(expanded_query & expanded_document)
    if not matched_terms:
        return 0, []
    precision = len(matched_terms) / max(len(expanded_document), 1)
    recall = len(matched_terms) / max(len(expanded_query), 1)
    score = round(max_score * ((precision * 0.35) + (recall * 0.65)))
    return max(0, min(max_score, score)), matched_terms[:12]


def _text_for_content(item: dict) -> str:
    return " ".join(
        str(item.get(key, ""))
        for key in ("title", "summary", "content", "destination", "location")
        if item.get(key)
    )


def build_user_content_evidence(user_id: str) -> dict:
    authored: list[str] = []
    interacted: list[str] = []
    profile = store.get("users", user_id) or {}
    profile_text = " ".join(
        str(profile.get(key, ""))
        for key in ("display_name", "bio", "gender")
        if profile.get(key)
    )

    for collection in ("fe_strategies", "fe_posts"):
        for item in store.list(collection):
            if item.get("status", "published") != "published":
                continue
            if item.get("author", {}).get("id") == user_id:
                authored.append(_text_for_content(item))

    for interaction in store.list("fe_interactions"):
        if interaction.get("userId") != user_id:
            continue
        kind = str(interaction.get("kind", ""))
        target_id = str(interaction.get("targetId", ""))
        if not target_id or not (kind.endswith("_like") or kind.endswith("_favorite")):
            continue
        collection = "fe_posts" if kind.startswith("post_") else "fe_strategies"
        item = store.get(collection, target_id)
        if item and item.get("status", "published") == "published":
            interacted.append(_text_for_content(item))

    text = " ".join([profile_text] + authored + interacted)
    keywords = sorted(_keyword_bag(text))
    profile_keywords = sorted(_keyword_bag(profile_text))
    return {
        "authoredCount": len(authored),
        "interactionCount": len(interacted),
        "profileKeywords": profile_keywords[:20],
        "keywords": keywords[:30],
        "summary": "、".join(keywords[:8]),
    }


def _intent_terms(intent: dict) -> set[str]:
    values: list[str] = []
    for key in ("destinationCanonical", "pace", "socialPreference"):
        if intent.get(key):
            values.append(str(intent[key]))
    for key in ("destinationAliases", "travelStyle", "positivePreferences"):
        values.extend(str(item) for item in intent.get(key, []) if item)
    return _keyword_bag(" ".join(values))


def _negative_terms(intent: dict) -> set[str]:
    return {
        str(item).lower().replace("避免", "").strip()
        for item in intent.get("negativePreferences", [])
        if str(item).strip()
    }


def _keyword_retrieval_features(intent: dict, candidate_text: str, content_evidence: dict | None = None) -> dict:
    query_terms = _intent_terms(intent)
    document_terms = _keyword_bag(candidate_text)
    evidence_terms = set(str(item).lower() for item in (content_evidence or {}).get("keywords", []))
    profile_terms = set(str(item).lower() for item in (content_evidence or {}).get("profileKeywords", []))
    matched_terms = sorted(query_terms & (document_terms | evidence_terms))
    negative_hits = sorted(term for term in _negative_terms(intent) if term and term in candidate_text.lower())
    bm25_score = min(40, len(matched_terms) * 6) - min(24, len(negative_hits) * 8)
    content_score = min(20, len(query_terms & evidence_terms) * 5)
    semantic_score, semantic_terms = _semantic_overlap_score(query_terms, document_terms | evidence_terms, max_score=30)
    profile_score, profile_matched_terms = _semantic_overlap_score(query_terms, profile_terms | evidence_terms, max_score=20)
    return {
        "keywordScore": max(0, bm25_score),
        "contentScore": max(0, content_score),
        "semanticScore": semantic_score,
        "profileScore": profile_score,
        "matchedKeywords": matched_terms[:10],
        "matchedSemanticTerms": semantic_terms[:10],
        "matchedProfileTerms": profile_matched_terms[:10],
        "negativeHits": negative_hits[:5],
    }


def build_query_plan(intent: dict, mode: str, *, candidate_count: int = 0, reflection: dict | None = None) -> dict:
    routes = [
        {
            "route": "rule",
            "purpose": "过滤自己、状态、日期、目的地旅行圈等硬条件",
        },
        {
            "route": "bm25_keyword",
            "purpose": "召回明确命中目的地、标签和偏好关键词的候选",
        },
        {
            "route": "vector_request",
            "purpose": "用本地语义相似度近似请求向量召回，后续可替换为 embedding",
        },
        {
            "route": "user_profile",
            "purpose": "根据用户资料、历史内容和互动形成画像召回信号",
        },
        {
            "route": "content_behavior",
            "purpose": "利用发布、点赞、收藏等内容行为证据增强排序",
        },
    ]
    base = {
        "mode": mode,
        "destinationCanonical": intent.get("destinationCanonical", ""),
        "positivePreferences": intent.get("positivePreferences", []),
        "negativePreferences": intent.get("negativePreferences", []),
        "candidateCount": candidate_count,
        "routes": routes,
    }
    if not _has_valid_llm_config():
        base["source"] = "heuristic"
        return base

    prompt = f"""
请为旅行找搭子推荐生成一个受控检索计划，只输出严格 JSON。

用户意图 JSON：
{json.dumps(intent, ensure_ascii=False)}

当前模式：{mode}
当前候选数：{candidate_count}
候选质量摘要：{json.dumps(reflection or {}, ensure_ascii=False)}

要求：
1. 不要删除硬规则过滤。
2. 普通推荐可以建议一次受控扩召回。
3. 实时匹配不能放宽日期重叠和安全条件。
4. 输出字段必须包含 llmRoutes, shouldRelax, queryRewrite, notes。

JSON 格式：
{{
  "llmRoutes": [
    {{"route": "string", "reason": "string", "enabled": true}}
  ],
  "shouldRelax": true,
  "queryRewrite": {{
    "destination": "string",
    "preferenceText": "string"
  }},
  "notes": ["string"]
}}
"""
    try:
        data = _chat_json(prompt, temperature=0.15, timeout=25)
        base["llmPlan"] = {
            "llmRoutes": data.get("llmRoutes", []),
            "shouldRelax": bool(data.get("shouldRelax", False)),
            "queryRewrite": data.get("queryRewrite", {}),
            "notes": data.get("notes", []),
        }
        base["source"] = "llm"
        return base
    except Exception:
        base["source"] = "heuristic"
        return base


def reflect_candidate_quality(
    candidates: list[dict],
    ranked: list[dict] | None = None,
    *,
    mode: str,
    intent: dict | None = None,
) -> dict:
    ranked_items = ranked or candidates
    scores = [
        int((item.get("ai") or {}).get("score", item.get("features", {}).get("ruleScore", 0)) or 0)
        for item in ranked_items
    ]
    routes = sorted(
        {
            route
            for item in candidates
            for route in item.get("features", {}).get("retrievalRoutes", [])
        }
    )
    candidate_count = len(candidates)
    average_score = round(sum(scores) / len(scores), 2) if scores else 0
    top_score = max(scores) if scores else 0
    min_candidates = 1 if mode == "realtime" else 3
    issues: list[str] = []
    actions: list[str] = []

    if candidate_count == 0:
        issues.append("no_candidates")
        if mode == "recruitment":
            actions.append("expand_date_and_stay_windows_once")
        else:
            actions.append("keep_hard_safety_filters_and_wait_for_more_requests")
    elif candidate_count < min_candidates:
        issues.append("low_candidate_count")
        if mode == "recruitment":
            actions.append("expand_date_and_stay_windows_once")
        else:
            actions.append("do_not_relax_date_overlap_for_realtime_safety")
    if top_score and top_score < 60:
        issues.append("low_top_score")
        actions.append("ask_for_more_preferences_or_wait_for_better_candidates")
    if routes == ["rule"]:
        issues.append("single_route_only")
        actions.append("collect_more_profile_content_or_behavior_evidence")

    base = {
        "candidateCount": candidate_count,
        "averageScore": average_score,
        "topScore": top_score,
        "routesCovered": routes,
        "quality": "good" if not issues else "needs_attention",
        "issues": issues,
        "suggestedActions": actions,
    }
    if not _has_valid_llm_config() or not candidates:
        base["source"] = "heuristic"
        return base

    prompt = f"""
请根据找搭子候选和统计结果给出检索质量反思，只输出严格 JSON。

用户意图：
{json.dumps(intent or {}, ensure_ascii=False)}

统计摘要：
{json.dumps(base, ensure_ascii=False)}

候选证据摘要：
{json.dumps([
    {
        "id": item["id"],
        "score": int((item.get("ai") or {}).get("score", item.get("features", {}).get("ruleScore", 0)) or 0),
        "routes": item.get("features", {}).get("retrievalRoutes", []),
        "keywords": item.get("features", {}).get("matchedKeywords", []),
        "semantic": item.get("features", {}).get("matchedSemanticTerms", []),
        "profile": item.get("features", {}).get("matchedProfileTerms", []),
    }
    for item in candidates[:5]
], ensure_ascii=False)}

请输出：
{{
  "summary": "string",
  "shouldExpand": true,
  "shouldWait": false,
  "diagnosis": "string",
  "actions": ["string"]
}}
"""
    try:
        data = _chat_json(prompt, temperature=0.15, timeout=25)
        base["llmReflection"] = {
            "summary": str(data.get("summary", "")).strip(),
            "shouldExpand": bool(data.get("shouldExpand", False)),
            "shouldWait": bool(data.get("shouldWait", False)),
            "diagnosis": str(data.get("diagnosis", "")).strip(),
            "actions": data.get("actions", []),
        }
        base["source"] = "llm"
        return base
    except Exception:
        base["source"] = "heuristic"
        return base


def _infer_styles(tags: list[str], preference_text: str) -> tuple[list[str], list[str]]:
    text = preference_text.strip()
    styles: list[str] = []
    negative: list[str] = []
    for tag in tags:
        if tag and tag not in styles:
            styles.append(tag)

    for style, keywords in STYLE_KEYWORDS.items():
        if any(keyword.lower() in text.lower() for keyword in keywords):
            target = negative if style == "特种兵" and any(flag in text for flag in NEGATIVE_KEYWORDS) else styles
            if style not in target:
                target.append(style)

    for keyword in STYLE_KEYWORDS["特种兵"]:
        if keyword in text and any(flag in text for flag in NEGATIVE_KEYWORDS):
            label = f"避免{keyword}"
            if label not in negative:
                negative.append(label)
    return styles, negative


def build_match_intent(destination: str, preference_tags: list[str] | None = None, preference_text: str = "") -> dict:
    tags = [tag.strip() for tag in preference_tags or [] if tag.strip()]
    text = preference_text.strip()
    fallback_styles, fallback_negative = _infer_styles(tags, text)
    fallback = {
        "destinationCanonical": normalize_destination(destination),
        "destinationAliases": destination_aliases(destination),
        "travelStyle": fallback_styles,
        "pace": "relaxed" if "慢旅行" in fallback_styles else "unknown",
        "budgetLevel": "unknown",
        "positivePreferences": sorted(extract_keywords(" ".join(tags) + " " + text)),
        "negativePreferences": fallback_negative,
        "socialPreference": "unknown",
        "safetyPreferences": ["公共场所", "白天见面"],
        "source": "heuristic",
    }
    if not _has_valid_llm_config():
        return fallback

    prompt = f"""
请把用户找旅行搭子的需求解析成结构化 JSON。

目的地：{destination}
偏好标签：{"、".join(tags) or "无"}
补充偏好：{text or "无"}

只输出 JSON，字段如下：
{{
  "destinationCanonical": "归一后的目的地，例如大理",
  "destinationAliases": ["同一旅行圈的地点别名"],
  "travelStyle": ["慢旅行/拍照/美食/自然风光/人文历史/早起/夜景/特种兵等"],
  "pace": "relaxed | normal | intense | unknown",
  "budgetLevel": "low | medium | high | unknown",
  "positivePreferences": ["用户喜欢的点"],
  "negativePreferences": ["用户明确不想要的点"],
  "socialPreference": "同行偏好，未知则 unknown",
  "safetyPreferences": ["安全偏好"]
}}
"""
    try:
        data = _chat_json(prompt, temperature=0.1, timeout=25)
        result = {**fallback, **{key: value for key, value in data.items() if value not in (None, "", [])}}
        result["destinationCanonical"] = normalize_destination(str(result.get("destinationCanonical") or destination))
        result["destinationAliases"] = result.get("destinationAliases") or destination_aliases(destination)
        result["source"] = "llm"
        return result
    except Exception:
        return fallback


def _parse_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _date_range_overlap_days(start_a: str | date, end_a: str | date, start_b: str | date, end_b: str | date) -> int:
    latest_start = max(_parse_date(start_a), _parse_date(start_b))
    earliest_end = min(_parse_date(end_a), _parse_date(end_b))
    if latest_start > earliest_end:
        return 0
    return (earliest_end - latest_start).days + 1


def _candidate_text(candidate: dict, mode: str) -> str:
    raw = candidate["raw"]
    features = candidate.get("features", {})
    content = features.get("contentEvidence") or {}
    content_summary = content.get("summary") or "无"
    if mode == "realtime":
        return (
            f"候选ID：{candidate['id']}\n"
            f"目的地：{raw.get('destination', '')}\n"
            f"日期：{raw.get('travel_start_date', '')} 到 {raw.get('travel_end_date', '')}\n"
            f"偏好标签：{'、'.join(raw.get('preference_tags', [])) or '无'}\n"
            f"补充偏好：{raw.get('preference_text', '') or '无'}\n"
            f"内容/行为证据关键词：{content_summary}\n"
            f"共同标签数：{features.get('sharedTags', 0)}；共同关键词数：{features.get('sharedKeywords', 0)}；重叠天数：{features.get('overlapDays', 0)}"
        )
    return (
        f"候选ID：{candidate['id']}\n"
        f"目的地：{raw.get('destination', '')}\n"
        f"开始日期：{raw.get('startDate', '')}\n"
        f"天数：{raw.get('days', 0)}\n"
        f"发布者：{raw.get('publisherNickname', '')}\n"
        f"招募描述：{raw.get('preferenceText', '') or raw.get('remarks', '') or '无'}\n"
        f"内容/行为证据关键词：{content_summary}\n"
        f"日期差：{features.get('startGap', 0)}；天数差：{features.get('stayGap', 0)}"
    )


def build_realtime_candidate(request: dict, peer_request: dict, intent: dict | None = None) -> dict:
    current_tags = set(request.get("preference_tags", []))
    peer_tags = set(peer_request.get("preference_tags", []))
    current_keywords = extract_keywords(request.get("preference_text", ""))
    peer_keywords = extract_keywords(peer_request.get("preference_text", ""))
    content_evidence = build_user_content_evidence(peer_request.get("user_id", ""))
    keyword_features = _keyword_retrieval_features(
        intent or {},
        " ".join(
            [
                peer_request.get("destination", ""),
                " ".join(peer_request.get("preference_tags", [])),
                peer_request.get("preference_text", ""),
            ]
        ),
        content_evidence,
    )
    overlap_days = _date_range_overlap_days(
        request["travel_start_date"],
        request["travel_end_date"],
        peer_request["travel_start_date"],
        peer_request["travel_end_date"],
    )
    shared_tags = len(current_tags & peer_tags)
    shared_keywords = len(current_keywords & peer_keywords)
    score = min(
        100,
        overlap_days * 12
        + shared_tags * 18
        + shared_keywords * 8
        + keyword_features["keywordScore"]
        + keyword_features["contentScore"]
        + keyword_features["semanticScore"]
        + keyword_features["profileScore"],
    )
    routes = ["rule"]
    if keyword_features["keywordScore"]:
        routes.append("bm25_keyword")
    if keyword_features["semanticScore"]:
        routes.append("vector_request")
    if keyword_features["profileScore"]:
        routes.append("user_profile")
    if keyword_features["contentScore"]:
        routes.append("content_behavior")
    return {
        "id": peer_request["id"],
        "raw": peer_request,
        "features": {
            "overlapDays": overlap_days,
            "sharedTags": shared_tags,
            "sharedKeywords": shared_keywords,
            "retrievalRoutes": routes,
            "contentEvidence": content_evidence,
            **keyword_features,
            "ruleScore": score,
        },
    }


def build_recruitment_candidate(
    recruitment: dict,
    *,
    start_gap: int,
    stay_gap: int,
    application_status: str,
    intent: dict | None = None,
) -> dict:
    content_evidence = build_user_content_evidence(recruitment.get("publisherUserId", ""))
    keyword_features = _keyword_retrieval_features(
        intent or {},
        " ".join(
            [
                recruitment.get("destination", ""),
                recruitment.get("preferenceText", ""),
                recruitment.get("remarks", ""),
            ]
        ),
        content_evidence,
    )
    score = max(
        0,
        min(
            100,
            100
            - start_gap * 18
            - stay_gap * 12
            + keyword_features["keywordScore"]
            + keyword_features["contentScore"]
            + keyword_features["semanticScore"]
            + keyword_features["profileScore"],
        ),
    )
    routes = ["rule"]
    if keyword_features["keywordScore"]:
        routes.append("bm25_keyword")
    if keyword_features["semanticScore"]:
        routes.append("vector_request")
    if keyword_features["profileScore"]:
        routes.append("user_profile")
    if keyword_features["contentScore"]:
        routes.append("content_behavior")
    return {
        "id": recruitment["id"],
        "raw": recruitment,
        "features": {
            "startGap": start_gap,
            "stayGap": stay_gap,
            "applicationStatus": application_status,
            "retrievalRoutes": routes,
            "contentEvidence": content_evidence,
            **keyword_features,
            "ruleScore": score,
        },
    }


def _reward_from_feedback(item: dict) -> float:
    if item.get("reward") is not None:
        try:
            return float(item.get("reward", 0))
        except (TypeError, ValueError):
            return 0.0
    rating = item.get("rating")
    if rating is None:
        rating = (item.get("extra") or {}).get("rating")
    if rating is not None:
        try:
            return (float(rating) - 3.0) / 2.0
        except (TypeError, ValueError):
            return 0.0
    action = str(item.get("action", "")).lower()
    if action == "accept":
        return 0.35
    if action in {"reject", "block", "report"}:
        return -0.75
    return 0.0


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _candidate_peer_user_id(candidate: dict, mode: str) -> str:
    raw = candidate.get("raw", {})
    if mode == "realtime":
        return str(raw.get("user_id", ""))
    return str(raw.get("publisherUserId", ""))


def _candidate_terms(candidate: dict, mode: str) -> set[str]:
    raw = candidate.get("raw", {})
    parts: list[str] = [str(raw.get("destination", ""))]
    if mode == "realtime":
        parts.extend(str(item) for item in raw.get("preference_tags", []))
        parts.extend(extract_keywords(raw.get("preference_text", "")))
    else:
        parts.extend(extract_keywords(raw.get("preferenceText", "")))
        parts.extend(extract_keywords(raw.get("remarks", "")))
    return {item.strip().lower() for item in parts if item and item.strip()}


def _build_feedback_profile(user_id: str, mode: str) -> dict[str, Any]:
    profile: dict[str, Any] = {"peer": {}, "destination": {}, "term": {}, "samples": 0}
    if not user_id:
        return profile
    for item in store.list(AI_MATCH_FEEDBACK_COLLECTION):
        if item.get("userId") != user_id:
            continue
        if mode and item.get("mode") not in {"", mode}:
            continue
        reward = _reward_from_feedback(item)
        if reward == 0:
            continue
        extra = item.get("extra") or {}
        profile["samples"] += 1
        peer_user_id = str(extra.get("peerUserId", ""))
        if peer_user_id:
            profile["peer"].setdefault(peer_user_id, []).append(reward)
        destination = str(extra.get("destination", "")).strip().lower()
        if destination:
            profile["destination"].setdefault(destination, []).append(reward)
        for term in extra.get("preferenceTags", []) or []:
            term = str(term).strip().lower()
            if term:
                profile["term"].setdefault(term, []).append(reward)
        for term in extract_keywords(str(extra.get("preferenceText", ""))):
            term = str(term).strip().lower()
            if term:
                profile["term"].setdefault(term, []).append(reward)
    return profile


def _feedback_bonus(candidate: dict, mode: str, profile: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    if not profile or not profile.get("samples"):
        return 0, {"feedbackReward": 0, "feedbackSamples": 0}
    values: list[float] = []
    peer_user_id = _candidate_peer_user_id(candidate, mode)
    if peer_user_id in profile["peer"]:
        values.extend(float(item) * 1.2 for item in profile["peer"][peer_user_id])
    raw = candidate.get("raw", {})
    destination = str(raw.get("destination", "")).strip().lower()
    if destination in profile["destination"]:
        values.extend(float(item) * 0.7 for item in profile["destination"][destination])
    for term in _candidate_terms(candidate, mode):
        if term in profile["term"]:
            values.extend(float(item) * 0.45 for item in profile["term"][term])
    if not values:
        return 0, {"feedbackReward": 0, "feedbackSamples": profile.get("samples", 0)}
    reward = max(-1.0, min(1.0, _avg(values)))
    return int(round(reward * 12)), {
        "feedbackReward": round(reward, 3),
        "feedbackSamples": profile.get("samples", 0),
    }


def _fallback_rank(
    intent: dict,
    candidates: list[dict],
    mode: str,
    *,
    feedback_profile: dict[str, Any] | None = None,
) -> list[dict]:
    ranked: list[dict] = []
    positive = set(str(item).lower() for item in intent.get("positivePreferences", []))
    negative = [str(item).lower() for item in intent.get("negativePreferences", [])]
    for candidate in candidates:
        text = _candidate_text(candidate, mode).lower()
        bonus = sum(4 for item in positive if item and item in text)
        penalty = sum(8 for item in negative if item and item.replace("避免", "") in text)
        feedback_delta, feedback_features = _feedback_bonus(candidate, mode, feedback_profile)
        features = {**candidate.get("features", {}), **feedback_features, "feedbackBonus": feedback_delta}
        score = max(0, min(100, int(features.get("ruleScore", 0)) + bonus - penalty + feedback_delta))
        reason = _build_default_reason(candidate, mode)
        risk = _build_default_risk(candidate, mode)
        ranked.append({**candidate, "features": features, "ai": {"score": score, "reason": reason, "risk": risk, "source": "heuristic"}})
    ranked.sort(key=lambda item: item["ai"]["score"], reverse=True)
    return ranked


def _build_default_reason(candidate: dict, mode: str) -> str:
    features = candidate.get("features", {})
    matched = features.get("matchedKeywords") or []
    semantic = features.get("matchedSemanticTerms") or []
    profile_terms = features.get("matchedProfileTerms") or []
    content = features.get("contentEvidence") or {}
    if matched:
        return f"候选信息命中“{matched[0]}”等偏好关键词，行程条件也满足基础匹配。"
    if semantic:
        return f"候选请求和你的需求在“{semantic[0]}”等语义偏好上相近，适合优先了解。"
    if profile_terms:
        return f"对方画像和历史兴趣命中“{profile_terms[0]}”等偏好，和你的旅行方式较接近。"
    if content.get("summary"):
        return f"对方历史内容或互动体现了“{content['summary']}”等兴趣，和你的需求有相似点。"
    if mode == "realtime":
        if features.get("sharedTags"):
            return f"你们有 {features['sharedTags']} 个共同旅行偏好，日期重叠 {features.get('overlapDays', 0)} 天。"
        if features.get("sharedKeywords"):
            return f"你们的补充偏好有相似关键词，日期重叠 {features.get('overlapDays', 0)} 天。"
        return f"你们的目的地和日期匹配，日期重叠 {features.get('overlapDays', 0)} 天。"
    return f"目的地匹配，出发日期相差 {features.get('startGap', 0)} 天，旅行天数相差 {features.get('stayGap', 0)} 天。"


def _build_default_risk(candidate: dict, mode: str) -> str:
    features = candidate.get("features", {})
    if features.get("negativeHits"):
        return f"对方信息中出现了你可能不喜欢的关键词：{features['negativeHits'][0]}，建议先沟通确认。"
    if mode == "realtime":
        if not features.get("sharedTags") and not features.get("sharedKeywords"):
            return "偏好证据较少，建议先确认旅行节奏。"
        return "初次见面建议选择白天公共地点。"
    if features.get("startGap", 0) or features.get("stayGap", 0):
        return "行程日期或天数存在差异，申请前建议先沟通确认。"
    return "初次沟通请注意保护个人隐私。"


def _llm_rank(intent: dict, candidates: list[dict], mode: str) -> dict[str, dict] | None:
    if not _has_valid_llm_config() or not candidates:
        return None
    evidence = "\n\n".join(_candidate_text(candidate, mode) for candidate in candidates[:MAX_LLM_RERANK_CANDIDATES])
    prompt = f"""
你要为旅行找搭子系统做候选重排。只能从给定候选ID中选择，不能编造新候选。

用户意图 JSON：
{json.dumps(intent, ensure_ascii=False)}

候选证据：
{evidence}

请输出严格 JSON：
{{
  "ranked": [
    {{
      "id": "候选ID",
      "score": 0到100的整数,
      "reason": "一句推荐理由",
      "risk": "一句风险提醒"
    }}
  ]
}}

要求：
1. 分数只根据目的地、日期、偏好、补充说明和证据判断。
2. 证据不足要降分。
3. 不要使用性别、年龄等敏感属性做偏见排序。
4. 不要输出候选证据之外的事实。
"""
    try:
        data = _chat_json(prompt, temperature=0.2, timeout=35)
        ranked = data.get("ranked", [])
        result: dict[str, dict] = {}
        valid_ids = {candidate["id"] for candidate in candidates}
        for item in ranked:
            item_id = str(item.get("id", ""))
            if item_id not in valid_ids:
                continue
            score = int(float(item.get("score", 0)))
            result[item_id] = {
                "score": max(0, min(100, score)),
                "reason": str(item.get("reason", "")).strip(),
                "risk": str(item.get("risk", "")).strip(),
                "source": "llm",
            }
        return result or None
    except Exception:
        return None


def rank_candidates(intent: dict, candidates: list[dict], *, mode: str, user_id: str = "") -> tuple[list[dict], dict]:
    feedback_profile = _build_feedback_profile(user_id, mode)
    fallback_ranked = _fallback_rank(intent, candidates, mode, feedback_profile=feedback_profile)
    llm_scores = _llm_rank(intent, fallback_ranked, mode)
    if not llm_scores:
        return fallback_ranked, {
            "rankingSource": "heuristic",
            "candidateCount": len(candidates),
            "feedbackSamples": feedback_profile.get("samples", 0),
        }

    merged: list[dict] = []
    fallback_by_id = {candidate["id"]: candidate for candidate in fallback_ranked}
    for item_id, candidate in fallback_by_id.items():
        llm = llm_scores.get(item_id)
        if not llm:
            merged.append(candidate)
            continue
        rule_score = int(candidate.get("features", {}).get("ruleScore", 0))
        feedback_delta = int(candidate.get("features", {}).get("feedbackBonus", 0))
        final_score = max(0, min(100, round(rule_score * 0.35 + int(llm["score"]) * 0.65 + feedback_delta)))
        merged.append(
            {
                **candidate,
                "ai": {
                    "score": final_score,
                    "llmScore": llm["score"],
                    "reason": llm["reason"] or candidate["ai"]["reason"],
                    "risk": llm["risk"] or candidate["ai"]["risk"],
                    "source": "llm",
                },
            }
        )
    merged.sort(key=lambda item: item["ai"]["score"], reverse=True)
    return merged, {
        "rankingSource": "llm",
        "candidateCount": len(candidates),
        "feedbackSamples": feedback_profile.get("samples", 0),
    }


def save_match_trace(
    *,
    mode: str,
    user_id: str,
    intent: dict,
    candidates: list[dict],
    ranked: list[dict],
    meta: dict | None = None,
) -> str:
    trace = store.create(
        AI_MATCH_TRACES_COLLECTION,
        {
            "mode": mode,
            "userId": user_id,
            "intent": intent,
            "candidateCount": len(candidates),
            "ranked": [
                {
                    "id": item["id"],
                    "score": item.get("ai", {}).get("score", 0),
                    "reason": item.get("ai", {}).get("reason", ""),
                    "risk": item.get("ai", {}).get("risk", ""),
                    "routes": item.get("features", {}).get("retrievalRoutes", []),
                    "matchedKeywords": item.get("features", {}).get("matchedKeywords", []),
                    "matchedSemanticTerms": item.get("features", {}).get("matchedSemanticTerms", []),
                    "matchedProfileTerms": item.get("features", {}).get("matchedProfileTerms", []),
                    "features": item.get("features", {}),
                }
                for item in ranked[:MAX_LLM_RERANK_CANDIDATES]
            ],
            "meta": meta or {},
            "createdAt": now_utc().isoformat(),
        },
    )
    return trace["id"]


def get_match_trace(trace_id: str) -> dict | None:
    return store.get(AI_MATCH_TRACES_COLLECTION, trace_id)


def public_ai_match(item: dict, trace_id: str = "") -> dict[str, Any]:
    ai = item.get("ai", {})
    features = item.get("features", {})
    return {
        "aiMatchScore": ai.get("score", 0),
        "aiMatchReason": ai.get("reason", ""),
        "aiMatchRisk": ai.get("risk", ""),
        "aiMatchSource": ai.get("source", "heuristic"),
        "aiMatchTraceId": trace_id,
        "aiMatchRoutes": features.get("retrievalRoutes", []),
        "aiMatchedKeywords": features.get("matchedKeywords", []),
        "aiMatchedSemanticTerms": features.get("matchedSemanticTerms", []),
        "aiMatchedProfileTerms": features.get("matchedProfileTerms", []),
    }


def save_match_feedback(
    *,
    user_id: str,
    action: str,
    target_id: str = "",
    trace_id: str = "",
    mode: str = "",
    extra: dict | None = None,
    rating: int | None = None,
    reward: float | None = None,
) -> dict:
    payload = {
        "userId": user_id,
        "action": action,
        "targetId": target_id,
        "traceId": trace_id,
        "mode": mode,
        "extra": extra or {},
        "createdAt": now_utc().isoformat(),
    }
    if rating is not None:
        payload["rating"] = rating
    if reward is not None:
        payload["reward"] = reward
    return store.create(AI_MATCH_FEEDBACK_COLLECTION, payload)
