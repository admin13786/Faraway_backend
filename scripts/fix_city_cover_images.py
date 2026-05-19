from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


DB_PATH = Path(__file__).resolve().parents[1] / "data" / "faraway_app.db"
TZ = timezone(timedelta(hours=8))

CITY_COVERS = {
    "\u5317\u4eac": "/static/cover/city-beijing.jpg",
    "\u4e0a\u6d77": "/static/cover/city-shanghai.jpg",
    "\u5e7f\u5dde": "/static/cover/city-guangzhou.jpg",
    "\u6df1\u5733": "/static/cover/city-shenzhen.jpg",
}


def normalize_media(item: dict, cover_url: str) -> None:
    item["coverUrl"] = cover_url
    if "imageList" in item:
        image_list = item.get("imageList") or []
        if image_list:
            first = dict(image_list[0])
            first["url"] = cover_url
            image_list[0] = first
        else:
            image_list = [{"url": cover_url, "type": "image"}]
        item["imageList"] = image_list
    if "mediaList" in item:
        media_list = item.get("mediaList") or []
        if media_list:
            first = dict(media_list[0])
            if first.get("type", "image") == "image":
                first["url"] = cover_url
                media_list[0] = first
        else:
            media_list = [{"type": "image", "url": cover_url}]
        item["mediaList"] = media_list


def main() -> None:
    now = datetime.now(TZ).replace(microsecond=0).isoformat()
    updated = 0
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        for collection in ("fe_posts", "fe_strategies"):
            rows = cur.execute(
                "SELECT id, data FROM documents WHERE collection = ?",
                (collection,),
            ).fetchall()
            for item_id, raw in rows:
                item = json.loads(raw)
                city = item.get("destination") or item.get("location")
                cover_url = CITY_COVERS.get(city)
                if not cover_url:
                    continue
                before = json.dumps(item, ensure_ascii=False, sort_keys=True)
                normalize_media(item, cover_url)
                item["updatedAt"] = now
                after = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if before != after:
                    cur.execute(
                        "UPDATE documents SET data = ? WHERE collection = ? AND id = ?",
                        (json.dumps(item, ensure_ascii=False), collection, item_id),
                    )
                    updated += 1
    print({"updated": updated, "db": str(DB_PATH)})


if __name__ == "__main__":
    main()
