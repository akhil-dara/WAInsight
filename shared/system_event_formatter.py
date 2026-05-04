"""Shared system event formatter — used by both backend ingestion and GUI rendering.

Extracted from gui/app/views/widgets/bubble_delegate.py so that the backend
pipeline can pre-compute display text for all 63+ WhatsApp system event types
during ingestion, enabling instant chat load in the GUI.
"""
from __future__ import annotations

import json as _json
import re as _re

# ---------------------------------------------------------------------------
# System event label -> icon mapping
# ---------------------------------------------------------------------------

SYSTEM_ICONS: dict[str, str] = {
    # ── Confirmed types ──
    "group_subject_changed": "\u270F\uFE0F",       # type 1 ✅
    "participant_added": "\u2795",                  # type 12 ✅
    "participant_left": "\u2796",                   # type 5, 13 ✅
    "participant_removed": "\u2796",                # type 14 ✅
    "security_code_changed": "\U0001F512",          # type 18 ✅
    "participant_joined_via_link": "\u2795",         # type 20 ✅
    "e2e_encrypted": "\U0001F512",                  # type 67 ✅
    "membership_approval_request": "\u2795",         # type 83 ✅
    "admin_promoted": "\U0001F451",                 # type 84 ✅
    "channel_created": "\U0001F4F0",                # type 134 ✅

    # ── Corrected types (from phone validation) ──
    "group_icon_changed": "\U0001F4F7",
    "group_name_changed": "\u270F\uFE0F",
    "number_changed": "\U0001F4F1",
    "community_or_group_created": "\U0001F4AC",
    "you_are_admin": "\U0001F451",
    "group_link_reset": "\U0001F517",
    "group_description_changed": "\U0001F4DD",
    "group_add_member_permission": "\u2699\uFE0F",
    "group_edit_permission": "\u2699\uFE0F",
    "group_send_message_permission": "\u2699\uFE0F",
    "disappearing_timer_updated": "\u23F0",
    "contact_blocked": "\U0001F6AB",
    "disappearing_timer_changed": "\u23F0",
    "business_meta_managed": "\U0001F3E2",
    "disappearing_messages_changed": "\u23F0",
    "participant_joined_community": "\u2795",
    "community_admin_changed": "\U0001F451",
    "group_join_permission": "\u2699\uFE0F",
    "group_add_permission_all": "\u2699\uFE0F",
    "group_add_permission_admins": "\u2699\uFE0F",
    "community_joined": "\u2795",
    "subgroup_removed": "\u2796",
    "subgroup_added": "\u2795",
    "subgroup_unlinked": "\U0001F4AC",
    "message_pinned": "\U0001F4CC",
    "participant_added_with_approval": "\u2795",
    "community_group_joined": "\u2795",
    "community_description_changed": "\U0001F4DD",
    "channel_privacy_notice": "\U0001F512",
    "channel_deleted": "\u2796",
    "group_auto_admin_restriction": "\u2699\uFE0F",
    "meta_ai_disclaimer": "\U0001F916",
    "community_linked_group_join": "\u2795",
    "community_created": "\U0001F4AC",
    "event_updated": "\U0001F4C5",
    "community_owner_changed": "\U0001F451",
    "group_invite_permission": "\u2699\uFE0F",

    # ── From screenshots + logical deduction ──
    "you_were_added": "\u2795",
    "default_disappearing_timer": "\u23F0",
    "community_add_permission": "\u2699\uFE0F",
    "community_settings_changed": "\u2699\uFE0F",
    "phone_number_privacy": "\U0001F512",
    "community_welcome_joined": "\U0001F44B",
    "community_group_invite_joined": "\u2795",
    "participant_joined_from_community": "\u2795",
    "contact_card_shown": "",
    "community_auto_added": "\u2795",
    "ai_disclaimer": "\U0001F916",
    "system_notification": "\u2139\uFE0F",
}


def fmt_phone(number: str) -> str:
    """Format a phone number with spaces for readability.
    For example a 12-digit Indian number is grouped as
    ``+91 NNNNN NNNNN`` and an 11-digit US number as
    ``+1 NNN NNN NNNN``.
    """
    if not number or not number.strip():
        return number or ""
    n = number.lstrip("+")
    if not n.isdigit():
        return number
    # Indian numbers: +91 XXXXX XXXXX
    if n.startswith("91") and len(n) == 12:
        return f"+91 {n[2:7]} {n[7:]}"
    # US/CA: +1 XXX XXX XXXX
    if n.startswith("1") and len(n) == 11:
        return f"+1 {n[1:4]} {n[4:7]} {n[7:]}"
    # Generic: +CC XXXX XXXX...
    if len(n) > 6:
        cc_len = 1 if n[0] in "17" else (3 if n[:2] in (
            "20","21","22","23","24","25","26","27","28","29",
            "30","31","33","34","35","36","37","38","39",
            "40","41","42","43","44","45","46","47","48","49",
            "50","51","52","53","54","55","56","57","58","59",
            "60","61","62","63","64","65","66","67","68","69",
            "70","71","72","73","74","75","76","77","78","79",
            "80","81","82","84","86","90","91","92","93","94","95","98",
        ) else 2)
        rest = n[cc_len:]
        parts = [rest[i:i+5] for i in range(0, len(rest), 5)]
        return f"+{n[:cc_len]} {' '.join(parts)}"
    return f"+{n}"


def _fmt_ephemeral_duration(secs: int) -> str:
    """Format ephemeral duration to human readable."""
    if secs <= 0:
        return "0 seconds"
    days = secs // 86400
    if days >= 1:
        return f"{days} days"
    hours = secs // 3600
    if hours >= 1:
        return f"{hours} hours"
    mins = secs // 60
    if mins >= 1:
        return f"{mins} minutes"
    return f"{secs} seconds"


def build_system_text(
    msg: dict,
    owner_phone: str = "",
    owner_name: str = "",
    owner_contact_id: int = 0,
    conv_name: str = "",
    chat_type: str = "",
) -> str:
    """Build descriptive system event text matching WhatsApp's display.

    This is a standalone function (not a method) so it can be called from
    both the GUI bubble delegate and the backend ingestion pipeline.

    Args:
        msg: Message dict with keys like system_event_label, system_event_data,
             system_event_actor, system_event_target, display_text, text_content,
             from_me, se_actor_id, se_target_id, message_type, type_label, etc.
        owner_phone: Phone owner's number (e.g. '15551234567')
        owner_name: Phone owner's display name (e.g. 'Device Owner')
        owner_contact_id: Phone owner's contact_id in the DB
        conv_name: Current conversation display name
        chat_type: 'personal', 'group', 'community', etc.
    """
    event_label = msg.get("system_event_label", "")
    event_data_raw = msg.get("system_event_data", "")
    actor = msg.get("system_event_actor", "") or ""
    target = msg.get("system_event_target", "") or ""
    text = msg.get("display_text") or msg.get("text_content") or ""

    # Format phone numbers with spaces for readability
    if actor and actor.lstrip("+").isdigit():
        actor = fmt_phone(actor)
    elif actor and "(+" in actor:
        actor = _re.sub(r'\(\+(\d+)\)', lambda m: '(' + fmt_phone(m.group(1)) + ')', actor)
    if target and target.lstrip("+").isdigit():
        target = fmt_phone(target)
    elif target and "(+" in target:
        target = _re.sub(r'\(\+(\d+)\)', lambda m: '(' + fmt_phone(m.group(1)) + ')', target)

    # Replace owner actor/target with forensic label
    owner_label = ""
    if owner_phone:
        owner_fmt = fmt_phone(owner_phone)
        owner_label = f"You (Owner: {owner_name}, {owner_fmt})" if owner_name else f"You ({owner_fmt})"
        # Helper: extract phone digits from a formatted "Name (+CC NNNNN NNNNN)" string
        def _extract_phone(s: str) -> str:
            """Extract phone digits from a name+phone string or raw JID."""
            if not s:
                return ""
            # Try extracting from parens: "Name (+91 12345 67890)"
            m = _re.search(r'\(?\+?(\d[\d\s]{6,})\)?', s)
            if m:
                return m.group(1).replace(" ", "")
            # Fallback: strip non-digits and JID suffix
            raw = s.lstrip("+").replace(" ", "").split("@")[0]
            # If it's mostly digits, use it
            digits = _re.sub(r'\D', '', raw)
            return digits if len(digits) >= 8 else ""

        def _is_owner_match(contact_id_val, raw_str: str) -> bool:
            """Check if a contact ID or phone string matches the owner."""
            # Primary: contact ID match (with type coercion)
            try:
                if owner_contact_id > 0 and int(contact_id_val or 0) == owner_contact_id:
                    return True
            except (ValueError, TypeError):
                pass
            # Secondary: phone match from formatted string
            phone = _extract_phone(raw_str)
            if phone and (phone == owner_phone or phone.endswith(owner_phone) or owner_phone.endswith(phone)):
                return True
            return False

        # Check if actor is the owner
        actor_is_owner = _is_owner_match(
            msg.get("se_actor_id"),
            msg.get("system_event_actor", "")
        )
        if actor_is_owner:
            actor = owner_label
        # Check if target is the owner
        target_is_owner = _is_owner_match(
            msg.get("se_target_id"),
            msg.get("system_event_target", "")
        )
        if target_is_owner:
            target = owner_label
        # from_me check for events with no actor/target
        skip_from_me = event_label in (
            "security_code_changed", "e2e_encrypted", "business_meta_managed",
            "contact_card_shown", "channel_privacy_notice", "phone_number_privacy",
            "group_auto_admin_restriction", "meta_ai_disclaimer", "ai_disclaimer",
            "you_are_admin", "you_were_added", "community_joined",
            "community_welcome_joined", "community_auto_added",
            "community_or_group_created", "community_created", "channel_created",
            "subgroup_added", "subgroup_removed", "subgroup_unlinked",
            "participant_joined_via_link", "participant_joined_community",
            "participant_added_with_approval", "community_group_joined",
            "community_linked_group_join",
            "membership_approval_request", "admin_promoted",
            "community_admin_changed", "community_owner_changed",
        )
        if msg.get("from_me") and not actor and not target and not skip_from_me:
            actor = owner_label

    # ── Message type 112: Advanced chat privacy ──
    if msg.get("message_type") == 112 or msg.get("type_label") in ("advanced_chat_privacy", "unknown_112"):
        from_me = msg.get("from_me", False)
        # Treat known "no actor" placeholders as missing, so we can switch
        # to passive voice rather than render "Unknown changed …".
        # WhatsApp sometimes emits this event with no sender_id at all
        # (initial enable on a new chat) — the user shouldn't see
        # "Unknown" as if a mystery person did it.
        sender = (msg.get("sender_name", "") or actor or "").strip()
        if sender.lower() in ("", "unknown", "a participant"):
            sender = ""

        raw = msg.get("display_text") or msg.get("text_content") or ""
        is_on = bool(raw) and ("1" in raw or "on" in raw.lower())
        is_off = bool(raw) and ("0" in raw or "off" in raw.lower())

        if from_me and owner_phone:
            who = owner_label or "You"
        elif sender:
            who = sender
        else:
            # No actor available — passive voice.
            if is_on:
                return "\U0001F512 Advanced chat privacy was turned on"
            if is_off:
                return "\U0001F512 Advanced chat privacy was turned off"
            return "\U0001F512 Advanced chat privacy settings changed"

        if is_on:
            return f"\U0001F512 {who} turned on advanced chat privacy"
        if is_off:
            return f"\U0001F512 {who} turned off advanced chat privacy"
        return f"\U0001F512 {who} changed advanced chat privacy settings"

    icon = SYSTEM_ICONS.get(event_label, "\u2139\uFE0F")

    # Parse event_data JSON once
    edata: dict = {}
    if event_data_raw:
        try:
            edata = _json.loads(event_data_raw) if isinstance(event_data_raw, str) else {}
        except (_json.JSONDecodeError, TypeError):
            pass

    # Supplement ephemeral_duration from message table if not in event_data
    if "ephemeral_duration" not in edata:
        dur = msg.get("ephemeral_duration")
        if dur is not None:
            edata["ephemeral_duration"] = dur

    # Helper: text_data from event_data
    text_data = edata.get("text_data", "")

    # ══════════════════════════════════════════════════════════
    # ✅ CONFIRMED event types
    # ══════════════════════════════════════════════════════════

    if event_label == "group_subject_changed":
        old_name = edata.get("old_subject", "")
        new_name = edata.get("new_subject", edata.get("subject", text or ""))
        who = actor or "Someone"
        if old_name and new_name:
            return f'{icon} {who} changed the group name from "{old_name}" to "{new_name}"'
        elif new_name:
            return f'{icon} {who} changed the group name to "{new_name}"'
        return f"{icon} {who} changed the group name"

    if event_label == "participant_added":
        # Detect owner self-add (actor and target are both the owner)
        if actor_is_owner and target_is_owner:
            return f"{icon} {owner_label} joined this group"
        if actor and target and actor != target:
            return f"{icon} {actor} added {target}"
        return f"{icon} {target or actor or 'Someone'} was added"

    if event_label == "participant_left":
        who = target or actor or "Someone"
        return f"{icon} {who} left"

    if event_label == "participant_removed":
        if actor and target and actor != target:
            return f"{icon} {actor} removed {target}"
        return f"{icon} {target or actor or 'Someone'} was removed"

    if event_label == "security_code_changed":
        who = target or actor or "a contact"
        return f"{icon} Your security code with {who} changed. Tap to learn more."

    if event_label == "participant_joined_via_link":
        who = target or actor or "Someone"
        return f"{icon} {who} joined using a group link"

    if event_label == "e2e_encrypted":
        return f"{icon} Messages and calls are end-to-end encrypted. Only people in this chat can read, listen to, or share them."

    if event_label == "membership_approval_request":
        who = target or actor or "Someone"
        return f"{icon} {who} requested to join"

    if event_label == "admin_promoted":
        who = target or actor or "Someone"
        if actor and actor != who:
            return f"{icon} {who} was made an admin by {actor}"
        return f"{icon} {who} was made an admin"

    if event_label == "channel_created":
        name = text_data or text or conv_name or ""
        if name:
            return f'{icon} The channel "{name}" was created'
        return f"{icon} This channel was created"

    # ══════════════════════════════════════════════════════════
    # 🔄 CORRECTED event types
    # ══════════════════════════════════════════════════════════

    if event_label == "group_icon_changed":
        who = actor or "Someone"
        return f"{icon} {who} changed this group's icon"

    if event_label == "number_changed":
        old_phone = msg.get("nc_old_phone", "")
        new_phone = msg.get("nc_new_phone", "")
        if old_phone and new_phone and old_phone != new_phone:
            old_fmt = fmt_phone(old_phone)
            new_fmt = fmt_phone(new_phone)
            return f"{icon} {old_fmt} changed to {new_fmt}"
        who = target or actor or "A contact"
        return f"{icon} {who} changed their phone number"

    if event_label == "community_or_group_created":
        # Only use actor from system_event table — never fallback to owner
        # based on from_me, because from_me=1 just means the message is in
        # the owner's DB, not that the owner performed the action.
        who = actor or "Someone"
        name = text_data or text or ""
        kind = "community" if chat_type == "community" else "group"
        if name:
            return f'{icon} {who} created {kind} "{name}"'
        return f"{icon} {who} created this {kind}"

    if event_label == "you_are_admin":
        who = target or owner_label or "You"
        if actor:
            return f"{icon} {who} was made an admin by {actor}"
        return f"{icon} {who} is now an admin"

    if event_label == "group_link_reset":
        who = actor or "An admin"
        return f"{icon} {who} reset the group link to invite others to this group"

    if event_label == "group_description_changed":
        who = actor or "Someone"
        return f"{icon} {who} changed the group description. Tap to view."

    if event_label == "group_add_member_permission":
        who = actor or "An admin"
        return f"{icon} {who} changed this group's settings to allow all members to add others to this group"

    if event_label == "group_edit_permission":
        who = actor or "An admin"
        return f"{icon} {who} changed the group's settings so all members can edit the group settings"

    if event_label == "group_send_message_permission":
        who = actor or "An admin"
        return f"{icon} {who} changed this group's settings to allow all members to send messages to this group"

    if event_label == "disappearing_timer_updated":
        who = actor or "An admin"
        dur_secs = edata.get("ephemeral_duration")
        if dur_secs is not None:
            try:
                dur_secs = int(dur_secs)
            except (ValueError, TypeError):
                dur_secs = None
        if dur_secs is not None and dur_secs == 0:
            return f"{icon} {who} turned off disappearing messages"
        if dur_secs is not None and dur_secs > 0:
            dur_str = _fmt_ephemeral_duration(dur_secs)
            return f"{icon} {who} updated the message timer. New messages will disappear from this chat {dur_str} after they're sent, except when kept."
        return f"{icon} {who} updated the message timer. New messages will disappear from this chat after they're sent, except when kept."

    if event_label == "disappearing_timer_changed":
        who = owner_label or "You"
        dur_secs = edata.get("ephemeral_duration")
        if dur_secs is not None:
            try:
                dur_secs = int(dur_secs)
            except (ValueError, TypeError):
                dur_secs = None
        if dur_secs is not None and dur_secs == 0:
            return f"{icon} {who} turned off disappearing messages"
        if dur_secs is not None and dur_secs > 0:
            dur_str = _fmt_ephemeral_duration(dur_secs)
            return f"{icon} {who} updated the message timer. New messages will disappear from this chat {dur_str} after they're sent, except when kept."
        return f"{icon} {who} updated the message timer. New messages will disappear from this chat after they're sent, except when kept."

    if event_label == "contact_blocked":
        who = owner_label or "You"
        is_blocked = text_data == "true" if text_data else True
        if is_blocked:
            return f"{icon} {who} blocked this contact"
        else:
            return f"{icon} {who} unblocked this contact"

    if event_label == "business_meta_managed":
        return f"{icon} This business is now using a secure service from Meta to manage this chat. Tap to learn more."

    if event_label == "disappearing_messages_changed":
        who = actor or "This contact"
        duration = edata.get("new_ephemeral_setting", "")
        if not duration:
            duration = edata.get("ephemeral_duration", "")
        if duration:
            try:
                secs = int(duration)
                dur_str = _fmt_ephemeral_duration(secs)
                if secs == 0:
                    return f"{icon} {who} turned off disappearing messages"
                return f"{icon} {who} uses a default timer for disappearing messages. New messages will disappear from this chat {dur_str} after they're sent, except when kept."
            except (ValueError, TypeError):
                pass
        return f"{icon} {who} uses a default timer for disappearing messages in new chats. New messages will disappear from this chat after they're sent, except when kept."

    if event_label == "participant_joined_community":
        who = target or actor or "Someone"
        community_name = msg.get("community_name", "")
        if community_name:
            return f'{icon} {who} joined from the community "{community_name}"'
        return f"{icon} {who} joined from the community"

    if event_label == "community_admin_changed":
        who = target or actor or "Someone"
        old_val = edata.get("old_value", "")
        if old_val == "admin":
            if actor and actor != who:
                return f"{icon} {actor} removed {who} as community admin"
            return f"{icon} {who} is no longer a community admin"
        if actor and actor != who:
            return f"{icon} {actor} made {who} a community admin"
        return f"{icon} {who} is now a community admin"

    if event_label == "group_join_permission":
        who = target or actor or "An admin"
        old_val = edata.get("old_value", "")
        if old_val == "regular":
            return f"{icon} {who} turned off admin permission to join this group"
        return f"{icon} {who} turned on admin permission to join this group"

    if event_label == "group_add_permission_all":
        who = actor or "An admin"
        return f"{icon} {who} changed this group's settings to allow all members to add others"

    if event_label == "group_add_permission_admins":
        who = actor or "An admin"
        return f"{icon} {who} changed this group's settings to allow only admins to add others"

    if event_label == "community_joined":
        community = msg.get("community_name") or text_data or text or ""
        if community:
            return f'{icon} You joined from the community "{community}"'
        return f"{icon} You joined from the community"

    if event_label == "subgroup_removed":
        who = actor or ""
        name = text_data or text or msg.get("community_name", "") or ""
        if who and name:
            return f'{icon} {who} removed the group "{name}"'
        elif name:
            return f'{icon} Group "{name}" was removed'
        return f"{icon} A group was removed from this community"

    if event_label == "subgroup_added":
        who = actor or ""
        name = text_data or text or msg.get("community_name", "") or ""
        if who and name:
            return f'{icon} {who} added the group "{name}"'
        elif name:
            return f'{icon} Group "{name}" was added'
        return f"{icon} A group was added to this community"

    if event_label == "subgroup_unlinked":
        who = actor or "Someone"
        name = msg.get("community_name") or text_data or text or ""
        if name:
            return f'{icon} {who} removed this group from the community "{name}"'
        return f"{icon} {who} removed this group from the community"

    if event_label == "message_pinned":
        who = actor or "Someone"
        return f"{icon} {who} pinned a message"

    if event_label == "participant_added_with_approval":
        if actor and target:
            return f"{icon} {target} added {actor}. Tap to change member permissions."
        who = target or actor or "Someone"
        return f"{icon} {who} was added"

    if event_label == "community_group_joined":
        name = text_data or text or ""
        if name:
            return f"{icon} You joined a group via invite in the community: {name}"
        return f"{icon} You joined a group via invite in the community"

    if event_label == "community_description_changed":
        who = actor or "Someone"
        return f"{icon} {who} changed the community description. Tap to view."

    if event_label == "channel_privacy_notice":
        return f"{icon} This channel has added privacy for your profile and phone number"

    if event_label == "channel_deleted":
        name = text_data or text or conv_name or "This channel"
        return f'{icon} The channel "{name}" was deleted'

    if event_label == "group_auto_admin_restriction":
        return f"{icon} This group has over 256 members so now only admins can edit the group settings"

    if event_label == "meta_ai_disclaimer":
        return f"{icon} Only messages that mention or people share with @AI can be read by Meta. Meta can't read any other messages in this chat."

    if event_label == "community_linked_group_join":
        name = text_data if (text_data and text_data != "linked_group_join") else ""
        if not name:
            name = conv_name or ""
        if name:
            return f"{icon} Welcome to the group: {name}\nYou joined this group. All community members can use this chat."
        return f"{icon} You joined this group. All community members can use this chat."

    if event_label == "community_created":
        # Only use actor from system_event table — same logic as community_or_group_created
        who = actor or "Someone"
        name = text_data or text or ""
        if name:
            return f'{icon} {who} created community "{name}"'
        return f"{icon} {who} created this community"

    if event_label == "event_updated":
        who = actor or "Someone"
        name = text_data or text or "an event"
        if "," in name:
            parts = name.split(",", 1)
            if parts[0].strip().isdigit():
                name = parts[1].strip()
        return f"{icon} {who} updated {name}"

    if event_label == "community_owner_changed":
        who = target or ""
        if who:
            return f"{icon} {who} is the new owner. Community owner has changed."
        return f"{icon} Community owner has changed"

    if event_label == "group_invite_permission":
        who = actor or "An admin"
        return f"{icon} {who} changed this group's settings to allow all members to invite people to this group using a group link"

    # ══════════════════════════════════════════════════════════
    # 📷 FROM SCREENSHOTS + LOGICAL DEDUCTION
    # ══════════════════════════════════════════════════════════

    if event_label == "you_were_added":
        who = owner_label or "You"
        if actor_is_owner:
            # Owner added themselves (created/joined group)
            return f"{icon} {who} joined this group"
        if actor:
            return f"{icon} {actor} added {who}"
        return f"{icon} {who} were added"

    if event_label == "default_disappearing_timer":
        who = actor or "A contact"
        dur_secs = edata.get("ephemeral_duration")
        if dur_secs is not None:
            try:
                dur_secs = int(dur_secs)
                dur_str = _fmt_ephemeral_duration(dur_secs)
                if dur_secs == 0:
                    return f"{icon} {who} turned off disappearing messages"
                return f"{icon} {who} uses a default timer for disappearing messages in new chats. New messages will disappear from this chat {dur_str} after they're sent, except when kept."
            except (ValueError, TypeError):
                pass
        return f"{icon} {who} uses a default timer for disappearing messages in new chats. New messages will disappear from this chat after they're sent, except when kept."

    if event_label == "community_add_permission":
        who = actor or "An admin"
        return f"{icon} {who} changed this community's settings to allow only admins to add others to this community."

    if event_label == "community_settings_changed":
        who = actor or ""
        if who:
            return f"{icon} {who} changed the community settings"
        return f"{icon} Community settings were changed"

    if event_label == "phone_number_privacy":
        return f"{icon} This chat has added privacy for your phone number. Tap to learn more."

    if event_label == "community_welcome_joined":
        return f"{icon} Welcome to the community! You joined this community"

    if event_label == "community_group_invite_joined":
        name = text_data or text or "the community"
        return f"{icon} You joined a group via invite in the community: {name}"

    if event_label == "subgroup_linked":
        who = actor or "Someone"
        community_name = msg.get("community_name", "")
        if community_name:
            return f'{icon} {who} added this group to the community "{community_name}"'
        return f"{icon} {who} added this group to a community"

    if event_label == "contact_card_shown":
        return ""  # Don't render — it's the contact card

    if event_label == "community_auto_added":
        return f"{icon} You were added because one of your groups was added to this community"

    if event_label == "ai_disclaimer":
        return f"{icon} Messages may be generated by AI and may be inaccurate or inappropriate. Tap to learn more."

    # ── Default: build from available info ──
    if not text or text == "system":
        label = event_label or msg.get("type_label", "system")
        text = label.replace("_", " ").title()

    if actor and target:
        return f"{icon} {actor}: {text} \u2192 {target}"
    elif actor:
        return f"{icon} {actor}: {text}"
    elif target:
        return f"{icon} {text}: {target}"
    return f"{icon} {text}"
