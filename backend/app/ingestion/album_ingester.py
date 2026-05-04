"""
Album ingestion from msgstore.message_album / message_association.

A WhatsApp album is a parent message with message_type=99 plus N child media
messages (type=1 image, type=3 video) joined via message_association where
association_type=2.  msgstore stores both the parent and every child as
independent rows in `message`, plus a side-table `message_album` recording
counts and `message_association` recording the parent-child graph.

Without this ingester the children land in analysis.db as standalone media
tiles and the parent renders as an empty "Forwarded" badge with no content.
The renderer collapses parent + children into a single grid card once these
tables are populated.

Forensic value of expected_image_count vs image_count: when WhatsApp's own
counter says "expected 7 photos, only 5 received" it means the sender posted
an album whose tail never reached this device (network drop, sender revoked
some, or our extraction missed the rows).  We surface that gap in the
`note` column so the analyst sees missing-children explicitly.

Run order: AFTER message_ingester so source_msg_id -> analysis.id mapping is
available.  Other association_types (4, 6, 7, 11, 12) are mirrored as-is for
forensic completeness even though the renderer currently only consumes
type=2 albums.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import AnalysisConnection, DatabaseManager
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)


def ingest_albums(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Copy message_album + message_association from msgstore into analysis.db.

    Returns the number of album rows inserted.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    # ---- Build msgstore._id -> analysis.id map ----
    rows = analysis_conn.fetchall(
        "SELECT id, source_msg_id FROM message WHERE source_msg_id IS NOT NULL"
    )
    src_to_ana: dict[int, int] = {r[1]: r[0] for r in rows if r[1] is not None}
    logger.info(
        "album_ingester: built %d source->analysis ID map", len(src_to_ana)
    )

    # ---- Source: message_album (parent counts) ----
    src_albums = reader.execute_raw("""
        SELECT message_row_id, image_count, video_count,
               expected_image_count, expected_video_count
        FROM message_album
    """)
    if not src_albums:
        logger.info("album_ingester: no albums in msgstore")
        if progress_callback:
            progress_callback(0, 0)
        return 0

    # ---- Source: message_association (children) ----
    src_assocs = reader.execute_raw("""
        SELECT child_message_row_id, parent_message_row_id, association_type
        FROM message_association
    """)

    # Group children by parent_src_id so we can compute sort_order + actual
    # child counts per parent in a single pass.
    children_by_parent: dict[int, list[tuple[int, int]]] = {}
    for src_child, src_parent, atype in src_assocs:
        children_by_parent.setdefault(src_parent, []).append((src_child, atype))

    album_insert_sql = """
        INSERT OR REPLACE INTO message_album (
            message_id, image_count, video_count,
            expected_image_count, expected_video_count,
            missing_image_count, missing_video_count,
            actual_child_count, note
        ) VALUES (?,?,?,?,?,?,?,?,?)
    """
    assoc_insert_sql = """
        INSERT OR IGNORE INTO message_association (
            parent_message_id, child_message_id, association_type, sort_order
        ) VALUES (?,?,?,?)
    """

    inserted_albums = 0
    skipped_albums_no_parent = 0
    inserted_assocs = 0
    skipped_assocs_no_parent = 0
    skipped_assocs_no_child = 0
    incomplete_albums = 0  # expected != actual

    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        # ---- Insert albums ----
        for src_parent, img_cnt, vid_cnt, exp_img, exp_vid in src_albums:
            ana_parent = src_to_ana.get(src_parent)
            if ana_parent is None:
                skipped_albums_no_parent += 1
                continue

            # Children that resolved into analysis.db (only album-type=2)
            children = children_by_parent.get(src_parent, [])
            album_children = [(c, a) for c, a in children if a == 2]
            actual_resolved = sum(1 for c, _ in album_children if c in src_to_ana)

            # Missing counts (only meaningful when expected is known)
            miss_img = (
                max(0, (exp_img or 0) - (img_cnt or 0))
                if exp_img is not None else 0
            )
            miss_vid = (
                max(0, (exp_vid or 0) - (vid_cnt or 0))
                if exp_vid is not None else 0
            )

            # Forensic note - only when something is off, leave NULL otherwise
            note_parts: list[str] = []
            present = (img_cnt or 0) + (vid_cnt or 0)
            if exp_img is not None or exp_vid is not None:
                expected = (exp_img or 0) + (exp_vid or 0)
                if expected > present:
                    note_parts.append(
                        f"WhatsApp expected {expected} item(s); "
                        f"{present} present, {expected - present} missing"
                    )
                    incomplete_albums += 1
            if actual_resolved < present:
                note_parts.append(
                    f"{present - actual_resolved} child row(s) absent from "
                    f"analysis.db (extraction gap)"
                )
            note = "; ".join(note_parts) if note_parts else None

            cursor.execute(album_insert_sql, (
                ana_parent,
                img_cnt or 0,
                vid_cnt or 0,
                exp_img,
                exp_vid,
                miss_img,
                miss_vid,
                actual_resolved,
                note,
            ))
            inserted_albums += 1

        # ---- Insert associations (mirror ALL types, not just type=2, for
        # forensic completeness).  Sort children by source _id (upload order)
        # so sort_order matches the original chronology. ----
        for src_parent, child_list in children_by_parent.items():
            ana_parent = src_to_ana.get(src_parent)
            if ana_parent is None:
                skipped_assocs_no_parent += len(child_list)
                continue
            child_list_sorted = sorted(child_list, key=lambda x: x[0])
            for sort_order, (src_child, atype) in enumerate(child_list_sorted):
                ana_child = src_to_ana.get(src_child)
                if ana_child is None:
                    skipped_assocs_no_child += 1
                    continue
                cursor.execute(assoc_insert_sql, (
                    ana_parent, ana_child, atype, sort_order,
                ))
                inserted_assocs += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    logger.info(
        "album_ingester: %d albums (%d skipped no-parent, %d incomplete), "
        "%d associations (%d skipped no-parent, %d skipped no-child)",
        inserted_albums, skipped_albums_no_parent, incomplete_albums,
        inserted_assocs, skipped_assocs_no_parent, skipped_assocs_no_child,
    )

    if progress_callback:
        progress_callback(inserted_albums, inserted_albums)

    return inserted_albums
