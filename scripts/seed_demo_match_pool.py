from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


DB_PATH = Path(__file__).resolve().parents[1] / "data" / "faraway_app.db"
TZ = timezone(timedelta(hours=8))

CITIES = {
    "\u5317\u4eac": ["\u6545\u5bab", "\u80e1\u540c", "Citywalk", "\u6444\u5f71"],
    "\u4e0a\u6d77": ["\u5916\u6ee9", "\u6b66\u5eb7\u8def", "\u5496\u5561", "\u5c55\u89c8"],
    "\u5e7f\u5dde": ["\u65e9\u8336", "\u73e0\u6c5f", "\u8001\u57ce\u533a", "\u7f8e\u98df"],
    "\u6df1\u5733": ["\u6d77\u8fb9", "\u5357\u5c71", "\u516c\u56ed", "\u79d1\u6280\u9986"],
}


def upsert(cur: sqlite3.Cursor, collection: str, item_id: str, data: dict) -> None:
    item = dict(data)
    item["id"] = item_id
    cur.execute(
        """
        INSERT INTO documents(collection, id, data) VALUES (?, ?, ?)
        ON CONFLICT(collection, id) DO UPDATE SET data=excluded.data
        """,
        (collection, item_id, json.dumps(item, ensure_ascii=False)),
    )


def main() -> None:
    now = datetime.now(TZ).replace(microsecond=0)
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute(
            """
            DELETE FROM documents
            WHERE id LIKE 'demo-match-%'
               OR id LIKE 'demo-match-request-%'
               OR id LIKE 'demo-match-hidden-%'
            """
        )

        for city, themes in CITIES.items():
            for idx in range(1, 7):
                theme = themes[(idx - 1) % len(themes)]
                user_id = f"demo-match-user-{idx:02d}-{city}"
                display_name = f"{city}\u642d\u5b50{idx:02d}"
                request_id = f"demo-match-request-{idx:02d}-{city}"
                hidden_id = f"demo-match-hidden-{idx:02d}-{city}"
                preference_text = (
                    f"\u6f14\u793a\u7528{city}\u642d\u5b50\uff0c"
                    "2026-05-20\u52302026-05-23\u90fd\u6709\u65f6\u95f4\uff0c"
                    f"\u559c\u6b22{theme}\u3001\u62cd\u7167\u548c\u7f8e\u98df\uff0c"
                    "\u63a5\u53d7\u4e00\u5929\u4ee5\u4e0a\u91cd\u53e0\u3002"
                )
                tags = ["\u7f8e\u98df", "\u62cd\u7167", theme]

                upsert(
                    cur,
                    "users",
                    user_id,
                    {
                        "uid": user_id,
                        "display_name": display_name,
                        "email": "",
                        "photo_url": "/static/avatar/user-default.jpg",
                        "bio": f"\u559c\u6b22{city}\u65c5\u884c\uff0c\u504f\u597d{theme}\u548c\u8f7b\u677e\u8def\u7ebf\u3002",
                        "gender": "unknown",
                        "created_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                    },
                )

                request = {
                    "id": request_id,
                    "destination": city,
                    "travel_start_date": "2026-05-20",
                    "travel_end_date": "2026-05-23",
                    "preference_tags": tags,
                    "preference_text": preference_text,
                    "ai_profile": {
                        "destinationCanonical": city,
                        "destinationAliases": [city],
                        "positivePreferences": tags,
                        "negativePreferences": [],
                        "travelStyle": ["\u8f7b\u677e", "\u5b89\u5168\u89c1\u9762"],
                    },
                    "user_id": user_id,
                    "status": "pending",
                    "match_deadline_at": (now + timedelta(days=30)).isoformat(),
                    "current_candidate_id": "",
                    "current_pair_id": "",
                    "recruitment_id": hidden_id,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
                hidden = {
                    "id": hidden_id,
                    "publisherUserId": user_id,
                    "publisherNickname": display_name,
                    "publisherAvatar": "/static/avatar/user-default.jpg",
                    "destination": city,
                    "travelStartDate": request["travel_start_date"],
                    "travelEndDate": request["travel_end_date"],
                    "preferenceTags": tags,
                    "preferenceText": preference_text,
                    "source": "realtime",
                    "visibility": "hidden",
                    "requestId": request_id,
                    "status": "open",
                    "applicationCount": 0,
                    "createdAt": now.isoformat(),
                    "updatedAt": now.isoformat(),
                }
                upsert(cur, "fe_realtime_match_requests", request_id, request)
                upsert(cur, "fe_match_recruitments", hidden_id, hidden)

    print("seeded demo realtime match pool:", ", ".join(CITIES))


if __name__ == "__main__":
    main()
