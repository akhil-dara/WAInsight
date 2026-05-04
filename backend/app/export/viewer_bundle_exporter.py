"""
V2 HTML Viewer Bundle Exporter.

Layout::

    output/
    ├── index.html                 app shell + inline CSS/JS
    ├── data/
    │   ├── manifest.js            window.__MANIFEST = {...} (JSONP)
    │   └── conv_<id>/
    │       └── shard-NNNN.js      window.__SHARD(key, [msgs...])
    └── media/
        └── <hash>.<ext>           media files, deduped by SHA-256

The ZIP is delivered as a single file; users unzip and double-click index.html.
`file://` constraints (no fetch, no Service Workers) are respected by using
JSONP-style shard delivery via <script src>.

Renders every message type the in-app chat_renderer supports:
    text, image, video, gif, voice note, audio, document, sticker,
    location / live location, vCard, poll, call (incl. synthesized
    voice-chats), system events, quoted replies, reactions, forwarded,
    edited, revoked, ghost, view-once, link previews.
"""
from __future__ import annotations

import base64
import hashlib
import html as _html
import json
import os
import re
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Iterable, Optional

from PySide6.QtCore import QThread, Signal

_ASSET_DIR = Path(__file__).resolve().parent / "viewer_assets"

# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

SHARD_SIZE = 2000          # messages per shard .js file
INCLUDE_MEDIA_TYPES = {"image", "video", "gif", "voice", "ptt", "audio",
                        "document", "sticker"}


class ViewerBundleExporter(QThread):
    """Export selected conversations as a self-contained viewer bundle.

    Signals:
        progress(int, int, str)  current, total, stage/current conv name
        finished(str, str)       output_path ("" on failure), error_msg
    """

    progress = Signal(int, int, str)
    finished = Signal(str, str)

    def __init__(
        self,
        conversation_ids: list[int],
        db_path: str,
        output_dir: str,
        *,
        include_media: bool = True,
        make_zip: bool = True,
        title: str = "WhatsApp Export",
        case_info: Optional[dict] = None,
        message_id_filter: Optional[dict[int, set[int]]] = None,
    ):
        super().__init__()
        self._conv_ids = list(conversation_ids)
        self._db_path = db_path
        self._output_dir = Path(output_dir)
        self._include_media = include_media
        self._make_zip = make_zip
        self._title = title
        self._case_info = case_info or {}
        self._cancelled = False
        # Optional per-conversation message-id whitelist.  When a
        # conversation is in this map, ONLY messages whose id is in
        # the corresponding set are emitted into that conv's shards.
        # Gaps between consecutive included messages get a synthetic
        # ``__compaction__`` marker so the viewer can render
        # "N messages hidden" banners — keeping the export readable
        # for tagged-message exports without leaking unrelated msgs.
        self._msg_id_filter: dict[int, set[int]] = message_id_filter or {}
        # Resolve the msgstore.db path from the case metadata so call-record
        # ↔ message linking can use msgstore.message_call_log (the
        # call_record table itself has no message_id column; the bridge
        # lives in msgstore).  Best-effort: on failure we fall back to
        # not loading calls — better than crashing the export.
        self._msgstore_path: Optional[str] = None
        # Owner identity used by the system_event formatter to
        # render "+ X added You (Owner: <name>, +<phone>)"-style
        # strings.  Pulled once from case_metadata.
        self._owner_phone: str = ""
        self._owner_name: str = ""
        self._owner_contact_id: int = 0
        try:
            import sqlite3 as _sql
            ana = _sql.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
            row = ana.execute(
                "SELECT value FROM case_metadata WHERE key = 'source_path_msgstore.db'"
            ).fetchone()
            for k in ("device_owner_phone", "device_owner_name"):
                r = ana.execute(
                    "SELECT value FROM case_metadata WHERE key = ?", (k,)
                ).fetchone()
                if r and r[0]:
                    if k == "device_owner_phone":
                        self._owner_phone = r[0]
                    else:
                        self._owner_name = r[0]
            if self._owner_phone:
                ocid = ana.execute(
                    "SELECT id FROM contact WHERE phone_number = ? LIMIT 1",
                    (self._owner_phone,),
                ).fetchone()
                if ocid:
                    self._owner_contact_id = int(ocid[0])
            ana.close()
            if row and row[0]:
                from pathlib import Path as _P
                p = _P(row[0])
                if p.exists():
                    self._msgstore_path = str(p)
        except Exception:
            pass

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            result = self._export()
            self.finished.emit(str(result), "")
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.finished.emit("", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Core pipeline
    # ------------------------------------------------------------------

    def _export(self) -> Path:
        ts_folder = time.strftime("%Y%m%d_%H%M%S")
        base = self._output_dir / f"wainsight_viewer_{ts_folder}"
        (base / "data").mkdir(parents=True, exist_ok=True)
        if self._include_media:
            (base / "media").mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA cache_size = -200000")
            conn.execute("PRAGMA mmap_size = 2000000000")
            conn.execute("PRAGMA temp_store = MEMORY")

            # Load each selected conversation, write shards, collect manifest
            manifest_convs = []
            total_msgs = 0
            media_mapping = {}   # analysis src path → bundle media/ relpath

            for i, cid in enumerate(self._conv_ids):
                if self._cancelled:
                    break
                conv_row = conn.execute(
                    "SELECT id, COALESCE(display_name, jid_raw_string, '#'||id) AS title, "
                    "       chat_type, jid_raw_string, subject, description, "
                    "       message_count, participant_count, "
                    "       first_message_ts, last_message_ts, avatar_blob "
                    "FROM conversation WHERE id = ?",
                    (cid,),
                ).fetchone()
                if not conv_row:
                    continue
                title = conv_row["title"] or f"#{cid}"
                self.progress.emit(i + 1, len(self._conv_ids), f"Exporting: {title}")

                shards_info, msg_count = self._write_conversation_shards(
                    conn, base, cid, media_mapping,
                )
                total_msgs += msg_count

                # Sidebar avatar (base64 for inline render).
                # For groups the DP is on
                # ``conversation.avatar_blob``.  For personal
                # chats WhatsApp typically stores the avatar on
                # the CONTACT row, not the conversation row.  Fall
                # back to the contact's ``avatar_blob`` so brand
                # / business display pictures still appear in the
                # sidebar list — otherwise the conversation would
                # show just a coloured letter.
                avatar_b64 = ""
                try:
                    avatar_blob = conv_row["avatar_blob"]
                    if (not avatar_blob
                            and (conv_row["chat_type"] or "personal") == "personal"
                            and conv_row["jid_raw_string"]):
                        ct_row = conn.execute(
                            "SELECT avatar_blob FROM contact "
                            "WHERE phone_jid = ? OR lid_jid = ? "
                            "LIMIT 1",
                            (conv_row["jid_raw_string"],
                             conv_row["jid_raw_string"]),
                        ).fetchone()
                        if ct_row and ct_row["avatar_blob"]:
                            avatar_blob = ct_row["avatar_blob"]
                    if avatar_blob:
                        avatar_b64 = ("data:image/jpeg;base64," +
                            base64.b64encode(avatar_blob).decode("ascii"))
                except Exception:
                    pass
                manifest_convs.append({
                    "id": f"conv_{cid}",
                    "raw_id": cid,
                    "jid": conv_row["jid_raw_string"] or "",
                    "title": title,
                    "subject": conv_row["subject"] or "",
                    "description": conv_row["description"] or "",
                    "type": conv_row["chat_type"] or "personal",
                    "participantCount": conv_row["participant_count"] or 0,
                    "messageCount": msg_count,
                    "firstMessageAt": conv_row["first_message_ts"],
                    "lastMessageAt": conv_row["last_message_ts"],
                    "avatar": avatar_b64,
                    "shards": shards_info,
                })
        finally:
            conn.close()

        # ── Tagged-messages catalog ──
        # When the export was driven by the tagged-messages flow
        # (``message_id_filter`` is set), build a quick-look index of
        # every tagged message so the viewer can show a sidebar with
        # click-to-jump entries.  Each entry carries enough context
        # (sender, timestamp, preview text, convId) for the sidebar
        # to render without re-loading the conversation shards.
        tagged_index: list[dict] = []
        if self._msg_id_filter:
            try:
                conn2 = sqlite3.connect(
                    f"file:{self._db_path}?mode=ro&immutable=1", uri=True
                )
                conn2.row_factory = sqlite3.Row
                # Resolve every (conv_id, msg_id) pair the user actually
                # tagged.  Note: ``message_id_filter`` for the buffer
                # mode contains the WIDER context window, not just the
                # tagged ids — so look up the tag table directly.
                # Detect the tag-label column — the schema varies
                # across analysis.db ingest versions: newer cases use
                # ``tag_label``, older cases used ``tag``.  Keep both
                # paths working without forcing a re-ingest.
                tag_cols = {
                    r["name"] for r in conn2.execute(
                        "PRAGMA table_info(message_tag)"
                    ).fetchall()
                }
                tag_col = "tag_label" if "tag_label" in tag_cols else (
                    "tag" if "tag" in tag_cols else "''"
                )
                tag_rows = conn2.execute(
                    f"SELECT mt.message_id, mt.{tag_col} AS tag,"
                    "       mt.note, mt.tagged_at,"
                    "       m.conversation_id, m.timestamp,"
                    "       SUBSTR(COALESCE(m.text_content, ''), 1, 140) AS preview,"
                    "       m.message_type, m.from_me,"
                    "       COALESCE(c.resolved_name, c.wa_name, c.phone_number, "
                    "                m.rendered_sender, '') AS sender_name "
                    "FROM message_tag mt "
                    "JOIN message m ON m.id = mt.message_id "
                    "LEFT JOIN contact c ON c.id = m.sender_id "
                    "WHERE m.conversation_id IN ("
                    + ",".join(str(int(cid)) for cid in self._conv_ids)
                    + ") ORDER BY m.timestamp"
                ).fetchall()
                for r in tag_rows:
                    tagged_index.append({
                        "convId":   f"conv_{r['conversation_id']}",
                        "msgId":    int(r["message_id"]),
                        "ts":       r["timestamp"],
                        "tag":      r["tag"] or "",
                        "note":     r["note"] or "",
                        "preview":  r["preview"] or "",
                        "sender":   "You" if r["from_me"] else (r["sender_name"] or "Unknown"),
                        "fromMe":   bool(r["from_me"]),
                        "type":     r["message_type"],
                    })
                conn2.close()
            except Exception as e:
                # Tag table may not exist on older cases — don't block export
                print(f"[ViewerBundle] tagged-index build failed: {e}")

        # Write manifest.js
        manifest = {
            "exportedAt": int(time.time() * 1000),
            "exporterVersion": "wainsight-viewer-v2.0",
            "title": self._title,
            "caseInfo": self._case_info,
            "totalMessages": total_msgs,
            "conversations": manifest_convs,
            "taggedMessages": tagged_index,
            "isTaggedExport": bool(self._msg_id_filter),
        }
        manifest_js = (
            "/* WAInsight viewer manifest - autogenerated */\n"
            "window.__MANIFEST = "
            + json.dumps(manifest, ensure_ascii=False, default=str)
            + ";\n"
        )
        (base / "data" / "manifest.js").write_text(manifest_js, encoding="utf-8")

        # Copy static assets (CSS + JS inlined into index.html)
        css = (_ASSET_DIR / "viewer.css").read_text(encoding="utf-8")
        js = (_ASSET_DIR / "viewer.js").read_text(encoding="utf-8")
        shell = (_ASSET_DIR / "index.html").read_text(encoding="utf-8")
        title_text = self._title
        shell = (
            shell.replace("__TITLE__", _html.escape(title_text))
                 .replace("__CSS__", css)
                 .replace("__JS__", js)
        )
        (base / "index.html").write_text(shell, encoding="utf-8")

        # README with case info
        readme = self._build_readme(total_msgs, len(manifest_convs))
        (base / "README.txt").write_text(readme, encoding="utf-8")

        # Zip it up.  We always build the folder first (atomic file writes
        # are easier and we want the user to be able to inspect partial
        # state if something fails); when ``_make_zip`` is True we package
        # the folder into a ZIP and then DELETE the folder so the user
        # gets exactly ONE artifact, not "folder + a redundant zip of the
        # same folder" sitting next to each other.
        if self._make_zip:
            self.progress.emit(len(self._conv_ids), len(self._conv_ids),
                              f"Packaging ZIP ({total_msgs:,} msgs)\u2026")
            zip_path = base.with_suffix(".zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for root, _, files in os.walk(base):
                    for f in files:
                        fp = Path(root) / f
                        arc = fp.relative_to(base.parent)
                        zf.write(fp, arc)
            # Remove the intermediate folder \u2014 user asked for a ZIP, give
            # them only the ZIP.  shutil.rmtree handles read-only files
            # on Windows by raising; we wrap it so an unexpected lock
            # (e.g. user has a file open in Explorer) doesn't fail the
            # whole export \u2014 they still got the ZIP.
            try:
                import shutil
                shutil.rmtree(base, ignore_errors=False)
            except Exception as e:
                # Don't fail the export \u2014 surface a warning.
                logger.warning("Could not remove intermediate folder %s: %s", base, e)
            return zip_path
        return base

    # ------------------------------------------------------------------
    # Per-conversation shard writer
    # ------------------------------------------------------------------

    def _write_conversation_shards(
        self, conn: sqlite3.Connection, base: Path, conv_id: int, media_mapping: dict,
    ) -> tuple[list[dict], int]:
        conv_dir = base / "data" / f"conv_{conv_id}"
        conv_dir.mkdir(parents=True, exist_ok=True)

        # Main core query — same shape (but narrower) as the in-app chat_renderer uses.
        # We grab everything needed to render every type.
        q = conn.execute(
            """
            SELECT m.id, m.source_msg_id, m.source_key_id,
                   m.from_me, m.timestamp, m.message_type, m.type_label,
                   COALESCE(m.text_content, '') AS text,
                   m.status, m.is_starred, m.is_forwarded, m.is_edited,
                   m.is_revoked, m.is_view_once, m.view_once_state,
                   m.is_ephemeral, m.ephemeral_duration,
                   m.reply_to_msg_id, m.reply_to_key_id,
                   m.quoted_text, m.quoted_type,
                   m.forward_score, m.last_edit_timestamp, m.edit_count,
                   m.rendered_sender, m.rendered_system_text,
                   m.sender_id,
                   m.is_bot_message,
                   -- Sender display name with bot/unknown fallback.
                   -- Bot messages whose contact wasn't resolved (older
                   -- ingestions before contact_resolver gained @bot
                   -- handling) used to show as "Unknown" — now they
                   -- render as "Meta AI" so the chat is readable
                   -- without requiring a re-ingest.
                   CASE
                     WHEN m.from_me = 1 THEN 'You'
                     WHEN c.is_business_api_bot = 1 THEN
                          COALESCE(NULLIF(c.resolved_name,''), NULLIF(c.display_name,''), 'Meta AI')
                     WHEN m.is_bot_message = 1 AND m.sender_id IS NULL THEN 'Meta AI'
                     ELSE COALESCE(
                          NULLIF(c.resolved_name, ''), NULLIF(c.display_name, ''),
                          CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''
                               THEN '+' || c.phone_number END,
                          NULLIF(c.wa_name, ''), 'Unknown')
                   END AS sender_name,
                   c.phone_number AS sender_phone,
                   c.phone_jid AS sender_jid,
                   c.lid_jid AS sender_lid,
                   c.avatar_blob AS sender_avatar,
                   gm.label AS sender_group_label,
                   gm.role AS sender_group_role,
                   me.file_path, me.resolved_file_path, me.file_exists,
                   me.mime_type, me.file_size, me.file_hash,
                   me.width AS media_width, me.height AS media_height,
                   me.duration_ms, me.media_caption, me.media_name,
                   me.thumbnail_blob, me.page_count,
                   -- Forensic provenance fields: tells the bundle viewer
                   -- whether the file shown for this message was actually
                   -- received on the device (recovery_method=NULL +
                   -- file_exists=1), tool-downloaded post-extraction
                   -- ('downloaded'), or hash-linked from another chat
                   -- ('hash_linked').  The hash_linked case is the
                   -- forensically-tricky one - the file content is
                   -- identical but THIS message never received it.
                   COALESCE(me.recovery_method, '') AS recovery_method,
                   me.media_url AS cdn_url,
                   CASE WHEN me.media_key IS NOT NULL AND LENGTH(me.media_key) > 0
                        THEN 1 ELSE 0 END AS has_key,
                   se.event_label, se.event_data, se.community_name,
                   se.actor_id AS se_actor_id, se.target_id AS se_target_id,
                   loc.latitude, loc.longitude, loc.place_name, loc.place_address,
                   loc.is_live, loc.live_duration,
                   loc.thumbnail_blob AS loc_thumb
            FROM message m
            LEFT JOIN contact c ON c.id = m.sender_id
            LEFT JOIN media me ON me.message_id = m.id
            LEFT JOIN system_event se ON se.message_id = m.id
            LEFT JOIN location loc ON loc.message_id = m.id
            LEFT JOIN group_member gm
                   ON gm.conversation_id = m.conversation_id
                  AND gm.contact_id = m.sender_id
            WHERE m.conversation_id = ?
            ORDER BY m.timestamp ASC, m.sort_id ASC
            """,
            (conv_id,),
        )

        # Auxiliary data: gather lazily (reactions, polls, calls, vcards, receipts)
        reactions_by_msg = self._load_reactions(conn, conv_id)
        polls_by_msg = self._load_polls(conn, conv_id)
        calls_by_msg, attached_call_record_ids = self._load_calls(conn, conv_id)
        vcards_by_msg = self._load_vcards(conn, conv_id)
        links_by_msg = self._load_link_previews(conn, conv_id)
        mentions_by_msg = self._load_mentions(conn, conv_id)
        receipts_by_msg = self._load_receipts(conn, conv_id)
        events_by_msg = self._load_events(conn, conv_id)
        ghosts_by_msg = self._load_ghost_recoveries(conn, conv_id)
        albums_by_parent = self._load_albums(conn, conv_id)
        # Synthetic voice-chat rows — call_records with no message link
        # (pure WhatsApp voice-chats, reconstructed from call_log +
        # call_log_participant_v2).  Emitted later as virtual messages.
        # Skip any call_record that _load_calls already attached to a
        # real (or analyzer-synthesized) message - otherwise the bundle
        # ships TWO entries for the same call: an empty call_log msg row
        # (no participants, just a header) plus the populated vc:N row.
        synth_voice_chats = self._load_synth_voice_chats(
            conn, conv_id, skip_call_record_ids=attached_call_record_ids,
        )

        # ---- Tagged-export early skip ----
        # When the user picked a "tagged-only" / "tagged + buffer"
        # bundle mode, ``self._msg_id_filter[conv_id]`` is the
        # whitelist of message ids that should be INCLUDED.  Without
        # this early skip, ``_build_msg_dict`` runs for every row in
        # the conversation — and its inner ``_copy_media`` call copies
        # the media file to disk for every media-bearing row.  Result:
        # the bundle directory grew to include the FULL media set for
        # the conversation even though only the tagged messages would
        # appear in the JSON shards.  At 13k-msg DealBotz scale that
        # was 13k unnecessary file copies.  The compaction-marker
        # logic below still needs to know how many rows were skipped
        # and what kind they were — we collect that lightweight
        # bucket count here from the raw row, no media copy involved.
        whitelist = self._msg_id_filter.get(conv_id)

        # Pre-collect skipped-row bucket counts so the compaction-pass
        # can build per-kind markers without re-scanning the cursor
        # (which has already been consumed).  Stored as a list of
        # (timestamp, kind) so we can preserve the original ordering
        # when interleaving with the kept messages.
        skipped_meta: list[tuple] = []

        def _row_kind(r) -> str:
            """Cheap bucket classifier for a raw cursor row, matching
            the categories ``_kind_of`` uses for already-built dicts:
            ``system`` (message_type 7), ``call`` (message_type 10 or
            type_label 'call_log'), else ``normal``."""
            mt = r["message_type"] or 0
            if mt == 7:
                return "system"
            tl = (r["type_label"] or "").lower()
            if mt == 10 or tl in ("call_log", "call"):
                return "call"
            return "normal"

        all_msgs: list[dict] = []
        for row in q:
            # Tagged export: skip the heavy build + media copy unless
            # this row's id is in the whitelist.
            if whitelist is not None and row["id"] not in whitelist:
                skipped_meta.append((row["timestamp"], _row_kind(row)))
                continue
            m = self._build_msg_dict(
                row, reactions_by_msg, polls_by_msg, calls_by_msg,
                vcards_by_msg, links_by_msg, mentions_by_msg, receipts_by_msg,
                base, media_mapping,
            )
            if m:
                # Attach scheduled-event data when this message is one.
                ev = events_by_msg.get(row["id"])
                if ev:
                    m["event"] = ev
                    m["type"] = "scheduled_event"
                # Attach ghost-recovery data for revoked messages whose
                # original text we managed to recover from a quoted reply.
                gh = ghosts_by_msg.get(row["id"])
                if gh:
                    m["ghost"] = gh
                all_msgs.append(m)
        # Append synthetic voice-chat messages — call_records that have
        # no msgstore message row (pure voice-chats).  These get a
        # synthetic id distinct from real messages so quote-navigation
        # can't accidentally collide.  Sorted into the timeline by ts.
        for sv in synth_voice_chats:
            all_msgs.append(sv)
        # Sort by timestamp ONLY — ``id`` is mixed type
        # (synthetic rows carry ``"vc:<n>"`` strings, real rows
        # carry int).  A tuple sort would raise
        # ``TypeError: '<' not supported between instances of
        # 'str' and 'int'`` whenever a synthetic voice-chat
        # shared a timestamp with a real message.  Stable sort
        # on a single key preserves DB-order as the tie-break.
        all_msgs.sort(key=lambda x: x.get("ts") or 0)

        # ---- Album collapse pass ----
        # WhatsApp albums (message_type=99) ship as a parent + N children.
        # Without grouping, the parent renders as an empty "Forwarded"
        # badge while the N children render as separate tiles.  Pull the
        # child msg dicts into the parent's `album.children` list, hoist
        # any first-child caption to the parent (so search hits the album
        # via the caption), then strip the child rows from the main
        # stream so they only render inside the parent's grid.
        if albums_by_parent:
            msg_by_id = {
                m["id"]: m for m in all_msgs
                if isinstance(m.get("id"), int)
            }
            child_ids_to_strip: set[int] = set()
            for parent_id, alb in albums_by_parent.items():
                parent_msg = msg_by_id.get(parent_id)
                if not parent_msg:
                    continue
                children_payloads: list[dict] = []
                caption = ""
                for child_id in alb["children_ids"]:
                    cm = msg_by_id.get(child_id)
                    if not cm:
                        continue
                    children_payloads.append(cm)
                    child_ids_to_strip.add(child_id)
                    # First non-empty caption wins (WhatsApp puts the
                    # album caption on the first photo in the upload
                    # order; we mirror that semantic here).
                    if not caption:
                        ct = (cm.get("text") or "").strip()
                        if ct:
                            caption = ct
                # Type-flip parent so the renderer dispatches to album code
                parent_msg["type"] = "album"
                parent_msg["album"] = {
                    "imageCount": alb["image_count"],
                    "videoCount": alb["video_count"],
                    "expectedImageCount": alb["expected_image_count"],
                    "expectedVideoCount": alb["expected_video_count"],
                    "missingImageCount": alb["missing_image_count"],
                    "missingVideoCount": alb["missing_video_count"],
                    "actualChildCount": alb["actual_child_count"],
                    "note": alb["note"] or "",
                    "caption": caption,
                    "children": children_payloads,
                }
                # Bubble caption to parent.text so global search hits it.
                if caption and not (parent_msg.get("text") or "").strip():
                    parent_msg["text"] = caption
            if child_ids_to_strip:
                all_msgs = [
                    m for m in all_msgs
                    if not (isinstance(m.get("id"), int)
                            and m["id"] in child_ids_to_strip)
                ]

        # Second pass: resolve quoted senders using our in-set id map
        id_to_sender = {m["id"]: m.get("senderName", "") for m in all_msgs}
        for m in all_msgs:
            q = m.get("quoted")
            if q and q.get("parentId") and not q.get("from"):
                q["from"] = id_to_sender.get(q["parentId"], "")

        # System-event enrichment: resolve actor/target IDs to "Name (+phone)"
        # strings and run each system msg through the shared formatter so
        # bundle output matches the in-app exactly (advanced chat privacy,
        # "+ X added You (Owner: …)", etc.).
        sys_msgs = [m for m in all_msgs if m.get("__se")]
        if sys_msgs:
            try:
                from shared.system_event_formatter import build_system_text
                # Collect actor/target IDs across this conv
                cids = set()
                for m in sys_msgs:
                    se = m["__se"]
                    if se.get("se_actor_id"): cids.add(se["se_actor_id"])
                    if se.get("se_target_id"): cids.add(se["se_target_id"])
                contact_label: dict[int, str] = {}
                if cids:
                    qmarks = ",".join("?" * len(cids))
                    label_sql = (
                        f"SELECT id, "
                        f" CASE "
                        f"   WHEN display_name IS NOT NULL AND display_name != ''"
                        f"        AND phone_number IS NOT NULL AND phone_number != ''"
                        f"   THEN display_name || ' (+' || phone_number || ')' "
                        f"   WHEN display_name IS NOT NULL AND display_name != '' THEN display_name "
                        f"   WHEN wa_name IS NOT NULL AND wa_name != ''"
                        f"        AND phone_number IS NOT NULL AND phone_number != ''"
                        f"   THEN '~' || wa_name || ' (+' || phone_number || ')' "
                        f"   WHEN wa_name IS NOT NULL AND wa_name != '' THEN '~' || wa_name "
                        f"   WHEN phone_number IS NOT NULL AND phone_number != '' THEN '+' || phone_number "
                        f"   ELSE 'Unknown' END "
                        f"FROM contact WHERE id IN ({qmarks})"
                    )
                    for r in conn.execute(label_sql, tuple(cids)).fetchall():
                        contact_label[r[0]] = r[1] or "Unknown"
                conv_name = conv_row["title"] or ""
                chat_type = conv_row["chat_type"] or "personal"
                for m in sys_msgs:
                    se = m.pop("__se")
                    se["system_event_actor"] = contact_label.get(se.get("se_actor_id") or 0, "")
                    se["system_event_target"] = contact_label.get(se.get("se_target_id") or 0, "")
                    try:
                        rendered = build_system_text(
                            se,
                            owner_phone=self._owner_phone,
                            owner_name=self._owner_name,
                            owner_contact_id=self._owner_contact_id,
                            conv_name=conv_name,
                            chat_type=chat_type,
                        )
                        if rendered and rendered.strip():
                            m["system_text"] = rendered
                    except Exception:
                        pass  # leave the pre-filled rendered_system_text
            except Exception:
                # Formatter import failed - leave system_text as-is.
                for m in sys_msgs:
                    m.pop("__se", None)

        # ---- Per-conversation message-id filter (compaction pass) ----
        # When this conversation has an explicit whitelist (tagged-msg
        # export modes), keep only the whitelisted messages and insert
        # synthetic ``__compaction__`` markers showing how many were
        # collapsed between each pair of kept rows.
        #
        # The marker carries a per-kind breakdown so the analyst sees
        # what was hidden — normal chat messages, system events
        # (group membership / privacy / disappearing / etc), and
        # call entries are counted separately.  This matters
        # forensically: 50 hidden chat messages vs. 50 hidden
        # admin actions are very different gaps.
        #
        # Album children carried inside an album parent's payload
        # are never dropped by the filter (they ride along with
        # their parent); this loop only operates at the top-level
        # message_list scope.
        def _kind_of(msg: dict) -> str:
            """Bucket a message into 'normal' | 'system' | 'call' for
            the compaction breakdown."""
            if msg.get("system") or msg.get("type") == "system":
                return "system"
            tl = msg.get("type") or msg.get("type_label") or ""
            if tl in ("call_log", "call"):
                return "call"
            return "normal"

        if whitelist is not None:
            # Merge the kept messages with the skipped-row metadata in
            # timestamp order, emitting a single ``__compaction__``
            # marker for each maximal run of skipped rows between two
            # kept rows (or before the first / after the last kept).
            #
            # Both inputs are already in DB-cursor order which is
            # timestamp-asc for our SELECT — but we still merge by ts
            # explicitly, because synthetic voice-chat rows + album
            # collapse can introduce mixed ordering.  ``key=ts`` is
            # stable on the kept side so an equal-timestamp tie keeps
            # the kept row visible (the analyst would expect that).
            #
            # ``skipped_meta`` items are (ts, kind) tuples — produced
            # by the main loop above without ever calling
            # ``_build_msg_dict`` and therefore without copying any
            # media.  The skipped entries never reach the bundle —
            # only the per-kind run total does, via the marker.
            kept_sorted = sorted(all_msgs, key=lambda m: m.get("ts") or 0)
            skipped_sorted = sorted(skipped_meta, key=lambda t: t[0] or 0)

            merged: list[dict] = []
            run = {"normal": 0, "system": 0, "call": 0}
            run_start_ts = None
            run_end_ts = None

            def _flush_run():
                total = sum(run.values())
                if total == 0:
                    return
                merged.append({
                    "id":              f"__c:{len(merged)}",
                    "type":            "__compaction__",
                    "ts":              run_end_ts or run_start_ts,
                    "skipped":         total,
                    "skipped_normal":  run["normal"],
                    "skipped_system":  run["system"],
                    "skipped_call":    run["call"],
                    "skipped_from":    run_start_ts,
                    "skipped_to":      run_end_ts,
                })
                run["normal"] = run["system"] = run["call"] = 0

            i = j = 0
            while i < len(kept_sorted) or j < len(skipped_sorted):
                k_ts = kept_sorted[i].get("ts") or 0 if i < len(kept_sorted) else None
                s_ts = skipped_sorted[j][0] or 0 if j < len(skipped_sorted) else None
                # Advance whichever side is earlier (kept wins ties so
                # the visible message anchors the compaction marker).
                take_kept = (
                    s_ts is None
                    or (k_ts is not None and k_ts <= s_ts)
                )
                if take_kept:
                    _flush_run()
                    run_start_ts = run_end_ts = None
                    merged.append(kept_sorted[i])
                    i += 1
                else:
                    if sum(run.values()) == 0:
                        run_start_ts = s_ts
                    run_end_ts = s_ts
                    run[skipped_sorted[j][1]] += 1
                    j += 1
            _flush_run()  # trailing run after the last kept message
            all_msgs = merged

        # Write shards
        shards = []
        if not all_msgs:
            return shards, 0
        for shard_idx in range(0, len(all_msgs), SHARD_SIZE):
            batch = all_msgs[shard_idx:shard_idx + SHARD_SIZE]
            shard_num = shard_idx // SHARD_SIZE
            fname = f"shard-{shard_num:04d}.js"
            shard_key = f"{shard_num:04d}"
            js_body = (
                f"window.__SHARD(\"conv_{conv_id}/{shard_key}\","
                + json.dumps(batch, ensure_ascii=False, default=str)
                + ");\n"
            )
            (conv_dir / fname).write_text(js_body, encoding="utf-8")
            shards.append({
                "key": shard_key,
                "path": f"data/conv_{conv_id}/{fname}",
                "count": len(batch),
            })

        return shards, len(all_msgs)

    # ------------------------------------------------------------------
    # Row → viewer-friendly dict
    # ------------------------------------------------------------------

    def _build_msg_dict(
        self, row: sqlite3.Row,
        reactions_by_msg, polls_by_msg, calls_by_msg,
        vcards_by_msg, links_by_msg, mentions_by_msg, receipts_by_msg,
        base: Path, media_mapping: dict,
    ) -> Optional[dict]:
        type_label = (row["type_label"] or "").lower()
        mt = row["message_type"] or 0

        # Normalize type
        vtype = _map_type(type_label, mt, row)

        # Sender DP (inline base64 for portability)
        sender_avatar_b64 = ""
        try:
            if row["sender_avatar"]:
                sender_avatar_b64 = ("data:image/jpeg;base64," +
                    base64.b64encode(row["sender_avatar"]).decode("ascii"))
        except Exception:
            pass

        # ``sender_name`` already handles the bot fallback at SQL level,
        # but we also expose the bot flag separately so the renderer can
        # apply special UI (e.g. a Meta-AI badge / avatar).
        is_bot = bool(row["is_bot_message"])
        msg: dict = {
            "id": row["id"],
            "sourceMsgId": row["source_msg_id"],
            "keyId": row["source_key_id"],
            "ts": row["timestamp"],
            "fromMe": bool(row["from_me"]),
            "type": vtype,
            "typeLabel": type_label,
            "senderName": row["sender_name"] if not row["from_me"] else "You",
            "senderPhone": row["sender_phone"] or "",
            "senderJid": row["sender_jid"] or "",
            "senderLid": row["sender_lid"] or "",
            "senderGroupLabel": (
                # group_member.label for this sender in this
                # group; empty for personal chats.
                row["sender_group_label"] if "sender_group_label" in row.keys() else ""
            ) or "",
            "senderGroupRole": (
                row["sender_group_role"] if "sender_group_role" in row.keys() else ""
            ) or "",
            "isBot": is_bot,
            "senderAvatar": sender_avatar_b64,
            "text": row["text"] or "",
            "status": _status_label(row["status"]),
            "starred": bool(row["is_starred"]),
            "edited": bool(row["is_edited"]),
            "editCount": row["edit_count"] or 0,
            "lastEditTs": row["last_edit_timestamp"],
            "revoked": bool(row["is_revoked"]),
            "viewOnce": bool(row["is_view_once"]),
            "ephemeral": bool(row["is_ephemeral"]),
            "forwardScore": row["forward_score"] or 0,
            "replyToMsgId": row["reply_to_msg_id"],
            "replyToKeyId": row["reply_to_key_id"],
        }

        # Revoked / deleted overrides type
        if row["is_revoked"]:
            msg["type"] = "revoked"

        # System message - run through the shared formatter so the bundle
        # gets the SAME rich text the in-app shows.  Owner name/phone come
        # from case_metadata; actor/target are resolved by ID via the
        # conversation-scoped contact map (built once per conv, see
        # _resolve_se_actors_targets).  Falls back to rendered_system_text
        # when the formatter has nothing to add.
        if vtype == "system":
            msg["system"] = True
            msg["eventLabel"] = row["event_label"] or ""
            try:
                from shared.system_event_formatter import build_system_text
                # Stage the data the formatter expects.  Actor/target name
                # strings are filled in later in a batch pass; for now we
                # have IDs and the raw label/data.
                msg["__se"] = {
                    "system_event_label": row["event_label"] or "",
                    "system_event_data": row["event_data"] or "",
                    "se_actor_id": row["se_actor_id"],
                    "se_target_id": row["se_target_id"],
                    "message_type": row["message_type"],
                    "type_label": row["type_label"] or "",
                    "from_me": bool(row["from_me"]),
                    "display_text": row["rendered_system_text"] or "",
                    "text_content": row["text"] or "",
                    "community_name": row["community_name"] or "",
                }
                # Pre-fill with rendered_system_text or text - finalised
                # later once actor/target names are resolved.
                msg["system_text"] = row["rendered_system_text"] or row["text"] or ""
            except Exception:
                msg["system_text"] = row["rendered_system_text"] or row["text"] or ""
            return msg

        # Quoted reply — sender name is filled in a second pass after we know
        # all message IDs (parent may or may not be in the exported set).
        if row["quoted_text"] or row["reply_to_key_id"]:
            msg["quoted"] = {
                "preview": row["quoted_text"] or "",
                "type": _map_quoted_type(row["quoted_type"]),
                "parentId": row["reply_to_msg_id"],
                "parentKey": row["reply_to_key_id"] or "",
                "from": "",  # filled below after all msgs loaded
            }

        # Media
        if vtype in ("image", "video", "gif", "sticker", "voice", "ptt", "audio",
                     "document") or row["resolved_file_path"]:
            media = {
                "mime": row["mime_type"] or "",
                "size": row["file_size"] or 0,
                "width": row["media_width"] or 0,
                "height": row["media_height"] or 0,
                "duration": (row["duration_ms"] or 0) / 1000.0,
                "caption": row["media_caption"] or "",
                "name": row["media_name"] or "",
                "pages": row["page_count"] or 0,
                "isViewOnce": bool(row["is_view_once"]),
                # Forensic provenance - the bundle viewer uses these to
                # avoid implying a hash-linked or tool-recovered file
                # was the original receipt for THIS message.
                "fileHash": row["file_hash"] or "",
                "fileExists": bool(row["file_exists"]),
                "recoveryMethod": (
                    row["recovery_method"]
                    if "recovery_method" in row.keys() else ""
                ) or "",
                "cdnUrl": (
                    row["cdn_url"]
                    if "cdn_url" in row.keys() else ""
                ) or "",
                "hasKey": bool(
                    row["has_key"] if "has_key" in row.keys() else 0
                ),
            }
            # Inline thumbnail (base64) — useful when file isn't on disk
            tb = row["thumbnail_blob"]
            if tb:
                try:
                    media["thumbnail"] = ("data:image/jpeg;base64,"
                                          + base64.b64encode(tb).decode("ascii"))
                except Exception:
                    pass
            # Copy media file into bundle (extension derived from mime when missing)
            rp = row["resolved_file_path"] or row["file_path"]
            if rp and self._include_media:
                bundled = _copy_media(rp, row["file_hash"], base, media_mapping,
                                      mime=row["mime_type"])
                if bundled:
                    media["path"] = bundled
            # Download filename — use the NAME WhatsApp gave it on disk:
            # IMG-YYYYMMDD-WAnnnn.jpg, VID-YYYYMMDD-WAnnnn.mp4, PTT-...,
            # AUD-..., DOC-..., STK-..., etc.
            # Priority: msgstore file_path basename (always present, preserves
            # WhatsApp's naming convention even if the file was never copied
            # to disk) → resolved path basename → media_name (useful for docs
            # where WhatsApp preserves the original filename) → generated.
            raw_basename = ""
            if row["file_path"]:
                raw_basename = os.path.basename(
                    row["file_path"].replace("\\", "/"))
            if not raw_basename and rp:
                raw_basename = os.path.basename(rp.replace("\\", "/"))
            doc_name = (row["media_name"] or "").strip()
            ext_map = {
                "image/jpeg": ".jpg", "image/png": ".png",
                "image/webp": ".webp", "image/gif": ".gif",
                "video/mp4": ".mp4", "video/webm": ".webm",
                "video/3gpp": ".3gp",
                "audio/ogg": ".opus", "audio/mpeg": ".mp3",
                "audio/amr": ".amr", "audio/wav": ".wav",
                "audio/aac": ".m4a",
                "application/pdf": ".pdf", "application/zip": ".zip",
                "application/vnd.android.package-archive": ".apk",
            }
            mime_main = (row["mime_type"] or "").split(";")[0].strip()
            ext = ext_map.get(mime_main, "")

            def _needs_ext(name: str) -> bool:
                # Extension present AND non-empty after the last dot
                if "." not in name:
                    return True
                tail = name.rsplit(".", 1)[-1]
                return not tail or len(tail) > 5 or not tail.isalnum()

            if raw_basename:
                dn = raw_basename
                # Safety: if WhatsApp path has no extension (e.g. "DOC-...-WA0064.")
                # append the mime-derived ext AND strip the trailing dot.
                if _needs_ext(dn) and ext:
                    dn = dn.rstrip(".") + ext
            elif doc_name:
                # Documents: real filename is in media_name
                dn = doc_name
                if _needs_ext(dn) and ext:
                    dn = dn.rstrip(".") + ext
            else:
                # Last-resort synthesis: prefix by type so it sorts nicely
                t_prefix = {
                    "image/jpeg": "IMG", "image/png": "IMG", "image/webp": "STK",
                    "image/gif": "GIF",
                    "video/mp4": "VID", "video/webm": "VID",
                    "audio/ogg": "PTT", "audio/mpeg": "AUD",
                    "application/pdf": "DOC",
                }.get(mime_main, "WA")
                dn = f"{t_prefix}-{row['source_msg_id'] or row['id']}{ext or '.bin'}"
            media["downloadName"] = dn
            msg["media"] = media

        # Location
        if row["latitude"] is not None and row["longitude"] is not None:
            loc = {
                "lat": row["latitude"],
                "lng": row["longitude"],
                "name": row["place_name"] or "",
                "address": row["place_address"] or "",
                "live": bool(row["is_live"]),
            }
            if row["loc_thumb"]:
                try:
                    loc["mapImage"] = ("data:image/png;base64,"
                                       + base64.b64encode(row["loc_thumb"]).decode("ascii"))
                except Exception:
                    pass
            msg["location"] = loc
            if msg["type"] not in ("location", "live_location"):
                msg["type"] = "live_location" if loc["live"] else "location"

        # Reactions
        rs = reactions_by_msg.get(row["id"])
        if rs:
            msg["reactions"] = rs

        # Poll
        pv = polls_by_msg.get(row["id"])
        if pv:
            msg["poll"] = pv
            msg["type"] = "poll"

        # Call
        cv = calls_by_msg.get(row["id"])
        if cv:
            msg["call"] = cv["call"]
            msg["isSynthesized"] = cv.get("synthesized", False)
            msg["type"] = "voice_chat" if cv.get("synthesized") else "call"

        # vCard(s).  ``vc`` is a LIST — a single contact share has length
        # 1, a vcard_list share (e.g. 24 contacts) has length 24.  The
        # viewer renders all of them.  We also expose ``vcard`` (the
        # first card) for backward compat with the older single-card
        # renderer path that some older exports still ship.
        vc = vcards_by_msg.get(row["id"])
        if vc:
            msg["vcards"] = vc
            msg["vcard"] = vc[0] if vc else {}
            if msg["type"] not in ("vcard",):
                msg["type"] = "vcard"

        # Link preview (pick the first if multiple)
        lp = links_by_msg.get(row["id"])
        if lp:
            msg["linkPreview"] = lp[0]

        # Mentions
        mn = mentions_by_msg.get(row["id"])
        if mn:
            msg["mentions"] = mn

        # Receipts (for messages from_me only, in group/personal alike)
        rc = receipts_by_msg.get(row["id"])
        if rc and (rc["delivered"] or rc["read"]):
            msg["receipts"] = rc

        return msg

    # ------------------------------------------------------------------
    # Auxiliary loaders (batch, one query per table)
    # ------------------------------------------------------------------

    def _load_reactions(self, conn, conv_id):
        """Per-message reaction detail: emoji + reactor name + phone + timestamp."""
        out: dict[int, list] = {}
        try:
            rows = conn.execute(
                "SELECT r.message_id, r.emoji, r.from_me,"
                " COALESCE(NULLIF(rc.resolved_name,''), NULLIF(rc.display_name,''),"
                "          CASE WHEN rc.phone_number IS NOT NULL AND rc.phone_number != ''"
                "               THEN '+' || rc.phone_number END, 'Unknown') AS reactor_name,"
                " rc.phone_number AS reactor_phone,"
                " r.timestamp"
                " FROM reaction r"
                " JOIN message m ON m.id = r.message_id"
                " LEFT JOIN contact rc ON rc.id = r.reactor_id"
                " WHERE m.conversation_id = ?"
                " ORDER BY r.message_id, r.timestamp",
                (conv_id,),
            ).fetchall()
        except Exception:
            return out
        by_msg: dict[int, dict] = {}
        for r in rows:
            buckets = by_msg.setdefault(r["message_id"], {})
            b = buckets.setdefault(r["emoji"],
                {"emoji": r["emoji"], "count": 0, "from": [], "detail": []})
            b["count"] += 1
            nm = "You" if r["from_me"] else r["reactor_name"]
            if nm not in b["from"]:
                b["from"].append(nm)
            b["detail"].append({
                "name": nm,
                "phone": r["reactor_phone"] or "",
                "ts": r["timestamp"] or 0,
                "fromMe": bool(r["from_me"]),
            })
        for mid, buckets in by_msg.items():
            out[mid] = list(buckets.values())
        return out

    def _load_receipts(self, conn, conv_id):
        """Per-message delivery / read / played receipts (outgoing messages only).

        Forensic detail: WhatsApp stores delivered timestamps in TWO
        places.  The per-USER ``receipt_user`` table holds a delivered
        timestamp only when the device records a distinct delivery
        event before the read event.  When a recipient reads the
        message immediately on receipt, msgstore frequently leaves
        ``receipt_user.receipt_timestamp`` NULL and writes the per-DEVICE
        delivery confirmation to ``receipt_device`` instead — that's the
        timestamp WhatsApp's own "Message info" UI shows.

        We mirror WhatsApp's behaviour: derive each recipient's
        delivered timestamp as ``MIN`` of:
          * ``receipt.delivered_ts`` (the user-level row, when present)
          * ``MIN(receipt_device_record.receipt_ts)`` over every device
            that maps to the same contact

        Without this merge, recipients whose devices reported delivery
        but whose user-row never got a ``delivered_ts`` (a common
        offline-read pattern) appear as "no Delivered timestamp" in the
        per-recipient detail panel — directly contradicting what the
        WhatsApp app shows for the same message.
        """
        out: dict[int, dict] = {}

        # Step 1: pull the per-user rows (read_ts, played_ts always come
        # from here; delivered_ts is the user-level value when present).
        try:
            rows = conn.execute(
                "SELECT r.message_id,"
                "       r.recipient_id,"
                "       NULLIF(c.resolved_name, '')  AS resolved_name,"
                "       NULLIF(c.display_name, '')   AS display_name,"
                "       NULLIF(c.wa_name, '')        AS wa_name,"
                "       c.phone_number               AS recipient_phone,"
                "       c.phone_jid                  AS recipient_jid,"
                "       COALESCE(c.is_saved, 0)      AS is_saved,"
                "       r.delivered_ts, r.read_ts, r.played_ts"
                "  FROM receipt r"
                "  JOIN message m ON m.id = r.message_id"
                "  LEFT JOIN contact c ON c.id = r.recipient_id"
                " WHERE m.conversation_id = ? AND m.from_me = 1",
                (conv_id,),
            ).fetchall()
        except Exception:
            return out

        # Step 2: pull the device-level delivered timestamps for the
        # same conversation, collapsed to MIN per (message_id, contact).
        # ``receipt_device_record`` is keyed by device_jid_row_id and
        # carries device_contact_id pre-resolved by ingestion.
        per_recipient_device_min: dict[tuple[int, int], int] = {}
        try:
            dev_rows = conn.execute(
                "SELECT rdr.message_id, rdr.device_contact_id,"
                "       MIN(rdr.receipt_ts) AS first_dev_ts"
                "  FROM receipt_device_record rdr"
                "  JOIN message m ON m.id = rdr.message_id"
                " WHERE m.conversation_id = ? AND m.from_me = 1"
                "   AND rdr.device_contact_id IS NOT NULL"
                "   AND rdr.receipt_ts IS NOT NULL"
                " GROUP BY rdr.message_id, rdr.device_contact_id",
                (conv_id,),
            ).fetchall()
            for d in dev_rows:
                per_recipient_device_min[(d["message_id"], d["device_contact_id"])] = d["first_dev_ts"]
        except Exception:
            # receipt_device_record may not exist on very old analysis DBs —
            # just continue with the user-level rows alone.
            pass

        for r in rows:
            rec = out.setdefault(r["message_id"], {"delivered": [], "read": [], "played": []})

            # Build the display name following WhatsApp's own
            # convention:
            #   * SAVED contacts (``contact.is_saved = 1``) →
            #     the resolved / display name as-is.
            #   * UNSAVED contacts → ``~<wa_name>`` if a
            #     ``wa_name`` is known (matches WhatsApp's
            #     ``~John`` rendering for non-saved pushnames),
            #     otherwise the phone number with a ``+`` prefix.
            # Always populate the full phone in the ``phone``
            # field so the per-recipient detail panel can show
            # ``<name>  +<phone>`` alongside the display name.
            phone = r["recipient_phone"] or ""
            jid = r["recipient_jid"] or ""
            is_saved = bool(r["is_saved"])
            if is_saved:
                name = (r["resolved_name"] or r["display_name"]
                        or r["wa_name"]
                        or (("+" + phone) if phone else "")
                        or "Unknown")
            else:
                # WhatsApp renders unsaved contacts with a leading "~".
                wa = r["wa_name"]
                if wa:
                    name = "~" + wa
                elif phone:
                    name = "+" + phone
                else:
                    name = "Unknown"

            # Merged delivered timestamp: prefer the earliest of the
            # user-level row and the earliest device-level row for the
            # same recipient.  If neither exists, delivered_ts stays None.
            user_delivered = r["delivered_ts"]
            device_delivered = per_recipient_device_min.get(
                (r["message_id"], r["recipient_id"])
            )
            candidates = [t for t in (user_delivered, device_delivered) if t]
            merged_delivered = min(candidates) if candidates else None

            if merged_delivered:
                rec["delivered"].append({
                    "name": name, "phone": phone, "jid": jid,
                    "isSaved": is_saved,
                    "ts": merged_delivered,
                    "fromDevice": (user_delivered is None and device_delivered is not None),
                })
            if r["read_ts"]:
                rec["read"].append({
                    "name": name, "phone": phone, "jid": jid,
                    "isSaved": is_saved,
                    "ts": r["read_ts"],
                })
            if r["played_ts"]:
                rec["played"].append({
                    "name": name, "phone": phone, "jid": jid,
                    "isSaved": is_saved,
                    "ts": r["played_ts"],
                })

        for _mid, rec in out.items():
            if rec["delivered"]:
                rec["firstDelivered"] = min(x["ts"] for x in rec["delivered"])
            if rec["read"]:
                rec["firstRead"] = min(x["ts"] for x in rec["read"])
        return out

    def _load_polls(self, conn, conv_id):
        """Polls + every option's vote count and voter list.

        Schema notes: the poll table has ``selectable_count`` and
        the option table has ``option_name`` / ``vote_total``.
        The poll question itself lives in
        ``message.text_content``, not on the poll row.
        """
        out: dict[int, dict] = {}
        try:
            rows = conn.execute(
                "SELECT p.id AS poll_id, p.message_id, p.selectable_count,"
                "       m.text_content AS question,"
                "       po.option_index, po.option_name, po.vote_total,"
                "       po.voter_names"
                "  FROM poll p"
                "  JOIN message m ON m.id = p.message_id"
                "  LEFT JOIN poll_option po ON po.poll_id = p.id"
                " WHERE m.conversation_id = ?"
                " ORDER BY p.message_id, po.option_index",
                (conv_id,),
            ).fetchall()
        except Exception:
            return out
        for r in rows:
            p = out.setdefault(r["message_id"], {
                "question": r["question"] or "",
                # ``selectable_count`` = 1 single-choice, >1 multi-choice
                "multi": (r["selectable_count"] or 0) > 1,
                "options": [],
                "totalVotes": 0,
            })
            if r["option_name"] is not None:
                votes = r["vote_total"] or 0
                # Parse the comma-separated voter list (Python ingestion
                # writes "Name (+phone), Name2 (+phone2)").  We expose
                # voters as an array so the renderer can list them.
                voter_names = (r["voter_names"] or "").strip()
                voters = [v.strip() for v in voter_names.split(",")] if voter_names else []
                p["options"].append({
                    "text": r["option_name"],
                    "votes": votes,
                    "voters": voters,
                })
                p["totalVotes"] += votes
        return out

    def _load_albums(self, conn, conv_id):
        """Album parent metadata + ordered child message ids per parent.

        Returns a dict keyed by parent message_id (analysis.id):
            {
                parent_id: {
                    "image_count": int, "video_count": int,
                    "expected_image_count": int|None,
                    "expected_video_count": int|None,
                    "missing_image_count": int, "missing_video_count": int,
                    "actual_child_count": int,
                    "note": str|None,
                    "children_ids": [child_id1, child_id2, ...] in sort order
                }
            }

        Only association_type=2 children (album members) are pulled - the
        other types (4/6/7/11/12) represent different relationships that
        the renderer doesn't currently collapse.

        Returns {} when message_album/message_association don't exist
        (older analysis.dbs without album backfill applied).
        """
        out: dict[int, dict] = {}
        try:
            album_rows = conn.execute(
                "SELECT message_id, image_count, video_count, "
                "       expected_image_count, expected_video_count, "
                "       missing_image_count, missing_video_count, "
                "       actual_child_count, note "
                "FROM message_album ma "
                "JOIN message m ON m.id = ma.message_id "
                "WHERE m.conversation_id = ?",
                (conv_id,),
            ).fetchall()
        except Exception:
            return out
        if not album_rows:
            return out
        for r in album_rows:
            out[r["message_id"]] = {
                "image_count": r["image_count"] or 0,
                "video_count": r["video_count"] or 0,
                "expected_image_count": r["expected_image_count"],
                "expected_video_count": r["expected_video_count"],
                "missing_image_count": r["missing_image_count"] or 0,
                "missing_video_count": r["missing_video_count"] or 0,
                "actual_child_count": r["actual_child_count"] or 0,
                "note": r["note"],
                "children_ids": [],
            }
        try:
            child_rows = conn.execute(
                "SELECT a.parent_message_id, a.child_message_id, a.sort_order "
                "FROM message_association a "
                "JOIN message m ON m.id = a.parent_message_id "
                "WHERE m.conversation_id = ? AND a.association_type = 2 "
                "ORDER BY a.parent_message_id, a.sort_order",
                (conv_id,),
            ).fetchall()
        except Exception:
            child_rows = []
        for r in child_rows:
            pid = r["parent_message_id"]
            if pid in out:
                out[pid]["children_ids"].append(r["child_message_id"])
        return out

    def _load_ghost_recoveries(self, conn, conv_id):
        """Recovered text + sender for deleted-for-everyone
        (revoke) messages.

        ``ghost_message`` holds entries reconstructed from a
        later quoted reply that preserved the original text:
        even though the sender deleted the message, the reply
        still carries what was said.  Surfacing these in the
        viewer bundle preserves the forensically valuable
        evidence that would otherwise render as a bare
        "deleted" placeholder.
        """
        out: dict[int, dict] = {}
        try:
            rows = conn.execute(
                "SELECT g.revoked_msg_id, g.recovered_from_msg_id,"
                "       g.original_text, g.original_type,"
                "       g.recovery_method,"
                "       COALESCE(NULLIF(c.resolved_name,''), NULLIF(c.display_name,''),"
                "                NULLIF(c.wa_name,''),"
                "                CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''"
                "                     THEN '+' || c.phone_number END,"
                "                'Unknown') AS sender_name"
                "  FROM ghost_message g"
                "  LEFT JOIN contact c ON c.id = g.original_sender_id"
                " WHERE g.conversation_id = ?",
                (conv_id,),
            ).fetchall()
        except Exception:
            return out
        for r in rows:
            out[r["revoked_msg_id"]] = {
                "recoveredText": r["original_text"] or "",
                "originalType":  r["original_type"] or "text",
                "recoveryMethod": r["recovery_method"] or "",
                "originalSender": r["sender_name"] or "Unknown",
                "fromMsgId": r["recovered_from_msg_id"],
            }
        return out

    def _load_synth_voice_chats(self, conn, conv_id, skip_call_record_ids=None):
        """Voice-chat call_records that have no corresponding ``message``
        row — emitted as synthetic message rows so the timeline shows
        them.  Pure WhatsApp voice-chats are reconstructed entirely
        from ``call_log`` + ``call_log_participant_v2``; msgstore
        doesn't write a real message for them.

        Each synthetic row carries a string id ``"vc:<call_record_id>"``
        so it can never collide with a real ``message.id`` (which are
        integers).  ``isSynthetic`` is exposed so the renderer can show
        a "reconstructed" badge.

        ``skip_call_record_ids`` lists call_records that were already
        attached to a real message by ``_load_calls`` - emitting them
        again here would produce duplicate cards in the timeline.
        """
        skip = set(skip_call_record_ids or ())
        synthetic: list[dict] = []
        try:
            cr_rows = conn.execute(
                "SELECT cr.id AS call_record_id, cr.timestamp, cr.duration_sec,"
                "       cr.is_video, cr.from_me, cr.contact_id, cr.call_category,"
                "       COALESCE(NULLIF(c.resolved_name,''), NULLIF(c.display_name,''),"
                "                NULLIF(c.wa_name,''),"
                "                CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''"
                "                     THEN '+' || c.phone_number END,"
                "                'Unknown') AS creator_name,"
                "       c.phone_number AS creator_phone,"
                "       c.phone_jid AS creator_jid"
                "  FROM call_record cr"
                "  LEFT JOIN contact c ON c.id = cr.contact_id"
                " WHERE cr.conversation_id = ? AND cr.call_category = 'voice_chat'",
                (conv_id,),
            ).fetchall()
            cr_rows = [r for r in cr_rows if r["call_record_id"] not in skip]
        except Exception:
            return synthetic
        # Pull participant lists per voice-chat record (joined / declined
        # statuses are reconstructed from call_log_participant_v2).
        part_by_call: dict[int, list] = {}
        try:
            part_rows = conn.execute(
                "SELECT cp.call_id, cp.call_result,"
                "       COALESCE(NULLIF(c.resolved_name,''), NULLIF(c.display_name,''),"
                "                NULLIF(c.wa_name,''),"
                "                CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''"
                "                     THEN '+' || c.phone_number END,"
                "                'Unknown') AS name,"
                "       c.phone_number AS phone"
                "  FROM call_participant cp"
                "  JOIN call_record cr ON cr.id = cp.call_id"
                "  LEFT JOIN contact c ON c.id = cp.contact_id"
                " WHERE cr.conversation_id = ? AND cr.call_category = 'voice_chat'",
                (conv_id,),
            ).fetchall()
            _RESULT_MAP = {0: "missed", 1: "joined", 2: "declined", 3: "ringing"}
            for p in part_rows:
                part_by_call.setdefault(p["call_id"], []).append({
                    "name": p["name"] or "Unknown",
                    "phone": p["phone"] or "",
                    "status": _RESULT_MAP.get(p["call_result"], ""),
                })
        except Exception:
            pass
        # Build synthetic message rows.
        for r in cr_rows:
            synthetic.append({
                "id": "vc:" + str(r["call_record_id"]),
                "sourceMsgId": None,
                "keyId": None,
                "ts": r["timestamp"],
                "fromMe": bool(r["from_me"]),
                "type": "voice_chat",
                "typeLabel": "voice_chat",
                "senderName": ("You" if r["from_me"]
                               else (r["creator_name"] or "Unknown")),
                "senderPhone": r["creator_phone"] or "",
                "senderJid": r["creator_jid"] or "",
                "senderLid": "",
                "isBot": False,
                "text": "",
                "isSynthetic": True,
                "call": {
                    "duration": r["duration_sec"] or 0,
                    "video": bool(r["is_video"]),
                    "group": True,             # voice-chats are always group
                    "result": "joined",
                    "result_code": 1,
                    "participants": part_by_call.get(r["call_record_id"], []),
                },
                "synthesized": True,
                # Keep these so common renderer code paths don't crash:
                "media": {},
                "edited": False,
                "starred": False,
                "revoked": False,
                "viewOnce": False,
                "ephemeral": False,
                "forwardScore": 0,
                "replyToMsgId": None,
                "replyToKeyId": None,
                "status": "",
                "editCount": 0,
                "lastEditTs": None,
                "isRevoked": False,
            })
        return synthetic

    def _load_events(self, conn, conv_id):
        """Scheduled-event data (calendar invites, scheduled calls).

        The bundle viewer renders an event card with name,
        start / end time, location, join link and cancellation
        state.  Without this load the export would show only a
        bare placeholder.
        """
        out: dict[int, dict] = {}
        try:
            rows = conn.execute(
                "SELECT message_id, name, description, start_time, end_time,"
                " location_name, location_address, location_latitude, location_longitude,"
                " join_link, is_schedule_call, is_canceled, event_state,"
                " has_reminder, reminder_offset_sec"
                "  FROM scheduled_event se"
                "  JOIN message m ON m.id = se.message_id"
                " WHERE m.conversation_id = ?",
                (conv_id,),
            ).fetchall()
        except Exception:
            return out
        for r in rows:
            out[r["message_id"]] = {
                "name":         r["name"] or "",
                "description":  r["description"] or "",
                "startTs":      r["start_time"],
                "endTs":        r["end_time"],
                "locationName": r["location_name"] or "",
                "locationAddr": r["location_address"] or "",
                "lat":          r["location_latitude"],
                "lng":          r["location_longitude"],
                "joinLink":     r["join_link"] or "",
                "isCall":       bool(r["is_schedule_call"]),
                "isCanceled":   bool(r["is_canceled"]),
                "state":        r["event_state"],
                "reminder":     bool(r["has_reminder"]),
                "reminderOffsetSec": r["reminder_offset_sec"],
            }
        return out

    def _load_calls(self, conn, conv_id):
        """Call records with per-participant join / leave / status detail.

        Schema notes: ``call_record`` has no ``message_id`` and
        no ``is_synthesized`` column — voice chats are identified
        via ``call_category = 'voice_chat'`` only.

        Linkage to the analysis ``message`` row goes through the
        ``source_call_id`` field: messages with
        ``type_label = 'call_log'`` have a corresponding row in
        msgstore's ``message_call_log(call_log_row_id,
        message_row_id)`` that maps
        ``call_record.source_call_id`` →
        ``message.source_msg_id``.  This linkage is mirrored at
        runtime here.
        """
        out: dict[int, dict] = {}
        attached_call_record_ids: set[int] = set()
        # Step 1: find every call_log message in this conv and resolve
        # its source_call_id via the msgstore bridge if available.
        try:
            call_msgs = conn.execute(
                "SELECT m.id AS analysis_msg_id, m.source_msg_id, m.timestamp,"
                "       m.sender_id"
                "  FROM message m"
                " WHERE m.conversation_id = ?"
                "   AND m.type_label = 'call_log'",
                (conv_id,),
            ).fetchall()
        except Exception:
            return out, attached_call_record_ids

        # Cross-DB resolution: source_msg_id -> source_call_id is in msgstore
        # (table message_call_log). The analysis DB doesn't carry that link,
        # so we open msgstore in read-only mode if the bundle exporter has
        # access to it; otherwise we fall back to matching by timestamp.
        src_msg_to_call: dict[int, int] = {}
        try:
            mss_path = getattr(self, "_msgstore_path", None)
            if mss_path:
                import sqlite3 as _sql
                mss = _sql.connect(f"file:{mss_path}?mode=ro&immutable=1", uri=True)
                mss.row_factory = _sql.Row
                ids = [r["source_msg_id"] for r in call_msgs if r["source_msg_id"]]
                for chunk_start in range(0, len(ids), 500):
                    chunk = ids[chunk_start:chunk_start + 500]
                    if not chunk: continue
                    qmarks = ",".join("?" * len(chunk))
                    bridge = mss.execute(
                        f"SELECT call_log_row_id, message_row_id FROM message_call_log "
                        f"WHERE message_row_id IN ({qmarks})",
                        chunk,
                    ).fetchall()
                    for b in bridge:
                        src_msg_to_call[b["message_row_id"]] = b["call_log_row_id"]
                mss.close()
        except Exception:
            pass

        # Step 2: for each call_log message, look up the matching call_record.
        # Build a map source_call_id -> analysis message id for join.
        call_to_msg: dict[int, int] = {}
        unbridged_msgs: list[dict] = []  # call_log msgs that need ts fallback
        for r in call_msgs:
            sm = r["source_msg_id"]
            sc = src_msg_to_call.get(sm) if sm and sm > 0 else None
            if sc:
                call_to_msg[sc] = r["analysis_msg_id"]
            else:
                # Either no msgstore bridge, OR this is an analyzer-
                # synthesized call_log row (negative source_msg_id).
                # We'll match it to a call_record by (timestamp, sender)
                # below.  Without this fallback the row is emitted as
                # an empty call card AND the vc:<id> synth row also
                # emits, producing duplicate entries.
                unbridged_msgs.append(dict(r))

        # Pull all call_records for this conversation, including
        # pure voice-chat synthetic rows that have no message
        # link.  Un-linked call_records are emitted later as
        # synthetic ``vc:<id>`` rows by ``_load_synth_voice_chats``.
        # Also fetch the call's ORIGIN conversation (group / multi-
        # person home chat) + creator name so the viewer can render
        # the "Go to original →" pill on per-participant echoes.
        try:
            rows = conn.execute(
                "SELECT cr.id AS call_id, cr.source_call_id, cr.duration_sec, cr.is_video,"
                "       cr.is_group_call, cr.call_result, cr.result_label,"
                "       cr.call_category, cr.timestamp AS call_ts,"
                "       cr.from_me AS call_from_me, cr.contact_id AS cr_contact_id,"
                "       cr.group_conversation_id, cr.conversation_id AS cr_conversation_id,"
                "       cr.call_id_text,"
                "       COALESCE(cc.resolved_name, cc.wa_name, cc.phone_number, '') AS creator_name"
                "  FROM call_record cr"
                "  LEFT JOIN contact cc ON cc.id = cr.creator_contact_id"
                " WHERE cr.conversation_id = ? OR cr.group_conversation_id = ?",
                (conv_id, conv_id),
            ).fetchall()
        except Exception:
            return out, attached_call_record_ids

        # Fallback: match unbridged call_log msgs to a call_record by
        # (timestamp, sender_id <-> cr.contact_id).  This catches
        # analyzer-synthesized call_log msgs (negative source_msg_id) and
        # exports where msgstore.message_call_log isn't accessible.
        if unbridged_msgs:
            for cr in rows:
                if cr["call_id"] in call_to_msg.values():
                    continue  # already linked via msgstore bridge
                ts = cr["call_ts"]
                cid = cr["cr_contact_id"]
                for um in unbridged_msgs:
                    if um.get("_matched"):
                        continue
                    if um["timestamp"] == ts and (
                        um["sender_id"] == cid or um["sender_id"] is None
                    ):
                        call_to_msg[cr["source_call_id"] or cr["call_id"]] = um["analysis_msg_id"]
                        attached_call_record_ids.add(cr["call_id"])
                        um["_matched"] = True
                        break

        # Re-map rows so each one has a ``message_id`` field for the
        # downstream loop that expects it.
        rows = [
            dict(r, message_id=(
                call_to_msg.get(r["source_call_id"])
                if r["source_call_id"] else None
            ))
            for r in rows
        ]
        # Bring in ts-fallback links too: rows that didn't match by
        # source_call_id but did match by ts above.
        for r in rows:
            if r["message_id"] is None and r["call_id"] in attached_call_record_ids:
                # Find the unbridged msg whose ts matches.
                for um in unbridged_msgs:
                    if um.get("_matched") and um["timestamp"] == r["call_ts"]:
                        r["message_id"] = um["analysis_msg_id"]
                        break
        rows = [r for r in rows if r["message_id"] is not None]
        # Mark the source_call_id-bridged ones as attached too.
        for r in rows:
            attached_call_record_ids.add(r["call_id"])
        # Per-participant detail — call_result is a numeric code WhatsApp uses
        # (0=missed, 1=joined/accepted, 2=declined, 3=ringing, etc.).
        # Keyed by analysis message_id (which we resolve via the
        # call_id → message_id map we built above).
        part_by_msg: dict[int, list] = {}
        _RESULT_MAP = {0: "missed", 1: "joined", 2: "declined", 3: "ringing"}
        try:
            part_rows = conn.execute(
                "SELECT cp.call_id,"
                " COALESCE(NULLIF(c.resolved_name,''), NULLIF(c.display_name,''),"
                "          CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''"
                "               THEN '+' || c.phone_number END, 'Unknown') AS name,"
                " c.phone_number AS phone,"
                " cp.call_result"
                " FROM call_participant cp"
                " JOIN call_record cr ON cr.id = cp.call_id"
                " LEFT JOIN contact c ON c.id = cp.contact_id"
                " WHERE cr.conversation_id = ?",
                (conv_id,),
            ).fetchall()
            # call_id -> analysis msg id (only for rows we can link)
            ana_by_call_record_id = {r["call_id"]: r["message_id"] for r in rows}
            for p in part_rows:
                analysis_msg = ana_by_call_record_id.get(p["call_id"])
                if not analysis_msg:
                    continue
                status_code = p["call_result"]
                status = _RESULT_MAP.get(status_code, "")
                part_by_msg.setdefault(analysis_msg, []).append({
                    "name": p["name"] or "Unknown",
                    "phone": p["phone"] or "",
                    "status": status,
                })
        except Exception:
            pass
        for r in rows:
            parts = part_by_msg.get(r["message_id"], [])
            # ``call_result`` is INTEGER in the schema; the
            # human-readable variant is in ``result_label``
            # ("answered", "missed", "rejected", etc.).  Use the
            # label for display and keep the numeric code for
            # forensic export.
            label = r["result_label"]
            if label is None:
                label = ""
            elif not isinstance(label, str):
                label = str(label)
            # Origin chat — when this call's home conversation is
            # different from the chat we're emitting into (group /
            # multi-person echo into a participant's 1:1), the JS
            # viewer renders a "Go to original →" pill that jumps
            # there.  When the call IS already in its home chat
            # (origin == this conv), originConvId stays None and
            # the pill is suppressed.
            origin_cid = (r["group_conversation_id"]
                          or r["cr_conversation_id"])
            origin_conv_js = ""
            origin_name = ""
            origin_msg_id = 0
            if origin_cid and origin_cid != conv_id:
                origin_conv_js = f"conv_{origin_cid}"
                # Best-effort name lookup
                try:
                    nrow = conn.execute(
                        "SELECT display_name FROM conversation WHERE id = ?",
                        (origin_cid,),
                    ).fetchone()
                    if nrow:
                        origin_name = nrow["display_name"] or ""
                except Exception:
                    pass
                # Find the original (non-synthetic) call message in
                # the origin conv via the source_key_id base (strip
                # any "::p<cid>" suffix used for per-participant
                # echoes — see call_ingester._create_participant_call_messages).
                try:
                    base_key = (r["call_id_text"] or "").replace("call:", "")
                    if base_key:
                        mrow = conn.execute(
                            "SELECT id FROM message"
                            " WHERE conversation_id = ? AND source_key_id = ?"
                            "   AND message_type = 90"
                            " ORDER BY"
                            "   CASE WHEN COALESCE(source_msg_id, -1) > 0 THEN 0 ELSE 1 END,"
                            "   id"
                            " LIMIT 1",
                            (origin_cid, base_key),
                        ).fetchone()
                        if mrow:
                            origin_msg_id = int(mrow["id"])
                except Exception:
                    pass

            out[r["message_id"]] = {
                "call": {
                    "duration":  r["duration_sec"] or 0,
                    "video":     bool(r["is_video"]),
                    "group":     bool(r["is_group_call"]),
                    "category":  r["call_category"] or "",
                    "creator":   r["creator_name"] or "",
                    "result":    label.lower(),
                    "result_code": r["call_result"],   # numeric, for forensic info
                    "participants": parts,             # name, phone, status
                    "originConvId":  origin_conv_js,
                    "originName":    origin_name,
                    "originMsgId":   origin_msg_id,
                },
                "synthesized": r["call_category"] == "voice_chat",
            }
        return out, attached_call_record_ids

    def _load_vcards(self, conn, conv_id):
        """Load all vCards per message, including ``vcard_list``
        messages that bundle multiple contacts in a single share.

        Returns a dict ``msg_id -> list[ {name, phones[]} ]``
        with one entry per contact in ``vcard_index`` order, so
        a single-share message that carries N contacts surfaces
        all N to the bundle viewer.
        """
        out: dict[int, list[dict]] = {}
        try:
            rows = conn.execute(
                "SELECT v.message_id, v.display_name, v.phone_numbers,"
                "       v.vcard_index"
                "  FROM message_vcard_data v"
                "  JOIN message m ON m.id = v.message_id"
                " WHERE m.conversation_id = ?"
                " ORDER BY v.message_id, v.vcard_index",
                (conv_id,),
            ).fetchall()
        except Exception:
            return out
        for r in rows:
            phones: list[str] = []
            pn = r["phone_numbers"] or ""
            if pn:
                try:
                    phones = json.loads(pn) if pn.startswith("[") else pn.split(",")
                except Exception:
                    phones = [pn]
            entry = {
                "name": r["display_name"] or "",
                "phones": phones,
            }
            out.setdefault(r["message_id"], []).append(entry)
        return out

    def _load_link_previews(self, conn, conv_id):
        out: dict[int, list] = {}
        try:
            rows = conn.execute(
                "SELECT ld.message_id, ld.url, ld.page_title, ld.description,"
                "       ld.domain, ld.thumbnail_blob"
                " FROM message_link_detail ld"
                " JOIN message m ON m.id = ld.message_id"
                " WHERE m.conversation_id = ?",
                (conv_id,),
            ).fetchall()
        except Exception:
            return out
        for r in rows:
            lp = {
                "url": r["url"] or "",
                "title": r["page_title"] or "",
                "desc": r["description"] or "",
                "domain": r["domain"] or "",
            }
            if r["thumbnail_blob"]:
                try:
                    lp["image"] = ("data:image/jpeg;base64,"
                                   + base64.b64encode(r["thumbnail_blob"]).decode("ascii"))
                except Exception:
                    pass
            out.setdefault(r["message_id"], []).append(lp)
        return out

    def _load_mentions(self, conn, conv_id):
        """Return mentions as rich dicts so the viewer can substitute
        @<phone>/@<jid-number> tokens in message text with @Name (JID)."""
        out: dict[int, list] = {}
        try:
            rows = conn.execute(
                "SELECT mn.message_id,"
                " COALESCE(NULLIF(mc.resolved_name,''), NULLIF(mc.display_name,''),"
                "          mn.display_name,"
                "          CASE WHEN mc.phone_number IS NOT NULL AND mc.phone_number != ''"
                "               THEN '+' || mc.phone_number END,"
                "          'Unknown') AS name,"
                " mc.phone_number AS phone,"
                " mc.phone_jid AS jid,"
                " mc.lid_jid AS lid"
                " FROM mention mn"
                " JOIN message m ON m.id = mn.message_id"
                " LEFT JOIN contact mc ON mc.id = mn.mentioned_id"
                " WHERE m.conversation_id = ?",
                (conv_id,),
            ).fetchall()
        except Exception:
            return out
        for r in rows:
            out.setdefault(r["message_id"], []).append({
                "name": r["name"] or "Unknown",
                "phone": r["phone"] or "",
                "jid": r["jid"] or "",
                "lid": r["lid"] or "",
            })
        return out

    # ------------------------------------------------------------------
    def _build_readme(self, total_msgs, conv_count):
        ci = self._case_info or {}
        bits = [
            f"WAInsight Viewer Bundle",
            f"========================",
            f"",
            f"Exported: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Title:    {self._title}",
            f"Messages: {total_msgs:,} across {conv_count} conversation(s)",
            "",
        ]
        if ci:
            bits.append("Case Information")
            bits.append("----------------")
            for k in ("case_id", "examiner", "notes", "analysis_db", "source_msgstore"):
                v = ci.get(k) or ""
                if v:
                    bits.append(f"  {k}: {v}")
            bits.append("")
        bits.extend([
            "How to view",
            "-----------",
            "  Unzip the bundle, then double-click `index.html`.",
            "  Works entirely from `file://` - no server needed.",
            "",
            "Keyboard",
            "--------",
            "  Ctrl+K or /   open search palette",
            "  Esc           close search",
            "",
            "Folder layout",
            "-------------",
            "  index.html           viewer shell + inline CSS/JS",
            "  data/manifest.js     conversation index",
            "  data/conv_N/         message shards for conversation N",
            "  media/               media files (images, voice, video, docs)",
            "",
            "Produced by WAInsight - WhatsApp Forensic Suite.",
        ])
        return "\n".join(bits)


# ==================================================================
# Helpers
# ==================================================================

def _map_type(type_label: str, mt: int, row) -> str:
    """Map analysis-DB ``type_label`` + ``message_type`` into a
    viewer type key.

    Preserves view-once and event distinctions so the renderer
    shows the right badge / card — collapsing
    ``view_once_image`` to plain ``image`` would drop the
    "View once" indicator from the rendered output.
    """
    tl = (type_label or "").lower()
    if tl in ("system", "call_start", "call_end", "group_notification",
              "e2e_notification"):
        return "system"
    if tl in ("image", "video", "gif", "sticker", "voice", "ptt", "audio",
              "document", "location", "live_location", "vcard", "poll", "call",
              "text"):
        return tl
    # View-once variants — keep the distinction so the renderer can show
    # the "👁 View once" badge (and a special card when the media file is
    # missing on disk).  We append _onceonly and the renderer special-
    # cases by typeLabel as well.
    if tl == "view_once_image":   return "view_once_image"
    if tl == "view_once_video":   return "view_once_video"
    if tl == "view_once_voice":   return "view_once_voice"
    # Scheduled events — bundle now has its own renderer branch.
    if tl == "scheduled_event":   return "scheduled_event"
    if tl == "event":             return "event"
    # Voice-chat reconstructed records (linked to the call_record table
    # but emitted as a synthetic message row).
    if tl == "voice_chat":        return "voice_chat"
    # Call-log message that doesn't have a corresponding call_record join.
    if tl == "call_log":          return "call"
    # Numeric fallback
    _NUM = {
        0: "text", 1: "image", 2: "audio", 3: "video", 5: "location", 7: "system",
        8: "vcard", 9: "document", 13: "gif", 14: "voice", 15: "sticker",
        16: "live_location", 64: "poll", 27: "text",  # button_message renders as text
        90: "call",                # call_log
        92: "scheduled_event",
        42: "view_once_image",
        43: "view_once_video",
        82: "view_once_voice",
    }
    return _NUM.get(mt, "text")


def _map_quoted_type(qt):
    try:
        qt = int(qt or 0)
    except Exception:
        return "text"
    return _map_type("", qt, None)


def _status_label(s):
    try:
        s = int(s or 0)
    except Exception:
        return ""
    if s >= 13: return "read"
    if s >= 5: return "delivered"
    if s >= 4: return "sent"
    return ""


_MIME_EXT_FALLBACK = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/3gpp": ".3gp",
    "audio/ogg": ".opus", "audio/mpeg": ".mp3", "audio/amr": ".amr",
    "audio/wav": ".wav", "audio/aac": ".m4a",
    "application/pdf": ".pdf", "application/zip": ".zip",
}


def _safe_name(path: str, file_hash: Optional[str], mime: Optional[str] = None) -> str:
    """Pick a stable, safe bundle filename. Prefer SHA-256 prefix; fall back to basename.

    Ensures the final filename has a real extension so the browser can MIME-sniff
    on `file://` (critical for direct-open video/audio tags).
    """
    ext = Path(path).suffix.lower() or ""
    # If path has no real extension, fall back to mime-derived
    if not ext or len(ext) > 6:
        ext = _MIME_EXT_FALLBACK.get(
            (mime or "").split(";")[0].strip(), ""
        )
    if file_hash:
        safe = re.sub(r'[^a-zA-Z0-9]', '', file_hash)[:16]
        if safe:
            return f"{safe}{ext}"
    h = hashlib.sha1(path.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"f{h}{ext}"


def _copy_media(src_path: str, file_hash: Optional[str], base: Path,
                media_mapping: dict, mime: Optional[str] = None) -> Optional[str]:
    """Copy a media file into bundle/media/ using a deduped, extension-correct filename."""
    if src_path in media_mapping:
        return media_mapping[src_path]
    try:
        if not os.path.isfile(src_path):
            return None
        fname = _safe_name(src_path, file_hash, mime)
        dest = base / "media" / fname
        if not dest.exists():
            shutil.copy2(src_path, dest)
        rel = f"media/{fname}"
        media_mapping[src_path] = rel
        return rel
    except Exception:
        return None
