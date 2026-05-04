"""
Unified contact resolution engine.

Merges identity data from five sources into a single ``contact``
table:

1. ``jid_map`` — LID-to-phone JID mappings.
2. ``wa.db wa_contacts`` — ``display_name``, ``wa_name``,
   given / family names.
3. ``lid_display_name`` — LID display names and usernames.
4. ``group_participant_user.label`` — per-group nicknames.
5. ``message_mentions.display_name`` — as used in @mentions.

Produces:
    * ``contact`` table — one row per unique human identity.
    * ``jid_to_contact`` table — maps every source ``jid._id``
      to ``contact.id``.

Name priority: ``display_name`` > ``wa_name`` >
``given + family`` > ``lid_display_name`` > ``lid_username`` >
``nickname`` > ``business_name`` > ``phone_number`` > raw JID.

Handles:
    * LID-only entries that have no phone mapping.
    * Number-change records (old JID → new JID merged into one
      contact).
    * Device JIDs mapped to their parent contact (agent / device
      suffixes stripped).
    * Group, broadcast, and newsletter JIDs excluded from
      contact creation but still recorded in ``jid_to_contact``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.db.connection import DatabaseManager, SourceConnection, AnalysisConnection
from app.db.source_reader import SourceReader
from app.models.enums import JidType, JidServer
from app.utils.jid_parser import parse_jid, extract_phone_number

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# shared_prefs lookup override
#
# Historically, contact_resolver looked up ``startup_prefs.xml`` and
# ``com.whatsapp_preferences_light.xml`` under ``<msgstore_dir>/../shared_prefs``.
# That required the GUI to physically copy pref files next to the source
# databases, which *mutates forensic evidence*.  The override below lets the
# orchestrator point at a GUI-staged temp folder instead, so the source
# extraction stays read-only.
# ---------------------------------------------------------------------------

_PREFS_DIR_OVERRIDE: Optional[str] = None


def set_prefs_dir_override(path: Optional[str]) -> None:
    """Set the directory where ``shared_prefs`` XML files live.

    Call once per ingestion run (from the orchestrator) before contact
    resolution begins.  Pass ``None`` to fall back to the legacy
    ``<msgstore_dir>/../shared_prefs`` lookup.
    """
    global _PREFS_DIR_OVERRIDE
    _PREFS_DIR_OVERRIDE = path or None


def _resolve_prefs_dir(msgstore_dir: str) -> str:
    """Return the directory to search for pref XML files.

    Priority: explicit override (GUI-staged temp dir) → legacy sibling
    ``../shared_prefs`` relative to the msgstore.db directory.
    """
    if _PREFS_DIR_OVERRIDE:
        return _PREFS_DIR_OVERRIDE
    import os as _os
    return _os.path.join(msgstore_dir, "..", "shared_prefs")


@dataclass
class _ContactBuilder:
    """Accumulates identity fragments before committing to the contact table.

    Multiple JID entries may resolve to the same contact. This builder
    collects all available names, phone numbers, and JID references
    before deciding the best ``resolved_name``.
    """

    phone_jid: Optional[str] = None
    lid_jid: Optional[str] = None
    phone_number: Optional[str] = None

    # Name sources (highest to lowest priority)
    display_name: Optional[str] = None
    wa_name: Optional[str] = None
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    nickname: Optional[str] = None
    lid_display_name: Optional[str] = None
    lid_username: Optional[str] = None

    # Business fields
    company: Optional[str] = None
    title: Optional[str] = None
    status_text: Optional[str] = None
    status_emoji: Optional[str] = None
    is_whatsapp_user: bool = True
    is_blocked: bool = False
    is_business: bool = False
    business_name: Optional[str] = None
    business_category: Optional[str] = None
    business_vertical: Optional[str] = None
    business_description: Optional[str] = None
    business_address: Optional[str] = None
    business_city: Optional[str] = None
    business_postal_code: Optional[str] = None
    business_latitude: Optional[float] = None
    business_longitude: Optional[float] = None
    business_location_name: Optional[str] = None
    business_email: Optional[str] = None
    business_website: Optional[str] = None
    business_hours_note: Optional[str] = None
    business_time_zone: Optional[str] = None
    business_member_since: Optional[str] = None
    business_cover_url: Optional[str] = None
    trust_tier: Optional[str] = None
    is_meta_verified: bool = False
    is_business_api_bot: bool = False
    fb_linked_name: Optional[str] = None
    fb_linked_likes: int = 0
    ig_linked_name: Optional[str] = None
    ig_linked_followers: int = 0
    lid_masked_phone: Optional[str] = None  # e.g. "+91∙∙∙∙∙∙∙∙89"

    # Source traceability
    source_wa_db_id: Optional[int] = None
    source_jid_row_id: Optional[int] = None
    source_lid_row_id: Optional[int] = None

    # All JID row IDs that resolve to this contact (for jid_to_contact)
    jid_row_ids: list[tuple[int, str, int | None]] = field(default_factory=list)
    """List of (jid_row_id, raw_string, jid_type) tuples."""

    @staticmethod
    def _is_placeholder(name: str | None) -> bool:
        """Return True if name is a WhatsApp placeholder (e.g., '.....')."""
        if not name:
            return True
        stripped = name.strip()
        if not stripped:
            return True
        # Dot-only names like ".....", "...", "."
        if all(c in '.\u2024\u2027\u00b7\u2022\u2219' for c in stripped):
            return True
        # Privacy-masked phone: "+91∙∙∙∙∙∙∙∙57" -- contains 3+ consecutive mask chars
        import re
        if re.search(r'[\u2219\u2022\u00b7\u2024\u2027.]{3,}', stripped):
            # It's a masked phone / partial number, not a real name
            return True
        return False

    @property
    def resolved_name(self) -> str:
        """Compute the best available name using the priority chain.

        Skips placeholder names like '.....' — falls through to phone number.
        """
        # 1. display_name (from wa.db contacts, most reliable)
        if not self._is_placeholder(self.display_name):
            return self.display_name.strip()
        # 2. wa_name (WhatsApp profile / push name)
        if not self._is_placeholder(self.wa_name):
            return self.wa_name.strip()
        # 3. given_name + family_name
        parts = []
        if self.given_name and self.given_name.strip():
            parts.append(self.given_name.strip())
        if self.family_name and self.family_name.strip():
            parts.append(self.family_name.strip())
        if parts:
            return " ".join(parts)
        # 4. lid_display_name
        if not self._is_placeholder(self.lid_display_name):
            return self.lid_display_name.strip()
        # 5. lid_username
        if not self._is_placeholder(self.lid_username):
            return self.lid_username.strip()
        # 6. nickname
        if not self._is_placeholder(self.nickname):
            return self.nickname.strip()
        # 7. business_name
        if not self._is_placeholder(self.business_name):
            return self.business_name.strip()
        # 8. phone_number
        if self.phone_number:
            return f"+{self.phone_number}"
        # 9. raw JID
        if self.phone_jid:
            return self.phone_jid
        if self.lid_jid:
            return self.lid_jid
        return "Unknown"

    def merge(self, other: _ContactBuilder) -> None:
        """Merge another builder's data into this one, preferring non-null values."""
        if other.phone_jid and not self.phone_jid:
            self.phone_jid = other.phone_jid
        if other.lid_jid and not self.lid_jid:
            self.lid_jid = other.lid_jid
        if other.phone_number and not self.phone_number:
            self.phone_number = other.phone_number
        if other.display_name and not self.display_name:
            self.display_name = other.display_name
        if other.wa_name and not self.wa_name:
            self.wa_name = other.wa_name
        if other.given_name and not self.given_name:
            self.given_name = other.given_name
        if other.family_name and not self.family_name:
            self.family_name = other.family_name
        if other.nickname and not self.nickname:
            self.nickname = other.nickname
        if other.lid_display_name and not self.lid_display_name:
            self.lid_display_name = other.lid_display_name
        if other.lid_username and not self.lid_username:
            self.lid_username = other.lid_username
        if other.company and not self.company:
            self.company = other.company
        if other.title and not self.title:
            self.title = other.title
        if other.status_text and not self.status_text:
            self.status_text = other.status_text
        if other.is_business:
            self.is_business = True
        if other.business_name and not self.business_name:
            self.business_name = other.business_name
        if other.business_category and not self.business_category:
            self.business_category = other.business_category
        if other.business_description and not self.business_description:
            self.business_description = other.business_description
        if other.business_address and not self.business_address:
            self.business_address = other.business_address
        if other.business_email and not self.business_email:
            self.business_email = other.business_email
        if other.business_website and not self.business_website:
            self.business_website = other.business_website
        if other.business_member_since and not self.business_member_since:
            self.business_member_since = other.business_member_since
        if other.trust_tier and not self.trust_tier:
            self.trust_tier = other.trust_tier
        if other.is_meta_verified:
            self.is_meta_verified = True
        if other.is_business_api_bot:
            self.is_business_api_bot = True
        if other.fb_linked_name and not self.fb_linked_name:
            self.fb_linked_name = other.fb_linked_name
            self.fb_linked_likes = other.fb_linked_likes
        if other.ig_linked_name and not self.ig_linked_name:
            self.ig_linked_name = other.ig_linked_name
            self.ig_linked_followers = other.ig_linked_followers
        if other.source_wa_db_id and not self.source_wa_db_id:
            self.source_wa_db_id = other.source_wa_db_id
        if other.source_jid_row_id and not self.source_jid_row_id:
            self.source_jid_row_id = other.source_jid_row_id
        if other.source_lid_row_id and not self.source_lid_row_id:
            self.source_lid_row_id = other.source_lid_row_id
        self.jid_row_ids.extend(other.jid_row_ids)


class ContactResolver:
    """Builds unified contact records from multiple identity sources.

    This is the most critical component of the ingestion pipeline. Every
    other ingester depends on the ``jid_to_contact`` mapping produced here
    to resolve sender/recipient references.

    The resolution process:

    1. Load all phone JIDs (type=0, server=s.whatsapp.net) as base contacts
    2. Load all LID user JIDs (type=18, server=lid) as potential contacts
    3. Use ``jid_map`` to link LID JIDs to their phone JID counterparts
    4. Enrich with wa.db contact names (display_name, wa_name, etc.)
    5. Enrich with lid_display_name for LID-only contacts
    6. Handle number changes (merge old_jid → new_jid contacts)
    7. Write unified ``contact`` rows
    8. Write ``jid_to_contact`` mapping for ALL jid._id entries
    """

    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db = db_manager
        self._msgstore: Optional[SourceConnection] = None
        self._wa_db: Optional[SourceConnection] = None

        # In-memory resolution maps built during processing
        self._phone_jid_to_builder: dict[str, _ContactBuilder] = {}
        """Maps phone JID raw string → ContactBuilder."""

        self._lid_jid_to_builder: dict[str, _ContactBuilder] = {}
        """Maps LID JID raw string → ContactBuilder."""

        self._jid_row_to_phone: dict[int, str] = {}
        """Maps jid._id → phone JID raw string (for fast lookups)."""

        self._jid_row_to_lid: dict[int, str] = {}
        """Maps jid._id → LID JID raw string."""

        self._lid_to_phone_map: dict[str, str] = {}
        """LID raw_string → phone raw_string from jid_map table."""

        # Device owner detection
        self._owner_name: Optional[str] = None
        self._owner_phone_jid: Optional[str] = None

        # Final output
        self._all_builders: list[_ContactBuilder] = []
        self._contact_count = 0

    def resolve(self, analysis_conn: AnalysisConnection) -> int:
        """Execute the full contact resolution pipeline.

        Args:
            analysis_conn: Write connection to analysis.db.

        Returns:
            Number of unified contacts created.
        """
        self._msgstore = self._db.get_msgstore()
        reader = SourceReader(self._msgstore)

        logger.info("=== Contact Resolution Pipeline ===")

        # Step 1: Load LID-to-phone mapping from jid_map (primary source)
        self._load_jid_map(reader)

        # Step 1b: Additional LID resolution from chat.account_jid_row_id
        self._resolve_lids_from_chat(reader)

        # Step 1c: Additional LID resolution from receipt_user cross-reference
        self._resolve_lids_from_receipts(reader)

        # Step 1d: Axolotl identity key matching (cryptographic verification)
        self._resolve_lids_from_axolotl()

        # Step 1e: VCard phone number extraction
        self._resolve_lids_from_vcards(reader)

        # Log LID resolution summary
        logger.info("LID resolution summary: %d total mappings", len(self._lid_to_phone_map))

        # Step 2: Load all JIDs from msgstore and build initial contact map
        self._load_jids(reader)

        # Step 3: Enrich from wa.db (contact names)
        self._enrich_from_wa_db()

        # Step 3b: Populate blocked contacts from msgstore block events
        self._enrich_blocked_from_msgstore(reader)

        # Step 4: Enrich from lid_display_name table
        self._enrich_from_lid_display_names(reader)

        # Step 5: Handle number changes
        self._handle_number_changes(reader)

        # Step 6: Consolidate - merge LID-only contacts that now have phone mappings
        self._consolidate_builders()

        # Step 7: Write to analysis.db
        self._contact_count = self._write_contacts(analysis_conn)

        # Step 8: Detect device owner
        self._owner_name, self._owner_phone_jid = self._detect_device_owner(reader)

        # Step 9: Flag Meta AI contact and short phone numbers
        self._flag_special_contacts(analysis_conn)

        logger.info(
            "Contact resolution complete: %d unified contacts, %d JID mappings",
            self._contact_count,
            sum(len(b.jid_row_ids) for b in self._all_builders),
        )
        return self._contact_count

    def _load_jid_map(self, reader: SourceReader) -> None:
        """Load LID-to-phone JID mappings from the jid_map table.

        The ``jid_map`` table in msgstore.db links WhatsApp's
        newer Local ID (LID) system to traditional phone-based
        JIDs.
        """
        if not reader.table_exists("jid_map"):
            logger.warning("jid_map table not found - LID resolution will be limited")
            return

        rows = reader.execute_raw(
            "SELECT jid_row_id, lid_row_id FROM jid_map"
        )

        # We need the raw_string for each jid_row_id and lid_row_id
        # Build a temporary id→raw map from jid table
        jid_rows = reader.execute_raw(
            "SELECT _id, raw_string FROM jid"
        )
        id_to_raw: dict[int, str] = {row[0]: row[1] for row in jid_rows if row[1]}

        mapped = 0
        for jid_row_id, lid_row_id in rows:
            phone_raw = id_to_raw.get(jid_row_id)
            lid_raw = id_to_raw.get(lid_row_id)
            if phone_raw and lid_raw:
                self._lid_to_phone_map[lid_raw] = phone_raw
                mapped += 1

        logger.info("Loaded %d LID-to-phone mappings from jid_map", mapped)

    def _resolve_lids_from_chat(self, reader: SourceReader) -> None:
        """Resolve additional LID→phone mappings from chat.account_jid_row_id.

        In personal chats, ``chat.account_jid_row_id`` contains the contact's
        LID JID, while ``chat.jid_row_id`` contains their phone JID.
        This provides supplementary mappings beyond jid_map.
        """
        # Build id→raw map
        jid_rows = reader.execute_raw("SELECT _id, raw_string, server FROM jid")
        id_to_raw: dict[int, str] = {}
        id_to_server: dict[int, str] = {}
        for row in jid_rows:
            if row[1]:
                id_to_raw[row[0]] = row[1]
                id_to_server[row[0]] = row[2] or ""

        # Query personal chats: account_jid_row_id (LID) paired with jid_row_id (phone)
        chat_rows = reader.execute_raw(
            "SELECT account_jid_row_id, jid_row_id FROM chat "
            "WHERE account_jid_row_id > 0 AND subject IS NULL"
        )

        new_mappings = 0
        for acct_jid_row, chat_jid_row in chat_rows:
            lid_raw = id_to_raw.get(acct_jid_row)
            phone_raw = id_to_raw.get(chat_jid_row)
            if not lid_raw or not phone_raw:
                continue
            # Verify: account_jid must be LID, chat_jid must be phone
            if id_to_server.get(acct_jid_row) != "lid":
                continue
            if id_to_server.get(chat_jid_row) != "s.whatsapp.net":
                continue
            if lid_raw not in self._lid_to_phone_map:
                self._lid_to_phone_map[lid_raw] = phone_raw
                new_mappings += 1

        logger.info("Resolved %d additional LID mappings from chat.account_jid_row_id", new_mappings)

    def _resolve_lids_from_receipts(self, reader: SourceReader) -> None:
        """Resolve additional LID→phone mappings from receipt_user cross-reference.

        In personal chats, receipt_user entries with LID JIDs can be traced
        back to the conversation's phone JID via the message→chat→jid chain.
        """
        if not reader.table_exists("receipt_user"):
            return

        # Build id→raw and id→server maps
        jid_rows = reader.execute_raw("SELECT _id, raw_string, server FROM jid")
        id_to_raw: dict[int, str] = {}
        id_to_server: dict[int, str] = {}
        for row in jid_rows:
            if row[1]:
                id_to_raw[row[0]] = row[1]
                id_to_server[row[0]] = row[2] or ""

        # Find unresolved LID JIDs in receipt_user
        unresolved_lid_rows = reader.execute_raw(
            "SELECT DISTINCT ru.receipt_user_jid_row_id "
            "FROM receipt_user ru "
            "JOIN jid j ON j._id = ru.receipt_user_jid_row_id "
            "WHERE j.server = 'lid' AND j.type = 18"
        )
        unresolved_lids = set()
        for (lid_row_id,) in unresolved_lid_rows:
            lid_raw = id_to_raw.get(lid_row_id)
            if lid_raw and lid_raw not in self._lid_to_phone_map:
                unresolved_lids.add(lid_row_id)

        if not unresolved_lids:
            logger.info("No unresolved LID receipt users to process")
            return

        # Build message→chat mapping
        msg_chat_rows = reader.execute_raw(
            "SELECT _id, chat_row_id FROM message"
        )
        msg_to_chat: dict[int, int] = {r[0]: r[1] for r in msg_chat_rows}

        # Build chat→jid mapping (for personal chats only)
        chat_rows = reader.execute_raw(
            "SELECT _id, jid_row_id FROM chat WHERE subject IS NULL"
        )
        chat_to_phone_jid: dict[int, int] = {}
        for chat_id, jid_row in chat_rows:
            if id_to_server.get(jid_row) == "s.whatsapp.net":
                chat_to_phone_jid[chat_id] = jid_row

        # For each unresolved LID, find a receipt linking it to a personal chat
        new_mappings = 0
        for lid_row_id in unresolved_lids:
            lid_raw = id_to_raw.get(lid_row_id)
            if not lid_raw:
                continue

            # Find one message that has this LID in receipt_user
            sample = reader.execute_raw(
                "SELECT message_row_id FROM receipt_user "
                "WHERE receipt_user_jid_row_id = ? LIMIT 1",
                (lid_row_id,),
            )
            if not sample:
                continue

            msg_id = sample[0][0]
            chat_id = msg_to_chat.get(msg_id)
            if chat_id is None:
                continue

            phone_jid_row = chat_to_phone_jid.get(chat_id)
            if phone_jid_row is None:
                continue

            phone_raw = id_to_raw.get(phone_jid_row)
            if phone_raw and lid_raw not in self._lid_to_phone_map:
                self._lid_to_phone_map[lid_raw] = phone_raw
                new_mappings += 1

        logger.info("Resolved %d additional LID mappings from receipt_user", new_mappings)

    def _resolve_lids_from_axolotl(self) -> None:
        """Resolve LID→phone mappings from axolotl.db identity key matching.

        The ``identities`` table stores public keys for both phone-based
        (recipient_type=0) and LID-based (recipient_type=1) contacts.
        Same person shares the same 33-byte public_key blob, enabling
        cryptographic cross-referencing between LID and phone identities.
        """
        try:
            axolotl = self._db.get_source("axolotl.db")
        except (FileNotFoundError, Exception) as exc:
            logger.info("axolotl.db not available (%s) - skipping identity key matching", exc)
            return

        axo_reader = SourceReader(axolotl)
        if not axo_reader.table_exists("identities"):
            logger.info("identities table not found in axolotl.db")
            return

        # Build phone_key map: public_key blob → phone number string
        phone_rows = axo_reader.execute_raw(
            "SELECT recipient_id, public_key, device_id FROM identities "
            "WHERE recipient_type = 0 AND public_key IS NOT NULL"
        )
        # Map (public_key, device_id) → phone_number for precise matching
        phone_keys: dict[tuple[bytes, int], str] = {}
        for recipient_id, public_key, device_id in phone_rows:
            key = (bytes(public_key), device_id or 0)
            phone_keys[key] = str(recipient_id)

        # Match LID identities against phone keys
        lid_rows = axo_reader.execute_raw(
            "SELECT recipient_id, public_key, device_id FROM identities "
            "WHERE recipient_type = 1 AND public_key IS NOT NULL"
        )

        new_mappings = 0
        for recipient_id, public_key, device_id in lid_rows:
            key = (bytes(public_key), device_id or 0)
            phone_number = phone_keys.get(key)
            if not phone_number:
                continue

            lid_jid = f"{recipient_id}@lid"
            phone_jid = f"{phone_number}@s.whatsapp.net"

            if lid_jid not in self._lid_to_phone_map:
                self._lid_to_phone_map[lid_jid] = phone_jid
                new_mappings += 1

        logger.info(
            "Resolved %d additional LID mappings from axolotl.db identity keys "
            "(%d phone keys, %d LID entries checked)",
            new_mappings, len(phone_keys), len(lid_rows),
        )

    def _resolve_lids_from_vcards(self, reader: SourceReader) -> None:
        """Resolve LID→phone mappings from shared vCard contact data.

        When a contact is shared as a vCard, the ``message_vcard`` table
        contains the vCard text with TEL fields (real phone numbers), and
        ``message_vcard_jid`` links vCards to JID entries. If a vCard's
        JID is a LID, we can extract the phone from the TEL field.
        """
        import re

        if not reader.table_exists("message_vcard_jid") or not reader.table_exists("message_vcard"):
            return

        # Find vCards that reference LID JIDs
        lid_vcard_rows = reader.execute_raw(
            "SELECT mvj.message_row_id, j.raw_string "
            "FROM message_vcard_jid mvj "
            "JOIN jid j ON j._id = mvj.vcard_jid_row_id "
            "WHERE j.server = 'lid'"
        )

        if not lid_vcard_rows:
            return

        # Build msg_row_id → LID raw string mapping
        msg_to_lid: dict[int, str] = {}
        for msg_row_id, lid_raw in lid_vcard_rows:
            if lid_raw and lid_raw not in self._lid_to_phone_map:
                msg_to_lid[msg_row_id] = lid_raw

        if not msg_to_lid:
            logger.info("No unresolved LID vCards to process")
            return

        # Load vCard text for matching messages
        placeholders = ",".join("?" * len(msg_to_lid))
        vcard_rows = reader.execute_raw(
            f"SELECT message_row_id, vcard FROM message_vcard "
            f"WHERE message_row_id IN ({placeholders})",
            tuple(msg_to_lid.keys()),
        )

        tel_pattern = re.compile(r'TEL[^:]*:[\s]*([\+]?\d[\d\s\-\.]+)', re.IGNORECASE)
        new_mappings = 0

        for msg_row_id, vcard_text in vcard_rows:
            if not vcard_text:
                continue
            lid_raw = msg_to_lid.get(msg_row_id)
            if not lid_raw:
                continue

            match = tel_pattern.search(vcard_text)
            if not match:
                continue

            # Clean phone number: remove spaces, dashes, dots, leading +
            phone = re.sub(r'[\s\-\.\(\)]', '', match.group(1))
            if phone.startswith('+'):
                phone = phone[1:]

            if len(phone) >= 7:  # Minimum viable phone number
                phone_jid = f"{phone}@s.whatsapp.net"
                if lid_raw not in self._lid_to_phone_map:
                    self._lid_to_phone_map[lid_raw] = phone_jid
                    new_mappings += 1

        logger.info("Resolved %d additional LID mappings from vCard data", new_mappings)

    def _detect_device_owner(self, reader: SourceReader) -> tuple[Optional[str], Optional[str]]:
        """Detect the device owner's name and phone JID.

        Uses multiple signals in priority order:
        1. "My Number" / self-saved contact in wa.db (most reliable)
        2. self_display_name from shared_prefs (masked phone ending)
        3. props.user_push_name for name
        4. from_me=1 group message sender (fallback, can be wrong for shared phones)

        Returns:
            (owner_name, owner_phone_jid) tuple.
        """
        owner_name = None
        owner_phone_jid = None

        # Signal 1: "My Number" contact in wa.db
        try:
            wa_conn = self._db.get_wa_db()
            wa_reader_local = SourceReader(wa_conn)
            if wa_reader_local.table_exists("wa_contacts"):
                # Look for contacts saved as "My Number", "Self", "Me" etc.
                my_rows = wa_reader_local.execute_raw(
                    "SELECT jid, display_name, wa_name FROM wa_contacts "
                    "WHERE lower(display_name) IN ('my number', 'my no', 'self', 'me', 'myself') "
                    "AND jid LIKE '%@s.whatsapp.net' LIMIT 1"
                )
                if my_rows:
                    owner_phone_jid = my_rows[0][0]
                    logger.info("Owner detected via 'My Number' contact: %s", owner_phone_jid)
        except (FileNotFoundError, Exception):
            pass

        # Signal 2: self_display_name from shared_prefs (masked phone number ending)
        if not owner_phone_jid:
            try:
                import os, re, xml.etree.ElementTree as ET
                msgstore_dir = (
                    os.path.dirname(reader._db_path)
                    if hasattr(reader, '_db_path') else ""
                )
                prefs_dir = _resolve_prefs_dir(msgstore_dir)
                prefs_file = os.path.join(prefs_dir, "com.whatsapp_preferences_light.xml")
                if os.path.exists(prefs_file):
                    tree = ET.parse(prefs_file)
                    for elem in tree.iter("string"):
                        if elem.get("name") == "self_display_name" and elem.text:
                            # Extract last 2 digits from masked number like "+91∙∙∙∙∙∙∙∙09"
                            digits = re.findall(r'\d', elem.text)
                            if len(digits) >= 2:
                                suffix = "".join(digits[-2:])
                                # Match against wa_contacts
                                try:
                                    wa_conn2 = self._db.get_wa_db()
                                    wa_r2 = SourceReader(wa_conn2)
                                    matches = wa_r2.execute_raw(
                                        "SELECT jid FROM wa_contacts "
                                        "WHERE jid LIKE ? AND jid LIKE '%@s.whatsapp.net' "
                                        "AND (display_name = '' OR display_name IS NULL OR "
                                        "     lower(display_name) IN ('my number','self','me','myself'))",
                                        (f"%{suffix}@%",),
                                    )
                                    if matches and len(matches) == 1:
                                        owner_phone_jid = matches[0][0]
                                        logger.info("Owner detected via self_display_name suffix: %s", owner_phone_jid)
                                except Exception:
                                    pass
            except Exception:
                pass

        # Signal 3: Owner push_name from props table OR shared_prefs/startup_prefs.xml
        if reader.table_exists("props"):
            push = reader.execute_raw(
                "SELECT value FROM props WHERE key = 'user_push_name'"
            )
            if push and push[0][0]:
                owner_name = push[0][0]

        if not owner_name:
            try:
                import os, xml.etree.ElementTree as ET
                db_dir = os.path.dirname(self._db._msgstore_path) if hasattr(self._db, '_msgstore_path') else ""
                if not db_dir:
                    # Try to find databases dir from reader
                    db_dir = getattr(self._db, '_databases_dir', "")
                prefs_dir = _resolve_prefs_dir(db_dir)
                startup = os.path.join(prefs_dir, "startup_prefs.xml")
                if os.path.exists(startup):
                    tree = ET.parse(startup)
                    for elem in tree.iter("string"):
                        if elem.get("name") == "push_name" and elem.text:
                            owner_name = elem.text.strip()
                            logger.info("Owner name from startup_prefs.xml: %s", owner_name)
                            break
            except Exception as e:
                logger.debug("Could not read startup_prefs.xml: %s", e)

        # Signal 4: from_me=1 -> message_details.author_device_jid -> jid table
        # This is the MOST RELIABLE method: the device JID for sent messages IS the owner
        if not owner_phone_jid and reader.table_exists("message_details"):
            owner_jid = reader.execute_raw(
                "SELECT j.raw_string FROM message m "
                "JOIN message_details md ON md.message_row_id = m._id "
                "JOIN jid j ON j._id = md.author_device_jid "
                "WHERE m.from_me = 1 AND j.server = 's.whatsapp.net' AND j.device = 0 "
                "GROUP BY j.user "
                "ORDER BY COUNT(*) DESC LIMIT 1"
            )
            if owner_jid and owner_jid[0][0]:
                # raw_string is like "15551234567.0:0@s.whatsapp.net", extract user part
                raw = owner_jid[0][0]
                user_part = raw.split(".")[0] if "." in raw else raw.split("@")[0]
                owner_phone_jid = f"{user_part}@s.whatsapp.net"
                logger.info("Owner detected via message_details.author_device_jid: %s", owner_phone_jid)

        # Signal 5 (last fallback): Most common sender_jid in from_me=1 NON-SYSTEM messages
        if not owner_phone_jid:
            owner_jid = reader.execute_raw(
                "SELECT j.raw_string FROM message m "
                "JOIN jid j ON j._id = m.sender_jid_row_id "
                "WHERE m.from_me = 1 AND j.server = 's.whatsapp.net' "
                "AND m.sender_jid_row_id > 0 "
                "AND m.message_type != 7 "
                "GROUP BY m.sender_jid_row_id "
                "ORDER BY COUNT(*) DESC LIMIT 1"
            )
            if owner_jid and owner_jid[0][0]:
                owner_phone_jid = owner_jid[0][0]
                logger.info("Owner detected via from_me=1 sender_jid (last fallback): %s", owner_phone_jid)

        # Resolve name from wa.db if still missing
        if not owner_name and owner_phone_jid:
            try:
                wa_conn3 = self._db.get_wa_db()
                wa_r3 = SourceReader(wa_conn3)
                if wa_r3.table_exists("wa_contacts"):
                    wa_row = wa_r3.execute_raw(
                        "SELECT wa_name, display_name FROM wa_contacts WHERE jid = ?",
                        (owner_phone_jid,),
                    )
                    if wa_row:
                        dn = wa_row[0][1]
                        wn = wa_row[0][0]
                        # Skip "My Number" as owner name, use wa_name instead
                        if dn and dn.lower() not in ('my number', 'my no', 'self', 'me', 'myself'):
                            owner_name = dn
                        elif wn:
                            owner_name = wn
            except (FileNotFoundError, Exception) as e:
                logger.debug("Could not look up owner name in wa.db: %s", e)

        if owner_name or owner_phone_jid:
            logger.info(
                "Device owner detected: name=%s, phone=%s",
                owner_name, owner_phone_jid,
            )

        return owner_name, owner_phone_jid

    def _flag_special_contacts(self, analysis_conn: AnalysisConnection) -> None:
        """Flag Meta AI contact and short phone number contacts."""
        # Flag Meta AI contact (JID 13135550002@s.whatsapp.net)
        analysis_conn.execute(
            "UPDATE contact SET is_business = 1, business_name = 'Meta AI' "
            "WHERE id = ("
            "  SELECT contact_id FROM jid_to_contact "
            "  WHERE jid_raw_string = '13135550002@s.whatsapp.net' LIMIT 1"
            ")"
        )
        meta_ai_check = analysis_conn.fetchone(
            "SELECT COUNT(*) FROM contact WHERE business_name = 'Meta AI'"
        )
        if meta_ai_check and meta_ai_check[0]:
            logger.info("Flagged Meta AI contact")

        # Flag short phone numbers (< 10 digits) as non-WhatsApp
        # These are carrier service numbers (91100, Jio helplines, etc.)
        before = analysis_conn.fetchone(
            "SELECT COUNT(*) FROM contact WHERE phone_number IS NOT NULL "
            "AND LENGTH(phone_number) < 10 AND is_whatsapp_user = 1"
        )
        short_count = before[0] if before else 0
        if short_count > 0:
            analysis_conn.execute(
                "UPDATE contact SET is_whatsapp_user = 0 "
                "WHERE phone_number IS NOT NULL "
                "AND LENGTH(phone_number) < 10 "
                "AND is_whatsapp_user = 1"
            )
            logger.info("Flagged %d short phone number contacts as non-WhatsApp", short_count)

    def _load_jids(self, reader: SourceReader) -> None:
        """Load all JIDs from msgstore.db and create initial contact builders.

        Creates _ContactBuilder entries for:
        - Phone user JIDs (type=0, server=s.whatsapp.net)
        - LID user JIDs (type=18, server=lid)

        Skips group, broadcast, device, and newsletter JIDs for contact
        creation but still records them for jid_to_contact mapping.
        """
        rows = reader.execute_raw(
            "SELECT _id, raw_string, type, server, agent, device, \"user\" FROM jid"
        )

        phone_count = 0
        lid_count = 0
        device_count = 0
        skipped = 0

        for row in rows:
            jid_id, raw_string, jid_type, server, agent, device_num, user_part = row

            if not raw_string:
                skipped += 1
                continue

            # Record raw mappings for all JIDs
            if server == JidServer.WHATSAPP and jid_type == JidType.USER:
                self._jid_row_to_phone[jid_id] = raw_string
            elif server == JidServer.LID and jid_type == JidType.LID_USER:
                self._jid_row_to_lid[jid_id] = raw_string

            # Device JIDs: explicit type 17 (PHONE_DEVICE) or type 19 (LID_DEVICE),
            # or any JID with agent > 0 or device > 0
            is_device = (
                jid_type == JidType.PHONE_DEVICE
                or jid_type == JidType.LID_DEVICE
                or (agent and agent > 0)
                or (device_num and device_num > 0)
            )

            # Create contact builders only for user-type JIDs
            if server == JidServer.WHATSAPP and jid_type == JidType.USER:
                if raw_string not in self._phone_jid_to_builder:
                    phone_number = extract_phone_number(raw_string)
                    builder = _ContactBuilder(
                        phone_jid=raw_string,
                        phone_number=phone_number,
                        source_jid_row_id=jid_id,
                    )
                    builder.jid_row_ids.append((jid_id, raw_string, jid_type))
                    self._phone_jid_to_builder[raw_string] = builder
                    phone_count += 1
                else:
                    self._phone_jid_to_builder[raw_string].jid_row_ids.append(
                        (jid_id, raw_string, jid_type)
                    )

            elif server == JidServer.LID and jid_type == JidType.LID_USER:
                if raw_string not in self._lid_jid_to_builder:
                    builder = _ContactBuilder(
                        lid_jid=raw_string,
                        source_lid_row_id=jid_id,
                    )
                    builder.jid_row_ids.append((jid_id, raw_string, jid_type))
                    self._lid_jid_to_builder[raw_string] = builder
                    lid_count += 1
                else:
                    self._lid_jid_to_builder[raw_string].jid_row_ids.append(
                        (jid_id, raw_string, jid_type)
                    )

            elif is_device:
                # Device JIDs: resolve to parent user. The user part (without
                # agent/device suffix) should match a phone or LID JID.
                parent_raw = f"{user_part}@{server}" if user_part and server else None
                if parent_raw:
                    if parent_raw in self._phone_jid_to_builder:
                        self._phone_jid_to_builder[parent_raw].jid_row_ids.append(
                            (jid_id, raw_string, jid_type)
                        )
                    elif parent_raw in self._lid_jid_to_builder:
                        self._lid_jid_to_builder[parent_raw].jid_row_ids.append(
                            (jid_id, raw_string, jid_type)
                        )
                    # If parent not found, we'll pick it up in consolidation
                device_count += 1

            else:
                # Group, broadcast, newsletter, bot JIDs -- not contacts
                skipped += 1

        logger.info(
            "Loaded JIDs: %d phone users, %d LID users, %d device JIDs, %d skipped",
            phone_count, lid_count, device_count, skipped,
        )

    def _enrich_from_wa_db(self) -> None:
        """Enrich contact builders with names from wa.db wa_contacts table."""
        try:
            wa_conn = self._db.get_wa_db()
        except FileNotFoundError:
            logger.warning("wa.db not found - contact names will be limited")
            return

        wa_reader = SourceReader(wa_conn)
        if not wa_reader.table_exists("wa_contacts"):
            logger.warning("wa_contacts table not found in wa.db")
            return

        # Check available columns
        columns = wa_reader.get_column_names("wa_contacts")
        select_cols = ["_id", "jid"]

        # Add optional columns that may or may not exist
        optional_cols = [
            "display_name", "wa_name", "given_name", "family_name",
            "nickname", "status", "company", "title",
            "is_whatsapp_user", "number",
        ]
        for col in optional_cols:
            if col in columns:
                select_cols.append(col)

        rows = wa_reader.execute_raw(
            f"SELECT {', '.join(select_cols)} FROM wa_contacts"
        )

        enriched = 0
        for row in rows:
            row_dict = dict(zip(select_cols, row))
            jid = row_dict.get("jid")
            if not jid:
                continue

            builder = self._phone_jid_to_builder.get(jid)
            if not builder and "@lid" in jid:
                # LID-only contacts: look up by LID JID
                builder = self._lid_jid_to_builder.get(jid)
            if not builder:
                continue

            builder.source_wa_db_id = row_dict.get("_id")
            if row_dict.get("display_name"):
                builder.display_name = row_dict["display_name"]
            if row_dict.get("wa_name"):
                builder.wa_name = row_dict["wa_name"]
            if row_dict.get("given_name"):
                builder.given_name = row_dict["given_name"]
            if row_dict.get("family_name"):
                builder.family_name = row_dict["family_name"]
            if row_dict.get("nickname"):
                builder.nickname = row_dict["nickname"]
            if row_dict.get("status"):
                builder.status_text = row_dict["status"]
            if row_dict.get("company"):
                builder.company = row_dict["company"]
            if row_dict.get("title"):
                builder.title = row_dict["title"]
            # Honour ``is_whatsapp_user`` from wa.db so contacts
            # marked non-WhatsApp don't get WA-user treatment.
            is_wa = row_dict.get("is_whatsapp_user")
            if is_wa is not None and not is_wa:
                builder.is_whatsapp_user = False
            enriched += 1

        # Also check for business contacts
        if wa_reader.table_exists("wa_vnames"):
            biz_rows = wa_reader.execute_raw(
                "SELECT jid, verified_name FROM wa_vnames WHERE verified_name IS NOT NULL"
            )
            biz_count = 0
            for jid, name in biz_rows:
                builder = self._phone_jid_to_builder.get(jid)
                if builder:
                    builder.is_business = True
                    builder.business_name = name
                    biz_count += 1
            logger.info("Found %d business contacts in wa_vnames", biz_count)

        # Populate is_blocked from wa_block_list
        if wa_reader.table_exists("wa_block_list"):
            block_rows = wa_reader.execute_raw("SELECT jid FROM wa_block_list")
            block_count = 0
            for (jid,) in block_rows:
                builder = self._phone_jid_to_builder.get(jid)
                if not builder and jid:
                    # Try LID lookup
                    builder = self._lid_jid_to_builder.get(jid)
                if builder:
                    builder.is_blocked = True
                    block_count += 1
            logger.info("Marked %d contacts as blocked from wa_block_list (%d total in list)",
                        block_count, len(block_rows))

        logger.info("Enriched %d contacts from wa.db", enriched)

        # Enrich business data from wa_biz_profiles and wa_biz_integrity_signals
        self._enrich_business_data(wa_reader)

    def _enrich_business_data(self, wa_reader) -> None:
        """Enrich contacts with WhatsApp Business profile and verification data.

        Sources:
          - wa_biz_profiles: description, address, email, category, member_since
          - wa_biz_profiles_categories: category names
          - wa_biz_profiles_linked_accounts_table: FB/IG linked accounts
          - wa_biz_integrity_signals: trust_tier (TIER_0/1/2), spam data
            NOTE: integrity_signals uses BOTH phone JID and LID JID as keys.
        """
        enriched = 0

        # --- Business Profiles (batch all queries upfront to avoid N+1) ---
        if wa_reader.table_exists("wa_biz_profiles"):
            # Pre-load all categories, websites, linked accounts in bulk
            cat_map: dict[int, list[str]] = {}
            if wa_reader.table_exists("wa_biz_profiles_categories"):
                for c in wa_reader.execute_raw(
                    "SELECT wa_biz_profile_id, category_name FROM wa_biz_profiles_categories"
                ):
                    cat_map.setdefault(c[0], []).append(c[1])

            site_map: dict[int, str] = {}
            if wa_reader.table_exists("wa_biz_profiles_websites"):
                for s in wa_reader.execute_raw(
                    "SELECT wa_biz_profile_id, websites FROM wa_biz_profiles_websites"
                ):
                    if s[1] and s[0] not in site_map:
                        site_map[s[0]] = s[1]

            linked_map: dict[int, list[tuple]] = {}
            if wa_reader.table_exists("wa_biz_profiles_linked_accounts_table"):
                for la in wa_reader.execute_raw(
                    "SELECT wa_biz_profile_id, account_type, account_display_name, account_fan_count "
                    "FROM wa_biz_profiles_linked_accounts_table"
                ):
                    linked_map.setdefault(la[0], []).append((la[1], la[2], la[3]))

            # Now iterate profiles with O(1) lookups - pull EVERY useful
            # column wa_biz_profiles offers so the contact-detail page
            # has the same richness investigators see in WhatsApp itself.
            for row in wa_reader.execute_raw(
                "SELECT _id, jid, business_description, address, email,"
                "       member_since, vertical, latitude, longitude,"
                "       address_city_name, address_postal_code,"
                "       location_name, hours_note, time_zone,"
                "       cover_photo_url "
                "FROM wa_biz_profiles"
            ):
                (bp_id, jid, descr, addr, email, member_since,
                 vertical, lat, lng, city, postal, loc_name,
                 hours_note, time_zone, cover_url) = row
                builder = self._phone_jid_to_builder.get(jid)
                if builder:
                    builder.is_business = True
                    if descr:
                        builder.business_description = descr[:500]
                    if addr:
                        builder.business_address = addr
                    if email:
                        builder.business_email = email
                    if member_since:
                        builder.business_member_since = member_since
                    if vertical:
                        builder.business_vertical = vertical
                    if lat is not None:
                        builder.business_latitude = float(lat)
                    if lng is not None:
                        builder.business_longitude = float(lng)
                    if city:
                        builder.business_city = city
                    if postal:
                        builder.business_postal_code = postal
                    if loc_name:
                        builder.business_location_name = loc_name
                    if hours_note:
                        builder.business_hours_note = hours_note
                    if time_zone:
                        builder.business_time_zone = time_zone
                    if cover_url:
                        builder.business_cover_url = cover_url
                    enriched += 1

                    # Categories (from pre-loaded map)
                    cats = cat_map.get(bp_id)
                    if cats:
                        builder.business_category = ", ".join(c for c in cats if c)

                    # Website (from pre-loaded map)
                    site = site_map.get(bp_id)
                    if site:
                        builder.business_website = site

                    # Linked accounts (from pre-loaded map)
                    for la in linked_map.get(bp_id, []):
                        if la[0] == 0 and not builder.fb_linked_name:
                            builder.fb_linked_name = la[1]
                            builder.fb_linked_likes = la[2] or 0
                        elif la[0] == 1 and not builder.ig_linked_name:
                            builder.ig_linked_name = la[1]
                            builder.ig_linked_followers = la[2] or 0

            logger.info("Enriched %d contacts from wa_biz_profiles", enriched)

        # --- Integrity Signals (trust_tier, Meta Verified) ---
        if wa_reader.table_exists("wa_biz_integrity_signals"):
            tier_count = 0
            for row in wa_reader.execute_raw(
                "SELECT jid, trust_tier, fb_linked_page_number_of_likes, "
                "ig_linked_page_number_of_followers "
                "FROM wa_biz_integrity_signals"
            ):
                sig_jid = row[0]
                trust_tier = row[1]

                # Try direct phone JID match
                builder = self._phone_jid_to_builder.get(sig_jid)
                # Try LID match (integrity_signals uses both formats)
                if not builder:
                    builder = self._lid_jid_to_builder.get(sig_jid)

                if builder and trust_tier:
                    builder.trust_tier = trust_tier
                    # Only ``TIER_1`` / ``TIER_2`` are reliable
                    # business indicators.  ``TIER_0`` /
                    # ``UNTIERED`` / ``SUSPICIOUS`` appear on
                    # ordinary personal accounts (e.g. anyone
                    # with an ``individual_spam`` integrity tag),
                    # so we never flip ``is_business`` purely
                    # from those tiers — that comes from the
                    # authoritative wa_vnames / wa_biz_profiles
                    # tables.
                    if trust_tier in ("TIER_1", "TIER_2"):
                        builder.is_business = True
                    if trust_tier == "TIER_2":
                        builder.is_meta_verified = True
                    # Use FB/IG from integrity signals as fallback
                    if row[2] and row[2] > 0 and not builder.fb_linked_likes:
                        builder.fb_linked_likes = int(row[2])
                    if row[3] and row[3] > 0 and not builder.ig_linked_followers:
                        builder.ig_linked_followers = int(row[3])
                    tier_count += 1

            logger.info(
                "Enriched %d contacts from wa_biz_integrity_signals", tier_count
            )

    def _enrich_blocked_from_msgstore(self, reader: SourceReader) -> None:
        """Populate is_blocked from message_system_block_contact in msgstore.db.

        This table records block/unblock system events. We take the most recent
        event per chat JID to determine current blocked status. This is a
        fallback for when wa_block_list (in wa.db) is unavailable.
        """
        # Skip if we already found blocked contacts from wa.db
        already_blocked = sum(1 for b in self._all_builders if b.is_blocked)
        if already_blocked > 0:
            logger.info("Already have %d blocked contacts from wa.db, skipping msgstore fallback", already_blocked)
            return

        if not reader.table_exists("message_system_block_contact"):
            logger.info("message_system_block_contact table not found - skipping block enrichment")
            return

        # Get the most recent block state per chat JID
        # message_system_block_contact has (message_row_id, is_blocked)
        # Join through message → chat → jid to get the contact JID
        # Use MAX(message_row_id) per chat as proxy for latest event (IDs are monotonic)
        try:
            rows = reader.execute_raw(
                """
                SELECT j.raw_string, msbc.is_blocked
                FROM message_system_block_contact msbc
                JOIN message m ON m._id = msbc.message_row_id
                JOIN chat c ON c._id = m.chat_row_id
                JOIN jid j ON j._id = c.jid_row_id
                WHERE msbc.message_row_id IN (
                    SELECT MAX(msbc2.message_row_id)
                    FROM message_system_block_contact msbc2
                    JOIN message m2 ON m2._id = msbc2.message_row_id
                    GROUP BY m2.chat_row_id
                )
                """
            )
        except Exception as e:
            logger.warning("Failed to read message_system_block_contact: %s", e)
            return

        block_count = 0
        for jid, is_blocked in rows:
            if not is_blocked:
                continue
            builder = self._phone_jid_to_builder.get(jid)
            if not builder and jid:
                builder = self._lid_jid_to_builder.get(jid)
            if builder:
                builder.is_blocked = True
                block_count += 1

        logger.info(
            "Marked %d contacts as blocked from message_system_block_contact (%d total events)",
            block_count, len(rows),
        )

    def _enrich_from_lid_display_names(self, reader: SourceReader) -> None:
        """Enrich LID contacts with display names from lid_display_name table."""
        if not reader.table_exists("lid_display_name"):
            logger.info("lid_display_name table not found - skipping LID name enrichment")
            return

        columns = reader.get_column_names("lid_display_name")
        has_username = "username" in columns

        if has_username:
            rows = reader.execute_raw(
                "SELECT lid_row_id, display_name, username FROM lid_display_name"
            )
        else:
            rows = reader.execute_raw(
                "SELECT lid_row_id, display_name FROM lid_display_name"
            )

        enriched = 0
        for row in rows:
            jid_row_id = row[0]
            display_name = row[1]
            username = row[2] if has_username and len(row) > 2 else None

            # Find the LID raw string for this jid_row_id
            lid_raw = self._jid_row_to_lid.get(jid_row_id)
            if not lid_raw:
                continue

            builder = self._lid_jid_to_builder.get(lid_raw)
            if not builder:
                continue

            if display_name:
                builder.lid_display_name = display_name
                # WhatsApp's masked-phone hint (e.g.
                # ``+91∙∙∙∙∙∙∙∙89`` — country code + last two
                # digits visible, the rest dotted out).
                # Identified by the literal ``∙`` (U+2219 BULLET
                # OPERATOR) WhatsApp uses for the privacy mask.
                # We carry it as a separate column so the GUI
                # can show the masked phone instead of the raw
                # ``...@lid`` for contacts whose real phone is
                # never resolved.  ``resolved_name`` keeps its
                # existing behaviour for backwards-compat.
                if "∙" in display_name or "•" in display_name:
                    builder.lid_masked_phone = display_name
            if username:
                builder.lid_username = username
            enriched += 1

        logger.info("Enriched %d LID contacts from lid_display_name", enriched)

    def _handle_number_changes(self, reader: SourceReader) -> None:
        """Handle phone number changes by merging old and new JID contacts.

        The ``message_system_number_change`` table records when a contact
        changed their phone number. We merge the old JID's contact builder
        into the new one so both numbers map to the same contact.

        Also performs LID chain resolution: if old_jid has a LID mapping
        but new_jid does not (or vice versa), propagate the mapping.
        """
        if not reader.table_exists("message_system_number_change"):
            logger.info("No number change records found")
            return

        rows = reader.execute_raw(
            "SELECT old_jid_row_id, new_jid_row_id FROM message_system_number_change"
        )

        merged = 0
        chain_resolved = 0
        for old_jid_row, new_jid_row in rows:
            old_raw = self._jid_row_to_phone.get(old_jid_row)
            new_raw = self._jid_row_to_phone.get(new_jid_row)

            if not old_raw or not new_raw:
                continue
            if old_raw == new_raw:
                continue

            # LID chain resolution: propagate LID mappings across number changes
            old_lid = None
            new_lid = None
            for lid_jid, phone_jid in self._lid_to_phone_map.items():
                if phone_jid == old_raw:
                    old_lid = lid_jid
                if phone_jid == new_raw:
                    new_lid = lid_jid

            # If old phone has a LID mapping, create the same LID→new_phone mapping
            if old_lid and not new_lid:
                # The person kept their LID but changed phone numbers
                # Both phone JIDs should be reachable from their LID
                pass  # LID already maps to old_raw; builder merge handles the rest

            old_builder = self._phone_jid_to_builder.get(old_raw)
            new_builder = self._phone_jid_to_builder.get(new_raw)

            if old_builder and new_builder and old_builder is not new_builder:
                # Merge old into new (new number takes precedence)
                new_builder.merge(old_builder)
                # Point old JID raw string to the merged builder
                self._phone_jid_to_builder[old_raw] = new_builder
                merged += 1

        logger.info("Merged %d number-change contact pairs", merged)

    def _consolidate_builders(self) -> None:
        """Consolidate LID-only builders into phone builders where mappings exist.

        After loading jid_map, some LID JIDs now have known phone JID
        counterparts. Merge LID builders into their phone builder.
        """
        merged = 0
        lid_only = 0

        for lid_raw, lid_builder in list(self._lid_jid_to_builder.items()):
            phone_raw = self._lid_to_phone_map.get(lid_raw)
            if phone_raw:
                phone_builder = self._phone_jid_to_builder.get(phone_raw)
                if phone_builder and phone_builder is not lid_builder:
                    # Merge LID data into phone builder
                    phone_builder.merge(lid_builder)
                    # Update lid_jid on the phone builder
                    if not phone_builder.lid_jid:
                        phone_builder.lid_jid = lid_raw
                    if not phone_builder.source_lid_row_id and lid_builder.source_lid_row_id:
                        phone_builder.source_lid_row_id = lid_builder.source_lid_row_id
                    # Re-point the LID builder reference
                    self._lid_jid_to_builder[lid_raw] = phone_builder
                    merged += 1
                    continue

            # This is a LID-only contact (no phone mapping)
            lid_only += 1

        # Collect unique builders for the final write
        seen_builders: set[int] = set()
        self._all_builders = []

        for builder in self._phone_jid_to_builder.values():
            builder_id = id(builder)
            if builder_id not in seen_builders:
                seen_builders.add(builder_id)
                self._all_builders.append(builder)

        for builder in self._lid_jid_to_builder.values():
            builder_id = id(builder)
            if builder_id not in seen_builders:
                seen_builders.add(builder_id)
                self._all_builders.append(builder)

        logger.info(
            "Consolidation: %d LID->phone merges, %d LID-only contacts, %d total unique builders",
            merged, lid_only, len(self._all_builders),
        )

    def _write_contacts(self, analysis_conn: AnalysisConnection) -> int:
        """Write all contact builders to the contact and jid_to_contact tables.

        Args:
            analysis_conn: Write connection to analysis.db.

        Returns:
            Number of contacts written.
        """
        contact_sql = """
            INSERT INTO contact (
                phone_jid, lid_jid, phone_number,
                display_name, wa_name, given_name, family_name, nickname,
                lid_display_name, lid_username, resolved_name,
                company, title, status_text, status_emoji,
                is_whatsapp_user, is_blocked, is_business,
                business_name, business_category, business_vertical,
                business_description, business_address,
                business_city, business_postal_code,
                business_latitude, business_longitude, business_location_name,
                business_email, business_website,
                business_hours_note, business_time_zone,
                business_member_since, business_cover_url,
                trust_tier, is_meta_verified, is_business_api_bot,
                fb_linked_name, fb_linked_likes, ig_linked_name, ig_linked_followers,
                lid_masked_phone,
                source_wa_db_id, source_jid_row_id, source_lid_row_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """

        jid_map_sql = """
            INSERT OR IGNORE INTO jid_to_contact (jid_row_id, contact_id, jid_raw_string, jid_type)
            VALUES (?,?,?,?)
        """

        contact_count = 0
        jid_mapping_count = 0

        # Write in a single transaction for atomicity and speed
        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()

            for builder in self._all_builders:
                # Insert the contact row
                cursor.execute(contact_sql, (
                    builder.phone_jid,
                    builder.lid_jid,
                    builder.phone_number,
                    builder.display_name,
                    builder.wa_name,
                    builder.given_name,
                    builder.family_name,
                    builder.nickname,
                    builder.lid_display_name,
                    builder.lid_username,
                    builder.resolved_name,
                    builder.company,
                    builder.title,
                    builder.status_text,
                    builder.status_emoji,
                    builder.is_whatsapp_user,
                    builder.is_blocked,
                    builder.is_business,
                    builder.business_name,
                    builder.business_category,
                    builder.business_vertical,
                    builder.business_description,
                    builder.business_address,
                    builder.business_city,
                    builder.business_postal_code,
                    builder.business_latitude,
                    builder.business_longitude,
                    builder.business_location_name,
                    builder.business_email,
                    builder.business_website,
                    builder.business_hours_note,
                    builder.business_time_zone,
                    builder.business_member_since,
                    builder.business_cover_url,
                    builder.trust_tier,
                    builder.is_meta_verified,
                    builder.is_business_api_bot,
                    builder.fb_linked_name,
                    builder.fb_linked_likes,
                    builder.ig_linked_name,
                    builder.ig_linked_followers,
                    builder.lid_masked_phone,
                    builder.source_wa_db_id,
                    builder.source_jid_row_id,
                    builder.source_lid_row_id,
                ))

                # Get the auto-generated contact.id
                contact_id = analysis_conn.raw_connection.last_insert_rowid()
                contact_count += 1

                # Write jid_to_contact entries for all JID row IDs
                for jid_row_id, raw_string, jid_type in builder.jid_row_ids:
                    cursor.execute(jid_map_sql, (
                        jid_row_id, contact_id, raw_string, jid_type,
                    ))
                    jid_mapping_count += 1

            analysis_conn.commit()

        except Exception:
            analysis_conn.rollback()
            raise

        logger.info(
            "Wrote %d contacts and %d JID mappings to analysis.db",
            contact_count, jid_mapping_count,
        )
        return contact_count

    def _build_jid_to_contact_for_remaining(
        self, reader: SourceReader, analysis_conn: AnalysisConnection
    ) -> int:
        """Map remaining JIDs (groups, broadcasts, devices) to contacts.

        After the main contact resolution, there are JID entries that don't
        represent contacts (groups, broadcasts, newsletters) but need
        ``jid_to_contact`` entries for efficient lookups during message
        ingestion.

        Device JIDs that weren't caught earlier are resolved to their
        parent user contact by stripping the agent/device suffixes.

        Returns:
            Number of additional jid_to_contact rows written.
        """
        # Get all jid rows that haven't been mapped yet
        all_jids = reader.execute_raw(
            "SELECT _id, raw_string, type, server, agent, device, \"user\" FROM jid"
        )

        # Get already-mapped jid_row_ids
        mapped_rows = analysis_conn.fetchall(
            "SELECT jid_row_id FROM jid_to_contact"
        )
        mapped_ids = {row[0] for row in mapped_rows}

        # Pre-load raw_string -> contact_id for fast parent lookups
        raw_contact_rows = analysis_conn.fetchall(
            "SELECT jid_raw_string, contact_id FROM jid_to_contact"
        )
        raw_to_contact: dict[str, int] = {
            r[0]: r[1] for r in raw_contact_rows if r[0] is not None
        }

        jid_map_sql = """
            INSERT OR IGNORE INTO jid_to_contact (jid_row_id, contact_id, jid_raw_string, jid_type)
            VALUES (?,?,?,?)
        """

        added = 0
        bots_added = 0
        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()

            # Cache existing bot contacts by their bot number so we don't
            # create duplicates if multiple JIDs point at the same bot.
            existing_bot_contacts: dict[str, int] = {}
            try:
                bot_rows = analysis_conn.fetchall(
                    "SELECT id, source_jid_row_id, phone_jid "
                    "FROM contact WHERE is_business_api_bot = 1"
                )
                for r in bot_rows:
                    if r[2]:
                        # Key on the JID's user-part (the numeric bot id)
                        num = str(r[2]).split("@")[0].split(":")[0]
                        if num:
                            existing_bot_contacts[num] = r[0]
            except Exception:
                pass

            for row in all_jids:
                jid_id, raw_string, jid_type, server, agent, device_num, user_part = row
                if jid_id in mapped_ids:
                    continue
                if not raw_string:
                    continue

                # WhatsApp bot JIDs (Meta AI etc. — ``server =
                # 'bot'``).  These never appear in jid_map /
                # wa_contacts / lid_display_name, so they slip
                # through the main resolver and would end up with
                # ``sender_id = NULL`` on every message they
                # send.  Create a synthetic contact so the bot is
                # named (e.g. "Meta AI") and its messages have a
                # real ``sender_id`` wired up.
                if server == "bot":
                    bot_num = (user_part or "").split(":")[0]
                    if not bot_num:
                        # Fall back to parsing the raw_string ("xxxx@bot")
                        bot_num = raw_string.split("@")[0].split(":")[0]
                    if not bot_num:
                        continue

                    contact_id = existing_bot_contacts.get(bot_num)
                    if not contact_id:
                        # Heuristic: WhatsApp's Meta AI bot uses
                        # very large numeric ids (15+ digits);
                        # bot numbers are not phone numbers.
                        # Default-label as "Meta AI" — analyst
                        # can rename if a different bot.
                        display = "Meta AI"
                        cursor.execute(
                            "INSERT INTO contact ("
                            "  phone_jid, display_name, resolved_name, "
                            "  is_whatsapp_user, is_business_api_bot, "
                            "  source_jid_row_id"
                            ") VALUES (?, ?, ?, 1, 1, ?)",
                            (raw_string, display, display, jid_id),
                        )
                        # SQLite-with-APSW: get the new row id via last_insert_rowid()
                        contact_id = analysis_conn.fetchone(
                            "SELECT last_insert_rowid()"
                        )[0]
                        existing_bot_contacts[bot_num] = contact_id
                        bots_added += 1

                    cursor.execute(
                        jid_map_sql,
                        (jid_id, contact_id, raw_string, jid_type),
                    )
                    added += 1
                    continue

                # For device JIDs, try to find the parent user contact
                is_device = (agent and agent > 0) or (device_num and device_num > 0)
                if is_device and user_part and server:
                    parent_raw = f"{user_part}@{server}"
                    # Look up parent via pre-loaded mapping
                    contact_id = raw_to_contact.get(parent_raw)
                    if contact_id:
                        cursor.execute(jid_map_sql, (jid_id, contact_id, raw_string, jid_type))
                        added += 1

            analysis_conn.commit()
        except Exception:
            analysis_conn.rollback()
            raise

        logger.info(
            "Mapped %d additional JIDs to contacts (%d new bot contacts)",
            added, bots_added,
        )
        return added


def resolve_contacts(db_manager: DatabaseManager, analysis_conn: AnalysisConnection) -> tuple[int, Optional[str], Optional[str]]:
    """Top-level function to execute contact resolution.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.

    Returns:
        Tuple of (contact_count, owner_name, owner_phone_jid).
    """
    resolver = ContactResolver(db_manager)
    count = resolver.resolve(analysis_conn)

    # Second pass: map device JIDs to parent contacts
    msgstore_reader = SourceReader(db_manager.get_msgstore())
    resolver._build_jid_to_contact_for_remaining(msgstore_reader, analysis_conn)

    return count, resolver._owner_name, resolver._owner_phone_jid
