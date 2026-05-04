"""WAInsight Media Forensics Dashboard — folder-shaped offline artifact.

Replaces the previous single-file HTML report.  Produces a directory the
analyst can hand off, double-click ``index.html``, and open in any modern
browser **with no launcher, no server, no internet**.  Designed to scale
to ~200k media rows with thumbnails.

Output layout::

    <output_dir>/
      index.html                        ← shell — opens in browser
      vendor/
        app.css, app.js                 ← UI engine (no third-party CDN)
        uplot.min.js, uplot.min.css     ← histogram chart (small, vendored)
      data/
        manifest.js                     ← case info, facet vocab, schema
        meta_000.js, meta_001.js, …     ← chunked row arrays (≈30k rows each)
      thumbs/
        ab/cd/<sha>.jpg                 ← sharded thumbnails (deduped by hash)
      assets/                           ← (optional) bundled real media files

Architecture follows the file:// research doc shipped with the project:

  * **Folder-shaped, not one giant .html** so V8 string limits and
    file:// memory caps don't bite at 200k+ rows.
  * **Classic ``<script src=…>``** for chunk loading — no ``fetch()``,
    ``import()``, or ``new Worker('./…')`` (all blocked by file://).
  * **Sharded thumbnail tree** (``thumbs/<aa>/<bb>/<sha>.jpg``) so no
    single directory holds millions of files.
  * **Bitset crossfilter** in pure JS — sub-ms AND across 5 facets at
    200k rows.
  * **Virtual list + IntersectionObserver** — only the visible rows
    are in the DOM, only the visible thumbnails fetched.
  * **Cascading facets** — each facet's option counts reflect all
    *other* active filters (flight-fare style), so the analyst can
    see "if I add this filter, how many rows survive?".

The 12-state status pill from the previous report is preserved
verbatim so any forensic SOP that references status names still works.
"""

from __future__ import annotations

import base64
import hashlib
import html as _html
import io
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------- #
# Status taxonomy — IDENTICAL to previous report so SOP language carries
# ---------------------------------------------------------------------- #

STATUS_LABELS: list[tuple[str, str, str]] = [
    # (code,                       display label,                  css class)
    ("original",                   "Original",                     "ms-original"),
    ("downloaded",                 "Downloaded (CDN)",             "ms-downloaded"),
    ("hash_linked",                "Hash-linked",                  "ms-hashlinked"),
    ("hash_linked_after_delete",   "Hash-linked after delete",     "ms-hashlinked-del"),
    ("orphan_recovered",           "Recovered from orphan",        "ms-orphan"),
    ("missing_downloadable",       "Missing — downloadable",       "ms-missing-dl"),
    ("missing_no_key",             "Missing — no decrypt key",     "ms-missing-key"),
    ("missing_no_url",             "Missing — no CDN URL",         "ms-missing-url"),
    ("download_failed",            "Download failed",              "ms-fail"),
    ("expired",                    "CDN URL expired",              "ms-expired"),
    ("thumbnail_only",             "Thumbnail only",               "ms-thumb"),
    ("unknown",                    "Unknown",                      "ms-unknown"),
]
_STATUS_INDEX: dict[str, int] = {code: i for i, (code, _, _) in enumerate(STATUS_LABELS)}


def _classify_media(row: dict, now_ts_s: int) -> str:
    """Distil (recovery_method, media_status, file_exists, url+key, expiry,
    thumbnail) into a single status code.  Identical semantics to the
    previous report so existing analyst notes stay valid.
    """
    method = (row.get("recovery_method") or "").strip()
    status = (row.get("media_status") or "").strip()
    exists = bool(row.get("file_exists"))
    has_url = bool(row.get("media_url"))
    has_key = bool(row.get("media_key"))
    expiry = row.get("cdn_expiry_ts") or 0
    thumb = bool(row.get("thumbnail_blob"))

    if exists and method:
        if method in ("hash_linked", "hash_linked_after_delete",
                      "orphan_recovered", "downloaded"):
            return method
        return "downloaded"
    if exists:
        return "original"

    if status == "download_failed":
        return "download_failed"
    if has_url and has_key:
        if expiry:
            try:
                exp_s = int(expiry)
                if exp_s > 10_000_000_000:
                    exp_s //= 1000
                if exp_s and exp_s < now_ts_s:
                    return "expired"
            except (TypeError, ValueError):
                pass
        return "missing_downloadable"
    if has_url and not has_key:
        return "missing_no_key"
    if thumb:
        return "thumbnail_only"
    return "missing_no_url"


# ---------------------------------------------------------------------- #
# Public API
# ---------------------------------------------------------------------- #

def generate_media_report(
    analysis_db_path: str | Path,
    output_path: str | Path,
    *,
    conversation_id: Optional[int] = None,
    conversation_ids: Optional[list[int]] = None,
    sections: Optional[dict[str, bool]] = None,
    hide_stickers: bool = False,
    layout: str = "dashboard",                  # kept for API compat
    top_n_per_chat: int = 0,                     # ignored — dashboard shows all
    include_thumbnails: bool = True,
    thumbnail_quality: str = "medium",           # "low" | "medium" | "high"
    chunk_rows: int = 30_000,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> Path:
    """Produce the offline media-availability dashboard.

    Args:
        analysis_db_path:  Case ``analysis.db`` (read-only).
        output_path:       Either a directory path (preferred) or an
                           ``index.html`` path inside the desired output
                           directory.  In both cases the surrounding
                           folder becomes the dashboard root.
        conversation_id:   Restrict to one conversation's media; ``None``
                           = whole case.
        sections:          Toggle map.  Recognised keys:
                             - ``orphans``  (default True)
                             - ``sharing``  (default True — affects sidebar tab)
        hide_stickers:     Filter out sticker rows everywhere.
        layout:            Accepted for backward compatibility; the new
                           dashboard layout is always rich.
        top_n_per_chat:    Accepted for backward compatibility; the
                           dashboard pages all rows via virtual scrolling.
        include_thumbnails: Emit the ``thumbs/`` tree.  When False the
                           UI shows generic file-type icons only.
        thumbnail_quality: ``"low"`` (≈80px, q60), ``"medium"`` (≈160px,
                           q72) or ``"high"`` (≈320px, q82).  Re-encodes
                           the existing WhatsApp thumbnail blob.
        chunk_rows:        Rows per ``data/meta_NNN.js`` chunk.  Smaller
                           chunks = more files, but each parses faster.
                           Default 30k stays well under V8's per-string
                           limits while keeping chunk count reasonable.
        progress_cb:       ``cb(stage_label, current, total)`` for GUI
                           progress dialogs.  ``current`` may equal
                           ``total`` when the stage is undeterminate.

    Returns:
        Path to the dashboard ``index.html`` inside the output folder.
    """
    sections = sections or {"orphans": True, "sharing": True}
    output_dir = _coerce_to_dir(output_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Normalise scope:
    #   conversation_ids (list)  → multi-conv scope
    #   conversation_id (int)    → single-conv scope (legacy alias)
    #   neither                  → whole case
    scope_ids: list[int] = []
    if conversation_ids:
        scope_ids = [int(x) for x in conversation_ids if x is not None]
    elif conversation_id is not None:
        scope_ids = [int(conversation_id)]

    # ---- prepare folder tree ----
    (output_dir / "vendor").mkdir(exist_ok=True)
    (output_dir / "data").mkdir(exist_ok=True)
    if include_thumbnails:
        (output_dir / "thumbs").mkdir(exist_ok=True)

    # ---- copy static assets (HTML shell, CSS, JS) ----
    _emit_static_assets(output_dir)

    # ---- read DB ----
    conn = sqlite3.connect(
        f"file:{analysis_db_path}?mode=ro&immutable=1", uri=True
    )
    conn.row_factory = sqlite3.Row
    try:
        if progress_cb:
            progress_cb("Loading case metadata…", 0, 0)
        case_info = _collect_case_info(conn, analysis_db_path)
        owner_info = _collect_owner_info(conn)
        scope_label = _resolve_scope_label_multi(conn, scope_ids)

        if progress_cb:
            progress_cb("Fetching media rows…", 0, 0)
        media_rows = _fetch_media(conn, scope_ids, hide_stickers)

        if progress_cb:
            progress_cb("Indexing cross-chat sharing…", 0, 0)
        share_count_by_hash = _build_share_counts(conn, hide_stickers)

        # Orphans only on whole-case scope (chat-less by definition)
        orphan_rows: list[dict] = []
        if (sections.get("orphans", True) and not scope_ids):
            try:
                if progress_cb:
                    progress_cb("Loading orphan media…", 0, 0)
                orphan_rows = _fetch_orphans(conn, hide_stickers)
            except Exception:
                orphan_rows = []

        # ---- build facet vocabularies ----
        if progress_cb:
            progress_cb("Building facet vocabularies…", 0, 0)
        facets = _build_facet_vocab(media_rows, owner_info)

        # ---- pull avatars for the conversations + senders we know ----
        if progress_cb:
            progress_cb("Extracting avatars…", 0, 0)
        conv_ids = {c["id"] for c in facets["conv"] if isinstance(c.get("id"), int)}
        sender_ids = {s["id"] for s in facets["sender"]
                      if isinstance(s.get("id"), int) and s["id"] >= 0}
        conv_avatars, sender_avatars = _collect_avatars(
            conn, conv_ids, sender_ids
        )
        # Inline avatar data URLs into the facet objects so the dashboard
        # JS can render <img> directly without an extra fetch.
        for c in facets["conv"]:
            cid = c.get("id")
            if cid in conv_avatars:
                c["avatar"] = conv_avatars[cid]
        for s in facets["sender"]:
            sid = s.get("id")
            if sid in sender_avatars:
                s["avatar"] = sender_avatars[sid]

        # ---- emit data chunks ----
        if progress_cb:
            progress_cb("Writing metadata chunks…", 0, len(media_rows))
        chunk_descriptors, thumb_jobs = _emit_chunks(
            output_dir / "data",
            media_rows,
            facets,
            share_count_by_hash,
            chunk_rows=chunk_rows,
            progress_cb=progress_cb,
        )

        # ---- emit orphan chunk (separate, lazy-displayed) ----
        if orphan_rows:
            if progress_cb:
                progress_cb("Writing orphan chunk…", 0, 0)
            orphan_thumb_jobs = _emit_orphans(
                output_dir / "data", orphan_rows
            )
        else:
            orphan_thumb_jobs = []

        # ---- emit thumbnail tree (sharded by hash prefix) ----
        thumb_count = 0
        thumb_ext = "jpg"
        if include_thumbnails and (thumb_jobs or orphan_thumb_jobs):
            if progress_cb:
                progress_cb(
                    "Writing thumbnails (sharded by hash prefix)…",
                    0, len(thumb_jobs) + len(orphan_thumb_jobs),
                )
            thumb_count, thumb_ext = _emit_thumbs(
                output_dir / "thumbs",
                thumb_jobs + orphan_thumb_jobs,
                quality=thumbnail_quality,
                progress_cb=progress_cb,
            )

        # ---- compute totals + histogram ----
        if progress_cb:
            progress_cb("Building histogram…", 0, 0)
        totals, hist = _summarise(media_rows, share_count_by_hash)

        # ---- emit manifest ----
        if progress_cb:
            progress_cb("Writing manifest…", 0, 0)
        _emit_manifest(
            output_dir / "data" / "manifest.js",
            case_info=case_info,
            owner_info=owner_info,
            scope_label=scope_label,
            scope_conv_ids=scope_ids,
            totals=totals,
            facets=facets,
            chunks=chunk_descriptors,
            hist=hist,
            hide_stickers=hide_stickers,
            include_thumbnails=include_thumbnails,
            orphan_count=len(orphan_rows),
            thumb_count=thumb_count,
            thumb_ext=thumb_ext,
            sections=sections,
        )

        # ---- finally, stamp index.html ----
        if progress_cb:
            progress_cb("Stamping index.html…", 0, 0)
        index_path = _stamp_index(output_dir, scope_label, chunk_descriptors,
                                  has_orphans=bool(orphan_rows))
        if progress_cb:
            progress_cb("Done.", 1, 1)
        return index_path
    finally:
        conn.close()


# ---------------------------------------------------------------------- #
# Path helpers
# ---------------------------------------------------------------------- #

def _coerce_to_dir(p: str | Path) -> Path:
    """Accept either a directory or an index.html path; return the dir."""
    p = Path(p)
    if p.suffix.lower() in (".html", ".htm"):
        return p.parent
    return p


# ---------------------------------------------------------------------- #
# Asset emission
# ---------------------------------------------------------------------- #

_ASSET_DIR = Path(__file__).parent / "dashboard_assets"


def _emit_static_assets(out: Path) -> None:
    """Copy index.html / app.css / app.js (and optional vendor libs)
    from the bundled ``dashboard_assets/`` directory.  The HTML is
    copied to the output root, the CSS/JS go under ``vendor/`` so the
    HTML's relative refs resolve.
    """
    src = _ASSET_DIR
    if not src.is_dir():
        raise FileNotFoundError(f"dashboard_assets/ missing at {src}")

    # HTML shell
    shell = (src / "index.html").read_text(encoding="utf-8")
    # We re-stamp later; for now write a placeholder so the dir is valid
    # even if subsequent steps fail.  _stamp_index() will overwrite it.
    (out / "index.html").write_text(shell, encoding="utf-8")

    vendor_out = out / "vendor"
    for fn in ("app.css", "app.js"):
        srcf = src / fn
        if srcf.is_file():
            shutil.copy2(srcf, vendor_out / fn)

    # uPlot histogram (optional vendor)
    for fn in ("uplot.min.js", "uplot.min.css"):
        srcf = src / "vendor" / fn
        if srcf.is_file():
            shutil.copy2(srcf, vendor_out / fn)


def _stamp_index(out: Path, scope_label: str, chunks: list[dict],
                 has_orphans: bool) -> Path:
    shell = (_ASSET_DIR / "index.html").read_text(encoding="utf-8")
    title = f"WAInsight — Media Dashboard — {_h(scope_label)}"
    chunk_tags: list[str] = []
    for ch in chunks:
        chunk_tags.append(f'<script src="{_h(ch["src"])}"></script>')
    if has_orphans:
        chunk_tags.append('<script src="data/orphans.js"></script>')
    chunk_block = "\n".join(chunk_tags)

    # Optional uPlot include
    uplot_css = '<link rel="stylesheet" href="vendor/uplot.min.css">' \
        if (out / "vendor" / "uplot.min.css").exists() else ""
    uplot_js = '<script src="vendor/uplot.min.js"></script>' \
        if (out / "vendor" / "uplot.min.js").exists() else ""

    stamped = (
        shell
        .replace("__TITLE__", title)
        .replace("__CHUNK_TAGS__", chunk_block)
        .replace("__UPLOT_CSS__", uplot_css)
        .replace("__UPLOT_JS__", uplot_js)
    )
    idx = out / "index.html"
    idx.write_text(stamped, encoding="utf-8")
    return idx


# ---------------------------------------------------------------------- #
# Data fetch (re-uses the previous report's SQL so semantics match)
# ---------------------------------------------------------------------- #

def _resolve_scope_label_multi(conn, conv_ids: list[int]) -> str:
    if not conv_ids:
        return "Whole case"
    if len(conv_ids) == 1:
        row = conn.execute(
            "SELECT display_name FROM conversation WHERE id = ?",
            (conv_ids[0],)
        ).fetchone()
        return (row["display_name"] if row else f"Conversation #{conv_ids[0]}") \
            or f"#{conv_ids[0]}"
    placeholders = ",".join("?" * len(conv_ids))
    rows = conn.execute(
        f"SELECT display_name FROM conversation WHERE id IN ({placeholders}) "
        f"ORDER BY display_name", tuple(conv_ids)
    ).fetchall()
    names = [r["display_name"] or "?" for r in rows]
    if len(names) <= 3:
        return " · ".join(names) + f"  ({len(names)} chats)"
    return (f"{names[0]}, {names[1]} + {len(names) - 2} more "
            f"({len(names)} chats)")


def _table_columns(conn, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _fetch_media(conn, conv_ids: list[int],
                 hide_stickers: bool) -> list[dict]:
    """Schema-flex fetch.  ``conv_ids`` empty list = whole-case scope.

    Older case databases lack some of the newer columns
    (``was_transferred``, ``is_hd_twin``, ``hd_twin_msg_id``,
    ``assoc_kind``).  We probe ``PRAGMA table_info(media)`` first and
    substitute ``NULL AS <col>`` for any that are missing — so the
    dashboard renders cleanly against any case the tool has ever
    produced.
    """
    media_cols = _table_columns(conn, "media")

    def col(name: str, default_sql: str = "NULL") -> str:
        return f"me.{name}" if name in media_cols else f"{default_sql} AS {name}"

    where = ["1=1"]
    params: list = []
    if conv_ids:
        if len(conv_ids) == 1:
            where.append("m.conversation_id = ?")
            params.append(conv_ids[0])
        else:
            placeholders = ",".join("?" * len(conv_ids))
            where.append(f"m.conversation_id IN ({placeholders})")
            params.extend(conv_ids)
    if hide_stickers:
        where.append(
            "(m.message_type != 20 AND COALESCE(m.type_label, '') != 'sticker')"
        )
    where_sql = " AND ".join(where)
    sql = f"""
        SELECT
          me.id            AS media_row_id,
          me.message_id,
          m.conversation_id,
          m.timestamp,
          m.from_me,
          COALESCE(m.type_label, '')                   AS type_label,
          m.message_type,
          me.file_path, me.resolved_file_path, me.file_exists,
          me.file_size, me.mime_type,
          me.width, me.height, me.duration_ms,
          me.media_name, me.media_caption,
          me.file_hash, me.enc_file_hash,
          me.media_url, me.media_key,
          me.cdn_expiry_ts, me.media_status,
          me.recovery_method, me.recovery_timestamp,
          {col("was_transferred")},
          {col("is_hd_twin")},
          {col("hd_twin_msg_id")},
          {col("assoc_kind")},
          me.thumbnail_blob,
          conv.display_name                            AS conv_name,
          conv.chat_type                               AS conv_chat_type,
          conv.jid_raw_string                          AS conv_jid,
          COALESCE(c.resolved_name, c.wa_name, c.phone_number, '') AS sender_name,
          c.id                                          AS sender_id,
          c.phone_jid                                  AS sender_jid,
          c.lid_jid                                    AS sender_lid
        FROM media me
        JOIN message m       ON m.id = me.message_id
        JOIN conversation conv ON conv.id = m.conversation_id
        LEFT JOIN contact c ON c.id = m.sender_id
        WHERE {where_sql}
        ORDER BY m.timestamp
    """
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _build_share_counts(conn, hide_stickers: bool) -> dict[str, int]:
    extra = ""
    if hide_stickers:
        extra = (" AND (m.message_type != 20 AND "
                 "COALESCE(m.type_label, '') != 'sticker')")
    rows = conn.execute(
        f"""
        SELECT me.file_hash, COUNT(*) AS n
        FROM media me
        JOIN message m ON m.id = me.message_id
        WHERE me.file_hash IS NOT NULL AND me.file_hash != ''
          {extra}
        GROUP BY me.file_hash
        """
    ).fetchall()
    return {r["file_hash"]: r["n"] for r in rows}


def _fetch_orphans(conn, hide_stickers: bool) -> list[dict]:
    extra = ""
    if hide_stickers:
        extra = " AND (mime_type IS NULL OR mime_type NOT LIKE 'image/webp%')"
    rows = conn.execute(
        f"""
        SELECT id, file_path, file_name, folder, file_size, mime_type,
               parsed_date_ts, file_hash, matched_message_id,
               matched_conv_name, source_type, thumbnail_blob,
               width, height, duration_ms
        FROM orphaned_media
        WHERE 1=1 {extra}
        ORDER BY parsed_date_ts DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------- #
# Case + owner info
# ---------------------------------------------------------------------- #

def _collect_case_info(conn, analysis_db_path) -> dict:
    info = {
        "case_id": "", "examiner": "", "notes": "", "created": "",
        "source_paths": {}, "hashes": {},
    }
    try:
        meta_path = Path(analysis_db_path).parent / "metadata.json"
        if meta_path.exists():
            m = json.loads(meta_path.read_text(encoding="utf-8"))
            info["case_id"] = str(m.get("case_id") or "")
            info["examiner"] = str(m.get("examiner") or "")
            info["notes"] = str(m.get("notes") or "")
            info["created"] = str(m.get("created") or "")
            sp = m.get("source_paths") or {}
            if isinstance(sp, dict):
                info["source_paths"]["databases_dir"] = (
                    sp.get("databases_dir") or "")
                info["source_paths"]["analysis_db"] = (
                    sp.get("analysis_db") or str(analysis_db_path))
                if isinstance(sp.get("databases"), dict):
                    info["source_paths"]["databases"] = sp["databases"]
    except Exception:
        pass
    try:
        per_db = info["source_paths"].setdefault("databases", {})
        for k, v in conn.execute(
            "SELECT key, value FROM case_metadata "
            "WHERE key LIKE 'source_hash_%' OR key LIKE 'source_path_%' "
            "   OR key LIKE 'source_size_%' OR key = 'source_databases_dir'"
        ).fetchall():
            if k.startswith("source_hash_"):
                name = k[len("source_hash_"):]
                info["hashes"][name] = v
                per_db.setdefault(name, {})["sha256"] = v
            elif k.startswith("source_path_"):
                per_db.setdefault(k[len("source_path_"):], {})["path"] = v
            elif k.startswith("source_size_"):
                try:
                    per_db.setdefault(
                        k[len("source_size_"):], {}
                    )["size_bytes"] = int(v)
                except (TypeError, ValueError):
                    pass
            elif k == "source_databases_dir":
                info["source_paths"].setdefault("databases_dir", v)
    except Exception:
        pass
    info["source_paths"].setdefault("analysis_db", str(analysis_db_path))
    return info


def _collect_owner_info(conn) -> dict:
    """Pull device-owner identity from case_metadata so the dashboard
    can show "You (owner)" with the full JID rather than a bare ``You``.
    """
    owner = {"name": "", "phone": "", "jid": "", "lid_jid": "", "contact_id": None}
    try:
        kv = dict(conn.execute(
            "SELECT key, value FROM case_metadata WHERE key IN "
            "('device_owner_name', 'device_owner_phone', 'device_owner_jid', "
            " 'device_owner_lid_jid', 'device_owner_contact_id')"
        ).fetchall())
        owner["name"] = kv.get("device_owner_name") or ""
        owner["phone"] = kv.get("device_owner_phone") or ""
        owner["jid"] = kv.get("device_owner_jid") or ""
        owner["lid_jid"] = kv.get("device_owner_lid_jid") or ""
        try:
            owner["contact_id"] = int(kv.get("device_owner_contact_id") or "") \
                if kv.get("device_owner_contact_id") else None
        except (TypeError, ValueError):
            owner["contact_id"] = None
        if not owner["jid"] and owner["phone"]:
            digits = "".join(ch for ch in owner["phone"] if ch.isdigit())
            if digits:
                owner["jid"] = digits + "@s.whatsapp.net"
    except Exception:
        pass
    return owner


# ---------------------------------------------------------------------- #
# Facet vocabulary build
# ---------------------------------------------------------------------- #

def _collect_avatars(conn, conv_ids: set[int],
                     contact_ids: set[int]) -> tuple[dict[int, str], dict[int, str]]:
    """Extract conversation + contact avatars and re-encode them as
    small inline data URLs that can ship in the manifest.

    Returns ``(conv_avatars, contact_avatars)`` where each value is a
    ``data:image/...;base64,...`` string ready to slot into ``<img src>``.

    Tries 64×64 AVIF first (≈1-2KB each), falls back to JPEG when AVIF
    isn't available.  Skipped entirely if the case has no avatars or
    PIL isn't installed.
    """
    conv_av: dict[int, str] = {}
    contact_av: dict[int, str] = {}
    if not conv_ids and not contact_ids:
        return conv_av, contact_av
    encoder, ext = _pick_encoder()
    if encoder == "passthrough":
        return conv_av, contact_av  # No PIL → skip

    try:
        from PIL import Image
    except Exception:
        return conv_av, contact_av

    def _encode_blob(blob: bytes) -> str | None:
        try:
            img = Image.open(io.BytesIO(blob))
            img.load()
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if img.width > 64 or img.height > 64:
                img.thumbnail((64, 64), Image.LANCZOS)
            buf = io.BytesIO()
            if encoder == "avif":
                img.save(buf, "AVIF", quality=64, speed=8)
                return ("data:image/avif;base64,"
                        + base64.b64encode(buf.getvalue()).decode("ascii"))
            else:
                img.save(buf, "JPEG", quality=72, optimize=True, subsampling=0)
                return ("data:image/jpeg;base64,"
                        + base64.b64encode(buf.getvalue()).decode("ascii"))
        except Exception:
            return None

    # Conversations — only those that appear in our facet vocab
    if conv_ids:
        try:
            placeholders = ",".join("?" * len(conv_ids))
            for cid, blob in conn.execute(
                f"SELECT id, avatar_blob FROM conversation WHERE id IN "
                f"({placeholders}) AND avatar_blob IS NOT NULL",
                tuple(conv_ids)
            ).fetchall():
                if not blob:
                    continue
                enc = _encode_blob(bytes(blob))
                if enc:
                    conv_av[int(cid)] = enc
        except Exception:
            pass

    # Contacts — only those who appear as senders.  Try the SMALLER
    # ``avatar_thumbnail`` first per-row, fall back to ``avatar_blob``
    # per-row.  COALESCE in SQL means "thumbnail exists for this
    # contact" wins, otherwise we get the full blob (which our 64×64
    # downscale handles fine).  Picking the column up-front would miss
    # the common case where the schema HAS ``avatar_thumbnail`` but
    # it's only sparsely populated.
    if contact_ids:
        try:
            cols = _table_columns(conn, "contact")
            has_thumb = "avatar_thumbnail" in cols
            has_blob = "avatar_blob" in cols
            if has_thumb or has_blob:
                if has_thumb and has_blob:
                    src_expr = "COALESCE(avatar_thumbnail, avatar_blob)"
                    where_expr = ("(avatar_thumbnail IS NOT NULL OR "
                                  "avatar_blob IS NOT NULL)")
                elif has_thumb:
                    src_expr = "avatar_thumbnail"
                    where_expr = "avatar_thumbnail IS NOT NULL"
                else:
                    src_expr = "avatar_blob"
                    where_expr = "avatar_blob IS NOT NULL"
                placeholders = ",".join("?" * len(contact_ids))
                for cid, blob in conn.execute(
                    f"SELECT id, {src_expr} FROM contact WHERE id IN "
                    f"({placeholders}) AND {where_expr}",
                    tuple(contact_ids)
                ).fetchall():
                    if not blob:
                        continue
                    enc = _encode_blob(bytes(blob))
                    if enc:
                        contact_av[int(cid)] = enc
        except Exception:
            pass

    return conv_av, contact_av


def _build_facet_vocab(media_rows: list[dict], owner: dict) -> dict:
    """Build the facet dictionaries that the dashboard JS uses for
    cascading filtering.  Each facet is an ordered list (ordering
    drives display order in the sidebar) plus a value→index lookup.

    Returns a dict with keys:
        status, mime, ext, conv, sender   (each: list[dict|str])
        idx_status, idx_mime, idx_ext, idx_conv, idx_sender (lookups)
    """
    # status — fixed taxonomy
    status_list = [
        {"code": code, "label": label, "cls": cls}
        for code, label, cls in STATUS_LABELS
    ]
    idx_status = dict(_STATUS_INDEX)

    # mime + ext
    mime_set: set[str] = set()
    ext_set: set[str] = set()
    for r in media_rows:
        m = (r.get("mime_type") or "").split(";")[0].strip().lower()
        if m:
            mime_set.add(m)
        ext = _ext_for(r)
        if ext:
            ext_set.add(ext)
    mime_list = sorted(mime_set)
    ext_list = sorted(ext_set)
    idx_mime = {m: i for i, m in enumerate(mime_list)}
    idx_ext = {e: i for i, e in enumerate(ext_list)}

    # conv
    conv_set: dict[int, dict] = {}
    for r in media_rows:
        cid = r["conversation_id"]
        if cid not in conv_set:
            conv_set[cid] = {
                "id": cid,
                "name": r.get("conv_name") or f"#{cid}",
                "type": r.get("conv_chat_type") or "personal",
                "jid": r.get("conv_jid") or "",
            }
    conv_list = sorted(
        conv_set.values(), key=lambda c: (c["name"] or "").lower()
    )
    idx_conv = {c["id"]: i for i, c in enumerate(conv_list)}

    # sender — owner injected as senderId=0 (so ``from_me`` rows have a
    # known slot even when the case has no contact for the device owner)
    sender_set: dict[int, dict] = {}
    OWNER_KEY = -1
    sender_set[OWNER_KEY] = {
        "id": OWNER_KEY,
        "name": (owner.get("name") or "Device Owner") + " (you)",
        "jid": owner.get("jid") or "",
        "lid": owner.get("lid_jid") or "",
        "is_owner": True,
    }
    for r in media_rows:
        if r.get("from_me"):
            continue
        sid = r.get("sender_id")
        if sid is None:
            continue
        if sid not in sender_set:
            sender_set[sid] = {
                "id": sid,
                "name": r.get("sender_name") or f"Contact #{sid}",
                "jid": r.get("sender_jid") or "",
                "lid": r.get("sender_lid") or "",
                "is_owner": False,
            }
    # Sort: owner first, then alphabetical
    sender_list = [sender_set[OWNER_KEY]] + sorted(
        (s for s in sender_set.values() if not s["is_owner"]),
        key=lambda s: (s["name"] or "").lower()
    )
    idx_sender = {s["id"]: i for i, s in enumerate(sender_list)}

    return {
        "status": status_list,
        "mime": mime_list,
        "ext": ext_list,
        "conv": conv_list,
        "sender": sender_list,
        "idx_status": idx_status,
        "idx_mime": idx_mime,
        "idx_ext": idx_ext,
        "idx_conv": idx_conv,
        "idx_sender": idx_sender,
        "owner_sender_idx": idx_sender[OWNER_KEY],
    }


def _ext_for(r: dict) -> str:
    name = (r.get("media_name") or "").strip()
    if not name:
        path = r.get("file_path") or ""
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()[:8]
    mime = (r.get("mime_type") or "").split(";")[0].strip().lower()
    if "/" in mime:
        return mime.rsplit("/", 1)[-1][:8]
    return ""


# ---------------------------------------------------------------------- #
# Chunk emission
# ---------------------------------------------------------------------- #

# Schema — order matters; mirror in app.js
SCHEMA_COLS = [
    "id",          # 0  media row id
    "msgId",       # 1  message id
    "convIdx",     # 2  index into manifest.conv[]
    "senderIdx",   # 3  index into manifest.sender[]  (-1 = unknown)
    "ts",          # 4  ms epoch
    "statusIdx",   # 5  index into manifest.status[]
    "mimeIdx",     # 6  index into manifest.mime[]    (-1 = unknown)
    "extIdx",      # 7  index into manifest.ext[]     (-1 = unknown)
    "size",        # 8  bytes  (0 = unknown)
    "hash",        # 9  WhatsApp's file_hash (base64 SHA-256, "" if unknown)
    "name",        # 10 file name (string)
    "caption",     # 11 caption (string, "" = none)
    "thumbId",     # 12 thumbnail filename stem (string, "" = no thumb)
    "shareCount",  # 13 number of chats this hash appears in (>=1)
    "flags",       # 14 bitfield: 1=fromMe, 2=hdTwin, 4=hasThumb,
                   #              8=isSticker, 16=onDisk, 32=hasUrl, 64=hasKey
    "w",           # 15 width
    "h",           # 16 height
    "dur",         # 17 duration_ms
    "path",        # 18 resolved or stored file path (string)
    "convJid",     # 19 conversation JID (denorm for detail flyout)
    "senderJid",   # 20 sender JID (denorm)
    "senderLid",   # 21 sender LID (denorm)
    "encHash",     # 22 WhatsApp's enc_file_hash (base64 SHA-256 of the
                   #    encrypted CDN file, "" = unknown)
    "url",         # 23 media_url (string, "" = unknown)
    "recovery",    # 24 recovery_method (string, "" = original)
    "recoveryTs",  # 25 recovery_timestamp (ms epoch, 0 = none)
    "expiry",      # 26 cdn_expiry_ts
    "rawStatus",   # 27 media_status (string)
    "assocKind",   # 28 assoc_kind (string)
    "hdTwinMsgId", # 29 hd_twin_msg_id (int, 0 = none)
    "sha256",      # 30 standard hex SHA-256 of the FILE bytes — derived
                   #    from `hash` (base64-decoded → hex).  This is what
                   #    most forensic SOPs reference; the base64 form
                   #    survives in `hash` for provenance.
    "encSha256",   # 31 standard hex SHA-256 of the ENCRYPTED CDN file
                   #    bytes — derived from `encHash` similarly.
]
FLAG_FROM_ME = 1
FLAG_HD_TWIN = 2
FLAG_HAS_THUMB = 4
FLAG_IS_STICKER = 8
FLAG_ON_DISK = 16
FLAG_HAS_URL = 32
FLAG_HAS_KEY = 64

# Sentinel used inside the (thumb_id, blob, w, h) job tuples to flag
# "the blob payload is actually a UTF-8 file path; render the thumbnail
# from disk rather than decoding the bytes as a JPEG".  The marker is a
# single non-JPEG header byte so PIL can never accidentally interpret a
# thumbnail blob that happens to start the same way as a real image.
_DISK_MARKER = b"\x00DISK:"


def _emit_chunks(
    out_dir: Path,
    media_rows: list[dict],
    facets: dict,
    share_counts: dict[str, int],
    *,
    chunk_rows: int,
    progress_cb: Optional[Callable] = None,
) -> tuple[list[dict], list[tuple[str, bytes, int, int]]]:
    """Emit ``data/meta_NNN.js`` files plus return a list of thumbnail
    jobs ``(thumb_id, blob_bytes, w, h)`` to write to disk separately.
    """
    out_dir.mkdir(exist_ok=True)
    now_s = int(datetime.now(timezone.utc).timestamp())
    chunks: list[dict] = []
    thumb_jobs: list[tuple[str, bytes, int, int]] = []
    seen_thumb_ids: set[str] = set()
    n = len(media_rows)
    for i in range(0, n, chunk_rows):
        batch = media_rows[i:i + chunk_rows]
        rows_out: list[list] = []
        for r in batch:
            row_arr, thumb_job = _row_to_array(
                r, facets, share_counts, now_s
            )
            rows_out.append(row_arr)
            if thumb_job and thumb_job[0] not in seen_thumb_ids:
                seen_thumb_ids.add(thumb_job[0])
                thumb_jobs.append(thumb_job)

        chunk_idx = len(chunks)
        fname = f"meta_{chunk_idx:03d}.js"
        path = out_dir / fname
        # Emit as IIFE pushing to window.META.  Compact JSON.
        body = json.dumps(rows_out, separators=(",", ":"), ensure_ascii=False)
        path.write_text(
            "window.META=window.META||[];window.META.push(" + body + ");",
            encoding="utf-8"
        )
        chunks.append({
            "src": f"data/{fname}",
            "rows": len(rows_out),
            "bytes": path.stat().st_size,
        })
        if progress_cb:
            progress_cb("Writing metadata chunks…", min(i + chunk_rows, n), n)
    return chunks, thumb_jobs


def _row_to_array(r: dict, facets: dict, share_counts: dict[str, int],
                  now_s: int) -> tuple[list, Optional[tuple[str, bytes, int, int]]]:
    status_code = _classify_media(r, now_s)
    status_idx = facets["idx_status"].get(status_code, len(STATUS_LABELS) - 1)

    mime = (r.get("mime_type") or "").split(";")[0].strip().lower()
    mime_idx = facets["idx_mime"].get(mime, -1)

    ext = _ext_for(r)
    ext_idx = facets["idx_ext"].get(ext, -1)

    conv_idx = facets["idx_conv"].get(r["conversation_id"], -1)

    sender_idx: int
    if r.get("from_me"):
        sender_idx = facets["owner_sender_idx"]
    else:
        sid = r.get("sender_id")
        sender_idx = facets["idx_sender"].get(sid, -1)

    # Filename fallback chain — ONLY real names, never invented ones.
    # If WhatsApp didn't record a name and we have no path, ``name``
    # stays empty.  The dashboard JS surfaces the caption in that case
    # (with an honest "(no filename)" marker) so the analyst can never
    # mistake a derived label for a real recorded filename.
    name = (r.get("media_name") or "").strip()
    if not name:
        for path_field in ("file_path", "resolved_file_path"):
            fp = r.get(path_field) or ""
            if fp:
                cand = fp.replace("\\", "/").rsplit("/", 1)[-1]
                if cand:
                    name = cand
                    break
    name = name[:200]

    caption = (r.get("media_caption") or "").strip()
    if len(caption) > 800:
        caption = caption[:800] + "…"

    sha = (r.get("file_hash") or "").strip()
    enc_sha = (r.get("enc_file_hash") or "").strip()
    share_n = share_counts.get(sha, 1) if sha else 1

    # ---- Thumbnail strategy --------------------------------------- #
    # Priority order:
    #   1. file_exists + renderable on disk (image / video / PDF) →
    #      thumbnail from the ORIGINAL file (proper, near-original
    #      visual quality at ~99% size reduction via AVIF).
    #        - images: PIL
    #        - videos: ffmpeg (frame extraction)
    #        - PDFs:   PyMuPDF if installed, else fall through
    #   2. fallback to the WhatsApp ``thumbnail_blob`` (low-res but
    #      always present for images, video posters, PDF first page).
    #   3. nothing → file-type icon in the dashboard.
    #
    # Audio / Word / Excel / generic docs fall through to (2) → (3)
    # since we don't bundle a renderer for them.
    #
    # Dedup key = SHA-256 of either:
    #   - the WhatsApp file_hash (when known) — collapses identical
    #     bytes across chats into one thumbnail on disk
    #   - else the disk path / blob bytes — still stable per-file
    # MUST be hex so the sharded directory path stays filesystem-safe.
    thumb_id = ""
    thumb_job: Optional[tuple[str, bytes, int, int]] = None

    blob = r.get("thumbnail_blob")
    is_image = mime.startswith("image/") and not mime.endswith("svg+xml")
    is_video = mime.startswith("video/")
    is_pdf = mime == "application/pdf" or mime.endswith("/pdf")
    on_disk = bool(r.get("file_exists"))
    disk_path = r.get("resolved_file_path") or r.get("file_path") or ""

    # PDF policy:
    #   • prefer the WhatsApp ``thumbnail_blob`` (the first-page preview
    #     WhatsApp generated — it's already the right thing visually,
    #     no PyMuPDF needed)
    #   • fall back to disk render only when no blob
    # Image / video policy unchanged (disk-first for near-original
    # quality via ffmpeg / PIL).
    use_blob_first = is_pdf
    can_disk_render = on_disk and disk_path and (is_image or is_video or is_pdf)
    if blob and use_blob_first:
        h = hashlib.sha256(bytes(blob)).hexdigest()
        thumb_id = h
        thumb_job = (h, bytes(blob),
                     int(r.get("width") or 0), int(r.get("height") or 0))
    elif can_disk_render:
        seed = (sha or disk_path).encode("utf-8", "ignore")
        key = hashlib.sha256(seed).hexdigest()
        thumb_id = key
        # Pack the fallback blob bytes after the disk path so the
        # worker can fall back to the blob if the disk render fails
        # (PyMuPDF missing, codec missing, unreadable file, etc.).
        payload = _DISK_MARKER + disk_path.encode("utf-8", "ignore")
        if blob:
            payload = payload + b"\x00BLOB:" + bytes(blob)
        thumb_job = (key, payload,
                     int(r.get("width") or 0), int(r.get("height") or 0))
    elif blob:
        h = hashlib.sha256(bytes(blob)).hexdigest()
        thumb_id = h
        thumb_job = (h, bytes(blob),
                     int(r.get("width") or 0), int(r.get("height") or 0))

    flags = 0
    if r.get("from_me"): flags |= FLAG_FROM_ME
    if r.get("is_hd_twin"): flags |= FLAG_HD_TWIN
    if blob: flags |= FLAG_HAS_THUMB
    if int(r.get("message_type") or 0) == 20 or \
            (r.get("type_label") or "").lower() == "sticker":
        flags |= FLAG_IS_STICKER
    if r.get("file_exists"): flags |= FLAG_ON_DISK
    if r.get("media_url"): flags |= FLAG_HAS_URL
    if r.get("media_key"): flags |= FLAG_HAS_KEY

    sha_hex = _b64_to_hex(sha)
    enc_sha_hex = _b64_to_hex(enc_sha)
    arr = [
        int(r["media_row_id"]),                          # 0  id
        int(r["message_id"] or 0),                       # 1  msgId
        conv_idx,                                        # 2  convIdx
        sender_idx,                                      # 3  senderIdx
        int(r.get("timestamp") or 0),                    # 4  ts
        status_idx,                                      # 5  statusIdx
        mime_idx,                                        # 6  mimeIdx
        ext_idx,                                         # 7  extIdx
        int(r.get("file_size") or 0),                    # 8  size
        sha,                                             # 9  hash (base64)
        name,                                            # 10 name
        caption,                                         # 11 caption
        thumb_id,                                        # 12 thumbId
        share_n,                                         # 13 shareCount
        flags,                                           # 14 flags
        int(r.get("width") or 0),                        # 15 w
        int(r.get("height") or 0),                       # 16 h
        int(r.get("duration_ms") or 0),                  # 17 dur
        (r.get("resolved_file_path") or r.get("file_path") or ""),  # 18 path
        r.get("conv_jid") or "",                         # 19 convJid
        r.get("sender_jid") or "",                       # 20 senderJid
        r.get("sender_lid") or "",                       # 21 senderLid
        enc_sha,                                         # 22 encHash (base64)
        r.get("media_url") or "",                        # 23 url
        r.get("recovery_method") or "",                  # 24 recovery
        int(r.get("recovery_timestamp") or 0),           # 25 recoveryTs
        int(r.get("cdn_expiry_ts") or 0),                # 26 expiry
        r.get("media_status") or "",                     # 27 rawStatus
        r.get("assoc_kind") or "",                       # 28 assocKind
        int(r.get("hd_twin_msg_id") or 0),               # 29 hdTwinMsgId
        sha_hex,                                         # 30 sha256 (hex)
        enc_sha_hex,                                     # 31 encSha256 (hex)
    ]
    return arr, thumb_job


def _b64_to_hex(b64: str) -> str:
    """Convert WhatsApp's base64-encoded SHA-256 (32 raw bytes packed
    as 44 base64 chars) to canonical hex.

    Returns "" for empty / malformed inputs — the dashboard JS treats
    "" as "unknown" and never surfaces it as a hash.
    """
    if not b64:
        return ""
    try:
        # Tolerate WhatsApp's occasional missing '=' padding
        s = b64.strip()
        rem = len(s) % 4
        if rem:
            s = s + ("=" * (4 - rem))
        raw = base64.b64decode(s, validate=False)
        # SHA-256 = 32 bytes = 64 hex chars
        if len(raw) != 32:
            return ""
        return raw.hex()
    except Exception:
        return ""


def _emit_orphans(out_dir: Path, orphans: list[dict]
                  ) -> list[tuple[str, bytes, int, int]]:
    """Emit ``data/orphans.js`` (a single chunk pushing to
    ``window.ORPHANS``) and return any thumb jobs.
    """
    rows: list[list] = []
    thumb_jobs: list[tuple[str, bytes, int, int]] = []
    seen: set[str] = set()
    for o in orphans:
        blob = o.get("thumbnail_blob")
        thumb_id = ""
        if blob:
            h = hashlib.sha256(bytes(blob)).hexdigest()
            thumb_id = h
            if h not in seen:
                seen.add(h)
                thumb_jobs.append((h, bytes(blob),
                                   int(o.get("width") or 0),
                                   int(o.get("height") or 0)))
        rows.append([
            int(o["id"]),
            o.get("file_path") or "",
            o.get("file_name") or "",
            o.get("folder") or "",
            int(o.get("file_size") or 0),
            (o.get("mime_type") or "").lower(),
            int(o.get("parsed_date_ts") or 0),
            o.get("file_hash") or "",
            int(o.get("matched_message_id") or 0),
            o.get("matched_conv_name") or "",
            o.get("source_type") or "",
            thumb_id,
            int(o.get("width") or 0),
            int(o.get("height") or 0),
            int(o.get("duration_ms") or 0),
        ])
    body = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    (out_dir / "orphans.js").write_text(
        "window.ORPHANS=" + body + ";", encoding="utf-8"
    )
    return thumb_jobs


# ---------------------------------------------------------------------- #
# Thumbnail emission
# ---------------------------------------------------------------------- #

def _emit_thumbs(thumb_dir: Path,
                 jobs: list[tuple[str, bytes, int, int]],
                 *,
                 quality: str,
                 progress_cb: Optional[Callable] = None,
                 worker_count: int = 0) -> tuple[int, str]:
    """Write each (thumb_id, blob, w, h) into a sharded path
    ``thumb_dir/<aa>/<bb>/<sha>.<ext>``.

    For best visual quality at minimum disk cost we prefer **AVIF**
    output when PIL has the AVIF plugin available (PIL 12+ ships it
    natively).  AVIF beats JPEG by ≈40-50% at the same perceived
    quality and is supported by every modern browser at file://.

    On older PILs we fall back to JPEG with libjpeg-turbo + 4:4:4
    chroma at high quality.  That still produces ≈99% size reduction
    on photo originals.

    Encoding is parallelised across a thread pool — PIL releases the
    GIL inside libjpeg / libavif so threads scale linearly with cores
    on this CPU-bound workload.  Pass ``worker_count > 0`` to override
    the default (``min(cpu_count, 8)``).

    Returns ``(written_count, ext_used)`` so the manifest can record
    the right extension for the dashboard JS to construct ``<img src>``.
    """
    import os as _os
    from concurrent.futures import ThreadPoolExecutor, as_completed

    encoder, ext = _pick_encoder()
    max_dim, jpeg_q, avif_q = _quality_preset(quality)
    n = len(jobs)
    if n == 0:
        return 0, ext

    # Defensive: drop any malformed thumb_id (must be ≥4 hex chars to
    # form a valid sharded path).  Should never happen now that the
    # builder always hex-hashes, but cheap safety net.
    jobs = [j for j in jobs if j[0] and len(j[0]) >= 4
            and all(c in "0123456789abcdef" for c in j[0][:4])]
    n = len(jobs)
    if n == 0:
        return 0, ext

    # Pre-create shard subdirectories to avoid per-write mkdir contention
    seen_dirs: set[Path] = set()
    for h, _, _, _ in jobs:
        d = thumb_dir / h[:2] / h[2:4]
        if d not in seen_dirs:
            d.mkdir(parents=True, exist_ok=True)
            seen_dirs.add(d)

    pil_ok = encoder != "passthrough"
    if not pil_ok:
        # No PIL → pass-through blobs only.  Trivial / fast path.
        written = 0
        for h, blob, _, _ in jobs:
            if not blob or blob.startswith(_DISK_MARKER):
                continue
            p = thumb_dir / h[:2] / h[2:4] / f"{h}.{ext}"
            if not p.exists():
                p.write_bytes(blob)
                written += 1
        if progress_cb:
            progress_cb("Writing thumbnails (sharded by hash prefix)…", n, n)
        return written, ext

    n_workers = worker_count or max(2, min(_os.cpu_count() or 4, 8))

    def _one(job):
        h, payload, w, hgt = job
        path = thumb_dir / h[:2] / h[2:4] / f"{h}.{ext}"
        if path.exists():
            return False
        from PIL import Image

        # Unpack the payload.  Two encodings:
        #   * ``_DISK_MARKER + disk_path [+ b'\x00BLOB:' + blob_bytes]``
        #     → render from disk_path, fall back to blob_bytes if disk
        #       render fails / returns None
        #   * raw blob bytes → JPEG/PNG/AVIF, decode + re-encode
        disk_path = None
        fallback_blob = None
        if payload.startswith(_DISK_MARKER):
            rest = payload[len(_DISK_MARKER):]
            sep = rest.find(b"\x00BLOB:")
            if sep >= 0:
                disk_path = rest[:sep].decode("utf-8", "ignore")
                fallback_blob = rest[sep + len(b"\x00BLOB:"):]
            else:
                disk_path = rest.decode("utf-8", "ignore")
                fallback_blob = None
        else:
            fallback_blob = payload   # also doubles as the source

        def _from_blob(b):
            if not b:
                return None
            try:
                img = Image.open(io.BytesIO(b))
                img.load()
                return img
            except Exception:
                return None

        def _save(img):
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            if max_dim and (img.width > max_dim or img.height > max_dim):
                img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            if encoder == "avif":
                img.save(path, "AVIF", quality=avif_q, speed=8)
            else:
                img.save(path, "JPEG", quality=jpeg_q,
                         optimize=True, subsampling=0,
                         progressive=False)

        # ---- 1. try disk render (if present) ----
        if disk_path:
            try:
                img = _render_disk_thumb(disk_path, max_dim)
                if img is not None:
                    _save(img)
                    return True
            except Exception:
                pass
            # disk render failed — fall through to blob fallback

        # ---- 2. try blob (either the only source, or a fallback) ----
        img = _from_blob(fallback_blob)
        if img is not None:
            try:
                _save(img)
                return True
            except Exception:
                # Save failed — write raw bytes as last resort
                try:
                    fb = thumb_dir / h[:2] / h[2:4] / f"{h}.jpg"
                    if not fb.exists():
                        fb.write_bytes(fallback_blob)
                    return True
                except Exception:
                    pass
        return False

    written = 0
    done = 0
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(_one, j) for j in jobs]
        for fut in as_completed(futures):
            done += 1
            if fut.result():
                written += 1
            if progress_cb and (done % 200 == 0):
                progress_cb("Writing thumbnails (sharded by hash prefix)…",
                            done, n)
    if progress_cb:
        progress_cb("Writing thumbnails (sharded by hash prefix)…", n, n)
    return written, ext


def _render_disk_thumb(disk_path: str, max_dim: int):
    """Open ``disk_path`` and return a PIL Image suitable for thumbnail
    encoding.  Dispatches by file extension (cheaper than MIME re-detect):

      * image  → PIL.Image.open
      * video  → ffmpeg single-frame extract (binary in PATH)
      * pdf    → PyMuPDF (fitz) first-page render — if installed
      * other  → returns None (caller falls through to icon)

    Errors during rendering raise so the surrounding try/except in the
    worker can fall back to the WhatsApp blob.
    """
    from PIL import Image
    p = disk_path.lower()
    # Cheap extension dispatch — no MIME re-detection on every row
    if p.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
                   ".tif", ".tiff", ".heic", ".heif")):
        img = Image.open(disk_path)
        img.load()
        return img

    if p.endswith((".mp4", ".mov", ".m4v", ".3gp", ".webm", ".mkv",
                   ".avi", ".wmv")):
        return _video_frame_to_image(disk_path, max_dim)

    if p.endswith(".pdf"):
        return _pdf_first_page_to_image(disk_path, max_dim)

    # Audio / docs / other → no on-disk renderer.  Caller will fall
    # back to the WhatsApp thumbnail_blob (when present) or the
    # generic file-type icon in the dashboard.
    return None


# Cache lookups so we don't shutil.which() per row
_FFMPEG_BIN: Optional[str] = None
_FFMPEG_PROBED = False
_FITZ_AVAILABLE: Optional[bool] = None


def _video_frame_to_image(disk_path: str, max_dim: int):
    """Use ffmpeg to extract a frame near 1.0 s into the video, return
    as a PIL Image.  ffmpeg is fast (≈40-100 ms / frame), threadsafe
    via subprocess, no Python deps.  Returns None if ffmpeg isn't on
    PATH or the extraction fails.
    """
    global _FFMPEG_BIN, _FFMPEG_PROBED
    if not _FFMPEG_PROBED:
        import shutil as _sh
        _FFMPEG_BIN = _sh.which("ffmpeg")
        _FFMPEG_PROBED = True
    if not _FFMPEG_BIN:
        return None

    import subprocess
    from PIL import Image
    target = max_dim or 224
    # ffmpeg outputs to stdout pipe so we never touch the disk for the
    # intermediate frame — keeps the I/O budget reasonable at scale.
    cmd = [
        _FFMPEG_BIN,
        "-loglevel", "error",
        "-ss", "1.0",          # 1 second in (skip black title cards)
        "-i", disk_path,
        "-frames:v", "1",
        "-vf", f"scale={target}:-2:force_original_aspect_ratio=decrease",
        "-f", "image2",
        "-vcodec", "mjpeg",
        "-q:v", "5",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30,
                              creationflags=getattr(subprocess,
                                  "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0 or not proc.stdout:
            # Some videos are < 1s — retry from t=0
            cmd[3] = "0"
            proc = subprocess.run(cmd, capture_output=True, timeout=30,
                                  creationflags=getattr(subprocess,
                                      "CREATE_NO_WINDOW", 0))
        if proc.returncode == 0 and proc.stdout:
            img = Image.open(io.BytesIO(proc.stdout))
            img.load()
            return img
    except Exception:
        pass
    return None


def _pdf_first_page_to_image(disk_path: str, max_dim: int):
    """Render the first page of ``disk_path`` to a PIL Image via PyMuPDF
    (``pip install pymupdf``).  Returns None if PyMuPDF isn't available.

    PyMuPDF is the lightest pure-pip option for PDF rendering — single
    wheel, no system deps (no Poppler, no Ghostscript).
    """
    global _FITZ_AVAILABLE
    if _FITZ_AVAILABLE is False:
        return None
    try:
        import fitz   # PyMuPDF
        _FITZ_AVAILABLE = True
    except Exception:
        _FITZ_AVAILABLE = False
        return None
    try:
        from PIL import Image
        doc = fitz.open(disk_path)
        if len(doc) == 0:
            doc.close(); return None
        page = doc.load_page(0)
        target = max_dim or 224
        # Compute zoom so the longer side matches target
        rect = page.rect
        zoom = target / max(rect.width, rect.height)
        mtx = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mtx, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        doc.close()
        return img
    except Exception:
        return None


def _pick_encoder() -> tuple[str, str]:
    """Return ``(encoder_name, file_extension)``:
        - ``("avif", "avif")``  if PIL has AVIF support
        - ``("jpeg", "jpg")``    if PIL is installed (always works)
        - ``("passthrough", "jpg")`` if PIL is missing entirely
    AVIF is preferred because it gives the user near-original quality
    at ~50% the JPEG size, which matters at 200k thumbnails.
    """
    try:
        from PIL import Image, features
    except Exception:
        return ("passthrough", "jpg")
    try:
        if features.check("avif"):
            return ("avif", "avif")
    except Exception:
        pass
    return ("jpeg", "jpg")


def _quality_preset(quality: str) -> tuple[int, int, int]:
    """Return ``(max_dim, jpeg_quality, avif_quality)``.

    Sized for "thumbnail with near-original visual quality".  Defaults
    target ~5KB AVIF / ~12KB JPEG per thumb on photo content — a 99%
    reduction from a typical 1-3MB phone photo.
    """
    quality = (quality or "medium").lower()
    if quality == "low":
        return (128, 78, 60)        # ≈2-3KB AVIF / ≈4KB JPEG
    if quality == "high":
        return (384, 92, 80)        # ≈10-15KB AVIF / ≈25KB JPEG
    return (224, 88, 72)            # medium: ≈4-6KB AVIF / ≈12KB JPEG


# ---------------------------------------------------------------------- #
# Summaries
# ---------------------------------------------------------------------- #

def _summarise(media_rows: list[dict],
               share_counts: dict[str, int]) -> tuple[dict, dict]:
    on_disk = 0
    total_bytes = 0
    days: dict[int, int] = {}     # day-bucket-ms -> count
    DAY_MS = 86400000
    min_size = None
    max_size = 0
    for r in media_rows:
        if r.get("file_exists"):
            on_disk += 1
        sz = int(r.get("file_size") or 0)
        if sz:
            total_bytes += sz
            if min_size is None or sz < min_size: min_size = sz
            if sz > max_size: max_size = sz
        ts = int(r.get("timestamp") or 0)
        if ts > 0:
            day = (ts // DAY_MS) * DAY_MS
            days[day] = days.get(day, 0) + 1

    if days:
        start = min(days)
        end = max(days)
        bins = []
        counts = []
        cur = start
        while cur <= end:
            bins.append(cur)
            counts.append(days.get(cur, 0))
            cur += DAY_MS
        hist = {"dayMs": DAY_MS, "startDay": start,
                "bins": bins, "counts": counts}
    else:
        hist = {"dayMs": DAY_MS, "startDay": 0, "bins": [], "counts": []}

    totals = {
        "rows": len(media_rows),
        "onDisk": on_disk,
        "missing": len(media_rows) - on_disk,
        "totalBytes": total_bytes,
        "sharedHashes": sum(1 for n in share_counts.values() if n > 1),
        "uniqueHashes": len(share_counts),
        "minSize": min_size or 0,
        "maxSize": max_size,
    }
    return totals, hist


# ---------------------------------------------------------------------- #
# Manifest emission
# ---------------------------------------------------------------------- #

def _emit_manifest(path: Path, *, case_info: dict, owner_info: dict,
                   scope_label: str, scope_conv_ids: list[int],
                   totals: dict, facets: dict, chunks: list[dict],
                   hist: dict, hide_stickers: bool,
                   include_thumbnails: bool, orphan_count: int,
                   thumb_count: int, thumb_ext: str, sections: dict) -> None:
    manifest = {
        "schemaVersion": 1,
        "generatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        "case": case_info,
        "owner": owner_info,
        "scope": {
            "label": scope_label,
            "convIds": scope_conv_ids,
            "convId": scope_conv_ids[0] if len(scope_conv_ids) == 1 else None,
        },
        "totals": totals,
        "schema": {"cols": SCHEMA_COLS, "flagBits": {
            "fromMe": FLAG_FROM_ME,
            "hdTwin": FLAG_HD_TWIN,
            "hasThumb": FLAG_HAS_THUMB,
            "isSticker": FLAG_IS_STICKER,
            "onDisk": FLAG_ON_DISK,
            "hasUrl": FLAG_HAS_URL,
            "hasKey": FLAG_HAS_KEY,
        }},
        "chunks": chunks,
        "status": facets["status"],
        "mime": facets["mime"],
        "ext": facets["ext"],
        "conv": facets["conv"],
        "sender": facets["sender"],
        "ownerSenderIdx": facets["owner_sender_idx"],
        "hist": hist,
        "hideStickers": hide_stickers,
        "includeThumbnails": include_thumbnails,
        "orphanCount": orphan_count,
        "thumbCount": thumb_count,
        "thumbsBase": "thumbs/",
        "thumbsExt": thumb_ext,
        "sections": sections,
    }
    body = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False)
    path.write_text("window.__MANIFEST=" + body + ";", encoding="utf-8")


# ---------------------------------------------------------------------- #
# Tiny helpers
# ---------------------------------------------------------------------- #

def _h(v) -> str:
    return _html.escape(str(v)) if v not in (None, "") else ""
