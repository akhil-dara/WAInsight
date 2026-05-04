"""Forensic provenance tracker — records exactly which DB tables/columns/logic
produced each displayed field in a message.

For every message, this module builds a provenance dict that tells an investigator:
- Where the sender name came from (which contact columns, resolution path)
- Where the message text came from (text_content, recovered ghost text, media caption)
- Where media info came from (media table, file resolution, thumbnail source)
- Where system event text came from (event_label, actor/target resolution, event_data parsing)
- Where timestamps came from (message.timestamp, receipt, receipt_server_timestamp)
- Where reactions, mentions, polls, calls, locations came from
- What joins were used and why

This is stored in the pre-computed cache so the GUI can show a "Forensic Info"
panel for any message without running any SQL at display time.
"""
from __future__ import annotations


def build_provenance(msg: dict, row_context: dict | None = None) -> dict:
    """Build a forensic provenance dict for a message.

    Args:
        msg: The fully built message dict (after _build_msg_dict)
        row_context: Optional raw SQL row data for deeper provenance

    Returns:
        Dict with provenance info per field category.
    """
    prov: dict = {}

    # ── Identity / Sender ──
    sender_prov = _build_sender_provenance(msg)
    if sender_prov:
        prov["sender"] = sender_prov

    # ── Message Content ──
    content_prov = _build_content_provenance(msg)
    if content_prov:
        prov["content"] = content_prov

    # ── Media ──
    media_prov = _build_media_provenance(msg)
    if media_prov:
        prov["media"] = media_prov

    # ── Timestamps ──
    ts_prov = _build_timestamp_provenance(msg)
    if ts_prov:
        prov["timestamps"] = ts_prov

    # ── System Event ──
    if msg.get("message_type") == 7:
        se_prov = _build_system_event_provenance(msg)
        if se_prov:
            prov["system_event"] = se_prov

    # ── Delivery / Read Receipts ──
    receipt_prov = _build_receipt_provenance(msg)
    if receipt_prov:
        prov["receipts"] = receipt_prov

    # ── Reactions ──
    if msg.get("reactions_str") or msg.get("reaction_count"):
        prov["reactions"] = {
            "source": "reaction table JOIN contact (reactor_id)",
            "emoji_concat": "GROUP_CONCAT(reaction.emoji)",
            "detail": "GROUP_CONCAT(emoji:reactor_name, ';;')" if msg.get("reactions_detail") else None,
            "count": msg.get("reaction_count", 0),
        }

    # ── Mentions ──
    if msg.get("mentions_str"):
        prov["mentions"] = {
            "source": "mention table JOIN contact (mentioned_id)",
            "format": "name::contact_id::phone::lid::display_name, ';;' separated",
            "raw": msg.get("mentions_str"),
        }

    # ── Poll ──
    if msg.get("poll_options"):
        prov["poll"] = {
            "source": "poll + poll_option + poll_vote tables",
            "options_from": "poll_option.option_name::vote_total (JOIN poll ON message_id)",
            "voters_from": "COUNT(DISTINCT poll_vote.voter_id)",
            "total_voters": msg.get("poll_total_voters", 0),
        }

    # ── Call ──
    if msg.get("call_duration") is not None or msg.get("call_result_label"):
        prov["call"] = {
            "source": "call_record (matched by ABS(timestamp - msg.timestamp) < 2000ms)",
            "fields": {
                "duration_sec": "call_record.duration_sec",
                "is_video": "call_record.is_video",
                "result_label": "call_record.result_label",
                "is_group_call": "call_record.is_group_call",
            },
            "participants_from": "call_participant JOIN contact (contact_id)",
        }

    # ── Location ──
    if msg.get("loc_latitude") is not None:
        prov["location"] = {
            "source": "location table (JOIN on message_id)",
            "fields": {
                "latitude": "location.latitude",
                "longitude": "location.longitude",
                "place_name": "location.place_name",
                "place_address": "location.place_address",
                "is_live": "location.is_live",
                "live_duration": "location.live_duration",
            },
        }

    # ── vCard ──
    if msg.get("vcard_data"):
        prov["vcard"] = {
            "source": "message_vcard_data (JOIN on message_id, ORDER BY vcard_index)",
            "format": "display_name||phone_numbers, ';;' separated",
        }

    # ── Link Preview ──
    if msg.get("link_details"):
        prov["links"] = {
            "source": "message_link_detail (JOIN on message_id)",
            "format": "page_title||url||description||domain, ';;' separated",
        }

    # ── Scheduled Event ──
    if msg.get("scheduled_event_data"):
        prov["scheduled_event"] = {
            "source": "scheduled_event (JOIN on message_id)",
            "format": "name||description||location||join_link||start_time||is_canceled",
        }

    # ── Device Info ──
    device_prov = _build_device_provenance(msg)
    if device_prov:
        prov["device"] = device_prov

    # ── Flags ──
    flags_prov = _build_flags_provenance(msg)
    if flags_prov:
        prov["flags"] = flags_prov

    # ── Ghost / Deleted ──
    if msg.get("is_ghost"):
        prov["ghost"] = {
            "source": "ghost_message table (matched by revoked_msg_id = message.id)",
            "original_text_from": "ghost_message.original_text",
            "recovery_method": "ghost_message.recovery_method",
            "note": "Message was deleted (revoked) by sender but original text was recovered from notification/quote cache",
        }

    # ── Revoked by Admin ──
    if msg.get("revoked_by_admin_id"):
        prov["admin_revoke"] = {
            "source": "message.revoked_by_admin_id JOIN contact",
            "admin_id": msg.get("revoked_by_admin_id"),
            "admin_name": msg.get("revoked_by_admin_name"),
        }

    # ── Tags ──
    if msg.get("is_tagged"):
        prov["tag"] = {
            "source": "message_tag table (message_id)",
            "note": "Flagged by investigator",
        }

    return prov


def _build_sender_provenance(msg: dict) -> dict | None:
    sender_name = msg.get("sender_name", "")
    if not sender_name and not msg.get("sender_id"):
        return None

    prov = {
        "table": "message.sender_id -> contact table",
        "contact_id": msg.get("sender_id"),
        "final_name": sender_name,
    }

    # Determine which resolution path was used
    display_name = msg.get("display_name")
    wa_name = msg.get("wa_name")
    phone_jid = msg.get("phone_jid")
    lid_jid = msg.get("lid_jid")
    is_bot = msg.get("is_bot_message")

    if is_bot and not msg.get("sender_id"):
        prov["resolution"] = "Bot message with NULL sender_id -> 'Meta AI'"
        prov["path"] = "is_bot_message=1 AND sender_id IS NULL"
    elif display_name and "~" not in sender_name:
        phone = msg.get("phone_jid", "").replace("@s.whatsapp.net", "") if msg.get("phone_jid") else ""
        if phone and f"(+{phone[:3]}" in sender_name:
            prov["resolution"] = "Saved contact: display_name + phone_number"
            prov["path"] = "contact.is_saved=1 -> contact.display_name || ' (+' || contact.phone_number || ')'"
        else:
            prov["resolution"] = "Saved contact: display_name only"
            prov["path"] = "contact.is_saved=1 -> contact.display_name"
    elif wa_name and sender_name.startswith("~"):
        prov["resolution"] = "Unsaved contact: wa_name (push name) with ~ prefix"
        prov["path"] = "contact.wa_name IS NOT NULL -> '~' || contact.wa_name [|| ' (+' || phone_number || ')']"
    elif sender_name.startswith("+"):
        prov["resolution"] = "No name available: phone number only"
        prov["path"] = "contact.phone_number -> '+' || phone_number"
    elif sender_name.startswith("LID:"):
        prov["resolution"] = "LID-only contact (no phone mapping found)"
        prov["path"] = "contact.lid_jid -> 'LID:' || SUBSTR(lid_jid, 1, 12) || '...'"
    else:
        prov["resolution"] = "Fallback"
        prov["path"] = "COALESCE chain in SQL CASE expression"

    if phone_jid:
        prov["phone_jid"] = phone_jid
    if lid_jid:
        prov["lid_jid"] = lid_jid
    if wa_name:
        prov["wa_name"] = wa_name
    if display_name:
        prov["display_name"] = display_name

    # Member label (group-specific admin tag)
    if msg.get("member_label"):
        prov["member_label"] = {
            "value": msg["member_label"],
            "source": "group_member.label (JOIN on sender_id + conversation_id)",
        }

    return prov


def _build_content_provenance(msg: dict) -> dict | None:
    prov: dict = {}
    msg_type = msg.get("message_type", 0)
    type_label = msg.get("type_label", "")

    prov["message_type"] = {
        "value": msg_type,
        "source": "message.message_type (WhatsApp internal type code)",
    }
    prov["type_label"] = {
        "value": type_label,
        "source": "message.type_label (human-readable type string, mapped during ingestion)",
    }

    text = msg.get("text_content", "")
    display = msg.get("display_text", "")
    caption = msg.get("media_caption", "")

    if msg.get("is_ghost"):
        prov["text_source"] = "ghost_message.original_text (recovered deleted text)"
    elif msg.get("is_revoked"):
        prov["text_source"] = "Revoked: original text unavailable -> 'This message was deleted'"
    elif caption and display == caption:
        prov["text_source"] = "media.media_caption"
    elif text:
        prov["text_source"] = "message.text_content"
    else:
        prov["text_source"] = "Empty (no text_content or media_caption)"

    if msg.get("quoted_text"):
        prov["quoted_text"] = {
            "source": "message.quoted_text (stored inline on reply message)",
            "reply_to_key_id": msg.get("reply_to_key_id"),
            "quoted_type": msg.get("quoted_type"),
        }

    if msg.get("is_forwarded"):
        prov["forwarded"] = {
            "source": "message.is_forwarded",
            "forward_score": msg.get("forward_score"),
            "note": "forward_score > 4 means 'Forwarded many times'",
        }

    return prov


def _build_media_provenance(msg: dict) -> dict | None:
    if not msg.get("mime_type") and not msg.get("file_path") and not msg.get("thumbnail_blob"):
        return None

    prov: dict = {
        "source": "media table (JOIN media ON media.message_id = message.id LIMIT 1)",
    }

    if msg.get("resolved_file_path"):
        prov["file_display"] = {
            "path": msg["resolved_file_path"],
            "source": "media.resolved_file_path (set during ingestion path resolution or post-download re-mapping)",
            "exists": msg.get("media_file_exists", False),
        }
    elif msg.get("file_path"):
        prov["file_display"] = {
            "path": msg["file_path"],
            "source": "media.file_path (original WhatsApp relative path)",
            "exists": msg.get("media_file_exists", False),
        }

    if msg.get("thumbnail_blob") and msg.get("has_thumb"):
        prov["thumbnail"] = {
            "source": "media.thumbnail_blob (inline JPEG thumbnail from WhatsApp DB, typically ~100x100px)",
            "size_bytes": len(msg["thumbnail_blob"]) if msg.get("thumbnail_blob") else 0,
        }

    if msg.get("media_url"):
        prov["download"] = {
            "has_url": True,
            "has_key": bool(msg.get("media_key")),
            "url_source": "media.media_url (WhatsApp CDN URL, expires ~30 days)",
            "key_source": "media.media_key (AES-256-CBC key, 32 bytes BLOB)",
            "decrypt_method": "AES-256-CBC + HKDF-SHA256 + HMAC-SHA256(10-byte truncated)",
        }

    if msg.get("file_hash"):
        prov["hash"] = {
            "value": msg["file_hash"],
            "source": "media.file_hash (content SHA-256 for dedup/forwarded tracking)",
        }

    if msg.get("type_label") == "sticker":
        prov["sticker_note"] = "Sticker WebP file base64-encoded for animation in chat view"

    prov["dimensions"] = {
        "width": msg.get("media_width"),
        "height": msg.get("media_height"),
        "duration_ms": msg.get("media_duration_ms"),
        "source": "media.width, media.height, media.duration_ms",
    }

    return prov


def _build_timestamp_provenance(msg: dict) -> dict:
    prov: dict = {}

    prov["message_timestamp"] = {
        "value": msg.get("timestamp"),
        "source": "message.timestamp (Unix ms, WhatsApp server timestamp for received, local for sent)",
    }

    if msg.get("received_timestamp"):
        prov["received_timestamp"] = {
            "value": msg["received_timestamp"],
            "source": "message.received_timestamp (when device received the message)",
        }

    if msg.get("receipt_server_timestamp"):
        prov["receipt_server_timestamp"] = {
            "value": msg["receipt_server_timestamp"],
            "source": "message.receipt_server_timestamp (WhatsApp server confirmation)",
            "note": "Used as fallback when first_read_ts/first_delivered_ts is NULL",
        }

    return prov


def _build_system_event_provenance(msg: dict) -> dict | None:
    prov: dict = {
        "source": "system_event table (JOIN on message_id)",
        "event_label": {
            "value": msg.get("system_event_label"),
            "source": "system_event.event_label (human-readable label mapped from WhatsApp event_type integer during ingestion)",
        },
    }

    if msg.get("system_event_data"):
        prov["event_data"] = {
            "raw": msg["system_event_data"],
            "source": "system_event.event_data (JSON string parsed from WhatsApp protobuf/action_type fields)",
            "note": "Contains ephemeral_duration, old_value, new_value, text_data, etc.",
        }

    actor = msg.get("system_event_actor")
    if actor:
        prov["actor"] = {
            "display_name": actor,
            "contact_id": msg.get("se_actor_id"),
            "source": "system_event.actor_id -> contact table (same name resolution CASE as sender_name)",
            "note": "The person who performed the action",
        }

    target = msg.get("system_event_target")
    if target:
        prov["target"] = {
            "display_name": target,
            "contact_id": msg.get("se_target_id"),
            "source": "system_event.target_id -> contact table (same name resolution CASE as sender_name)",
            "note": "The person affected by the action",
        }

    if msg.get("nc_old_phone") or msg.get("nc_new_phone"):
        prov["number_change"] = {
            "old_phone": msg.get("nc_old_phone"),
            "new_phone": msg.get("nc_new_phone"),
            "source": "number_change table (JOIN on system_event_id -> jid_to_contact for phone resolution)",
        }

    if msg.get("community_name"):
        prov["community_name"] = {
            "value": msg["community_name"],
            "source": "COALESCE(system_event.community_name, parent_conversation.display_name)",
        }

    prov["display_text_builder"] = "shared.system_event_formatter.build_system_text() — 63+ WhatsApp event types with owner/actor/target resolution"

    return prov


def _build_receipt_provenance(msg: dict) -> dict | None:
    if not msg.get("first_delivered_ts") and not msg.get("first_read_ts"):
        return None

    prov: dict = {
        "source": "receipt table (JOIN on message_id)",
    }

    if msg.get("first_delivered_ts"):
        prov["first_delivered"] = {
            "value": msg["first_delivered_ts"],
            "source": "MIN(receipt.delivered_ts) WHERE message_id = msg.id",
        }

    if msg.get("first_read_ts"):
        prov["first_read"] = {
            "value": msg["first_read_ts"],
            "source": "MIN(receipt.read_ts) WHERE message_id = msg.id",
        }

    if not msg.get("first_read_ts") and not msg.get("first_delivered_ts"):
        if msg.get("receipt_server_timestamp"):
            prov["fallback"] = {
                "note": "Using receipt_server_timestamp as fallback (no receipt rows found)",
                "source": "message.receipt_server_timestamp",
            }

    return prov


def _build_device_provenance(msg: dict) -> dict | None:
    dev_num = msg.get("sender_device_number", -1)
    origin = msg.get("origin", 0)

    if dev_num == -1 and origin == 0:
        return None

    prov: dict = {}

    if dev_num >= 0:
        prov["sender_device"] = {
            "device_number": dev_num,
            "is_primary": msg.get("sender_is_primary"),
            "platform_label": msg.get("sender_platform_label", ""),
            "source": "message_device table (JOIN on message_id)",
            "note": "device_number: 0=phone, 1-98=companion device, 99=Cloud API",
        }

    if origin > 0:
        prov["origin"] = {
            "value": origin,
            "source": "message.origin (>0 means sent from companion device)",
        }

    flags = msg.get("origination_flags", 0)
    if flags:
        # ``origination_flags`` is a pure bitmask — each bit
        # below is independent.  The composite values seen on
        # the wire are OR-combinations of the individual flags.
        flag_meanings = []
        if flags & 1:           flag_meanings.append("forwarded")
        if flags & 64:          flag_meanings.append("multi-contact-image")
        if flags & 256:         flag_meanings.append("ephemeral")
        if flags & 512:         flag_meanings.append("system-message")
        if flags & 2048:        flag_meanings.append("pdf-or-url-or-status-video")
        if flags & 32768:       flag_meanings.append("voice-note")
        if flags & 131072:      flag_meanings.append("edited-or-meta-ai")
        if flags & 67108864:    flag_meanings.append("media-album")
        if flags & 536870912:   flag_meanings.append("scheduled-event")
        prov["origination_flags"] = {
            "value": flags,
            "meanings": flag_meanings,
            "source": "message.origination_flags (bitmask —",
        }

    return prov


def _build_flags_provenance(msg: dict) -> dict | None:
    flags = {}

    if msg.get("is_starred"):
        flags["starred"] = {"source": "message.is_starred", "value": True}
    if msg.get("is_view_once"):
        flags["view_once"] = {"source": "message.is_view_once", "value": True}
    if msg.get("is_ephemeral"):
        flags["ephemeral"] = {
            "source": "message.is_ephemeral",
            "duration": msg.get("ephemeral_duration"),
            "duration_source": "message.ephemeral_duration (seconds)",
        }
    if msg.get("broadcast"):
        flags["broadcast"] = {"source": "message.broadcast", "value": True}
    if msg.get("is_edited"):
        flags["edited"] = {"source": "message.is_edited", "value": True}

    return flags if flags else None
