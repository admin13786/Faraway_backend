from __future__ import annotations

import json
import re

import httpx
from fastapi import HTTPException, status

from app.core.config import settings
from app.models.ai import DailyPlan, GenerateStrategyRequest, GeneratedStrategy


def _fallback_strategy(req: GenerateStrategyRequest) -> GeneratedStrategy:
    daily_plans = [
        DailyPlan(
            day=day,
            activities=[
                f"{req.destination}核心景点探索",
                "当地街区漫步和拍照",
                "根据体力安排自由活动",
            ],
            food=["当地特色小吃", "推荐餐厅打卡"],
            accommodation=req.hotel_req or "交通便利区域酒店",
        )
        for day in range(1, req.days + 1)
    ]
    return GeneratedStrategy(
        destination=req.destination,
        overview=f"这是一份为{req.destination}定制的{req.days}天旅行计划，预算为{req.budget}，节奏{req.pace}。",
        daily_plans=daily_plans,
        tips=["提前确认天气和交通", "重要证件和药品随身携带", "热门景点建议提前预约"],
    )


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def generate_safe_meeting_place(destination: str, preference_tags: list[str] | None = None, preference_text: str = "") -> str:
    destination = destination.strip() or "目的地"
    fallback = f"{destination}主城区地标广场附近"
    if not settings.dashscope_api_key or "你的阿里云百炼API Key" in settings.dashscope_api_key:
        return fallback

    tags_text = "、".join(tag.strip() for tag in preference_tags or [] if tag.strip()) or "无"
    extra_text = preference_text.strip() or "无"
    prompt = f"""
请为两位第一次见面的旅行搭子推荐一个安全见面地点。

要求：
1. 地点在 {destination}
2. 必须适合白天初次见面
3. 必须是公共地点
4. 必须交通方便、容易辨认、人流较多
5. 避免偏僻、封闭、夜间、私人住宅场景

用户偏好标签：{tags_text}
补充偏好：{extra_text}

只返回一句简体中文地点文本，不要解释，不要 Markdown，不要序号。
"""
    url = settings.dashscope_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.dashscope_model,
        "messages": [
            {"role": "system", "content": "你是旅行安全助手，回答必须简洁且可直接展示给用户。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
    }
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.dashscope_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        content = content.splitlines()[0].strip(" -：:")
        return content or fallback
    except Exception:
        return fallback


def generate_strategy(req: GenerateStrategyRequest) -> GeneratedStrategy:
    if not settings.dashscope_api_key or "你的阿里云百炼API Key" in settings.dashscope_api_key:
        return _fallback_strategy(req)

    prompt = f"""
请用简体中文生成旅行攻略，只返回严格 JSON，不要输出 Markdown。

目的地：{req.destination}
天数：{req.days}
预算：{req.budget}
酒店要求：{req.hotel_req}
过敏/忌口：{req.allergies}
旅行节奏：{req.pace}
出行方式：{req.group_type}

JSON 字段：
{{
  "destination": "string",
  "overview": "string",
  "daily_plans": [
    {{
      "day": 1,
      "activities": ["string"],
      "food": ["string"],
      "accommodation": "string"
    }}
  ],
  "tips": ["string"]
}}
"""
    url = settings.dashscope_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.dashscope_model,
        "messages": [
            {"role": "system", "content": "你是专业旅行规划师，输出必须是可解析 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }
    try:
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.dashscope_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        data = _extract_json(content)
        if "dailyPlans" in data and "daily_plans" not in data:
            data["daily_plans"] = data.pop("dailyPlans")
        return GeneratedStrategy.model_validate(data)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Aliyun DashScope request failed: {exc}",
        ) from exc
