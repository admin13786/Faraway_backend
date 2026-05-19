from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from random import Random
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.auth_service import (  # noqa: E402
    AUTH_ACCOUNTS_COLLECTION,
    AUTH_IDENTITIES_COLLECTION,
    _hash_password,
)
from app.services.store import store  # noqa: E402


PASSWORD = "Demo123456"
RANDOM = Random(20260516)
TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)

DESTINATIONS = [
    {
        "name": "北京",
        "aliases": ["故宫", "三里屯", "什刹海"],
        "themes": ["人文历史", "美食", "城市漫步"],
        "avoid": ["赶路", "排队过久"],
        "image": "/static/cover/city-beijing.jpg",
    },
    {
        "name": "上海",
        "aliases": ["外滩", "武康路", "陆家嘴"],
        "themes": ["城市漫步", "拍照", "美食"],
        "avoid": ["行程过满", "太晚见面"],
        "image": "/static/cover/city-shanghai.jpg",
    },
    {
        "name": "广州",
        "aliases": ["珠江新城", "永庆坊", "沙面"],
        "themes": ["美食", "人文历史", "夜景"],
        "avoid": ["重体力徒步", "太赶"],
        "image": "/static/cover/city-guangzhou.jpg",
    },
    {
        "name": "深圳",
        "aliases": ["南山", "福田", "深圳湾"],
        "themes": ["城市漫步", "自然风光", "拍照"],
        "avoid": ["封闭小众地点", "临时变更"],
        "image": "/static/cover/city-shenzhen.jpg",
    },
]

NAMES = [
    "林夏", "许知远", "阿南", "小周", "Mia", "Leo", "橙子", "阿澈", "Nora", "沈一",
    "周末旅行家", "海边来信", "风景收藏夹", "城市散步者", "山野笔记", "慢慢走", "鹿岛", "白茶", "青木", "南风",
]

COMMENTS = [
    "这个路线很适合第一次去，收藏了。",
    "照片氛围太好了，想照着走一遍。",
    "预算和交通写得很清楚，演示很完整。",
    "这个地方我也去过，确实适合慢旅行。",
    "如果是周末出发，这个安排刚刚好。",
]


def iso_days_ago(days: int, hours: int = 0) -> str:
    return (NOW - timedelta(days=days, hours=hours)).isoformat()


def future_date(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def author_for(user: dict) -> dict:
    return {
        "id": user["uid"],
        "nickname": user["display_name"],
        "avatar": user.get("photo_url", ""),
    }


def identity_key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def upsert_showcase_account(index: int, destination: dict, like_same_place: bool) -> dict:
    uid = f"showcase-user-{index:03d}"
    username = f"showcase{index:03d}"
    phone = f"1880001{index:04d}"
    name = f"{NAMES[(index - 1) % len(NAMES)]}{index:02d}"
    favorite = destination["name"]
    dislike_destination = DESTINATIONS[(index + 3) % len(DESTINATIONS)]["name"]
    themes = destination["themes"]
    avoid = destination["avoid"]
    bio = (
        f"喜欢{favorite}、{themes[0]}、{themes[1]}，偏好真实旅行记录和安全搭子。"
        f"不喜欢{dislike_destination}的快节奏玩法，也不喜欢{avoid[0]}、{avoid[1]}。"
        if like_same_place
        else f"最近想避开{favorite}，更想找小众城市散步、安静咖啡馆和轻松行程。"
    )
    now = NOW.isoformat()
    avatar_seed = 100 + index
    avatar = f"https://i.pravatar.cc/160?img={(avatar_seed % 70) + 1}"
    user = {
        "uid": uid,
        "display_name": name,
        "email": f"{username}@faraway.demo",
        "photo_url": avatar,
        "bio": bio,
        "gender": "female" if index % 3 == 0 else ("male" if index % 3 == 1 else "unknown"),
        "created_at": iso_days_ago(40 - index % 30),
        "updated_at": now,
    }
    store.upsert("users", uid, user)
    account = {
        "uid": uid,
        "username": username,
        "username_key": username,
        "phone": phone,
        "email": user["email"],
        "display_name": name,
        "password_hash": _hash_password(PASSWORD),
        "login_types": ["password", "phone"],
        "created_at": user["created_at"],
        "updated_at": now,
        "status": "active",
    }
    store.upsert(AUTH_ACCOUNTS_COLLECTION, uid, account)
    store.upsert(AUTH_IDENTITIES_COLLECTION, identity_key("username", username), {"uid": uid, "kind": "username", "value": username})
    store.upsert(AUTH_IDENTITIES_COLLECTION, identity_key("phone", phone), {"uid": uid, "kind": "phone", "value": phone})
    return user


def content_profile(target_type: str, item: dict, destination: dict, themes: list[str]) -> dict:
    source_updated = item.get("updatedAt") or item.get("createdAt") or ""
    return {
        "id": f"{target_type}:{item['id']}",
        "targetType": target_type,
        "targetId": item["id"],
        "destinations": [destination["name"], *destination["aliases"][:2]],
        "themes": list(dict.fromkeys([*themes, *destination["themes"], "小红书旅行", "真实体验"]))[:12],
        "travelStyle": "relaxed" if "慢旅行" in themes else ("outdoor" if "徒步" in themes or "自驾" in themes else "general"),
        "budgetLevel": "medium",
        "suitableUsers": ["第一次去的用户", "喜欢真实笔记的用户", "找搭子用户"],
        "titleScore": 0.82,
        "contentQualityScore": 0.84,
        "usefulnessScore": 0.86,
        "authenticityScore": 0.8,
        "commercialRiskScore": 0.03,
        "clickbaitRiskScore": 0.04,
        "overallAiQualityScore": 0.84,
        "summary": f"{destination['name']} / {' / '.join(themes[:3])}",
        "analysisMode": "seeded_showcase",
        "model": "seeded-profile-v1",
        "sourceUpdatedAt": source_updated,
        "updatedAt": NOW.isoformat(),
    }


def seed_posts(users: list[dict]) -> list[dict]:
    posts: list[dict] = []
    for i in range(1, 81):
        destination = DESTINATIONS[(i - 1) % len(DESTINATIONS)]
        author = users[(i * 7) % len(users)]
        themes = list(dict.fromkeys([destination["themes"][i % 3], destination["themes"][(i + 1) % 3], "小红书旅行"]))
        title_patterns = [
            f"{destination['name']}这条路线真的适合第一次去",
            f"{destination['aliases'][0]}半日散步，比热门景点舒服",
            f"在{destination['name']}住了两晚，整理一份真实避坑",
            f"{destination['name']}拍照机位和交通预算都放这里",
        ]
        content = (
            f"这是一篇{destination['name']}旅行笔记，重点是{themes[0]}、{themes[1]}。"
            f"Day 1 建议先到{destination['aliases'][0]}，下午留给咖啡馆和散步；"
            f"Day 2 去{destination['aliases'][1]}，人均预算约{300 + (i % 8) * 80}元。"
            f"适合想要{destination['themes'][0]}的人，不太适合讨厌{destination['avoid'][0]}的人。"
        )
        post = {
            "id": f"showcase-post-{i:03d}",
            "type": "vlog",
            "title": title_patterns[i % len(title_patterns)],
            "location": destination["name"],
            "content": content,
            "tags": themes + [destination["name"]],
            "coverUrl": destination["image"],
            "mediaList": [{"type": "image", "url": destination["image"]}],
            "status": "published",
            "author": author_for(author),
            "likeCount": 80 + (i * 13) % 520,
            "favoriteCount": 20 + (i * 9) % 260,
            "commentCount": 2 + i % 9,
            "shareCount": 5 + i % 50,
            "createdAt": iso_days_ago(i % 18, i % 7),
            "updatedAt": iso_days_ago(i % 18, i % 7),
        }
        store.upsert("fe_posts", post["id"], post)
        store.upsert("post_ai_profiles", f"post:{post['id']}", content_profile("post", post, destination, themes))
        posts.append(post)
    return posts


def seed_strategies(users: list[dict]) -> list[dict]:
    strategies: list[dict] = []
    for i in range(1, 25):
        destination = DESTINATIONS[(i * 2 - 1) % len(DESTINATIONS)]
        author = users[(i * 5) % len(users)]
        days = 2 + i % 5
        themes = [destination["themes"][0], destination["themes"][1], "路线规划"]
        strategy = {
            "id": f"showcase-strategy-{i:03d}",
            "title": f"{destination['name']}{days}天旅行攻略：{destination['aliases'][0]}到{destination['aliases'][1]}",
            "summary": f"适合喜欢{themes[0]}和{themes[1]}的人，包含交通、预算、住宿和避坑。",
            "content": "\n".join(
                [
                    f"Day 1：抵达{destination['name']}，安排{destination['aliases'][0]}和周边散步。",
                    f"Day 2：前往{destination['aliases'][1]}，下午保留拍照和咖啡时间。",
                    f"Day 3：如果时间充裕，可以补充{destination['aliases'][2]}，预算按中等水平准备。",
                    f"避坑：不建议把行程排得太满，尤其是不喜欢{destination['avoid'][0]}的人。",
                ][: min(days, 4)]
            ),
            "destination": destination["name"],
            "days": days,
            "coverUrl": destination["image"],
            "imageList": [{"url": destination["image"], "type": "image"}],
            "tags": themes + [destination["name"]],
            "status": "published",
            "author": author_for(author),
            "likeCount": 120 + i * 17,
            "favoriteCount": 60 + i * 11,
            "viewCount": 1200 + i * 230,
            "commentCount": 3 + i % 7,
            "shareCount": 8 + i % 40,
            "createdAt": iso_days_ago(i % 20),
            "updatedAt": iso_days_ago(i % 20),
        }
        store.upsert("fe_strategies", strategy["id"], strategy)
        store.upsert("post_ai_profiles", f"strategy:{strategy['id']}", content_profile("strategy", strategy, destination, themes))
        strategies.append(strategy)
    return strategies


def seed_recruitments_and_realtime(users: list[dict]) -> None:
    for i, user in enumerate(users[:70], start=1):
        destination = DESTINATIONS[(i - 1) % len(DESTINATIONS)]
        start_offset = 18 + (i % 18)
        days = 3 + i % 5
        now = iso_days_ago(i % 12)
        recruitment = {
            "id": f"showcase-recruitment-{i:03d}",
            "destination": destination["name"],
            "startDate": future_date(start_offset),
            "days": days,
            "publisherUserId": user["uid"],
            "publisherNickname": user["display_name"],
            "publisherAvatar": user.get("photo_url", ""),
            "preferenceText": f"想找同样喜欢{destination['themes'][0]}、{destination['themes'][1]}的搭子，不喜欢{destination['avoid'][0]}。",
            "remarks": f"{destination['name']}路线，可一起讨论住宿和安全见面地点。",
            "status": "open",
            "applicationCount": i % 4,
            "createdAt": now,
            "updatedAt": now,
        }
        store.upsert("fe_match_recruitments", recruitment["id"], recruitment)

    for i, user in enumerate(users[70:100], start=1):
        destination = DESTINATIONS[(i - 1) % len(DESTINATIONS)]
        start_offset = 4 + (i % 8)
        end_offset = start_offset + 2 + i % 4
        created = iso_days_ago(i % 6)
        request = {
            "id": f"showcase-realtime-request-{i:03d}",
            "destination": destination["name"],
            "travel_start_date": future_date(start_offset),
            "travel_end_date": future_date(end_offset),
            "preference_tags": [],
            "preference_text": f"希望找喜欢{destination['themes'][0]}、{destination['themes'][1]}的搭子，避免{destination['avoid'][0]}。",
            "ai_profile": {
                "destinationCanonical": destination["name"],
                "destinationAliases": destination["aliases"],
                "positivePreferences": destination["themes"],
                "negativePreferences": destination["avoid"],
                "travelStyle": destination["themes"],
            },
            "user_id": user["uid"],
            "status": "pending",
            "match_deadline_at": datetime.fromisoformat(future_date(start_offset)).replace(tzinfo=TZ).isoformat(),
            "current_candidate_id": "",
            "current_pair_id": "",
            "recruitment_id": f"showcase-hidden-recruitment-{i:03d}",
            "created_at": created,
            "updated_at": created,
        }
        store.upsert("fe_realtime_match_requests", request["id"], request)
        hidden = {
            "id": f"showcase-hidden-recruitment-{i:03d}",
            "publisherUserId": user["uid"],
            "publisherNickname": user["display_name"],
            "publisherAvatar": user.get("photo_url", ""),
            "destination": destination["name"],
            "travelStartDate": request["travel_start_date"],
            "travelEndDate": request["travel_end_date"],
            "preferenceTags": [],
            "preferenceText": request["preference_text"],
            "source": "realtime",
            "visibility": "hidden",
            "requestId": request["id"],
            "status": "open",
            "applicationCount": 0,
            "createdAt": created,
            "updatedAt": created,
        }
        store.upsert("fe_match_recruitments", hidden["id"], hidden)


def seed_interactions_and_comments(users: list[dict], posts: list[dict], strategies: list[dict]) -> None:
    targets = [("post", item) for item in posts] + [("strategy", item) for item in strategies]
    for i, user in enumerate(users, start=1):
        preferred_destination = DESTINATIONS[(i - 1) % len(DESTINATIONS)]["name"]
        disliked_destination = DESTINATIONS[(i + 3) % len(DESTINATIONS)]["name"]
        preferred = [target for target in targets if target[1].get("location") == preferred_destination or target[1].get("destination") == preferred_destination]
        disliked = [target for target in targets if target[1].get("location") == disliked_destination or target[1].get("destination") == disliked_destination]
        for event_index, (target_type, item) in enumerate(preferred[:5], start=1):
            kind_prefix = "post" if target_type == "post" else "strategy"
            for action in ("like", "favorite") if event_index <= 2 else ("like",):
                kind = f"{kind_prefix}_{action}"
                iid = f"{user['uid']}:{kind}:{item['id']}"
                store.upsert(
                    "fe_interactions",
                    iid,
                    {
                        "id": iid,
                        "userId": user["uid"],
                        "kind": kind,
                        "targetId": item["id"],
                        "createdAt": iso_days_ago(event_index),
                        "updatedAt": iso_days_ago(event_index),
                    },
                )
                event_id = f"showcase-event-{user['uid']}-{target_type}-{item['id']}-{action}"
                store.upsert(
                    "recommendation_events",
                    event_id,
                    {
                        "id": event_id,
                        "userId": user["uid"],
                        "eventType": action,
                        "targetType": target_type,
                        "targetId": item["id"],
                        "traceId": "",
                        "metadata": {"seeded": True},
                        "createdAt": iso_days_ago(event_index),
                        "updatedAt": iso_days_ago(event_index),
                    },
                )
        for target_type, item in disliked[:2]:
            event_id = f"showcase-event-{user['uid']}-{target_type}-{item['id']}-dislike"
            store.upsert(
                "recommendation_events",
                event_id,
                {
                    "id": event_id,
                    "userId": user["uid"],
                    "eventType": "dislike",
                    "targetType": target_type,
                    "targetId": item["id"],
                    "traceId": "",
                    "metadata": {"seeded": True, "reason": "not_interested_destination"},
                    "createdAt": iso_days_ago(1),
                    "updatedAt": iso_days_ago(1),
                },
            )

    for i, post in enumerate(posts[:30], start=1):
        user = users[(i * 3) % len(users)]
        for j in range(2):
            comment_id = f"showcase-comment-post-{i:03d}-{j + 1}"
            store.upsert(
                "fe_comments",
                comment_id,
                {
                    "id": comment_id,
                    "targetCollection": "fe_posts",
                    "targetType": "post",
                    "targetId": post["id"],
                    "content": COMMENTS[(i + j) % len(COMMENTS)],
                    "author": author_for(user),
                    "createdAt": iso_days_ago((i + j) % 7),
                    "updatedAt": iso_days_ago((i + j) % 7),
                },
            )


def seed_user_interest_profiles(users: list[dict]) -> None:
    for i, user in enumerate(users, start=1):
        destination = DESTINATIONS[(i - 1) % len(DESTINATIONS)]
        disliked = DESTINATIONS[(i + 3) % len(DESTINATIONS)]
        payload = {
            "id": user["uid"],
            "userId": user["uid"],
            "favoriteDestinations": [destination["name"], *destination["aliases"]],
            "favoriteThemes": destination["themes"] + ["真实体验", "小红书旅行"],
            "negativeThemes": disliked["themes"][:2] + destination["avoid"],
            "shortTermInterests": [destination["name"], destination["themes"][0], destination["themes"][1]],
            "longTermInterests": destination["themes"],
            "authorAffinity": [],
            "profileText": " ".join([destination["name"], *destination["aliases"], *destination["themes"]]),
            "updatedAt": NOW.isoformat(),
        }
        store.upsert("user_interest_profiles", user["uid"], payload)


def main() -> None:
    users = []
    for i in range(1, 101):
        destination = DESTINATIONS[(i - 1) % len(DESTINATIONS)]
        users.append(upsert_showcase_account(i, destination, like_same_place=i % 5 != 0))
    posts = seed_posts(users)
    strategies = seed_strategies(users)
    seed_recruitments_and_realtime(users)
    seed_interactions_and_comments(users, posts, strategies)
    seed_user_interest_profiles(users)
    print(
        {
            "users": len(users),
            "posts": len(posts),
            "strategies": len(strategies),
            "recruitments": 70,
            "realtime_requests": 30,
            "demo_username": "showcase001",
            "demo_password": PASSWORD,
        }
    )


if __name__ == "__main__":
    main()
