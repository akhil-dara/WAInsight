"""
Message tagging service — add / remove / list investigator tags on messages.

Backed by the existing `message_tag` table in analysis.db. The table stores
every (message_id, tag_label) pair plus a note and who tagged it when.

Usage:
    svc = MessageTagService.instance()
    tag_id = svc.ensure_tag("cred_templates")   # idempotent
    svc.bulk_tag(tag_id, [msg_id_1, msg_id_2, ...])
    svc.list_tags_for(msg_id)                   # -> list of tag_label
    svc.list_messages_with(tag_label)           # -> list of message_id
"""
from __future__ import annotations

from app.services.database import Database


class MessageTagService:
    """Thin wrapper around the `message_tag` table.

    The schema stores tags per message (no separate `tag` catalogue), so
    `ensure_tag` just returns the tag label string itself — we keep the
    signature 'tag_id' for forward compatibility with a future tag table.
    """

    _instance: "MessageTagService | None" = None

    @classmethod
    def instance(cls) -> "MessageTagService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._db = Database.get()
        self._ensure_table()

    def _ensure_table(self):
        try:
            self._db.execute_write("""
                CREATE TABLE IF NOT EXISTS message_tag (
                    id          INTEGER PRIMARY KEY,
                    message_id  INTEGER NOT NULL,
                    tag_label   TEXT DEFAULT 'flagged',
                    note        TEXT DEFAULT '',
                    tagged_at   TEXT DEFAULT (datetime('now')),
                    tagged_by   TEXT DEFAULT 'investigator'
                )
            """)
            self._db.execute_write(
                "CREATE INDEX IF NOT EXISTS idx_message_tag_mid "
                "ON message_tag(message_id)"
            )
            self._db.execute_write(
                "CREATE INDEX IF NOT EXISTS idx_message_tag_label "
                "ON message_tag(tag_label)"
            )
        except Exception as e:
            print(f"[MessageTagService] ensure_table warning: {e}")

    def ensure_tag(self, label: str) -> str:
        """Idempotent. We don't have a catalogue table yet, so this just
        returns the cleaned label."""
        return (label or "flagged").strip() or "flagged"

    def bulk_tag(self, tag_label: str, message_ids: list[int],
                 note: str = "", tagged_by: str = "investigator") -> int:
        """Add `tag_label` to every message_id (skips pairs that already exist).

        Returns how many NEW pairs were inserted.
        """
        if not message_ids:
            return 0
        label = self.ensure_tag(tag_label)
        added = 0
        # Fetch current (message_id, tag_label) pairs to avoid duplicates
        try:
            placeholders = ",".join("?" * len(message_ids))
            existing = set()
            for row in self._db.fetchall(
                f"SELECT message_id FROM message_tag "
                f"WHERE tag_label = ? AND message_id IN ({placeholders})",
                (label, *message_ids),
            ):
                existing.add(row[0])
        except Exception:
            existing = set()

        for mid in message_ids:
            if mid in existing or not mid:
                continue
            try:
                self._db.execute_write(
                    "INSERT INTO message_tag (message_id, tag_label, note, tagged_by) "
                    "VALUES (?, ?, ?, ?)",
                    (int(mid), label, note, tagged_by),
                )
                added += 1
            except Exception as e:
                print(f"[MessageTagService] insert failed for mid={mid}: {e}")
        try:
            self._db.checkpoint_and_reconnect()
        except Exception:
            pass
        return added

    def untag(self, tag_label: str, message_ids: list[int]) -> int:
        if not message_ids:
            return 0
        label = self.ensure_tag(tag_label)
        placeholders = ",".join("?" * len(message_ids))
        try:
            self._db.execute_write(
                f"DELETE FROM message_tag "
                f"WHERE tag_label = ? AND message_id IN ({placeholders})",
                (label, *message_ids),
            )
            self._db.checkpoint_and_reconnect()
            return len(message_ids)
        except Exception as e:
            print(f"[MessageTagService] untag failed: {e}")
            return 0

    def list_tags_for(self, message_id: int) -> list[str]:
        try:
            return [
                r[0] for r in self._db.fetchall(
                    "SELECT DISTINCT tag_label FROM message_tag "
                    "WHERE message_id = ?", (message_id,)
                )
            ]
        except Exception:
            return []

    def list_messages_with(self, tag_label: str) -> list[int]:
        try:
            return [
                r[0] for r in self._db.fetchall(
                    "SELECT DISTINCT message_id FROM message_tag "
                    "WHERE tag_label = ?", (tag_label,)
                )
            ]
        except Exception:
            return []

    def all_labels(self) -> list[tuple[str, int]]:
        """Return [(tag_label, count)] sorted by count desc."""
        try:
            return self._db.fetchall(
                "SELECT tag_label, COUNT(*) AS c FROM message_tag "
                "GROUP BY tag_label ORDER BY c DESC"
            ) or []
        except Exception:
            return []
