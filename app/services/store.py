from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock
from uuid import uuid4

from app.core.config import settings


class SQLiteStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = settings.resolved_sqlite_path if str(db_path) == settings.sqlite_path else Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        try:
            self._init_db()
        except sqlite3.OperationalError:
            self._recover_from_broken_database()
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA journal_mode={settings.sqlite_journal_mode}")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _recover_from_broken_database(self) -> None:
        journal_candidates = [
            self.db_path.with_name(self.db_path.name + "-journal"),
            self.db_path.with_name(self.db_path.name + "-wal"),
            self.db_path.with_name(self.db_path.name + "-shm"),
        ]
        for extra_file in journal_candidates:
            if extra_file.exists():
                try:
                    extra_file.unlink()
                except PermissionError:
                    pass

        if self.db_path.exists():
            backup_name = f"{self.db_path.stem}.broken.{uuid4().hex}{self.db_path.suffix}"
            backup_path = self.db_path.with_name(backup_name)
            try:
                self.db_path.replace(backup_path)
            except PermissionError:
                self.db_path = self.db_path.with_name(
                    f"{self.db_path.stem}.runtime.{uuid4().hex}{self.db_path.suffix}"
                )

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    collection TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (collection, id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_documents_collection
                ON documents(collection)
                """
            )

    def create(self, collection: str, payload: dict) -> dict:
        item = dict(payload)
        item["id"] = item.get("id") or uuid4().hex
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO documents(collection, id, data) VALUES (?, ?, ?)",
                (collection, item["id"], json.dumps(item, ensure_ascii=False)),
            )
        return dict(item)

    def upsert(self, collection: str, item_id: str, payload: dict) -> dict:
        item = dict(payload)
        item["id"] = item_id
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO documents(collection, id, data) VALUES (?, ?, ?)
                ON CONFLICT(collection, id) DO UPDATE SET data=excluded.data
                """,
                (collection, item_id, json.dumps(item, ensure_ascii=False)),
            )
        return dict(item)

    def get(self, collection: str, item_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM documents WHERE collection = ? AND id = ?",
                (collection, item_id),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def list(self, collection: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM documents WHERE collection = ?",
                (collection,),
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def update(self, collection: str, item_id: str, updates: dict) -> dict | None:
        with self._lock:
            existing = self.get(collection, item_id)
            if not existing:
                return None
            existing.update({key: value for key, value in updates.items() if value is not None})
            self.upsert(collection, item_id, existing)
            return dict(existing)

    def delete(self, collection: str, item_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM documents WHERE collection = ? AND id = ?",
                (collection, item_id),
            )
            return cursor.rowcount > 0


store = SQLiteStore(settings.sqlite_path)
