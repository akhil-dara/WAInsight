"""
Ingestion of live location route points from WhatsApp's location.db.

location.db is a separate database that stores temporary real-time
location data during live location sharing sessions. It contains:

  - location_cache: Multiple GPS coordinate points with accuracy, speed,
    and bearing — forming the route/trajectory of a live location share.
  - location_sharer: Records of who has shared live location.

The database is typically empty (WhatsApp purges it after
sessions end), but route data is often recoverable from the
WAL (Write-Ahead Log) file.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection

logger = logging.getLogger(__name__)


def ingest_location_db(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest live location route points from location.db.

    This stage is optional — if location.db is not present in the
    extraction, it is silently skipped.

    Returns:
        Total number of location_point + location_sharer records ingested.
    """
    try:
        loc_db = db_manager.get_source("location.db")
    except (FileNotFoundError, Exception) as e:
        logger.info("location.db not found — skipping route point ingestion (%s)", e)
        return 0

    total = 0
    total += _ingest_location_cache(loc_db, analysis_conn)
    total += _ingest_location_sharer(loc_db, analysis_conn)

    if progress_callback:
        progress_callback(total, total)
    return total


def _ingest_location_cache(loc_db, analysis_conn: AnalysisConnection) -> int:
    """Read location_cache table and insert route points."""
    try:
        # Check if table exists
        exists = loc_db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='location_cache'"
        ).fetchone()
        if not exists:
            logger.info("location_cache table not found in location.db")
            return 0
    except Exception as e:
        logger.warning("Error checking location_cache: %s", e)
        return 0

    # Get available columns (WhatsApp uses "latitud" — a typo in the schema)
    try:
        cols_info = loc_db.execute("PRAGMA table_info(location_cache)").fetchall()
        col_names = {c[1] for c in cols_info}
    except Exception as e:
        logger.warning("Could not read location_cache columns: %s", e)
        return 0

    # Determine latitude column name (WhatsApp's typo: "latitud" vs "latitude")
    lat_col = "latitud" if "latitud" in col_names else "latitude"
    lon_col = "longitude" if "longitude" in col_names else "longitud"

    select_cols = ["_id", "jid", lat_col, lon_col]
    for optional in ("accuracy", "speed", "bearing", "location_ts"):
        if optional in col_names:
            select_cols.append(optional)
        else:
            select_cols.append(f"NULL AS {optional}")

    try:
        rows = loc_db.execute(
            f"SELECT {', '.join(select_cols)} FROM location_cache "
            f"WHERE {lat_col} IS NOT NULL AND {lon_col} IS NOT NULL"
        ).fetchall()
    except Exception as e:
        logger.warning("Failed to read location_cache: %s", e)
        return 0

    if not rows:
        logger.info("location_cache is empty (0 rows)")
        return 0

    logger.info("Found %d location cache points in location.db", len(rows))

    # Build JID → contact_id mapping
    # location_cache.jid contains LID strings like "1483464537559458@lid"
    jid_to_contact: dict[str, int] = {}
    try:
        for r in analysis_conn.fetchall(
            "SELECT c.phone_jid, c.id FROM contact c WHERE c.phone_jid IS NOT NULL"
        ):
            jid_to_contact[r[0]] = r[1]
        for r in analysis_conn.fetchall(
            "SELECT c.lid_jid, c.id FROM contact c WHERE c.lid_jid IS NOT NULL"
        ):
            jid_to_contact[r[0]] = r[1]
    except Exception:
        pass

    # Build location_id lookup: contact_id → list of (location.id, start_ts, end_ts)
    # to correlate cache points with live location messages
    location_sessions: dict[int, list[tuple[int, int, int]]] = {}
    try:
        for r in analysis_conn.fetchall(
            "SELECT l.id, m.sender_id, m.timestamp, l.live_duration "
            "FROM location l JOIN message m ON m.id = l.message_id "
            "WHERE l.is_live = 1"
        ):
            loc_id, sender_id, ts, dur = r
            if sender_id and ts:
                end_ts = ts + (dur or 3600) * 1000  # duration in seconds → ms
                location_sessions.setdefault(sender_id, []).append((loc_id, ts, end_ts))
    except Exception:
        pass

    insert_sql = """
        INSERT OR IGNORE INTO location_point (
            location_id, contact_id, latitude, longitude,
            accuracy, speed, bearing, timestamp, source_row_id
        ) VALUES (?,?,?,?,?,?,?,?,?)
    """

    count = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()
        for row in rows:
            source_id = row[0]
            jid = row[1]
            lat = row[2]
            lon = row[3]
            accuracy = row[4] if len(row) > 4 else None
            speed = row[5] if len(row) > 5 else None
            bearing = row[6] if len(row) > 6 else None
            location_ts = row[7] if len(row) > 7 else None

            # Clean up invalid values
            if accuracy is not None and accuracy < 0:
                accuracy = None
            if speed is not None and speed < 0:
                speed = None
            if bearing is not None and bearing < 0:
                bearing = None

            # Resolve JID to contact_id
            contact_id = None
            if jid:
                # Try exact match first
                contact_id = jid_to_contact.get(jid)
                # Try stripping @lid or @s.whatsapp.net suffix
                if not contact_id and "@" in jid:
                    bare = jid.split("@")[0]
                    for k, v in jid_to_contact.items():
                        if k.startswith(bare):
                            contact_id = v
                            break

            # Try to correlate with a live location session
            location_id = None
            if contact_id and location_ts and contact_id in location_sessions:
                for loc_id, start_ts, end_ts in location_sessions[contact_id]:
                    if start_ts <= location_ts <= end_ts:
                        location_id = loc_id
                        break

            cursor.execute(insert_sql, (
                location_id, contact_id, lat, lon,
                accuracy, speed, bearing, location_ts, source_id,
            ))
            count += 1

        analysis_conn.commit()
    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Ingested %d route points from location_cache", count)
    return count


def _ingest_location_sharer(loc_db, analysis_conn: AnalysisConnection) -> int:
    """Read location_sharer table and insert sharer records.

    Actual schema from WhatsApp:
        _id, remote_jid, from_me, remote_resource (LID), expires, message_id
    """
    try:
        exists = loc_db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='location_sharer'"
        ).fetchone()
        if not exists:
            logger.info("location_sharer table not found in location.db")
            return 0
    except Exception:
        return 0

    try:
        cols_info = loc_db.execute("PRAGMA table_info(location_sharer)").fetchall()
        col_names = {c[1] for c in cols_info}
    except Exception:
        return 0

    # Build dynamic SELECT based on available columns
    select_parts = ["_id"]
    # JID: try remote_resource (LID) first, then remote_jid, then jid
    jid_col = "remote_resource" if "remote_resource" in col_names else (
        "remote_jid" if "remote_jid" in col_names else (
            "jid" if "jid" in col_names else "NULL"
        )
    )
    select_parts.append(jid_col + " AS jid_val")
    # Group JID for context
    group_col = "remote_jid" if "remote_jid" in col_names and jid_col != "remote_jid" else "NULL"
    select_parts.append(group_col + " AS group_jid")
    # Timeout / expiry
    timeout_col = "expires" if "expires" in col_names else (
        "timeout" if "timeout" in col_names else "NULL"
    )
    select_parts.append(timeout_col + " AS timeout_val")
    # from_me
    if "from_me" in col_names:
        select_parts.append("from_me")
    else:
        select_parts.append("NULL AS from_me")

    try:
        rows = loc_db.execute(
            f"SELECT {', '.join(select_parts)} FROM location_sharer"
        ).fetchall()
    except Exception as e:
        logger.warning("Failed to read location_sharer: %s", e)
        return 0

    if not rows:
        logger.info("location_sharer is empty")
        return 0

    logger.info("Found %d location sharer records in location.db", len(rows))

    # JID resolution (both phone JIDs and LID JIDs)
    jid_to_contact: dict[str, int] = {}
    try:
        for r in analysis_conn.fetchall(
            "SELECT c.phone_jid, c.id FROM contact c WHERE c.phone_jid IS NOT NULL"
        ):
            jid_to_contact[r[0]] = r[1]
        for r in analysis_conn.fetchall(
            "SELECT c.lid_jid, c.id FROM contact c WHERE c.lid_jid IS NOT NULL"
        ):
            jid_to_contact[r[0]] = r[1]
    except Exception:
        pass

    insert_sql = """
        INSERT OR IGNORE INTO location_sharer (
            contact_id, raw_jid, timeout_duration, creation_timestamp
        ) VALUES (?,?,?,?)
    """

    count = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()
        for row in rows:
            # row: (_id, jid_val, group_jid, timeout_val, from_me)
            jid = row[1]
            timeout = row[3]

            contact_id = None
            if jid:
                contact_id = jid_to_contact.get(jid)
                if not contact_id and "@" in str(jid):
                    bare = str(jid).split("@")[0]
                    for k, v in jid_to_contact.items():
                        if bare in str(k):
                            contact_id = v
                            break

            cursor.execute(insert_sql, (
                contact_id,
                str(jid) if jid else None,
                timeout,
                None,  # creation_timestamp not in this schema variant
            ))
            count += 1

        analysis_conn.commit()
    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Ingested %d location sharer records", count)
    return count
