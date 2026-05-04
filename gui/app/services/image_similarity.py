"""
Perceptual image similarity search engine.

Multi-hash approach:
  - pHash (perceptual hash) -- captures luminance structure
  - dHash (difference hash) -- captures gradient direction
  - Edge-map hash (Canny edges + pHash) -- captures structural edges

Three-tier matching:
  Tier 1: Exact / near-exact (pHash <= 8 AND dHash <= 5)
  Tier 2: Near-duplicate (pHash <= 16 OR dHash <= 12)
  Tier 3: Template match (edge_hash <= 20) -- same-app-different-data

Requires: imagehash, Pillow, opencv-python-headless (cv2)
"""

from __future__ import annotations

import base64
import logging
import os
import sqlite3
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy-import optional deps so the rest of the app loads even without them.
# ---------------------------------------------------------------------------
_LIBS_AVAILABLE = False
_IMPORT_ERROR_MSG = (
    "Image similarity requires additional libraries.\n"
    "Install them with:\n"
    "  pip install imagehash Pillow"
)


def _ensure_libs():
    """Import heavy deps on first use, raising a clear error if missing.

    Also opportunistically registers pillow-heif (if installed) so HEIC
    files can be hashed.  If the plugin is missing, HEIC files fall
    through to the UNHASHABLE sentinel branch in build_index — they
    don't keep re-surfacing as "new (not yet indexed)" forever.
    """
    global _LIBS_AVAILABLE
    if _LIBS_AVAILABLE:
        return

    try:
        import imagehash  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise ImportError(_IMPORT_ERROR_MSG) from exc

    # Optional HEIC support — if pillow-heif is installed, register it
    # so PIL.Image.open() can decode .heic / .heif files (iPhone photos
    # forwarded to WhatsApp as documents).  Silent fall-through when
    # absent: the build loop's exception path will mark the row
    # UNHASHABLE so it stops being counted as "new".
    try:
        import pillow_heif  # type: ignore
        pillow_heif.register_heif_opener()
    except ImportError:
        logger.debug("pillow-heif not installed — HEIC files will be sentinel-only")
    except Exception as exc:
        logger.debug("pillow-heif registration failed: %s", exc)

    _LIBS_AVAILABLE = True


# ---------------------------------------------------------------------------
# Hash computation helpers
# ---------------------------------------------------------------------------

def _compute_hashes(image_path: str) -> tuple[str, str, str]:
    """Return (phash_hex, dhash_hex, edge_hash_hex) for a single image file.

    Uses Pillow for image loading (handles all formats including animated WebP)
    and numpy for Canny edge detection to avoid cv2.imread animated WebP errors.
    Skips animated WebP/GIF files (stickers) to avoid noisy errors and misleading hashes.
    """
    import imagehash
    import numpy as np
    from PIL import Image, ImageFilter

    img = Image.open(image_path)

    # Skip animated images (WebP stickers, GIFs) -- they produce misleading hashes
    # and can cause noisy errors with cv2 or other downstream consumers.
    try:
        if getattr(img, "n_frames", 1) > 1:
            raise ValueError(f"Animated image skipped: {image_path}")
    except Exception as e:
        if "Animated" in str(e):
            raise
        # n_frames may not be available for all formats; continue if so

    img = img.convert("RGB")

    # Perceptual hash (DCT-based)
    phash = imagehash.phash(img, hash_size=16)

    # Difference hash (gradient-based)
    dhash = imagehash.dhash(img, hash_size=16)

    # Edge-map hash: use Pillow's FIND_EDGES instead of cv2.Canny
    # This avoids OpenCV's animated WebP limitation entirely
    gray = img.convert("L").resize((256, 256), Image.LANCZOS)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_hash = imagehash.phash(edges, hash_size=16)

    return str(phash), str(dhash), str(edge_hash)


def _hamming_distance(hex_a: str, hex_b: str) -> int:
    """Hamming distance between two hex-encoded hashes of equal length."""
    if not hex_a or not hex_b or len(hex_a) != len(hex_b):
        return 999  # incomparable
    # Convert hex to int, XOR, count bits
    xor_val = int(hex_a, 16) ^ int(hex_b, 16)
    return bin(xor_val).count("1")


# ---------------------------------------------------------------------------
# Image extensions we attempt to index.
#
# IMPORTANT: keep these in sync with `_eligible_filter_sql()` below — the
# total-count SQL must enumerate the same set, otherwise rows that are
# eligible-by-MIME but-not-by-our-filter (historically: HEIC) inflate the
# "N new (not yet indexed)" counter forever, even though they will never
# actually be indexed.  HEIC is included here so it gets at least an
# UNHASHABLE sentinel row when Pillow can't decode it (no pillow-heif
# plugin installed), which prevents it from re-surfacing as "new" on
# subsequent Update Index runs.
# ---------------------------------------------------------------------------
_IMAGE_MIME_PREFIXES = (
    "image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp",
    "image/heic", "image/heif",
)
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif"}


def _eligible_filter_sql() -> tuple[str, list]:
    """SQL fragment that selects exactly the rows `build_index` would process.

    Used by `count_index_status` so the total never drifts from the
    indexable set.  Returns a (sql, params) tuple where the SQL is a
    boolean predicate to be ANDed with the caller's WHERE clause.
    """
    mime_clauses = " OR ".join(f"me.mime_type LIKE '{p}%'" for p in _IMAGE_MIME_PREFIXES)
    # GLOB is case-sensitive, so spell out [aA] for each letter.  We
    # apply LOWER() to the path so the pattern can stay all-lowercase.
    path_expr = "LOWER(COALESCE(me.resolved_file_path, me.file_path, ''))"
    ext_clauses = " OR ".join(f"{path_expr} GLOB '*{ext}'" for ext in sorted(_IMAGE_EXTENSIONS))
    sql = (
        "me.file_exists = 1 "
        "AND me.resolved_file_path IS NOT NULL AND me.resolved_file_path != '' "
        f"AND ({mime_clauses} OR {ext_clauses})"
    )
    return sql, []


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class ImageSimilarityEngine:
    """Perceptual image similarity engine backed by SQLite hash storage."""

    # Thresholds for 256-bit hashes (hash_size=16, range 0–256).
    # Calibrated against real-world payment / banking app
    # screenshot sets (typical "same template, different
    # transaction amount" use case).
    TIER1_PHASH = 15    # Exact / near-exact: same image, minor compression
    TIER1_DHASH = 10
    TIER2_PHASH = 30    # Near-duplicate: resize, recompression, minor text diff
    TIER2_DHASH = 20
    TIER3_EDGE = 50     # Template: same app layout, different data
    TIER3_PHASH = 65    # Also template if pHash is in the ballpark
    TIER3_DHASH = 30    # Or if dHash is close (gradient structure matches)

    def __init__(self, db):
        """
        Parameters
        ----------
        db : app.services.database.Database
            The Database singleton (provides execute, fetchall, scalar,
            execute_write, checkpoint_and_reconnect).
        """
        self._db = db

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        """Create the image_hash table if it doesn't exist."""
        self._db.execute_write("""
            CREATE TABLE IF NOT EXISTS image_hash (
                message_id  INTEGER PRIMARY KEY,
                phash       TEXT NOT NULL,
                dhash       TEXT NOT NULL,
                edge_hash   TEXT NOT NULL
            )
        """)
        self._db.checkpoint_and_reconnect()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_indexed(self) -> bool:
        """Return True if the image_hash table exists and has rows."""
        try:
            count = self._db.scalar(
                "SELECT COUNT(*) FROM image_hash"
            )
            return (count or 0) > 0
        except Exception:
            return False

    def count_index_status(self) -> dict:
        """Return index statistics: indexed count, total eligible, new unindexed.

        Returns dict with keys: indexed, total, new, percentage,
        unique_paths, unique_indexed.

        The total uses the same eligibility filter as `build_index`, so
        the "N new (not yet indexed)" counter never drifts because of
        SQL-vs-Python filter mismatch (e.g. HEIC files counted by MIME
        but skipped by the Python extension whitelist used to live).
        """
        try:
            indexed = self._db.scalar(
                "SELECT COUNT(*) FROM image_hash"
            ) or 0
        except Exception:
            indexed = 0

        eligible_sql, _ = _eligible_filter_sql()
        try:
            total = self._db.scalar(
                f"SELECT COUNT(*) FROM media me "
                f"JOIN message m ON m.id = me.message_id "
                f"WHERE {eligible_sql}"
            ) or 0
        except Exception:
            total = 0

        # Unique-paths stats — many message rows can share the same
        # resolved_file_path (hash-linked siblings, forwards), so the
        # raw "N new" can be misleading.  Surface unique-file numbers
        # so the UI can reassure the user that work is bounded.
        try:
            unique_paths = self._db.scalar(
                f"SELECT COUNT(DISTINCT me.resolved_file_path) FROM media me "
                f"JOIN message m ON m.id = me.message_id "
                f"WHERE {eligible_sql}"
            ) or 0
        except Exception:
            unique_paths = 0
        try:
            unique_indexed = self._db.scalar(
                f"SELECT COUNT(DISTINCT me.resolved_file_path) FROM media me "
                f"JOIN image_hash h ON h.message_id = me.message_id "
                f"WHERE {eligible_sql}"
            ) or 0
        except Exception:
            unique_indexed = 0

        new = max(0, total - indexed)
        pct = round(indexed / total * 100, 1) if total > 0 else 0.0
        return {
            "indexed": indexed, "total": total, "new": new,
            "percentage": pct,
            "unique_paths": unique_paths,
            "unique_indexed": unique_indexed,
        }

    def build_index(self, progress_callback: Optional[Callable[[int, int], None]] = None) -> int:
        """Compute hashes for all indexable images and store in image_hash.

        Parameters
        ----------
        progress_callback : callable(current, total), optional
            Called after each image is processed.

        Returns
        -------
        int
            Number of images successfully indexed.
        """
        _ensure_libs()
        self._ensure_table()

        # Gather candidate images from media table
        rows = self._db.fetchall("""
            SELECT me.message_id, me.resolved_file_path, me.mime_type
            FROM media me
            WHERE me.file_exists = 1
              AND me.resolved_file_path IS NOT NULL
              AND me.resolved_file_path != ''
        """)

        # Filter to image types
        candidates = []
        for r in rows:
            msg_id = r[0]
            fpath = r[1]
            mime = (r[2] or "").lower()

            # Check mime type
            is_image_mime = any(mime.startswith(p) for p in _IMAGE_MIME_PREFIXES)
            # Also check extension as fallback
            ext = os.path.splitext(fpath)[1].lower() if fpath else ""
            is_image_ext = ext in _IMAGE_EXTENSIONS

            if is_image_mime or is_image_ext:
                candidates.append((msg_id, fpath))

        total = len(candidates)
        if total == 0:
            return 0

        # Find already-indexed message IDs to skip
        try:
            existing = {
                r[0] for r in self._db.fetchall("SELECT message_id FROM image_hash")
            }
        except Exception:
            existing = set()

        indexed = 0
        skipped_unreadable = 0
        batch = []
        batch_size = 100

        # Sentinel: an empty hash marks rows that can't be
        # indexed (file missing, animated WebP, corrupt, etc.).
        # Persisting the sentinel prevents a subsequent "Update
        # Index" run from re-counting those rows as "new".
        UNHASHABLE = ""

        # Per-path hash cache.  Many message rows share the same
        # ``resolved_file_path`` (hash-linked siblings, forwards,
        # broadcast spam).  Caching the hash triple per unique
        # path means each on-disk file is hashed exactly once
        # per ``build_index`` run; subsequent ``message_id``s
        # that share the path just reuse the cached values.
        path_cache: dict[str, tuple[str, str, str]] = {}
        cache_hits = 0

        for i, (msg_id, fpath) in enumerate(candidates):
            if progress_callback:
                progress_callback(i + 1, total)

            if msg_id in existing:
                indexed += 1
                continue

            cached = path_cache.get(fpath)
            if cached is not None:
                phash, dhash, edge_hash = cached
                batch.append((msg_id, phash, dhash, edge_hash))
                cache_hits += 1
                indexed += 1
            elif not os.path.isfile(fpath):
                # Mark as unhashable so we don't revisit it on the next run
                triple = (UNHASHABLE, UNHASHABLE, UNHASHABLE)
                path_cache[fpath] = triple
                batch.append((msg_id, *triple))
                skipped_unreadable += 1
                indexed += 1
            else:
                try:
                    phash, dhash, edge_hash = _compute_hashes(fpath)
                    triple = (phash, dhash, edge_hash)
                    path_cache[fpath] = triple
                    batch.append((msg_id, *triple))
                    indexed += 1
                except Exception as exc:
                    logger.debug("Hash failed for %s: %s", fpath, exc)
                    # Also sentinel — same reason as the missing-file branch.
                    # Caching the failure prevents us from retrying the same
                    # corrupt/animated/HEIC file for every sibling message_id.
                    triple = (UNHASHABLE, UNHASHABLE, UNHASHABLE)
                    path_cache[fpath] = triple
                    batch.append((msg_id, *triple))
                    skipped_unreadable += 1
                    indexed += 1

            if len(batch) >= batch_size:
                self._flush_batch(batch)
                batch.clear()

        if batch:
            self._flush_batch(batch)

        if skipped_unreadable:
            logger.info("build_index: %d images marked unhashable "
                        "(missing on disk / animated / corrupt / HEIC w/o "
                        "plugin) — written as sentinel so they don't "
                        "re-surface as 'new'", skipped_unreadable)
        if cache_hits:
            logger.info("build_index: %d cache hits — %d unique on-disk "
                        "files hashed for %d message rows",
                        cache_hits, len(path_cache), len(candidates))

        self._db.checkpoint_and_reconnect()
        return indexed

    def find_similar(self, query_message_id: int, top_k: int = 50) -> list[dict]:
        """Find images similar to the given message_id.

        Returns list of dicts sorted by best tier, then lowest distance.
        """
        _ensure_libs()

        # Get the query hashes
        row = self._db.fetchone(
            "SELECT phash, dhash, edge_hash FROM image_hash WHERE message_id = ?",
            (query_message_id,),
        )
        if not row:
            raise ValueError(
                f"Message {query_message_id} not found in image_hash index. "
                "Run build_index() first."
            )

        return self._search(str(row[0]), str(row[1]), str(row[2]),
                            exclude_msg_id=query_message_id, top_k=top_k)

    def find_similar_by_path(self, image_path: str, top_k: int = 50) -> list[dict]:
        """Find images similar to an arbitrary image file on disk.

        The image does not need to be in the database.
        """
        _ensure_libs()

        if not os.path.isfile(image_path):
            raise FileNotFoundError(f"Image not found: {image_path}")

        phash, dhash, edge_hash = _compute_hashes(image_path)
        logger.info(
            "find_similar_by_path: path=%s  phash=%s  dhash=%s",
            image_path[-80:], phash[:20], dhash[:20],
        )
        return self._search(phash, dhash, edge_hash, exclude_msg_id=None, top_k=top_k)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _flush_batch(self, batch: list[tuple]) -> None:
        """Insert a batch of (message_id, phash, dhash, edge_hash) rows."""
        conn = self._db._get_write_conn()
        conn.executemany(
            "INSERT OR REPLACE INTO image_hash (message_id, phash, dhash, edge_hash) "
            "VALUES (?, ?, ?, ?)",
            batch,
        )
        conn.commit()

    def _search(
        self,
        q_phash: str,
        q_dhash: str,
        q_edge: str,
        exclude_msg_id: Optional[int],
        top_k: int,
    ) -> list[dict]:
        """Brute-force scan of image_hash table and classify matches.

        On every call, this also stores diagnostic information on
        ``self.last_search_diagnostics`` so the UI can render a
        helpful "no match" explanation when the result list is empty:
        scan size, nearest neighbour, and the active tier thresholds.
        """

        all_hashes = self._db.fetchall("SELECT message_id, phash, dhash, edge_hash FROM image_hash WHERE phash != ''")
        logger.info(
            "_search: scanning %d indexed hashes (exclude_msg_id=%s)",
            len(all_hashes), exclude_msg_id,
        )

        results = []
        # Track the nearest-by-phash row so we can explain "no match"
        # to the user later (instead of just an unhelpful empty card).
        nearest_phash = (999, 999, 999, None)  # phash_d, dhash_d, edge_d, msg_id
        nearest_edge  = (999, 999, 999, None)
        for r in all_hashes:
            msg_id = r[0]
            if msg_id == exclude_msg_id:
                continue

            phash_dist = _hamming_distance(q_phash, str(r[1]))
            dhash_dist = _hamming_distance(q_dhash, str(r[2]))
            edge_dist = _hamming_distance(q_edge, str(r[3]))

            if phash_dist + dhash_dist < nearest_phash[0] + nearest_phash[1]:
                nearest_phash = (phash_dist, dhash_dist, edge_dist, msg_id)
            if edge_dist < nearest_edge[2]:
                nearest_edge = (phash_dist, dhash_dist, edge_dist, msg_id)

            # Classify tier
            tier = self._classify_tier(phash_dist, dhash_dist, edge_dist)
            if tier is None:
                continue

            results.append({
                "message_id": msg_id,
                "tier": tier,
                "phash_dist": phash_dist,
                "dhash_dist": dhash_dist,
                "edge_dist": edge_dist,
            })

        # Stash diagnostics for the UI's no-match explanation
        self.last_search_diagnostics = {
            "scanned": len(all_hashes),
            "exclude_msg_id": exclude_msg_id,
            "nearest_phash": {
                "phash_dist": nearest_phash[0],
                "dhash_dist": nearest_phash[1],
                "edge_dist":  nearest_phash[2],
                "message_id": nearest_phash[3],
            },
            "nearest_edge": {
                "phash_dist": nearest_edge[0],
                "dhash_dist": nearest_edge[1],
                "edge_dist":  nearest_edge[2],
                "message_id": nearest_edge[3],
            },
            "thresholds": {
                "tier1_phash": self.TIER1_PHASH, "tier1_dhash": self.TIER1_DHASH,
                "tier2_phash": self.TIER2_PHASH, "tier2_dhash": self.TIER2_DHASH,
                "tier3_phash": self.TIER3_PHASH, "tier3_dhash": self.TIER3_DHASH,
                "tier3_edge":  self.TIER3_EDGE,
            },
        }

        if not results:
            logger.warning(
                "_search: zero matches in %d indexed hashes — nearest was "
                "msg_id=%s (phash_d=%d, dhash_d=%d, edge_d=%d). "
                "Tier thresholds: T1 phash<=%d & dhash<=%d, T2 phash<=%d | "
                "dhash<=%d, T3 edge<=%d | phash<=%d | dhash<=%d.",
                len(all_hashes), nearest_phash[3], nearest_phash[0],
                nearest_phash[1], nearest_phash[2],
                self.TIER1_PHASH, self.TIER1_DHASH,
                self.TIER2_PHASH, self.TIER2_DHASH,
                self.TIER3_EDGE, self.TIER3_PHASH, self.TIER3_DHASH,
            )

        # Sort: tier ascending, then sum of distances ascending
        results.sort(key=lambda x: (x["tier"], x["phash_dist"] + x["dhash_dist"]))
        results = results[:top_k]

        # Enrich with file_path and thumbnail
        self._enrich_results(results)
        return results

    def expand_search(
        self,
        seed_results: list[dict],
        original_query_id: Optional[int] = None,
        max_rounds: int = 3,
        top_k: int = 500,
    ) -> list[dict]:
        """Flood-fill expansion: use every previously found image
        as a fresh query to find further matches.

        Catches images that are similar to a match but not to the
        original query — e.g. when the same app's screen evolves
        across layout versions, the chain v1 → v1' → v2 only
        connects through intermediate matches.

        Parameters
        ----------
        seed_results : list of dicts from a previous find_similar_* call
        original_query_id : message_id of the original query (excluded from results)
        max_rounds : maximum expansion rounds (usually converges in 2-3)
        top_k : maximum total results to return

        Returns
        -------
        Combined list of all matches (original + expanded), sorted by tier + distance.
        Each result has an extra 'expansion_round' field (0 = original, 1+ = expanded).
        """
        all_hashes = self._db.fetchall(
            "SELECT message_id, phash, dhash, edge_hash FROM image_hash WHERE phash != ''"
        )
        hash_map = {r[0]: (str(r[1]), str(r[2]), str(r[3])) for r in all_hashes}

        # Start with seed results
        found_ids = set()
        if original_query_id:
            found_ids.add(original_query_id)
        all_results = {}
        for r in seed_results:
            mid = r["message_id"]
            found_ids.add(mid)
            all_results[mid] = dict(r, expansion_round=0)

        # Flood-fill: each round uses NEW matches as queries
        frontier = set(all_results.keys())

        for round_num in range(1, max_rounds + 1):
            new_found = {}
            for qid in frontier:
                if qid not in hash_map:
                    continue
                qh = hash_map[qid]
                for mid, h in hash_map.items():
                    if mid in found_ids or mid in new_found:
                        continue
                    pd = _hamming_distance(qh[0], h[0])
                    dd = _hamming_distance(qh[1], h[1])
                    ed = _hamming_distance(qh[2], h[2])
                    tier = self._classify_tier(pd, dd, ed)
                    if tier is not None:
                        new_found[mid] = {
                            "message_id": mid,
                            "tier": tier,
                            "phash_dist": pd,
                            "dhash_dist": dd,
                            "edge_dist": ed,
                            "expansion_round": round_num,
                        }

            if not new_found:
                break

            found_ids |= set(new_found.keys())
            all_results.update(new_found)
            frontier = set(new_found.keys())

            if len(all_results) >= top_k:
                break

        # Sort and trim
        combined = sorted(
            all_results.values(),
            key=lambda x: (x["expansion_round"], x["tier"], x["phash_dist"] + x["dhash_dist"]),
        )[:top_k]

        self._enrich_results(combined)
        return combined

    def _classify_tier(self, phash_dist: int, dhash_dist: int, edge_dist: int) -> Optional[int]:
        """Return tier (1, 2, 3) or None if no match.

        Thresholds calibrated for 256-bit hashes (hash_size=16)
        against real-world payment / banking app screenshot
        sets: identical copies sit near p=8 / d=4, while
        same-layout-different-data variants spread across p=20-60
        / d=4-25 / e=24-48.
        """
        if phash_dist <= self.TIER1_PHASH and dhash_dist <= self.TIER1_DHASH:
            return 1
        if phash_dist <= self.TIER2_PHASH or dhash_dist <= self.TIER2_DHASH:
            return 2
        # Template match: ANY of the three hashes being close enough
        if edge_dist <= self.TIER3_EDGE or phash_dist <= self.TIER3_PHASH or dhash_dist <= self.TIER3_DHASH:
            return 3
        return None

    def _enrich_results(self, results: list[dict]) -> None:
        """Add file_path, thumb, conversation_id, conv_name,
        sender_name, timestamp, ``from_me`` and ``file_hash`` to
        each result.

        ``from_me`` is forensically critical — without it, a
        visual-mode hit on a message the device owner actually
        sent would render as "Received" in the UI.  Surfacing
        ``file_hash`` here lets the UI build SHA-256 jump-links
        and dedup annotations without a second round-trip to
        the DB.
        """
        if not results:
            return

        msg_ids = [r["message_id"] for r in results]

        # Owner-name fallback for from_me=1 rows (WhatsApp leaves
        # message.sender_id NULL on outgoing messages, so the contact
        # JOIN below produces empty sender_name without this).
        owner_name = ""
        try:
            row = self._db.fetchone(
                "SELECT value FROM case_metadata WHERE key = 'device_owner_name'"
            )
            if row and row[0]:
                owner_name = row[0]
        except Exception:
            pass

        # Batch fetch media info -- build a parameterized IN clause
        placeholders = ",".join("?" * len(msg_ids))
        rows = self._db.fetchall(
            f"SELECT me.message_id, "
            f"       COALESCE(me.resolved_file_path, me.file_path, '') AS fpath, "
            f"       me.thumbnail_blob, "
            f"       m.conversation_id, "
            f"       COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name, "
            f"       COALESCE(NULLIF(c.resolved_name,''), NULLIF(c.display_name,''), "
            f"               CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != '' "
            f"                    THEN '+' || c.phone_number ELSE '' END, '') AS sender_name, "
            f"       m.timestamp, "
            f"       m.from_me, "
            f"       me.file_hash "
            f"FROM media me "
            f"JOIN message m ON m.id = me.message_id "
            f"LEFT JOIN conversation conv ON conv.id = m.conversation_id "
            f"LEFT JOIN contact c ON c.id = m.sender_id "
            f"WHERE me.message_id IN ({placeholders})",
            tuple(msg_ids),
        )

        lookup = {}
        for r in rows:
            thumb_b64 = ""
            if r[2]:
                try:
                    if isinstance(r[2], bytes):
                        thumb_b64 = base64.b64encode(r[2]).decode("ascii")
                    else:
                        thumb_b64 = str(r[2])
                except Exception:
                    pass
            from_me = bool(r[7])
            sender_name = r[5] or ""
            if not sender_name and from_me:
                sender_name = (owner_name + " (You)") if owner_name else "You"
            lookup[r[0]] = {
                "file_path": r[1] or "",
                "thumb": thumb_b64,
                "conversation_id": r[3],
                "conv_name": r[4] or "",
                "sender_name": sender_name,
                "timestamp": r[6] or 0,
                "from_me": from_me,
                "file_hash": r[8] or "",
            }

        for result in results:
            info = lookup.get(result["message_id"], {})
            result["file_path"] = info.get("file_path", "")
            result["thumb"] = info.get("thumb", "")
            result["conversation_id"] = info.get("conversation_id", 0)
            result["conv_name"] = info.get("conv_name", "")
            result["sender_name"] = info.get("sender_name", "")
            result["timestamp"] = info.get("timestamp", 0)
            result["from_me"] = info.get("from_me", False)
            result["file_hash"] = info.get("file_hash", "")

    # ------------------------------------------------------------------
    # Exact-duplicate matching via media.file_hash (SHA-256)
    # ------------------------------------------------------------------

    def find_exact_duplicates_by_msg_id(self, message_id: int) -> list[dict]:
        """Return EVERY share instance of the same SHA-256 as message_id.

        Unlike find_similar (perceptual), this returns only rows whose
        media.file_hash exactly equals the query's file_hash — forensically
        strong "same file" evidence (not "looks similar"). One row per share
        instance, so if the same image was forwarded 724 times, you get 724
        rows enriched with conversation / sender / timestamp.
        """
        row = self._db.fetchone(
            "SELECT file_hash FROM media WHERE message_id = ?",
            (message_id,),
        )
        if not row or not row[0]:
            return []
        return self._search_by_file_hash(row[0], exclude_msg_id=None)

    def find_exact_duplicates_by_hash(self, file_hash: str) -> list[dict]:
        """Return every share instance for a given SHA-256 (base64 or hex)."""
        if not file_hash:
            return []
        return self._search_by_file_hash(file_hash, exclude_msg_id=None)

    def find_exact_duplicates_by_path(self, image_path: str) -> list[dict]:
        """Hash the file on disk (SHA-256, base64) and return every matching share.

        WhatsApp's media.file_hash is the SHA-256 of the decrypted bytes,
        stored base64. We match the user's local file by computing the same.
        """
        import hashlib

        if not os.path.isfile(image_path):
            raise FileNotFoundError(f"Image not found: {image_path}")

        sha = hashlib.sha256()
        with open(image_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                sha.update(chunk)
        digest = sha.digest()
        b64 = base64.b64encode(digest).decode("ascii")
        hex_digest = digest.hex()

        # Try base64 first (WhatsApp's native format), then hex as a fallback.
        rows = self._search_by_file_hash(b64, exclude_msg_id=None)
        if rows:
            for r in rows:
                r["query_hash_b64"] = b64
                r["query_hash_hex"] = hex_digest
            return rows
        rows = self._search_by_file_hash(hex_digest, exclude_msg_id=None)
        for r in rows:
            r["query_hash_b64"] = b64
            r["query_hash_hex"] = hex_digest
        return rows

    def _search_by_file_hash(self, file_hash: str, exclude_msg_id) -> list[dict]:
        """Fetch every media row sharing the exact file_hash, enriched.

        Also includes ORPHANED MEDIA — files in WhatsApp's media folder
        with no surviving message link (cleared chats, reinstalls,
        deleted conversations, GBWhatsApp).  Forensically these are
        critical: an orphaned file matching the queried SHA-256 means
        the file existed on the device at some point but the chat
        record is gone.  Marked with ``is_orphan=True`` so UI code can
        distinguish them from message-bound results.

        Returns results shaped like perceptual-hash matches (message_id,
        conversation_id, conv_name, sender_name, timestamp, file_path, thumb)
        plus `match_mode='exact'` and `tier=0` so UI code can distinguish them.
        """
        # Resolve the device-owner saved name once per call so
        # from_me=1 rows (where WhatsApp leaves message.sender_id
        # NULL on outgoing messages) get a real sender_name in
        # the result dicts instead of a generic "You".
        owner_name = ""
        try:
            row = self._db.fetchone(
                "SELECT value FROM case_metadata WHERE key = 'device_owner_name'"
            )
            if row and row[0]:
                owner_name = row[0]
        except Exception:
            pass

        rows = self._db.fetchall(
            "SELECT me.message_id, "
            "       COALESCE(me.resolved_file_path, me.file_path, '') AS fpath, "
            "       me.thumbnail_blob, "
            "       m.conversation_id, "
            "       COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name, "
            "       COALESCE(NULLIF(c.resolved_name,''), NULLIF(c.display_name,''), "
            "               CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != '' "
            "                    THEN '+' || c.phone_number ELSE '' END, '') AS sender_name, "
            "       m.timestamp, m.from_me, me.file_size, me.mime_type, me.file_hash "
            "FROM media me "
            "JOIN message m ON m.id = me.message_id "
            "LEFT JOIN conversation conv ON conv.id = m.conversation_id "
            "LEFT JOIN contact c ON c.id = m.sender_id "
            "WHERE me.file_hash = ? "
            "ORDER BY m.timestamp ASC",
            (file_hash,),
        )

        results: list[dict] = []
        for r in rows:
            if exclude_msg_id is not None and r[0] == exclude_msg_id:
                continue
            thumb_b64 = ""
            if r[2]:
                try:
                    thumb_b64 = (
                        base64.b64encode(r[2]).decode("ascii")
                        if isinstance(r[2], bytes) else str(r[2])
                    )
                except Exception:
                    pass
            from_me = bool(r[7])
            sender_name = r[5] or ""
            # Owner-sent fallback: real saved name beats the generic "You".
            if not sender_name and from_me:
                sender_name = (owner_name + " (You)") if owner_name else "You"
            results.append({
                "message_id": r[0],
                "file_path": r[1],
                "thumb": thumb_b64,
                "conversation_id": r[3],
                "conv_name": r[4] or "",
                "sender_name": sender_name,
                "timestamp": r[6] or 0,
                "from_me": from_me,
                "file_size": r[8] or 0,
                "mime_type": r[9] or "",
                "file_hash": r[10] or "",
                # Distances are all zero for exact hash matches
                "tier": 0,
                "phash_dist": 0, "dhash_dist": 0, "edge_dist": 0,
                "match_mode": "exact",
                "is_orphan": False,
            })

        # ----- Orphaned media with the same SHA-256 -----
        # Their file_hash is set by the Orphaned Media page's hash-match
        # pass.  If the user hasn't run that pass yet, the table simply
        # has NULL hashes and contributes nothing here — gracefully
        # silent.  When matches exist, surface them so the analyst sees
        # every place this byte-identical file lived on the device,
        # even ones the chat history can no longer prove.
        try:
            orph_rows = self._db.fetchall(
                "SELECT om.id, om.file_path, om.thumbnail_blob, "
                "       om.matched_conversation_id, om.matched_conv_name, "
                "       om.parsed_date_ts, om.file_size, om.mime_type, "
                "       om.file_hash, om.matched_message_id, om.source_type, "
                "       om.file_name, om.folder "
                "FROM orphaned_media om "
                "WHERE om.file_hash = ? "
                "ORDER BY om.parsed_date_ts ASC",
                (file_hash,),
            )
        except Exception:
            # orphaned_media table may not exist on older cases —
            # skip silently rather than break the search.
            orph_rows = []

        # De-dup: if the orphan is already represented by a media row
        # in `results` (because it was hash-linked to a message), skip
        # it.  The dedup key is normalised file_path; orphan and
        # media-row paths can differ in case/separators on Windows so
        # we lower-case + replace separators before comparing.
        def _norm(p: str) -> str:
            return (p or "").lower().replace("\\", "/")
        seen_paths = {_norm(r["file_path"]) for r in results}

        for o in orph_rows:
            ofp = o[1] or ""
            if _norm(ofp) in seen_paths:
                continue
            seen_paths.add(_norm(ofp))
            othumb_b64 = ""
            if o[2]:
                try:
                    othumb_b64 = (
                        base64.b64encode(o[2]).decode("ascii")
                        if isinstance(o[2], bytes) else str(o[2])
                    )
                except Exception:
                    pass
            # Orphaned files have no message_id of their own.  When the
            # hash-matcher linked this orphan to a message, surface
            # that message_id so the UI's "go to chat" path still
            # works.  Otherwise leave it 0 — UI handles is_orphan=True
            # by showing "Orphaned (no chat record)" instead of a
            # navigate link.
            sender_label = ""
            st = (o[10] or "").lower()
            if st == "sent":
                sender_label = "You (orphaned)"
            elif st == "received":
                sender_label = "Received (orphaned)"
            else:
                sender_label = f"Orphaned ({st or 'unknown'})"
            results.append({
                "message_id": o[9] or 0,
                "file_path": ofp,
                "thumb": othumb_b64,
                "conversation_id": o[3] or 0,
                "conv_name": o[4] or "Orphaned (no chat record)",
                "sender_name": sender_label,
                "timestamp": o[5] or 0,
                "from_me": (st == "sent"),
                "file_size": o[6] or 0,
                "mime_type": o[7] or "",
                "file_hash": o[8] or "",
                "tier": 0,
                "phash_dist": 0, "dhash_dist": 0, "edge_dist": 0,
                "match_mode": "exact",
                "is_orphan": True,
                "orphan_source_type": st,
                "orphan_id": o[0],
                "orphan_file_name": o[11] or "",
                "orphan_folder": o[12] or "",
            })
        return results
