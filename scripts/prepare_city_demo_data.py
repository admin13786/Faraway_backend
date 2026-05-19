from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.auth_service import AUTH_ACCOUNTS_COLLECTION, AUTH_IDENTITIES_COLLECTION, _hash_password  # noqa: E402
from app.services.store import store  # noqa: E402


PASSWORD = "Demo123456"
TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ)

CITY_PROFILES = [
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

PREFERENCE_TAGS = ["慢旅行", "拍照", "美食", "自然风光", "人文历史", "夜景"]
USER_COUNT = 120


def iso_now() -> str:
    return NOW.isoformat()


def future(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def identity_key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def author_for(user: dict) -> dict:
    return {
        "id": user["uid"],
        "nickname": user["display_name"],
        "avatar": user.get("photo_url", ""),
    }


def upsert_city_user(index: int, city: dict) -> dict:
    uid = f"city-demo-user-{index:03d}"
    username = f"citydemo{index:03d}"
    phone = f"1772000{index:04d}"
    name = f"{city['name']}搭子{index:03d}"
    avatar = f"https://i.pravatar.cc/160?img={(index % 70) + 1}"
    bio = (
        f"常去{city['name']}，喜欢{city['themes'][0]}、{city['themes'][1]}和安全见面。"
        f"演示账号可用于北上广深实时找搭子，偏好不赶路、公开地点见面。"
    )
    user = {
        "uid": uid,
        "display_name": name,
        "email": f"{username}@faraway.demo",
        "photo_url": avatar,
        "bio": bio,
        "gender": "unknown",
        "created_at": iso_now(),
        "updated_at": iso_now(),
    }
    account = {
        "uid": uid,
        "username": username,
        "username_key": username,
        "phone": phone,
        "email": user["email"],
        "display_name": name,
        "password_hash": _hash_password(PASSWORD),
        "login_types": ["password", "phone"],
        "created_at": iso_now(),
        "updated_at": iso_now(),
        "status": "active",
    }
    store.upsert("users", uid, user)
    store.upsert(AUTH_ACCOUNTS_COLLECTION, uid, account)
    store.upsert(AUTH_IDENTITIES_COLLECTION, identity_key("username", username), {"uid": uid, "kind": "username", "value": username})
    store.upsert(AUTH_IDENTITIES_COLLECTION, identity_key("phone", phone), {"uid": uid, "kind": "phone", "value": phone})
    store.upsert(
        "user_interest_profiles",
        uid,
        {
            "id": uid,
            "userId": uid,
            "favoriteDestinations": [city["name"], *city["aliases"]],
            "favoriteThemes": [*city["themes"], "真实体验", "安全见面"],
            "negativeThemes": city["avoid"],
            "shortTermInterests": [city["name"], city["themes"][0], city["themes"][1]],
            "longTermInterests": city["themes"],
            "authorAffinity": [],
            "profileText": " ".join([city["name"], *city["aliases"], *city["themes"]]),
            "updatedAt": iso_now(),
        },
    )
    return user


def content_profile(target_type: str, item: dict, city: dict, themes: list[str]) -> dict:
    target_id = item["id"]
    return {
        "id": f"{target_type}:{target_id}",
        "targetType": target_type,
        "targetId": target_id,
        "destinations": [city["name"], *city["aliases"]],
        "themes": [*themes, *city["themes"], "北上广深演示", "真实体验"],
        "travelStyle": "citywalk",
        "budgetLevel": "medium",
        "suitableUsers": ["第一次去的用户", "找搭子用户", "城市周末旅行用户"],
        "titleScore": 0.84,
        "contentQualityScore": 0.86,
        "usefulnessScore": 0.86,
        "authenticityScore": 0.82,
        "commercialRiskScore": 0.03,
        "clickbaitRiskScore": 0.03,
        "overallAiQualityScore": 0.85,
        "summary": f"{city['name']} / {' / '.join(themes[:3])}",
        "analysisMode": "seeded_city_demo",
        "model": "seeded-profile-v2",
        "sourceUpdatedAt": item.get("updatedAt") or item.get("createdAt") or iso_now(),
        "updatedAt": iso_now(),
    }


def seed_city_content(users: list[dict]) -> None:
    for i in range(1, 65):
        city = CITY_PROFILES[(i - 1) % len(CITY_PROFILES)]
        author = users[(i * 3) % len(users)]
        themes = [city["themes"][i % len(city["themes"])], city["themes"][(i + 1) % len(city["themes"])], "周末短途"]
        post = {
            "id": f"city-demo-post-{i:03d}",
            "type": "vlog",
            "title": f"{city['name']}周末路线：{city['aliases'][0]}到{city['aliases'][1]}",
            "location": city["name"],
            "content": (
                f"{city['name']}适合演示找搭子和旅行笔记。"
                f"推荐{city['aliases'][0]}、{city['aliases'][1]}，适合{themes[0]}和{themes[1]}，"
                f"建议公开地点见面，不安排太赶的路线。"
            ),
            "tags": [city["name"], *themes],
            "coverUrl": city["image"],
            "mediaList": [{"type": "image", "url": city["image"]}],
            "status": "published",
            "author": author_for(author),
            "likeCount": 120 + i * 9,
            "favoriteCount": 60 + i * 5,
            "commentCount": 3 + i % 8,
            "shareCount": 5 + i % 20,
            "createdAt": iso_now(),
            "updatedAt": iso_now(),
        }
        store.upsert("fe_posts", post["id"], post)
        store.upsert("post_ai_profiles", f"post:{post['id']}", content_profile("post", post, city, themes))

    for i in range(1, 33):
        city = CITY_PROFILES[(i - 1) % len(CITY_PROFILES)]
        author = users[(i * 5) % len(users)]
        themes = [city["themes"][0], city["themes"][1], "路线规划"]
        strategy = {
            "id": f"city-demo-strategy-{i:03d}",
            "title": f"{city['name']}3天旅行攻略：{city['aliases'][0]}到{city['aliases'][1]}",
            "summary": f"适合演示用的{city['name']}攻略，包含交通、预算、见面地点和避坑。",
            "content": "\n".join(
                [
                    f"Day 1：抵达{city['name']}，先去{city['aliases'][0]}，下午城市漫步。",
                    f"Day 2：安排{city['aliases'][1]}和本地美食，保留拍照时间。",
                    f"Day 3：补充{city['aliases'][2]}，避免{city['avoid'][0]}。",
                ]
            ),
            "destination": city["name"],
            "days": 3,
            "coverUrl": city["image"],
            "imageList": [{"url": city["image"], "type": "image"}],
            "tags": [city["name"], *themes],
            "status": "published",
            "author": author_for(author),
            "likeCount": 180 + i * 11,
            "favoriteCount": 90 + i * 7,
            "viewCount": 1600 + i * 120,
            "commentCount": 4 + i % 6,
            "shareCount": 8 + i % 20,
            "createdAt": iso_now(),
            "updatedAt": iso_now(),
        }
        store.upsert("fe_strategies", strategy["id"], strategy)
        store.upsert("post_ai_profiles", f"strategy:{strategy['id']}", content_profile("strategy", strategy, city, themes))


def seed_city_realtime(users: list[dict]) -> None:
    per_city_users = {city["name"]: [] for city in CITY_PROFILES}
    for user_index, user in enumerate(users):
        city = CITY_PROFILES[user_index % len(CITY_PROFILES)]
        per_city_users[city["name"]].append(user)

    request_count = 0
    for city in CITY_PROFILES:
        city_users = per_city_users[city["name"]]
        for slot in range(18):
            user = city_users[slot % len(city_users)]
            start_offset = 4 + slot * 2
            end_offset = start_offset + 4
            tags = [tag for tag in city["themes"] if tag in PREFERENCE_TAGS][:2]
            if not tags:
                tags = ["慢旅行", "拍照"]
            request_id = f"city-demo-realtime-{city['name']}-{slot + 1:02d}"
            hidden_id = f"city-demo-hidden-{city['name']}-{slot + 1:02d}"
            preference_text = (
                f"演示用{city['name']}搭子，喜欢{city['themes'][0]}、{city['themes'][1]}，"
                f"可接受日期有一天重叠，公开地点见面。"
            )
            request = {
                "id": request_id,
                "destination": city["name"],
                "travel_start_date": future(start_offset),
                "travel_end_date": future(end_offset),
                "preference_tags": tags,
                "preference_text": preference_text,
                "ai_profile": {
                    "destinationCanonical": city["name"],
                    "destinationAliases": city["aliases"],
                    "positivePreferences": city["themes"],
                    "negativePreferences": city["avoid"],
                    "travelStyle": city["themes"],
                },
                "user_id": user["uid"],
                "status": "pending",
                "match_deadline_at": datetime.combine(date.today() + timedelta(days=start_offset - 2), datetime.min.time(), tzinfo=TZ).isoformat(),
                "current_candidate_id": "",
                "current_pair_id": "",
                "recruitment_id": hidden_id,
                "created_at": iso_now(),
                "updated_at": iso_now(),
            }
            hidden = {
                "id": hidden_id,
                "publisherUserId": user["uid"],
                "publisherNickname": user["display_name"],
                "publisherAvatar": user.get("photo_url", ""),
                "destination": city["name"],
                "travelStartDate": request["travel_start_date"],
                "travelEndDate": request["travel_end_date"],
                "preferenceTags": tags,
                "preferenceText": preference_text,
                "source": "realtime",
                "visibility": "hidden",
                "requestId": request_id,
                "status": "open",
                "applicationCount": 0,
                "createdAt": iso_now(),
                "updatedAt": iso_now(),
            }
            store.upsert("fe_realtime_match_requests", request_id, request)
            store.upsert("fe_match_recruitments", hidden_id, hidden)
            request_count += 1

    for i in range(1, 49):
        city = CITY_PROFILES[(i - 1) % len(CITY_PROFILES)]
        user = users[(i * 7) % len(users)]
        recruit_id = f"city-demo-recruitment-{i:03d}"
        store.upsert(
            "fe_match_recruitments",
            recruit_id,
            {
                "id": recruit_id,
                "destination": city["name"],
                "startDate": future(5 + i % 24),
                "days": 3 + i % 4,
                "publisherUserId": user["uid"],
                "publisherNickname": user["display_name"],
                "publisherAvatar": user.get("photo_url", ""),
                "preferenceText": f"找{city['name']}搭子，偏好{city['themes'][0]}、{city['themes'][1]}。",
                "remarks": f"{city['name']}演示招募帖，适合北上广深推荐。",
                "status": "open",
                "applicationCount": i % 5,
                "createdAt": iso_now(),
                "updatedAt": iso_now(),
            },
        )
    return request_count


def recode_existing_destinations() -> None:
    city_names = [city["name"] for city in CITY_PROFILES]
    city_by_name = {city["name"]: city for city in CITY_PROFILES}
    fields_by_collection = {
        "fe_realtime_match_requests": ["destination"],
        "fe_match_recruitments": ["destination"],
        "fe_posts": ["location"],
        "fe_strategies": ["destination"],
    }
    for collection, fields in fields_by_collection.items():
        for index, item in enumerate(store.list(collection)):
            if str(item.get("id", "")).startswith("city-demo-"):
                continue
            city = city_by_name[city_names[index % len(city_names)]]
            for field in fields:
                if field in item:
                    item[field] = city["name"]
            if collection in {"fe_posts", "fe_strategies"}:
                item["coverUrl"] = item.get("coverUrl") or city["image"]
                item["tags"] = list(dict.fromkeys([city["name"], *item.get("tags", [])]))
                item["updatedAt"] = iso_now()
            if collection == "fe_realtime_match_requests" and item.get("status") == "pending":
                item["ai_profile"] = {
                    "destinationCanonical": city["name"],
                    "destinationAliases": city["aliases"],
                    "positivePreferences": city["themes"],
                    "negativePreferences": city["avoid"],
                    "travelStyle": city["themes"],
                }
            store.upsert(collection, item["id"], item)


def main() -> None:
    users = [upsert_city_user(i, CITY_PROFILES[(i - 1) % len(CITY_PROFILES)]) for i in range(1, USER_COUNT + 1)]
    seed_city_content(users)
    request_count = seed_city_realtime(users)
    recode_existing_destinations()
    print(
        {
            "city_users": len(users),
            "city_realtime_requests": request_count,
            "cities": [city["name"] for city in CITY_PROFILES],
            "demo_password": PASSWORD,
        }
    )


if __name__ == "__main__":
    main()
