from __future__ import annotations

from datetime import timedelta

from app.models.common import now_utc
from app.services.auth_service import upsert_user
from app.services.store import store


DEMO_USER_ID = "demo-seed-author"
DEMO_AVATAR = "https://images.unsplash.com/photo-1494790108377-be9c29b29330?auto=format&fit=crop&q=80&w=400"


def _author() -> dict:
    return {
        "id": DEMO_USER_ID,
        "nickname": "远方旅行编辑",
        "avatar": DEMO_AVATAR,
    }


def _iso(days_ago: int) -> str:
    return (now_utc() - timedelta(days=days_ago)).isoformat()


def seed_demo_data_if_empty() -> None:
    """Populate a fresh deployment with a small, non-destructive demo feed."""
    if store.list("fe_strategies") or store.list("fe_posts"):
        return

    upsert_user(
        uid=DEMO_USER_ID,
        display_name="远方旅行编辑",
        photo_url=DEMO_AVATAR,
        bio="为演示准备的旅行内容账号",
    )
    author = _author()

    strategies = [
        {
            "id": "demo-strategy-dali",
            "title": "大理 3 天慢旅行路线",
            "summary": "从洱海骑行、古城夜游到苍山轻徒步，适合第一次去大理的轻松行程。",
            "content": "Day 1: 抵达大理古城 / 人民路散步 / 洱海边看日落\nDay 2: 喜洲古镇 / 海舌公园 / 周城扎染体验\nDay 3: 苍山索道 / 寂照庵 / 返程",
            "destination": "大理",
            "days": 3,
            "coverUrl": "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&q=80&w=1600",
            "imageList": [],
            "tags": ["慢旅行", "自然风光", "拍照"],
            "status": "published",
            "author": author,
            "likeCount": 128,
            "favoriteCount": 76,
            "viewCount": 3200,
            "commentCount": 0,
            "shareCount": 18,
            "createdAt": _iso(1),
            "updatedAt": _iso(1),
        },
        {
            "id": "demo-strategy-kyoto",
            "title": "京都寺社与小巷散步攻略",
            "summary": "避开拥挤路线，用两天时间走完清水寺、鸭川、岚山和几条安静小路。",
            "content": "Day 1: 清水寺 / 二年坂三年坂 / 鸭川散步\nDay 2: 岚山竹林 / 渡月桥 / 嵯峨野小火车",
            "destination": "京都",
            "days": 2,
            "coverUrl": "https://images.unsplash.com/photo-1493976040374-85c8e12f0c0e?auto=format&fit=crop&q=80&w=1600",
            "imageList": [],
            "tags": ["人文历史", "拍照", "慢旅行"],
            "status": "published",
            "author": author,
            "likeCount": 96,
            "favoriteCount": 64,
            "viewCount": 2410,
            "commentCount": 0,
            "shareCount": 12,
            "createdAt": _iso(2),
            "updatedAt": _iso(2),
        },
        {
            "id": "demo-strategy-iceland",
            "title": "冰岛南岸自驾 5 天",
            "summary": "瀑布、黑沙滩、冰川湖和极光观测点，适合风景向自驾演示。",
            "content": "Day 1: 雷克雅未克补给\nDay 2: 黄金圈\nDay 3: 塞里雅兰瀑布 / 斯科加瀑布\nDay 4: 黑沙滩 / 维克\nDay 5: 冰川湖返程",
            "destination": "冰岛",
            "days": 5,
            "coverUrl": "https://images.unsplash.com/photo-1500534314209-a25ddb2bd429?auto=format&fit=crop&q=80&w=1600",
            "imageList": [],
            "tags": ["自然风光", "特种兵", "夜景"],
            "status": "published",
            "author": author,
            "likeCount": 211,
            "favoriteCount": 143,
            "viewCount": 5800,
            "commentCount": 0,
            "shareCount": 31,
            "createdAt": _iso(3),
            "updatedAt": _iso(3),
        },
    ]

    posts = [
        {
            "id": "demo-post-yunnan",
            "type": "vlog",
            "title": "洱海边的傍晚风很舒服",
            "location": "大理",
            "content": "骑到海边时刚好赶上日落，适合放在首页和 Vlog 流里展示。",
            "tags": ["大理", "日落", "慢旅行"],
            "coverUrl": "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&q=80&w=1600",
            "mediaList": [
                {
                    "type": "image",
                    "url": "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&q=80&w=1600",
                }
            ],
            "status": "published",
            "author": author,
            "likeCount": 342,
            "favoriteCount": 88,
            "commentCount": 0,
            "shareCount": 24,
            "createdAt": _iso(1),
            "updatedAt": _iso(1),
        },
        {
            "id": "demo-post-forest",
            "type": "vlog",
            "title": "山林徒步的一分钟记录",
            "location": "张家界",
            "content": "森林、栈道和云雾，适合演示短内容浏览体验。",
            "tags": ["徒步", "自然风光"],
            "coverUrl": "https://images.unsplash.com/photo-1501785888041-af3ef285b470?auto=format&fit=crop&q=80&w=1600",
            "mediaList": [
                {
                    "type": "image",
                    "url": "https://images.unsplash.com/photo-1501785888041-af3ef285b470?auto=format&fit=crop&q=80&w=1600",
                }
            ],
            "status": "published",
            "author": author,
            "likeCount": 189,
            "favoriteCount": 55,
            "commentCount": 0,
            "shareCount": 11,
            "createdAt": _iso(2),
            "updatedAt": _iso(2),
        },
    ]

    for item in strategies:
        store.upsert("fe_strategies", item["id"], item)
    for item in posts:
        store.upsert("fe_posts", item["id"], item)
