"""
Complete DDL schema for analysis.db -- the normalized forensic analysis database.

This schema normalizes data from 15+ WhatsApp source databases (msgstore.db,
wa.db, status.db, chatsettings.db, etc.) into a single purpose-built database
optimized for forensic querying, FTS5 search, and analytical pre-computation.

Table groups:
    1.  Core Identity       -- contact, jid_to_contact
    2.  Conversations       -- conversation, group_member
    3.  Messages            -- message, message_fts
    4.  Media & Attachments -- media, location, message_link_detail
    5.  Device Tracking     -- message_device
    6.  Receipts            -- receipt, receipt_device_record
    7.  Interactions        -- reaction, mention, private_reply
    8.  Polls               -- poll, poll_option, poll_vote
    9.  Calls               -- call_record, call_participant
    10. Scheduled Events    -- scheduled_event
    11. System Events       -- system_event, number_change, group_event_detail,
                               group_past_participant
    12. Forensic Recovery   -- ghost_message, edit_history, recovered_data
    13. Pre-computed Stats  -- stats_daily_activity, stats_contact_activity,
                               stats_hourly_heatmap, stats_network_edge
    14. Case Metadata       -- case_metadata

Every table uses ``INTEGER PRIMARY KEY`` (SQLite rowid alias).  Foreign keys
reference valid parent tables.  Indexes are chosen for the query patterns used
by the forensic API endpoints.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import apsw

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema version -- bump when DDL changes require a migration.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2

# ---------------------------------------------------------------------------
# CREATE TABLE statements
# ---------------------------------------------------------------------------
# Keyed by table name in dependency order. Every column carries a comment
# explaining its purpose or origin.
# ---------------------------------------------------------------------------

TABLES: dict[str, str] = {}

# ---- 1. Core Identity -----------------------------------------------------

TABLES["contact"] = """
CREATE TABLE IF NOT EXISTS contact (
    -- Unified identity record merging wa.db contacts, phone JIDs, and LID JIDs.
    -- One row per unique human identity; both phone_jid and lid_jid may map to
    -- the same contact after LID-phone resolution.

    id                  INTEGER PRIMARY KEY,  -- SQLite rowid alias

    -- JID identifiers (at least one should be non-NULL)
    phone_jid           TEXT UNIQUE,           -- e.g. '12025551234@s.whatsapp.net'; NULL for LID-only contacts
    lid_jid             TEXT UNIQUE,           -- Linked-ID JID; NULL for legacy contacts without LID

    -- Human-readable identity fields
    phone_number        TEXT,                  -- E.164 phone number extracted from JID
    display_name        TEXT,                  -- Primary display name from wa.db wa_contacts table
    wa_name             TEXT,                  -- WhatsApp profile name (push name)
    given_name          TEXT,                  -- First name from address book sync
    family_name         TEXT,                  -- Last name from address book sync
    nickname            TEXT,                  -- User-assigned nickname in WhatsApp
    lid_display_name    TEXT,                  -- Display name resolved via LID mapping
    lid_username        TEXT,                  -- Username associated with LID identity

    -- Best available name, computed during ETL: coalesce(display_name, wa_name, given_name||family_name, phone_number, lid_display_name, phone_jid)
    resolved_name       TEXT NOT NULL,

    -- Business card / vCard fields
    company             TEXT,                  -- Organization from wa.db
    title               TEXT,                  -- Job title from wa.db

    -- WhatsApp "About" status
    status_text         TEXT,                  -- Free-text status / about string
    status_emoji        TEXT,                  -- Emoji portion of status if present

    -- Flags
    is_whatsapp_user    BOOLEAN DEFAULT 1,     -- 0 if number is not registered on WhatsApp
    is_blocked          BOOLEAN DEFAULT 0,     -- 1 if contact is in the block list
    is_business         BOOLEAN DEFAULT 0,     -- 1 if this is a WhatsApp Business account
    is_saved            BOOLEAN DEFAULT 0,     -- 1 if contact is saved in phone address book (has display_name)

    -- Business-specific fields
    business_name       TEXT,                  -- Verified business name
    business_category   TEXT,                  -- Self-reported business category
    business_vertical   TEXT,                  -- Vertical (LOCAL_SERVICE / RETAIL / etc.)
    business_description TEXT,                 -- Business profile description
    business_address    TEXT,                  -- Business address
    business_city       TEXT,                  -- Business city (wa_biz_profiles.address_city_name)
    business_postal_code TEXT,                 -- Business postal code
    business_latitude   REAL,                  -- Business location latitude
    business_longitude  REAL,                  -- Business location longitude
    business_location_name TEXT,               -- Business location name (e.g. shop name)
    business_email      TEXT,                  -- Business email
    business_website    TEXT,                  -- Business website URL
    business_hours_note TEXT,                  -- Free-text hours note ("Open daily 9-6")
    business_time_zone  TEXT,                  -- IANA timezone of the business
    business_member_since TEXT,                -- "Joined in March, 2019"
    business_cover_url  TEXT,                  -- Cover-photo URL on WhatsApp's CDN
    trust_tier          TEXT,                  -- TIER_0/TIER_1/TIER_2/UNTIERED/SUSPICIOUS from wa_biz_integrity_signals
    is_meta_verified    BOOLEAN DEFAULT 0,     -- 1 if trust_tier = TIER_2 (blue tick)
    is_business_api_bot BOOLEAN DEFAULT 0,     -- 1 if Business Cloud API (sends only 18-char key_ids)
    fb_linked_name      TEXT,                  -- Linked Facebook page name
    fb_linked_likes     INTEGER,               -- Facebook page likes count
    ig_linked_name      TEXT,                  -- Linked Instagram account name
    ig_linked_followers INTEGER,               -- Instagram followers count

    -- LID privacy hint: WhatsApp shows "+91••••••89" for contacts whose
    -- phone hasn't been resolved yet (last 2 digits visible).  Sourced
    -- from msgstore.lid_display_name keyed by source_lid_row_id.  We
    -- carry it through so the GUI can show *something* useful for the
    -- 16K+ unresolved-LID contacts a typical case has, instead of a
    -- raw "82991736999972@lid".
    lid_masked_phone    TEXT,                  -- e.g. "+91∙∙∙∙∙∙∙∙89"

    -- Profile picture (JPEG blob from Avatars directory)
    avatar_blob         BLOB,                  -- Full profile picture JPEG data (from Avatars/*.j)
    avatar_thumbnail    BLOB,                  -- Smaller thumbnail version if available

    -- Device platform heuristics (populated during analysis phase)
    platform_estimate   TEXT,                  -- 'ios', 'android', 'web', 'unknown'
    platform_confidence REAL DEFAULT 0,        -- 0.0 .. 1.0 confidence of the estimate

    -- Pre-computed aggregates (populated during stats phase)
    message_count       INTEGER DEFAULT 0,     -- Total messages sent by this contact
    conversation_count  INTEGER DEFAULT 0,     -- Number of conversations this contact appears in
    personal_msg_count  INTEGER DEFAULT 0,     -- Messages in 1-on-1 personal chats
    group_msg_count     INTEGER DEFAULT 0,     -- Messages in group chats

    -- Linked / companion devices (from msgstore user_device table)
    linked_device_count INTEGER DEFAULT 0,     -- Number of companion devices (WhatsApp Web, Desktop, iPad, etc.)

    -- Status post count (populated during status ingestion)
    status_count        INTEGER DEFAULT 0,     -- Number of status posts from this contact

    -- Lineage: IDs from source databases for traceability
    source_wa_db_id     INTEGER,               -- _id from wa.db wa_contacts table
    source_jid_row_id   INTEGER,               -- jid._id from msgstore.db jid table (phone JID row)
    source_lid_row_id   INTEGER                -- jid._id from msgstore.db jid table (LID JID row)
);
"""

TABLES["jid_to_contact"] = """
CREATE TABLE IF NOT EXISTS jid_to_contact (
    -- Maps every source jid._id (from msgstore.db) to the unified contact record.
    -- Used during ETL to resolve sender / recipient references in messages,
    -- receipts, and group memberships without repeated lookups.

    jid_row_id      INTEGER PRIMARY KEY,       -- Original jid._id from msgstore.db
    contact_id      INTEGER NOT NULL           -- Unified contact this JID belongs to
                        REFERENCES contact(id),
    jid_raw_string  TEXT,                      -- Full JID string (e.g. '12025551234@s.whatsapp.net')
    jid_type        INTEGER                    -- JID type code: 0=user, 1=group, 2=broadcast, 5=LID, etc.
);
"""

# ---- 2. Conversations -----------------------------------------------------

TABLES["conversation"] = """
CREATE TABLE IF NOT EXISTS conversation (
    -- Represents a single chat thread (personal, group, broadcast list,
    -- newsletter, status, or community).  Pre-computed aggregate columns
    -- (message_count, media_count, etc.) are populated after the message
    -- ETL pass to avoid expensive COUNT queries at API time.

    id                      INTEGER PRIMARY KEY,
    source_chat_id          INTEGER NOT NULL UNIQUE,  -- chat._id from msgstore.db
    jid_raw_string          TEXT,                     -- Raw JID string for this chat

    -- Chat classification
    chat_type               TEXT NOT NULL,             -- 'personal', 'group', 'broadcast', 'newsletter', 'status', 'community'

    -- Display information
    display_name            TEXT,                      -- Resolved name for display
    subject                 TEXT,                      -- Group subject / newsletter title
    description             TEXT,                      -- Group description text

    created_timestamp       INTEGER,                   -- Unix-ms when the group/chat was created

    -- User-facing state flags
    is_hidden               BOOLEAN DEFAULT 0,         -- hidden=1 in chat_view (not on WhatsApp home screen)
    sort_timestamp          INTEGER,                   -- WhatsApp's sort_timestamp from chat_view (home screen order)
    is_archived             BOOLEAN DEFAULT 0,
    is_pinned               BOOLEAN DEFAULT 0,
    pin_timestamp           INTEGER,                   -- When the chat was pinned (Unix-ms)
    is_muted                BOOLEAN DEFAULT 0,
    mute_end_time           INTEGER,                   -- Unix-ms when mute expires
    is_locked               BOOLEAN DEFAULT 0,         -- Chat lock (biometric) enabled

    -- Disappearing messages
    ephemeral_duration      INTEGER,                   -- Disappearing messages timer in seconds; NULL if off

    -- Group-specific
    group_type              INTEGER,                   -- WhatsApp internal group type code
    community_parent_id     INTEGER                    -- Parent community conversation; NULL if standalone
                                REFERENCES conversation(id),
    addressing_mode         TEXT,                      -- 'lid' = phone hidden (admin-only), 'pn' = phone visible; from wa_group_admin_settings

    -- Pre-computed aggregates (filled after message ETL)
    message_count           INTEGER DEFAULT 0,
    media_count             INTEGER DEFAULT 0,
    participant_count       INTEGER DEFAULT 0,
    first_message_ts        INTEGER,                   -- Timestamp of earliest message (Unix-ms)
    last_message_ts         INTEGER,                   -- Timestamp of latest message (Unix-ms)

    -- Newsletter-specific
    newsletter_subscribers  INTEGER,                   -- Subscriber count if newsletter
    newsletter_verified     BOOLEAN,                   -- Verified newsletter badge
    newsletter_handle       TEXT,                      -- Newsletter @handle

    -- Profile picture
    avatar_blob             BLOB,                      -- Group/chat icon JPEG data from Avatars directory

    -- Pre-computed last message info (populated in PRECOMPUTE stage)
    -- Eliminates 4 expensive correlated subqueries per row in the GUI
    last_msg_text           TEXT,                       -- COALESCE(text_content, type_label) of the most recent message
    last_msg_sender         TEXT,                       -- Resolved sender name of the most recent message
    last_msg_status         INTEGER,                    -- Delivery status of the most recent message
    last_msg_from_me        BOOLEAN DEFAULT 0,          -- 1 if the last message was sent by the owner
    ghost_count             INTEGER DEFAULT 0,          -- Number of ghost (deleted-for-everyone) messages in this chat
    unread_count            INTEGER DEFAULT 0,          -- Number of unread received messages (forensic: computed from status < 6)
    source_unseen_count     INTEGER,                    -- WhatsApp's own unseen_message_count from chat table (NULL if unavailable)

    -- Device-owner membership & group admin settings.  Sourced
    -- from:
    --   * ``chat.participation_status`` (msgstore.db)
    --   * ``wa_group_admin_settings.{announcement_group,
    --     restrict_mode, require_membership_approval,
    --     creator_jid, member_add_mode}`` (wa.db)

    -- participation_status — owner's role in THIS chat:
    --   NULL : channel / broadcast / (some individual chats)
    --   0    : individual chat (owner is the "user of this account")
    --   1    : owner NO LONGER a member of this group/community
    --   2    : owner IS a member (regular, not admin)
    --   3    : owner IS a member promoted to admin
    --   4    : owner IS a member AND creator/admin of the group
    participation_status    INTEGER,

    -- Group admin settings (only meaningful for chat_type='group'/'community')
    announcement_group      INTEGER,   -- 1 = only admins can send, 0 = anyone can send
    restrict_mode           INTEGER,   -- 1 = only admins can edit subject/icon/desc, 0 = anyone
    require_membership_approval INTEGER, -- 1 = admin must approve new joiners via link
    member_add_mode         INTEGER,    -- 0 = anyone can add members, 1 = admins only
    creator_jid_raw         TEXT        -- Original creator's raw JID/LID (from wa.db)
);
"""

TABLES["group_member"] = """
CREATE TABLE IF NOT EXISTS group_member (
    -- Tracks current and historical membership of group conversations.
    -- A contact may appear multiple times for the same group if they left
    -- and re-joined (distinguished by join_timestamp).

    id                  INTEGER PRIMARY KEY,
    conversation_id     INTEGER NOT NULL           -- Group conversation this membership belongs to
                            REFERENCES conversation(id),
    contact_id          INTEGER NOT NULL           -- The member contact
                            REFERENCES contact(id),
    role                TEXT NOT NULL DEFAULT 'member',  -- 'member', 'admin', or 'superadmin'
    label               TEXT,                      -- Per-group nickname / label set by admin

    -- Temporal bounds
    join_timestamp      INTEGER,                   -- Unix-ms when this member joined
    join_method         INTEGER,                   -- WhatsApp join method code (invite link, admin add, etc.)
    is_current          BOOLEAN DEFAULT 1,         -- 1 if still in the group, 0 if departed
    left_timestamp      INTEGER,                   -- Unix-ms when member left or was removed; NULL if current
    left_reason         TEXT,                      -- 'left', 'removed', 'removed_by_admin', etc.

    UNIQUE(conversation_id, contact_id, join_timestamp)
);
"""

# ---- 3. Messages -----------------------------------------------------------

TABLES["message"] = """
CREATE TABLE IF NOT EXISTS message (
    -- Core message table.  Every WhatsApp message (text, media, system, call,
    -- poll, etc.) gets exactly one row.  The type_label column provides a
    -- human-readable classification derived from message_type.

    id                      INTEGER PRIMARY KEY,
    source_msg_id           INTEGER NOT NULL UNIQUE,   -- message._id from msgstore.db
    conversation_id         INTEGER NOT NULL            -- Conversation this message belongs to
                                REFERENCES conversation(id),
    sender_id               INTEGER                    -- Contact who sent this message; NULL for system messages
                                REFERENCES contact(id),
    from_me                 BOOLEAN NOT NULL,           -- 1 if sent by device owner, 0 if received

    -- Timestamps
    timestamp               INTEGER NOT NULL,           -- Message timestamp (Unix-ms)
    received_timestamp      INTEGER,                    -- When message was received on device (Unix-ms); NULL for sent messages
    receipt_server_timestamp INTEGER,                   -- Server receipt timestamp (Unix-ms)
    sort_id                 INTEGER,                    -- Sort ordering key from msgstore sort_id column

    -- Type classification
    message_type            INTEGER NOT NULL,           -- Raw WhatsApp message type code
    type_label              TEXT NOT NULL,              -- Human-readable label: 'text', 'image', 'video', 'audio', 'document', 'sticker', 'gif', 'contact_card', 'location', 'live_location', 'poll', 'system', 'call', 'e2e_notification', 'group_notification', etc.

    -- Content
    text_content            TEXT,                       -- Message body text (NULL for pure media)

    -- Delivery status
    status                  INTEGER,                    -- WhatsApp status code: 0=received, 4=sent from server, 5=delivered, 13=read, etc.

    -- Flags
    is_starred              BOOLEAN DEFAULT 0,          -- 1 if user starred this message
    is_forwarded            BOOLEAN DEFAULT 0,          -- 1 if message was forwarded
    forward_score           INTEGER,                    -- Forward hop count; NULL if not forwarded
    is_ephemeral            BOOLEAN DEFAULT 0,          -- 1 if this was a disappearing message
    ephemeral_duration      INTEGER,                    -- Timer duration in seconds if ephemeral

    -- Revocation (deletion for everyone)
    is_revoked              BOOLEAN DEFAULT 0,          -- 1 if message was deleted for everyone
    revoke_timestamp        INTEGER,                    -- Unix-ms when revocation occurred
    revoked_key_id          TEXT,                       -- key_id of the admin revoke protocol message
    revoked_by_admin_id     INTEGER                    -- Admin who revoked (group context); NULL if self
                                REFERENCES contact(id),

    -- Edit tracking
    is_edited               BOOLEAN DEFAULT 0,          -- 1 if message has been edited at least once
    edit_count              INTEGER DEFAULT 0,          -- Number of times this message was edited
    original_key_id         TEXT,                       -- key_id of the original pre-edit message
    last_edit_timestamp     INTEGER,                    -- Unix-ms of the most recent edit

    -- Reply context
    reply_to_msg_id         INTEGER                    -- message.id of the quoted parent message; NULL if not a reply
                                REFERENCES message(id),
    reply_to_key_id         TEXT,                       -- key_id of the quoted message (for cross-DB matching)
    quoted_text             TEXT,                       -- Snapshot of the quoted message text at reply time
    quoted_type             INTEGER,                    -- Message type of the quoted message

    -- View-once media
    is_view_once            BOOLEAN DEFAULT 0,          -- 1 if this is a view-once photo/video
    view_once_state         INTEGER,                    -- View-once lifecycle: 0=not opened, 1=opened, 2=replayed

    -- AI / bot
    is_bot_message          BOOLEAN DEFAULT 0,          -- 1 if sent by a WhatsApp AI bot
    bot_model_type          INTEGER,                    -- Bot model type code if applicable

    -- Private reply (DM reply to group message)
    is_private_reply        BOOLEAN DEFAULT 0,          -- 1 if this is a private reply
    private_reply_source_chat_id INTEGER,               -- Original group chat._id that the private reply references

    -- Status reply (reply to a WhatsApp Status post)
    is_status_reply         BOOLEAN DEFAULT 0,          -- 1 if this message replies to a Status post

    -- Broadcast metadata
    broadcast               BOOLEAN DEFAULT 0,          -- 1 if sent via broadcast list
    recipient_count         INTEGER,                    -- Number of recipients for broadcast messages

    -- Multi-device tracking
    origin                  INTEGER DEFAULT 0,           -- Which of the owner's devices sent this outgoing msg (0=primary phone, >0=companion)
    origination_flags       INTEGER DEFAULT 0,           -- Bitmask: 512=multi-device sync, 1=forwarded, 256=broadcast, etc.

    -- Source traceability
    source_key_id           TEXT NOT NULL,              -- key_remote_jid + key_id from msgstore (globally unique message identifier)
    sender_jid_row_id       INTEGER,                   -- Raw sender_jid_row_id from msgstore message table (FK to jid._id)
    source_chat_row_id      INTEGER,                   -- Raw chat_row_id from msgstore message table (FK to chat._id)

    -- Pre-computed display fields (populated during PRECOMPUTE stage)
    rendered_sender         TEXT,                       -- Pre-resolved sender display name (e.g. "Name (+phone)" or "~wa_name (+phone)")
    rendered_system_text    TEXT                        -- Pre-computed system event display text (only for message_type=7)
);
"""

# ---- 4. Media & Attachments -----------------------------------------------

TABLES["media"] = """
CREATE TABLE IF NOT EXISTS media (
    -- Detailed metadata for media attachments.  One row per message that
    -- contains a file (image, video, audio, document, sticker, GIF).

    id                      INTEGER PRIMARY KEY,
    message_id              INTEGER NOT NULL UNIQUE     -- Parent message
                                REFERENCES message(id),

    -- File system
    file_path               TEXT,                       -- Original relative path from message_media table
    resolved_file_path      TEXT,                       -- Absolute path after extraction directory resolution
    file_exists             BOOLEAN DEFAULT 0,          -- 1 if the file was found on disk during analysis
    file_size               INTEGER,                    -- File size in bytes
    mime_type               TEXT,                       -- MIME type (e.g. 'image/jpeg', 'video/mp4')

    -- Dimensions and duration
    width                   INTEGER,                    -- Pixel width (images/videos)
    height                  INTEGER,                    -- Pixel height (images/videos)
    duration_ms             INTEGER,                    -- Duration in milliseconds (audio/video)

    -- Thumbnail
    thumbnail_blob          BLOB,                       -- Raw thumbnail bytes stored in msgstore

    -- Download / encryption metadata
    media_url               TEXT,                       -- WhatsApp CDN URL used for download
    direct_path             TEXT,                       -- CDN direct path
    file_hash               TEXT,                       -- SHA-256 of the decrypted file
    enc_file_hash           TEXT,                       -- SHA-256 of the encrypted file on CDN
    media_key               BLOB,                       -- AES-256 decryption key for the media file

    -- Content metadata
    media_caption           TEXT,                       -- Caption text sent with the media
    media_name              TEXT,                       -- Original filename (documents)
    is_animated_sticker     BOOLEAN DEFAULT 0,          -- 1 if this is a WebP animated sticker
    page_count              INTEGER,                    -- Number of pages (PDF documents)

    -- Enrichment (populated by optional analysis passes)
    transcription_text      TEXT,                       -- Speech-to-text transcription for audio/video
    ocr_text                TEXT,                       -- OCR-extracted text from images

    -- View-once tracking
    first_viewed_ts         INTEGER,                    -- Unix-ms when view-once media was first opened

    -- Accessibility
    accessibility_label     TEXT,                       -- Auto-generated alt-text from WhatsApp

    -- CDN URL expiry (parsed from oe= parameter at ingestion time, UTC Unix seconds)
    cdn_expiry_ts           INTEGER,                    -- 0 or NULL = no oe= found; >0 = UTC expiry timestamp

    -- Computed media status (set during ingestion, updated on download/hash-link)
    media_status            TEXT DEFAULT 'missing',     -- 'on_disk', 'downloadable', 'expired', 'thumb_only', 'missing', 'no_key'
                                                        -- on_disk: file found at resolved_file_path
                                                        -- downloadable: media_url + media_key + URL not expired
                                                        -- expired: has URL + key but oe= timestamp passed
                                                        -- no_key: has URL but media_key is NULL/empty
                                                        -- thumb_only: thumbnail exists but no file or URL
                                                        -- missing: no file, no URL, no thumbnail

    -- Recovery tracking (populated when media is downloaded/decrypted by the tool)
    recovery_method         TEXT,                       -- NULL=originally received in this chat AND still on disk
                                                        -- 'downloaded'=tool downloaded from CDN post-extraction
                                                        -- 'hash_linked'=NEVER received here; same SHA-256 came from another message
                                                        -- 'hash_linked_after_delete'=originally received here but local file deleted; same SHA-256 still exists in another message
                                                        -- 'orphan_recovered'=chat record was lost (cleared chat / reinstall / autoclean), but the file with this SHA-256 was still in the WhatsApp media folder as an "orphaned" file; resolved_file_path now points at that orphan. The bytes ARE the original.
    recovery_timestamp      INTEGER,                    -- Unix-ms when media was recovered

    -- Was the file originally transferred to the device for THIS message?
    -- 1 = msgstore.message_media.transferred was 1 (user actually downloaded
    --     the bytes via this message at the time of receipt)
    -- 0 = msgstore had transferred=0 (WhatsApp dedup quirk: file_path may
    --     still be set if another message had the same SHA-256, but the
    --     user never downloaded for this specific message)
    -- NULL = older WhatsApp schema where transferred column didn't exist
    was_transferred         INTEGER,

    -- WhatsApp dual-quality send: when the user enables HD upload, msgstore
    -- creates TWO `message` rows (same chat, same timestamp, same content)
    -- linked via `message_association.association_type = 7`:
    --   * parent = SD primary  (media_transcode_quality=3, what users see)
    --   * child  = HD twin     (media_transcode_quality=4, has 0x4000000
    --                            bit set on origination_flags)
    -- Reactions / replies / quotes / edits all attach to the parent (SD).
    -- We hide the HD twin from the chat list to avoid showing duplicate
    -- video bubbles, but keep the row intact for forensic value
    -- (separate hash, separate transferred flag, separate file path).
    is_hd_twin              INTEGER DEFAULT 0,          -- 1 = this row IS the HD twin (or motion-video, status-link, poll-image-option child) → hide in chat list
    hd_twin_msg_id          INTEGER,                    -- on PARENT (SD) row: analysis.message.id of the HD twin

    -- Motion-photo pair: WhatsApp sends Android Motion Photos as TWO rows
    -- linked via msgstore.message_association.association_type = 11:
    --   * parent = still image (mtype=1, duration=0)
    --   * child  = 1-2 second video clip (mtype=3, the "live" component)
    -- We hide the video child (it's not a separate message) and store
    -- its analysis msg_id here so the chat renderer can show a "▶ Live"
    -- badge on the still bubble that plays the motion clip on click.
    motion_video_msg_id     INTEGER,                    -- on PARENT (still) row: analysis.message.id of the motion-video twin

    -- Generic association-child back-pointer: when this row is the
    -- CHILD of a message_association entry (any type — HD twin / motion
    -- video / status link / poll image option), this holds the parent's
    -- analysis msg_id.  Lets us enumerate all children of a given
    -- parent without re-reading msgstore — used e.g. by the poll
    -- renderer to find each option's attached image (channel polls
    -- with image options use association_type=6).
    assoc_parent_msg_id     INTEGER,

    -- Which association_type this child belongs to.  Set alongside
    -- ``is_hd_twin = 1`` by ``_link_hd_twins_pass`` and used by the
    -- chat-list WHERE clause to decide which children to hide vs
    -- render as separate bubbles:
    --   * 'hd'      — types 7 (video) + 12 (image): the HD half of
    --                 a dual-quality pair; render as its own
    --                 forensic-correct bubble next to the SD parent.
    --   * 'motion'  — type 11: the 1-2 s motion clip of an Android
    --                 Motion Photo; scaffolding, hide.
    --   * 'status'  — type 4: status post link-preview metadata;
    --                 scaffolding, hide.
    --   * 'poll'    — type 6: channel poll image option;
    --                 scaffolding, hide.
    --   * NULL      — not an association child (regular message).
    -- Splitting this out as its own column lets the WHERE clause
    -- be a cheap indexed lookup instead of a correlated subquery
    -- against ``hd_twin_msg_id``.
    assoc_kind              TEXT,

    -- Source traceability (for forensic cross-referencing with msgstore.db)
    source_media_row_id     INTEGER                     -- message_media._id from msgstore.db (for cross-referencing)
);
"""

TABLES["location"] = """
CREATE TABLE IF NOT EXISTS location (
    -- GPS coordinates sent as location or live-location messages.

    id                  INTEGER PRIMARY KEY,
    message_id          INTEGER NOT NULL UNIQUE         -- Parent message
                            REFERENCES message(id),
    latitude            REAL NOT NULL,                  -- WGS-84 latitude
    longitude           REAL NOT NULL,                  -- WGS-84 longitude
    place_name          TEXT,                           -- Venue / place name if provided
    place_address       TEXT,                           -- Street address if provided
    is_live             BOOLEAN DEFAULT 0,              -- 1 if this is a live location share
    live_duration       INTEGER,                        -- Duration of live sharing in seconds
    final_latitude      REAL,                           -- End position latitude (live shares only)
    final_longitude     REAL,                           -- End position longitude (live shares only)
    final_timestamp     INTEGER,                        -- Unix-ms when live share ended
    map_preview_url     TEXT,                           -- URL to a static map preview image
    thumbnail_blob      BLOB                            -- Map preview JPEG from message_thumbnail table
);
"""

TABLES["location_point"] = """
CREATE TABLE IF NOT EXISTS location_point (
    -- Intermediate GPS coordinates from location.db's location_cache table.
    -- These represent the real-time route/trajectory during a live location
    -- sharing session. Multiple points per live location share.
    --
    -- Source: location.db → location_cache (data often
    -- recoverable via the WAL file).

    id                  INTEGER PRIMARY KEY,
    location_id         INTEGER REFERENCES location(id),    -- FK to the live location message record
    contact_id          INTEGER REFERENCES contact(id),     -- Contact who shared (resolved from LID jid)
    latitude            REAL NOT NULL,                       -- WGS-84 latitude
    longitude           REAL NOT NULL,                       -- WGS-84 longitude
    accuracy            REAL,                                -- GPS accuracy in meters
    speed               REAL,                                -- Speed at this point (m/s, may be -1 if unavailable)
    bearing             REAL,                                -- Heading in degrees (may be -1 if unavailable)
    timestamp           INTEGER NOT NULL,                    -- location_ts — Unix-ms UTC
    source_row_id       INTEGER                              -- _id from location_cache table
);
"""

TABLES["location_sharer"] = """
CREATE TABLE IF NOT EXISTS location_sharer (
    -- Records of contacts who have shared (or are sharing)
    -- live location with this device.  Source:
    -- ``location.db.location_sharer``.

    id                  INTEGER PRIMARY KEY,
    contact_id          INTEGER REFERENCES contact(id),     -- Resolved contact
    raw_jid             TEXT,                                -- Raw JID string from source
    timeout_duration    INTEGER,                             -- Duration in seconds
    creation_timestamp  INTEGER                              -- When sharing started (Unix-ms)
);
"""

TABLES["message_link_detail"] = """
CREATE TABLE IF NOT EXISTS message_link_detail (
    -- URL link previews extracted from messages.  A single message may contain
    -- multiple links, each stored as a separate row.

    id              INTEGER PRIMARY KEY,
    message_id      INTEGER NOT NULL                   -- Parent message
                        REFERENCES message(id),
    url             TEXT NOT NULL,                      -- Full URL
    page_title      TEXT,                              -- <title> from link preview
    description     TEXT,                              -- Meta description from link preview
    domain          TEXT,                              -- Extracted domain (e.g. 'youtube.com')
    link_index      INTEGER,                           -- 0-based position of this link within the message text
    thumbnail_blob  BLOB                               -- og:image preview thumbnail JPEG bytes
                                                        -- (source: msgstore.message_thumbnail.thumbnail)
);
"""

# ---- 4b. Albums (multi-photo / multi-video posts) -------------------------
# WhatsApp albums = a message_type=99 parent + N child media messages joined
# via message_association.association_type=2.  msgstore.db stores them across
# message_album (counts) and message_association (graph).  We mirror those
# tables so the renderer can collapse a 28-photo album into a single grid
# card instead of 28 separate tiles.
#
# Forensic value of expected_image_count vs image_count: when expected > actual
# WhatsApp itself didn't receive every child of the album (network drop, sender
# revoked some, or extraction missed them).  We record the gap so the analyst
# sees "expected 7 photos, only 5 present, 2 missing" instead of silently
# under-counting.

TABLES["message_album"] = """
CREATE TABLE IF NOT EXISTS message_album (
    -- One row per album-parent message (msgstore.message_type=99).

    message_id              INTEGER PRIMARY KEY            -- Parent message
                                REFERENCES message(id) ON DELETE CASCADE,
    image_count             INTEGER NOT NULL DEFAULT 0,    -- Children with type=1 actually present
    video_count             INTEGER NOT NULL DEFAULT 0,    -- Children with type=3 actually present
    expected_image_count    INTEGER,                       -- What WhatsApp expected (NULL on older clients)
    expected_video_count    INTEGER,                       -- What WhatsApp expected (NULL on older clients)
    missing_image_count     INTEGER NOT NULL DEFAULT 0,    -- expected_image_count - image_count (when known)
    missing_video_count     INTEGER NOT NULL DEFAULT 0,    -- expected_video_count - video_count (when known)
    actual_child_count      INTEGER NOT NULL DEFAULT 0,    -- Actual children rows inserted from message_association
    note                    TEXT                           -- Human note: "expected 7, only 5 present (2 missing)"
);
"""

TABLES["message_association"] = """
CREATE TABLE IF NOT EXISTS message_association (
    -- Generic parent-child link between messages.  For albums we use
    -- association_type=2.  msgstore.db also uses other type values
    -- (4, 6, 7, 11, 12) for replies / status threads / etc., which we
    -- mirror as-is for forensic completeness even though the renderer
    -- currently only consumes type=2.

    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_message_id       INTEGER NOT NULL                -- Parent (album container or thread root)
                                REFERENCES message(id) ON DELETE CASCADE,
    child_message_id        INTEGER NOT NULL                -- Child (one photo / video / reply)
                                REFERENCES message(id) ON DELETE CASCADE,
    association_type        INTEGER NOT NULL,               -- 2=album member, 4/6/7/11/12=other relationships
    sort_order              INTEGER,                         -- Render order within the parent (ascending child source_msg_id)
    UNIQUE(parent_message_id, child_message_id, association_type)
);
"""

# ---- 5. Device Tracking ---------------------------------------------------

TABLES["message_device"] = """
CREATE TABLE IF NOT EXISTS message_device (
    -- Records which device (phone, linked desktop, etc.) sent a message.
    -- Useful for multi-device forensic analysis.

    id                  INTEGER PRIMARY KEY,
    message_id          INTEGER NOT NULL UNIQUE         -- Parent message
                            REFERENCES message(id),
    device_jid_row_id   INTEGER,                       -- jid._id of the device JID
    device_agent        INTEGER,                       -- Agent byte from the message key
    device_number       INTEGER,                       -- Device registration number (0 = primary phone)
    is_primary          BOOLEAN,                       -- 1 if sent from the primary phone device
    platform_label      TEXT,                          -- key_id classified: 'android', 'iphone', 'companion', etc.
    platform_confidence REAL DEFAULT 0                 -- Classification confidence 0.0-1.0
);
"""

# ---- 6. Receipts ----------------------------------------------------------

TABLES["receipt"] = """
CREATE TABLE IF NOT EXISTS receipt (
    -- Per-recipient delivery / read / played timestamps for outgoing messages.
    -- Critical for proving message delivery in forensic timelines.

    id              INTEGER PRIMARY KEY,
    message_id      INTEGER NOT NULL                   -- The sent message
                        REFERENCES message(id),
    recipient_id    INTEGER NOT NULL                   -- Contact who received the message
                        REFERENCES contact(id),
    delivered_ts    INTEGER,                           -- Unix-ms when message was delivered to recipient
    read_ts         INTEGER,                           -- Unix-ms when recipient read the message
    played_ts       INTEGER,                           -- Unix-ms when recipient played voice/video (if applicable)
    delivery_delay_ms INTEGER,                         -- Computed: delivered_ts - message.timestamp
    read_delay_ms   INTEGER                            -- Computed: read_ts - delivered_ts
);
"""

TABLES["receipt_device_record"] = """
CREATE TABLE IF NOT EXISTS receipt_device_record (
    -- Device-level receipt records.  In multi-device setups each linked device
    -- sends its own receipt, which WhatsApp stores in receipt_device.

    id                      INTEGER PRIMARY KEY,
    message_id              INTEGER NOT NULL            -- The sent message
                                REFERENCES message(id),
    device_jid_row_id       INTEGER NOT NULL,           -- jid._id of the receiving device
    device_contact_id       INTEGER                    -- Resolved contact for the device owner
                                REFERENCES contact(id),
    receipt_ts              INTEGER,                    -- Unix-ms when this device acknowledged receipt
    primary_device_version  INTEGER                    -- Protocol version reported by the primary device
);
"""

# ---- 7. Interactions -------------------------------------------------------

TABLES["reaction"] = """
CREATE TABLE IF NOT EXISTS reaction (
    -- Emoji reactions on messages.  Each (message, reactor) pair is unique;
    -- changing a reaction UPSERTs rather than inserting a new row.

    id              INTEGER PRIMARY KEY,
    message_id      INTEGER NOT NULL                   -- Message that was reacted to
                        REFERENCES message(id),
    conversation_id INTEGER NOT NULL                   -- Conversation context (for efficient per-chat queries)
                        REFERENCES conversation(id),
    reactor_id      INTEGER                            -- Contact who reacted; NULL if unresolved
                        REFERENCES contact(id),
    from_me         BOOLEAN NOT NULL,                  -- 1 if the device owner placed this reaction
    emoji           TEXT NOT NULL,                      -- The emoji character(s) used as the reaction
    timestamp       INTEGER,                           -- Unix-ms when the reaction was placed

    UNIQUE(message_id, reactor_id)
);
"""

TABLES["mention"] = """
CREATE TABLE IF NOT EXISTS mention (
    -- @mentions within message text.  Includes individual mentions, group
    -- mentions, and @all / @everyone broadcasts.

    id              INTEGER PRIMARY KEY,
    message_id      INTEGER NOT NULL                   -- Message containing the mention
                        REFERENCES message(id),
    mentioned_id    INTEGER                            -- Contact being mentioned; NULL for @all
                        REFERENCES contact(id),
    display_name    TEXT,                              -- Display string used in the mention (may differ from contact name)
    mention_type    INTEGER                            -- 0 = regular @mention, 1 = group mention, 2 = @all / @everyone
);
"""

TABLES["private_reply"] = """
CREATE TABLE IF NOT EXISTS private_reply (
    -- Tracks private replies: direct messages sent in response to a group
    -- message.  Links the DM back to the original group conversation.

    id                          INTEGER PRIMARY KEY,
    message_id                  INTEGER NOT NULL UNIQUE -- The private-reply DM message
                                    REFERENCES message(id),
    source_conversation_id      INTEGER                -- Original group conversation
                                    REFERENCES conversation(id),
    source_message_key_id       TEXT,                  -- key_id of the group message being replied to
    quoted_text                 TEXT                   -- Snapshot of the quoted group message text
);
"""

# ---- 8. Polls --------------------------------------------------------------

TABLES["poll"] = """
CREATE TABLE IF NOT EXISTS poll (
    -- Poll metadata.  One row per poll message.

    id                  INTEGER PRIMARY KEY,
    message_id          INTEGER NOT NULL UNIQUE         -- Parent poll message
                            REFERENCES message(id),
    selectable_count    INTEGER,                       -- Max number of options a voter may select (0 = unlimited)
    poll_type           INTEGER                        -- WhatsApp poll type code
);
"""

TABLES["poll_option"] = """
CREATE TABLE IF NOT EXISTS poll_option (
    -- Individual answer options within a poll.

    id              INTEGER PRIMARY KEY,
    poll_id         INTEGER NOT NULL                   -- Parent poll
                        REFERENCES poll(id),
    option_name     TEXT NOT NULL,                      -- Display text of the option
    option_hash     TEXT,                              -- SHA-256 hash used by WhatsApp to identify this option
    vote_total      INTEGER DEFAULT 0,                 -- Pre-computed total votes for this option
    option_index    INTEGER,                           -- 0-based display order
    voter_names     TEXT                               -- Pre-computed comma-separated voter display names (ingested)
);
"""

TABLES["poll_vote"] = """
CREATE TABLE IF NOT EXISTS poll_vote (
    -- Individual votes cast in polls.  One row per (poll, voter) combination.
    -- The specific option(s) chosen are tracked via a junction with poll_option
    -- in the ETL layer; this table records the vote event.

    id              INTEGER PRIMARY KEY,
    poll_id         INTEGER NOT NULL                   -- Parent poll
                        REFERENCES poll(id),
    voter_id        INTEGER                            -- Contact who voted
                        REFERENCES contact(id),
    from_me         BOOLEAN NOT NULL,                  -- 1 if the device owner cast this vote
    timestamp       INTEGER                            -- Unix-ms when the vote was recorded
);
"""

TABLES["poll_vote_option"] = """
CREATE TABLE IF NOT EXISTS poll_vote_option (
    -- Junction table: which option(s) each voter selected.
    -- One row per (vote, option) pair — supports multi-select polls.

    id              INTEGER PRIMARY KEY,
    poll_vote_id    INTEGER NOT NULL                   -- Parent vote
                        REFERENCES poll_vote(id),
    poll_option_id  INTEGER NOT NULL                   -- Selected option
                        REFERENCES poll_option(id)
);
"""

# ---- 8b. vCard Data --------------------------------------------------------

TABLES["message_vcard_data"] = """
CREATE TABLE IF NOT EXISTS message_vcard_data (
    -- Parsed vCard contact data from shared contact messages.

    id              INTEGER PRIMARY KEY,
    message_id      INTEGER NOT NULL                   -- Parent message
                        REFERENCES message(id),
    display_name    TEXT,                               -- FN field from vCard
    phone_numbers   TEXT,                               -- Comma-separated phone numbers from TEL fields
    vcard_index     INTEGER DEFAULT 0                  -- 0-based index when message has multiple vCards
);
"""

# ---- 8c. Pinned Messages ---------------------------------------------------

TABLES["message_pin"] = """
CREATE TABLE IF NOT EXISTS message_pin (
    -- Pinned messages in a conversation, extracted from message_add_on (type 79).

    id                  INTEGER PRIMARY KEY,
    message_id          INTEGER NOT NULL                   -- The message that was pinned
                            REFERENCES message(id),
    conversation_id     INTEGER NOT NULL                   -- Conversation containing the pin
                            REFERENCES conversation(id),
    pinner_id           INTEGER                            -- Contact who pinned the message
                            REFERENCES contact(id),
    pin_timestamp       INTEGER,                           -- Unix-ms when the message was pinned
    expiry_duration     INTEGER,                           -- Duration in seconds before pin expires
    pin_state           INTEGER DEFAULT 1                  -- 1=pinned, 0=unpinned
);
"""

# ---- 8b. Comment Threads (Channel replies) ---------------------------------

TABLES["message_comment"] = """
CREATE TABLE IF NOT EXISTS message_comment (
    -- Comment threads on channel/announcement messages (WhatsApp Channels).
    -- Extracted from msgstore.db message_comment table.

    id                  INTEGER PRIMARY KEY,
    parent_message_id   INTEGER NOT NULL                   -- The parent message being replied to
                            REFERENCES message(id),
    reply_message_id    INTEGER NOT NULL                   -- The reply/comment message
                            REFERENCES message(id),
    conversation_id     INTEGER NOT NULL                   -- Conversation containing the thread
                            REFERENCES conversation(id)
);
CREATE INDEX IF NOT EXISTS idx_comment_parent ON message_comment(parent_message_id);
"""

# ---- 9. Calls --------------------------------------------------------------

TABLES["call_record"] = """
CREATE TABLE IF NOT EXISTS call_record (
    -- Voice and video call records extracted from msgstore.db call_log table.

    id                      INTEGER PRIMARY KEY,
    source_call_id          INTEGER NOT NULL UNIQUE,    -- call_log._id from msgstore.db
    contact_id              INTEGER                    -- Primary contact (1-on-1 calls)
                                REFERENCES contact(id),
    conversation_id         INTEGER                    -- Chat context (for group calls, the group conversation)
                                REFERENCES conversation(id),
    from_me                 BOOLEAN NOT NULL,           -- 1 if outgoing call, 0 if incoming
    timestamp               INTEGER NOT NULL,           -- Unix-ms when the call started
    is_video                BOOLEAN DEFAULT 0,          -- 1 if video call, 0 if voice
    duration_sec            INTEGER,                    -- Call duration in seconds; NULL if missed/failed
    call_result             INTEGER,                    -- WhatsApp result code: 0=success, 1=missed, 2=rejected, etc.
    result_label            TEXT,                       -- Human-readable result: 'answered', 'missed', 'rejected', 'unavailable', 'group_call', etc.
    bytes_transferred       INTEGER,                    -- Total bytes transferred during call
    call_id_text            TEXT,                       -- Unique call identifier string from WhatsApp

    -- Group call metadata
    is_group_call           BOOLEAN DEFAULT 0,          -- 1 if this was a group call
    group_conversation_id   INTEGER                    -- Group conversation for group calls
                                REFERENCES conversation(id),

    -- Voice chat detection (populated from call_log source fields)
    call_type               INTEGER,                    -- WhatsApp call_type: 0=normal, 2=voice_chat, 3=voice_chat_community
    offer_silence_reason    INTEGER,                    -- 0=normal ring, 6=voice_chat (silent), 8=voice_chat_muted, 100=DND
    call_category           TEXT,                       -- Derived: 'voice_chat', 'group_call', 'multi_person', 'personal'

    -- Call creator / initiator (who started or modified the call)
    creator_jid             TEXT,                       -- Full device JID of call creator (e.g. '15551234567.0:0@s.whatsapp.net')
    creator_contact_id      INTEGER                    -- Resolved contact who created/modified the call
                                REFERENCES contact(id),
    creator_device_type     TEXT,                       -- 'primary_phone' or 'companion_N' (parsed from JID device field)

    -- Participant who was unknown to the device owner (from call_unknown_caller table)
    is_unknown_caller       BOOLEAN DEFAULT 0           -- 1 if caller was not in contacts at call time

);
"""

TABLES["call_participant"] = """
CREATE TABLE IF NOT EXISTS call_participant (
    -- Participants in group calls.  For 1-on-1 calls the single participant
    -- is already captured in call_record.contact_id.

    id              INTEGER PRIMARY KEY,
    call_id         INTEGER NOT NULL                   -- Parent call record
                        REFERENCES call_record(id),
    contact_id      INTEGER                            -- Participating contact
                        REFERENCES contact(id),
    call_result     INTEGER                            -- Per-participant result code (joined, missed, declined)
);
"""

TABLES["scheduled_event"] = """
CREATE TABLE IF NOT EXISTS scheduled_event (
    -- Scheduled calls and calendar events extracted from msgstore message_event table.
    -- Linked 1:1 to the message record that announced the event.

    id                      INTEGER PRIMARY KEY,
    source_msg_row_id       INTEGER NOT NULL UNIQUE,   -- message_event.message_row_id (= message._id)
    message_id              INTEGER                    -- Resolved message.id in analysis.db
                                REFERENCES message(id),
    conversation_id         INTEGER                    -- Conversation where event was announced
                                REFERENCES conversation(id),

    -- Event metadata
    name                    TEXT NOT NULL,             -- Event/call name
    description             TEXT,                      -- Optional description

    -- Schedule
    start_time              INTEGER,                   -- Unix-ms start time
    end_time                INTEGER,                   -- Unix-ms end time

    -- Location (NULL for call-only events)
    location_latitude       REAL,
    location_longitude      REAL,
    location_name           TEXT,
    location_address        TEXT,

    -- Call details
    join_link               TEXT,                      -- WhatsApp video call join URL
    is_schedule_call        INTEGER DEFAULT 0,         -- 1 if this is a scheduled call
    is_canceled             INTEGER DEFAULT 0,         -- 1 if event was canceled
    event_state             INTEGER DEFAULT 0,         -- WhatsApp internal event state code
    allow_extra_guests      INTEGER DEFAULT 0,         -- Whether guests can invite others

    -- Reminders
    has_reminder            INTEGER,                   -- 1 if a reminder is set
    reminder_offset_sec     INTEGER,                   -- Seconds before event for reminder
    show_upcoming_banner    INTEGER,                   -- Whether to show upcoming banner

    -- From parent message
    timestamp               INTEGER                    -- Unix-ms from message.timestamp (when event was created)
);
"""

# ---- 10. System Events -----------------------------------------------------

TABLES["system_event"] = """
CREATE TABLE IF NOT EXISTS system_event (
    -- Parsed system messages: group changes, encryption notifications,
    -- number changes, admin actions, etc.  The raw event_type code is
    -- preserved alongside a human-readable event_label.

    id              INTEGER PRIMARY KEY,
    message_id      INTEGER NOT NULL UNIQUE             -- Source system message
                        REFERENCES message(id),
    conversation_id INTEGER NOT NULL                    -- Conversation where the event occurred
                        REFERENCES conversation(id),
    event_type      INTEGER NOT NULL,                   -- WhatsApp system message action type
    event_label     TEXT NOT NULL,                      -- Human-readable label: 'group_created', 'subject_changed', 'icon_changed', 'participant_added', 'participant_removed', 'admin_promoted', 'admin_demoted', 'number_changed', 'e2e_encrypted', 'disappearing_on', 'disappearing_off', etc.
    actor_id        INTEGER                            -- Contact who performed the action
                        REFERENCES contact(id),
    target_id       INTEGER                            -- Contact affected by the action (e.g. removed member)
                        REFERENCES contact(id),
    event_data      TEXT,                              -- JSON blob for complex events (e.g. new subject text, new timer value)
    community_name  TEXT,                              -- Community/group name from message_system_with_group_nodes
    timestamp       INTEGER NOT NULL                   -- Unix-ms when the event occurred
);
"""

TABLES["number_change"] = """
CREATE TABLE IF NOT EXISTS number_change (
    -- Details for "X changed their number" system events.
    -- Links old and new JIDs and their resolved contact records.

    id                  INTEGER PRIMARY KEY,
    system_event_id     INTEGER NOT NULL               -- Parent system_event row
                            REFERENCES system_event(id),
    old_jid_row_id      INTEGER,                       -- jid._id for the old phone number
    new_jid_row_id      INTEGER,                       -- jid._id for the new phone number
    old_contact_id      INTEGER                        -- Contact record for the old number
                            REFERENCES contact(id),
    new_contact_id      INTEGER                        -- Contact record for the new number
                            REFERENCES contact(id)
);
"""

TABLES["group_event_detail"] = """
CREATE TABLE IF NOT EXISTS group_event_detail (
    -- Extra detail rows for group join/leave/add/remove events where the
    -- system_event target_id is insufficient (e.g. bulk adds).

    id                      INTEGER PRIMARY KEY,
    system_event_id         INTEGER NOT NULL            -- Parent system_event row
                                REFERENCES system_event(id),
    participant_contact_id  INTEGER                    -- Contact involved in this group event
                                REFERENCES contact(id),
    is_me_joined            BOOLEAN                    -- 1 if the device owner is the one who joined
);
"""

TABLES["group_past_participant"] = """
CREATE TABLE IF NOT EXISTS group_past_participant (
    -- Historical record of past group participants, sourced from msgstore.db
    -- group_participant_user table with state != 0.  Provides evidence of
    -- participation even after a member has left.

    id                  INTEGER PRIMARY KEY,
    conversation_id     INTEGER NOT NULL               -- Group conversation
                            REFERENCES conversation(id),
    contact_id          INTEGER NOT NULL               -- Past member contact
                            REFERENCES contact(id),
    state               INTEGER,                       -- WhatsApp participant state code
    last_seen_ts        INTEGER                        -- Unix-ms when the participant was last observed
);
"""

TABLES["group_metadata_change"] = """
CREATE TABLE IF NOT EXISTS group_metadata_change (
    -- Forensic timeline of group metadata modifications: subject (name) changes,
    -- description changes, profile picture (icon/DP) changes, and group settings
    -- changes (admin-only messaging, invite link resets, approval mode, etc.).
    --
    -- Sourced from msgstore.db tables:
    --   message_system (action_type)
    --   message_system_value_change (old_data → previous value)
    --   message_system_photo_change (old_photo / new_photo BLOBs)
    --   message.text_data (new subject / new description text)
    --
    -- Action type mapping:
    --   1  = subject_changed      27 = description_changed
    --   6  = icon_changed         29 = admin_only_edit_on
    --   30 = admin_only_edit_off  31 = admin_only_send_on
    --   32 = admin_only_send_off  56 = disappearing_changed
    --   83 = invite_link_reset    84 = approval_mode_changed
    --   85 = membership_approval_changed

    id                  INTEGER PRIMARY KEY,
    conversation_id     INTEGER NOT NULL               -- Group where the change occurred
                            REFERENCES conversation(id),
    change_type         TEXT NOT NULL,                  -- 'subject', 'description', 'icon', 'admin_only_edit',
                                                       -- 'admin_only_send', 'disappearing', 'invite_link_reset',
                                                       -- 'approval_mode', 'membership_approval'
    old_value           TEXT,                           -- Previous text value (old subject, old description, old setting)
    new_value           TEXT,                           -- New text value (new subject, new description, new setting)
    old_photo           BLOB,                          -- Previous group DP/icon (JPEG thumbnail from WhatsApp)
    new_photo           BLOB,                          -- New group DP/icon (JPEG thumbnail from WhatsApp)
    changed_by_id       INTEGER                        -- Contact who made the change
                            REFERENCES contact(id),
    message_id          INTEGER                        -- Link to the system message in our message table
                            REFERENCES message(id),
    source_msg_id       INTEGER,                       -- Original message._id from msgstore.db (for forensic tracing)
    action_type         INTEGER,                       -- Raw WhatsApp action_type code from message_system
    timestamp           INTEGER NOT NULL               -- Unix-ms when the change occurred
);
"""

# ---- 11. Forensic Recovery -------------------------------------------------

TABLES["ghost_message"] = """
CREATE TABLE IF NOT EXISTS ghost_message (
    -- "Ghost" records: content of messages that were deleted-for-everyone
    -- (revoked) but whose original text survives in quoted replies, edit
    -- history, or WAL recovery.  Critical forensic artifact.

    id                      INTEGER PRIMARY KEY,
    revoked_msg_id          INTEGER                    -- The revoked message record (if it still exists)
                                REFERENCES message(id),
    recovered_from_msg_id   INTEGER                    -- The message whose quote/context preserved the ghost
                                REFERENCES message(id),
    original_text           TEXT,                       -- Recovered original message text
    original_type           INTEGER,                    -- Original message type code before revocation
    revoke_timestamp        INTEGER,                    -- Unix-ms when the revocation occurred
    original_sender_id      INTEGER                    -- Who originally sent the now-deleted message
                                REFERENCES contact(id),
    conversation_id         INTEGER                    -- Conversation context
                                REFERENCES conversation(id),
    recovery_method         TEXT DEFAULT 'quoted_text'  -- How the content was recovered: 'quoted_text', 'edit_history', 'wal', 'freelist'
);
"""

TABLES["edit_history"] = """
CREATE TABLE IF NOT EXISTS edit_history (
    -- Version history for edited messages.  Each row represents one edit
    -- event from message_edit_info, plus additional recovered text versions
    -- from quoted replies (message_quoted.text_data) and the FTS index
    -- (message_ftsv2_content.c0content).

    id                  INTEGER PRIMARY KEY,
    message_id          INTEGER NOT NULL               -- The edited message
                            REFERENCES message(id),
    original_key_id     TEXT,                          -- key_id of the original version
    edited_timestamp    INTEGER NOT NULL,              -- Unix-ms server timestamp of this edit
    sender_timestamp    INTEGER NOT NULL,              -- Unix-ms sender-side timestamp of this edit
    version             INTEGER DEFAULT 1,             -- Edit version number (1 = first edit, 2 = second, etc.)
    original_text       TEXT,                          -- Pre-edit text recovered from FTS index (lowercase/tokenized)
    recovery_method     TEXT DEFAULT 'fts_index'       -- How original_text was recovered: 'fts_index', 'quoted_reply'
);
"""

TABLES["edit_version"] = """
CREATE TABLE IF NOT EXISTS edit_version (
    -- Intermediate text versions recovered from quoted replies.
    -- When someone quotes an edited message BEFORE the edit, the quote
    -- preserves the text at that point in time, giving us intermediate
    -- versions that neither the FTS index nor message.text_data provide.

    id                  INTEGER PRIMARY KEY,
    message_id          INTEGER NOT NULL               -- The edited message
                            REFERENCES message(id),
    captured_text       TEXT NOT NULL,                 -- Text preserved in the quote
    captured_timestamp  INTEGER NOT NULL,              -- Unix-ms when the quoting message was sent
    quote_source_msg_id INTEGER                        -- The message that quoted and preserved this version
                            REFERENCES message(id),
    recovery_method     TEXT DEFAULT 'quoted_reply',   -- 'quoted_reply', 'fts_index'
    is_pre_edit         INTEGER DEFAULT 1              -- 1 = captured before edit, 0 = captured after edit

);
"""

TABLES["edit_addon_receipt"] = """
CREATE TABLE IF NOT EXISTS edit_addon_receipt (
    -- Original delivery receipts preserved in message_add_on_receipt_device.
    -- When a message is edited, WhatsApp OVERWRITES receipt_device timestamps
    -- with the edit delivery time. The ORIGINAL delivery timing is preserved
    -- only in this add-on receipt table (linked via message_add_on type=74).

    id                  INTEGER PRIMARY KEY,
    message_id          INTEGER NOT NULL               -- The edited message
                            REFERENCES message(id),
    original_receipt_ts INTEGER,                       -- Original delivery timestamp (pre-edit)
    edit_receipt_ts     INTEGER,                       -- Edit delivery timestamp (post-edit, from receipt_device)
    recipient_jid       TEXT,                          -- Recipient device JID
    addon_key_id        TEXT                           -- key_id from message_add_on (= original_key_id)
);
"""

TABLES["recovered_data"] = """
CREATE TABLE IF NOT EXISTS recovered_data (
    -- Catch-all table for data recovered from WAL files, freelist pages,
    -- or freeblock carving that does not map cleanly to a specific table.
    -- Raw recovered content is stored as JSON for manual review.

    id                  INTEGER PRIMARY KEY,
    source_table        TEXT NOT NULL,                  -- Original table the data was recovered from (e.g. 'message')
    source_db           TEXT NOT NULL,                  -- Source database file (e.g. 'msgstore.db')
    recovery_method     TEXT NOT NULL,                  -- 'wal', 'freelist', or 'freeblock'
    raw_data            TEXT,                           -- JSON representation of the recovered row data
    recovered_timestamp INTEGER NOT NULL               -- Unix-ms when recovery analysis was performed
);
"""

# ---- 12. Pre-computed Analytics -------------------------------------------

TABLES["stats_daily_activity"] = """
CREATE TABLE IF NOT EXISTS stats_daily_activity (
    -- Pre-aggregated daily message counts per conversation.
    -- Populated after the full message ETL pass to accelerate timeline
    -- and activity-chart API endpoints.

    id                  INTEGER PRIMARY KEY,
    conversation_id     INTEGER NOT NULL               -- Conversation being measured
                            REFERENCES conversation(id),
    date_str            TEXT NOT NULL,                  -- Date in 'YYYY-MM-DD' format (UTC)
    total_messages      INTEGER DEFAULT 0,             -- Total messages on this day
    sent_messages       INTEGER DEFAULT 0,             -- Messages sent by device owner
    received_messages   INTEGER DEFAULT 0,             -- Messages received from others
    text_count          INTEGER DEFAULT 0,             -- Plain text messages
    media_count         INTEGER DEFAULT 0,             -- Image + video + audio + document messages
    sticker_count       INTEGER DEFAULT 0,             -- Sticker messages
    link_count          INTEGER DEFAULT 0,             -- Messages containing URLs
    reaction_count      INTEGER DEFAULT 0,             -- Reactions placed on this day
    deleted_count       INTEGER DEFAULT 0,             -- Messages revoked on this day
    edited_count        INTEGER DEFAULT 0,             -- Messages edited on this day

    UNIQUE(conversation_id, date_str)
);
"""

TABLES["stats_contact_activity"] = """
CREATE TABLE IF NOT EXISTS stats_contact_activity (
    -- Per-contact, per-conversation aggregate statistics.  One row per
    -- (contact, conversation) pair.  Drives "who talks the most" analyses
    -- and per-member breakdowns in group chats.

    id                          INTEGER PRIMARY KEY,
    contact_id                  INTEGER NOT NULL        -- The contact being measured
                                    REFERENCES contact(id),
    conversation_id             INTEGER NOT NULL        -- Conversation scope
                                    REFERENCES conversation(id),

    -- Message totals by category
    total_messages              INTEGER DEFAULT 0,
    total_text                  INTEGER DEFAULT 0,
    total_media                 INTEGER DEFAULT 0,
    total_images                INTEGER DEFAULT 0,
    total_videos                INTEGER DEFAULT 0,
    total_audio                 INTEGER DEFAULT 0,
    total_documents             INTEGER DEFAULT 0,
    total_stickers              INTEGER DEFAULT 0,
    total_gifs                  INTEGER DEFAULT 0,
    total_links                 INTEGER DEFAULT 0,

    -- Interaction totals
    total_reactions_given       INTEGER DEFAULT 0,      -- Reactions this contact placed
    total_reactions_received    INTEGER DEFAULT 0,      -- Reactions received on this contact's messages
    total_mentions              INTEGER DEFAULT 0,      -- Times this contact was @mentioned
    total_forwards              INTEGER DEFAULT 0,      -- Forwarded messages sent by this contact
    total_edits                 INTEGER DEFAULT 0,      -- Messages edited by this contact
    total_deletes               INTEGER DEFAULT 0,      -- Messages deleted by this contact

    -- Response time
    avg_response_time_ms        REAL,                   -- Average response time in milliseconds

    -- Temporal bounds
    first_message_ts            INTEGER,                -- Unix-ms of first message from this contact in this conversation
    last_message_ts             INTEGER,                -- Unix-ms of last message from this contact in this conversation

    UNIQUE(contact_id, conversation_id)
);
"""

TABLES["stats_hourly_heatmap"] = """
CREATE TABLE IF NOT EXISTS stats_hourly_heatmap (
    -- 7x24 activity heatmap: message counts bucketed by day-of-week and
    -- hour-of-day.  Optionally scoped to a single contact and/or
    -- conversation (NULL means "all").

    id                  INTEGER PRIMARY KEY,
    contact_id          INTEGER                        -- NULL for conversation-wide heatmap
                            REFERENCES contact(id),
    conversation_id     INTEGER                        -- NULL for global heatmap
                            REFERENCES conversation(id),
    day_of_week         INTEGER NOT NULL,              -- 0 = Monday, 6 = Sunday (ISO weekday)
    hour_of_day         INTEGER NOT NULL,              -- 0-23 (hour in UTC or device-local TZ depending on config)
    message_count       INTEGER DEFAULT 0,

    UNIQUE(contact_id, conversation_id, day_of_week, hour_of_day)
);
"""

TABLES["stats_network_edge"] = """
CREATE TABLE IF NOT EXISTS stats_network_edge (
    -- Directed edges for social network analysis.  An edge (A -> B) in
    -- conversation C means A sent at least one message that B was part of
    -- (in a group) or that was directed at B (in a 1-on-1 chat).

    id                      INTEGER PRIMARY KEY,
    source_id               INTEGER NOT NULL            -- Contact who sent messages
                                REFERENCES contact(id),
    target_id               INTEGER NOT NULL            -- Contact who received / was in the audience
                                REFERENCES contact(id),
    conversation_id         INTEGER NOT NULL            -- Conversation context
                                REFERENCES conversation(id),
    message_count           INTEGER DEFAULT 0,          -- Number of messages along this edge
    first_interaction_ts    INTEGER,                    -- Unix-ms of the first message on this edge
    last_interaction_ts     INTEGER,                    -- Unix-ms of the last message on this edge

    UNIQUE(source_id, target_id, conversation_id)
);
"""

# ---- 13. Case Metadata ----------------------------------------------------

TABLES["case_metadata"] = """
CREATE TABLE IF NOT EXISTS case_metadata (
    -- Key-value store for case-level metadata: examiner name, case number,
    -- device IMEI, extraction tool version, schema version, timestamps, etc.

    id      INTEGER PRIMARY KEY,
    key     TEXT NOT NULL UNIQUE,               -- Metadata key (e.g. 'schema_version', 'case_number', 'examiner')
    value   TEXT NOT NULL                       -- Metadata value (always stored as text; caller parses as needed)
);
"""

TABLES["message_tag"] = """
CREATE TABLE IF NOT EXISTS message_tag (
    -- Investigator-applied tags/flags on individual messages for forensic review.
    -- GUI feature: right-click a message bubble -> "Tag Message".

    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL UNIQUE REFERENCES message(id),
    tag_label   TEXT DEFAULT 'flagged',              -- Tag category (e.g. 'flagged', 'evidence', 'suspicious')
    note        TEXT DEFAULT '',                     -- Investigator notes
    tagged_at   TEXT DEFAULT (datetime('now')),      -- When the tag was applied
    tagged_by   TEXT DEFAULT 'investigator'           -- Who applied the tag
);
"""

TABLES["image_hash"] = """
CREATE TABLE IF NOT EXISTS image_hash (
    -- Perceptual hashes for image similarity search.
    -- Populated by the GUI's Image Similarity indexer (not during ingestion).

    message_id      INTEGER PRIMARY KEY REFERENCES message(id),
    phash           TEXT,           -- Perceptual hash (DCT-based, 256-bit hex)
    dhash           TEXT,           -- Difference hash (gradient-based, 256-bit hex)
    edge_hash       TEXT            -- Edge-map hash (Canny + pHash, 256-bit hex)
);
"""

# ---- 14b. Orphaned Media -------------------------------------------------

TABLES["orphaned_media"] = """
CREATE TABLE IF NOT EXISTS orphaned_media (
    -- Media files on disk not linked to any message in msgstore.db.
    -- From cleared chats, reinstalled WhatsApp, deleted conversations, etc.
    -- Dates parsed from WhatsApp filename convention (IMG-YYYYMMDD-WAxxxx).

    id                      INTEGER PRIMARY KEY,
    file_path               TEXT NOT NULL UNIQUE,       -- Full path on disk
    file_name               TEXT,                       -- Basename (IMG-20230116-WA0004.jpg)
    folder                  TEXT,                       -- WhatsApp Images, WhatsApp Video, etc.
    file_size               INTEGER,                    -- Bytes
    mime_type               TEXT,                       -- image/jpeg, video/mp4, etc.

    -- Date parsed from filename
    parsed_date             TEXT,                       -- YYYY-MM-DD
    parsed_date_ts          INTEGER,                    -- Unix milliseconds

    -- File hash (computed on demand, not during ingestion)
    file_hash               TEXT,                       -- SHA-256 base64

    -- Image dimensions / media duration
    width                   INTEGER,
    height                  INTEGER,
    duration_ms             INTEGER,

    -- Hash match against media table (set by hash matching worker)
    matched_message_id      INTEGER,                    -- message.id if hash matched
    matched_conversation_id INTEGER,                    -- conversation.id if hash matched
    matched_conv_name       TEXT,                       -- conversation display_name

    -- Source classification
    source_type             TEXT,                       -- 'received', 'sent', 'private', 'status',
                                                        -- 'gbwhatsapp', 'links'

    -- Thumbnail for gallery display
    thumbnail_blob          BLOB
);
"""

# ---- 15. Status Posts ----------------------------------------------------

TABLES["status_post"] = """
CREATE TABLE IF NOT EXISTS status_post (
    -- Status updates ("Stories") posted by contacts.
    -- Sourced from messages in the status@broadcast chat in msgstore.db and
    -- optionally enriched with interaction data from status.db.

    id                  INTEGER PRIMARY KEY,
    message_id          INTEGER REFERENCES message(id),   -- Link to the message table
    contact_id          INTEGER REFERENCES contact(id),   -- Who posted this status
    conversation_id     INTEGER REFERENCES conversation(id), -- status@broadcast conversation
    timestamp           INTEGER,                          -- Unix-ms when status was posted
    type_label          TEXT,                              -- 'image', 'video', 'text', 'gif', 'voice', etc.
    text_content        TEXT,                              -- Text content / caption
    has_media           BOOLEAN DEFAULT 0,                 -- 1 if status has media attachment
    thumbnail_available BOOLEAN DEFAULT 0,                 -- 1 if thumbnail exists in media table
    media_mime_type     TEXT,                              -- MIME type of media (image/jpeg, video/mp4, etc.)
    media_file_path     TEXT,                              -- Resolved file path on disk (NULL if not available)
    media_downloadable  BOOLEAN DEFAULT 0,                 -- 1 if CDN URL / direct_path available for download
    view_count          INTEGER DEFAULT 0,                 -- Number of views (from status.db)
    reaction_count      INTEGER DEFAULT 0,                 -- Number of reactions (from status.db)
    source_msg_id       INTEGER                            -- Original message._id from msgstore.db
);
"""

# ---------------------------------------------------------------------------
# CREATE INDEX statements
# ---------------------------------------------------------------------------
# Keyed by index name. Every index includes a comment explaining the query
# pattern it accelerates.
# ---------------------------------------------------------------------------

INDEXES: dict[str, str] = {}

# -- orphaned_media --------------------------------------------------------

INDEXES["idx_orphaned_media_folder"] = """
CREATE INDEX IF NOT EXISTS idx_orphaned_media_folder ON orphaned_media(folder);
"""
INDEXES["idx_orphaned_media_parsed_date"] = """
CREATE INDEX IF NOT EXISTS idx_orphaned_media_parsed_date ON orphaned_media(parsed_date);
"""
INDEXES["idx_orphaned_media_file_hash"] = """
CREATE INDEX IF NOT EXISTS idx_orphaned_media_file_hash ON orphaned_media(file_hash);
"""
INDEXES["idx_orphaned_media_matched"] = """
CREATE INDEX IF NOT EXISTS idx_orphaned_media_matched ON orphaned_media(matched_message_id);
"""

# -- message_tag -----------------------------------------------------------

INDEXES["idx_message_tag_mid"] = """
-- Accelerates tag lookup by message ID (bubble rendering, tag checks).
CREATE INDEX IF NOT EXISTS idx_message_tag_mid
    ON message_tag(message_id);
"""

# -- contact ---------------------------------------------------------------

INDEXES["idx_contact_phone_number"] = """
-- Accelerates lookups by phone number (search, dedup, display).
CREATE INDEX IF NOT EXISTS idx_contact_phone_number
    ON contact(phone_number);
"""

INDEXES["idx_contact_resolved_name"] = """
-- Accelerates name-based search and ordering.
CREATE INDEX IF NOT EXISTS idx_contact_resolved_name
    ON contact(resolved_name);
"""

INDEXES["idx_contact_source_jid_row_id"] = """
-- Fast join from msgstore jid._id to unified contact during ETL.
CREATE INDEX IF NOT EXISTS idx_contact_source_jid_row_id
    ON contact(source_jid_row_id);
"""

INDEXES["idx_contact_source_lid_row_id"] = """
-- Fast join from LID jid._id to unified contact during ETL.
CREATE INDEX IF NOT EXISTS idx_contact_source_lid_row_id
    ON contact(source_lid_row_id);
"""

INDEXES["idx_contact_phone_jid"] = """
-- Fast avatar/contact lookup by phone JID string.
CREATE INDEX IF NOT EXISTS idx_contact_phone_jid
    ON contact(phone_jid);
"""

INDEXES["idx_contact_lid_jid"] = """
-- Fast avatar/contact lookup by LID JID string.
CREATE INDEX IF NOT EXISTS idx_contact_lid_jid
    ON contact(lid_jid);
"""

# -- jid_to_contact --------------------------------------------------------

INDEXES["idx_jid_to_contact_contact_id"] = """
-- Reverse lookup: find all JIDs belonging to a contact.
CREATE INDEX IF NOT EXISTS idx_jid_to_contact_contact_id
    ON jid_to_contact(contact_id);
"""

INDEXES["idx_jid_to_contact_jid_raw_string"] = """
-- Lookup contact by JID string (used for avatar/conversation joins).
CREATE INDEX IF NOT EXISTS idx_jid_to_contact_jid_raw_string
    ON jid_to_contact(jid_raw_string);
"""

# -- conversation ----------------------------------------------------------

INDEXES["idx_conversation_chat_type"] = """
-- Filter conversations by type (personal, group, broadcast, etc.).
CREATE INDEX IF NOT EXISTS idx_conversation_chat_type
    ON conversation(chat_type);
"""

INDEXES["idx_conversation_display_name"] = """
-- Alphabetical listing and name search of conversations.
CREATE INDEX IF NOT EXISTS idx_conversation_display_name
    ON conversation(display_name);
"""

INDEXES["idx_conversation_last_message_ts"] = """
-- Sort conversations by recency (default inbox ordering).
CREATE INDEX IF NOT EXISTS idx_conversation_last_message_ts
    ON conversation(last_message_ts DESC);
"""

# -- group_member ----------------------------------------------------------

INDEXES["idx_group_member_conversation_id"] = """
-- List all members of a specific group.
CREATE INDEX IF NOT EXISTS idx_group_member_conversation_id
    ON group_member(conversation_id);
"""

INDEXES["idx_group_member_contact_id"] = """
-- Find all groups a contact belongs to.
CREATE INDEX IF NOT EXISTS idx_group_member_contact_id
    ON group_member(contact_id);
"""

# -- message ---------------------------------------------------------------

INDEXES["idx_message_conversation_timestamp"] = """
-- Primary message listing: paginate messages in a conversation by time.
CREATE INDEX IF NOT EXISTS idx_message_conversation_timestamp
    ON message(conversation_id, timestamp);
"""

INDEXES["idx_message_conversation_sort_id"] = """
-- Alternative sort order using WhatsApp's internal sort_id.
CREATE INDEX IF NOT EXISTS idx_message_conversation_sort_id
    ON message(conversation_id, sort_id);
"""

INDEXES["idx_message_conv_ts_sortid"] = """
-- Covering index for keyset pagination: constant-time tile seeks at any offset.
-- Replaces O(n) OFFSET scanning with O(1) WHERE (timestamp, sort_id) >= (?, ?) seeks.
CREATE INDEX IF NOT EXISTS idx_message_conv_ts_sortid
    ON message(conversation_id, timestamp, sort_id);
"""

INDEXES["idx_message_sender_timestamp"] = """
-- Per-sender timeline: all messages from a contact across conversations.
CREATE INDEX IF NOT EXISTS idx_message_sender_timestamp
    ON message(sender_id, timestamp);
"""

INDEXES["idx_message_type"] = """
-- Filter messages by type (text, image, video, etc.).
CREATE INDEX IF NOT EXISTS idx_message_type
    ON message(message_type);
"""

INDEXES["idx_message_source_key_id"] = """
-- Lookup by globally-unique WhatsApp key_id (cross-reference, dedup).
CREATE INDEX IF NOT EXISTS idx_message_source_key_id
    ON message(source_key_id);
"""

INDEXES["idx_message_is_revoked"] = """
-- Partial index for revoked (deleted-for-everyone) messages.
CREATE INDEX IF NOT EXISTS idx_message_is_revoked
    ON message(is_revoked) WHERE is_revoked = 1;
"""

INDEXES["idx_message_is_edited"] = """
-- Partial index for edited messages.
CREATE INDEX IF NOT EXISTS idx_message_is_edited
    ON message(is_edited) WHERE is_edited = 1;
"""

INDEXES["idx_message_is_starred"] = """
-- Partial index for starred messages.
CREATE INDEX IF NOT EXISTS idx_message_is_starred
    ON message(is_starred) WHERE is_starred = 1;
"""

INDEXES["idx_message_timestamp"] = """
-- Global time-range queries across all conversations.
CREATE INDEX IF NOT EXISTS idx_message_timestamp
    ON message(timestamp);
"""

# -- media -----------------------------------------------------------------

INDEXES["idx_media_message_id"] = """
-- Join from message to its media attachment.
CREATE INDEX IF NOT EXISTS idx_media_message_id
    ON media(message_id);
"""

INDEXES["idx_media_mime_type"] = """
-- Filter media by MIME type (e.g. all images, all PDFs).
CREATE INDEX IF NOT EXISTS idx_media_mime_type
    ON media(mime_type);
"""

INDEXES["idx_media_file_hash"] = """
-- Deduplicate media files by content hash.
CREATE INDEX IF NOT EXISTS idx_media_file_hash
    ON media(file_hash);
"""

# Partial indexes for the message_association linkage columns.
# Most media rows have these NULL/0; the partial form keeps
# the index small (only the small minority of rows that are
# association children) while still letting the chat-list
# WHERE clause and the auxiliary HD-twin lookup run as cheap
# indexed probes instead of full table scans.

INDEXES["idx_media_is_hd_twin"] = """
-- Lets the chat-list WHERE clause find association-children
-- ("is_hd_twin = 1") without scanning the whole media table.
-- Only ~5% of media is an association child, so a partial
-- index over those rows is dramatically smaller than a full
-- one and just as selective for our query.
CREATE INDEX IF NOT EXISTS idx_media_is_hd_twin
    ON media(is_hd_twin)
    WHERE is_hd_twin = 1;
"""

INDEXES["idx_media_hd_twin_msg_id"] = """
-- Lets the auxiliary HD-twin lookup ("does any parent point
-- to me as its hd_twin_msg_id?") run as an indexed probe.
-- Without this the EXISTS subquery for distinguishing HD
-- pair members from scaffolding children was O(N²) over the
-- media table.  Partial because only SD parents of HD pairs
-- populate this column.
CREATE INDEX IF NOT EXISTS idx_media_hd_twin_msg_id
    ON media(hd_twin_msg_id)
    WHERE hd_twin_msg_id IS NOT NULL;
"""

INDEXES["idx_media_assoc_parent_msg_id"] = """
-- Lets the mirror lookup join from an HD twin row back to its
-- SD parent's media row in a single indexed probe.  Also used
-- by the channel-poll renderer to enumerate option-image
-- children of a poll msg without re-reading msgstore.
-- Partial because only association children populate it.
CREATE INDEX IF NOT EXISTS idx_media_assoc_parent_msg_id
    ON media(assoc_parent_msg_id)
    WHERE assoc_parent_msg_id IS NOT NULL;
"""

# -- location --------------------------------------------------------------

INDEXES["idx_location_message_id"] = """
-- Join from message to its location data.
CREATE INDEX IF NOT EXISTS idx_location_message_id
    ON location(message_id);
"""

# -- message_link_detail ---------------------------------------------------

INDEXES["idx_message_link_detail_message_id"] = """
-- All links within a specific message.
CREATE INDEX IF NOT EXISTS idx_message_link_detail_message_id
    ON message_link_detail(message_id);
"""

INDEXES["idx_message_link_detail_domain"] = """
-- Aggregate link statistics by domain.
CREATE INDEX IF NOT EXISTS idx_message_link_detail_domain
    ON message_link_detail(domain);
"""

# -- message_album / message_association -----------------------------------

INDEXES["idx_message_association_parent"] = """
-- All children of a given album-parent (renderer pulls children sorted).
CREATE INDEX IF NOT EXISTS idx_message_association_parent
    ON message_association(parent_message_id, sort_order);
"""

INDEXES["idx_message_association_child"] = """
-- "Is this message an album-child?" lookup, used to filter children
-- out of the main chat stream so they're only shown inside the parent grid.
CREATE INDEX IF NOT EXISTS idx_message_association_child
    ON message_association(child_message_id);
"""

INDEXES["idx_message_association_type"] = """
-- Filter by association_type (e.g. only association_type=2 album members).
CREATE INDEX IF NOT EXISTS idx_message_association_type
    ON message_association(association_type);
"""

# -- message_device --------------------------------------------------------

INDEXES["idx_message_device_message_id"] = """
-- Join from message to its device information.
CREATE INDEX IF NOT EXISTS idx_message_device_message_id
    ON message_device(message_id);
"""

# -- receipt ---------------------------------------------------------------

INDEXES["idx_receipt_message_id"] = """
-- All receipts for a specific outgoing message.
CREATE INDEX IF NOT EXISTS idx_receipt_message_id
    ON receipt(message_id);
"""

INDEXES["idx_receipt_recipient_id"] = """
-- All receipts involving a specific contact (delivery proof).
CREATE INDEX IF NOT EXISTS idx_receipt_recipient_id
    ON receipt(recipient_id);
"""

INDEXES["idx_receipt_read_ts"] = """
-- Partial index for receipts where read timestamp exists (read-receipt queries).
CREATE INDEX IF NOT EXISTS idx_receipt_read_ts
    ON receipt(read_ts) WHERE read_ts IS NOT NULL;
"""

# -- receipt_device_record -------------------------------------------------

INDEXES["idx_receipt_device_record_message_id"] = """
-- All device-level receipts for a message.
CREATE INDEX IF NOT EXISTS idx_receipt_device_record_message_id
    ON receipt_device_record(message_id);
"""

INDEXES["idx_receipt_device_record_contact_id"] = """
-- Device receipts by contact.
CREATE INDEX IF NOT EXISTS idx_receipt_device_record_contact_id
    ON receipt_device_record(device_contact_id);
"""

# -- reaction --------------------------------------------------------------

INDEXES["idx_reaction_message_id"] = """
-- All reactions on a specific message.
CREATE INDEX IF NOT EXISTS idx_reaction_message_id
    ON reaction(message_id);
"""

INDEXES["idx_reaction_reactor_id"] = """
-- All reactions placed by a specific contact.
CREATE INDEX IF NOT EXISTS idx_reaction_reactor_id
    ON reaction(reactor_id);
"""

INDEXES["idx_reaction_emoji"] = """
-- Aggregate reaction statistics by emoji type.
CREATE INDEX IF NOT EXISTS idx_reaction_emoji
    ON reaction(emoji);
"""

# -- mention ---------------------------------------------------------------

INDEXES["idx_mention_message_id"] = """
-- All mentions within a specific message.
CREATE INDEX IF NOT EXISTS idx_mention_message_id
    ON mention(message_id);
"""

INDEXES["idx_mention_mentioned_id"] = """
-- All messages mentioning a specific contact.
CREATE INDEX IF NOT EXISTS idx_mention_mentioned_id
    ON mention(mentioned_id);
"""

# -- private_reply ---------------------------------------------------------

INDEXES["idx_private_reply_source_conversation_id"] = """
-- Find private replies originating from a specific group conversation.
CREATE INDEX IF NOT EXISTS idx_private_reply_source_conversation_id
    ON private_reply(source_conversation_id);
"""

# -- poll_option -----------------------------------------------------------

INDEXES["idx_poll_option_poll_id"] = """
-- All options belonging to a poll.
CREATE INDEX IF NOT EXISTS idx_poll_option_poll_id
    ON poll_option(poll_id);
"""

# -- poll_vote -------------------------------------------------------------

INDEXES["idx_poll_vote_poll_id"] = """
-- All votes cast in a specific poll.
CREATE INDEX IF NOT EXISTS idx_poll_vote_poll_id
    ON poll_vote(poll_id);
"""

INDEXES["idx_poll_vote_voter_id"] = """
-- All polls a contact voted in.
CREATE INDEX IF NOT EXISTS idx_poll_vote_voter_id
    ON poll_vote(voter_id);
"""

# -- poll_vote_option ------------------------------------------------------

INDEXES["idx_poll_vote_option_vote_id"] = """
-- All options selected by a specific vote.
CREATE INDEX IF NOT EXISTS idx_poll_vote_option_vote_id
    ON poll_vote_option(poll_vote_id);
"""

INDEXES["idx_poll_vote_option_option_id"] = """
-- All votes for a specific option.
CREATE INDEX IF NOT EXISTS idx_poll_vote_option_option_id
    ON poll_vote_option(poll_option_id);
"""

# -- message_vcard_data ----------------------------------------------------

INDEXES["idx_vcard_data_message_id"] = """
-- Fast lookup of vcard data by message.
CREATE INDEX IF NOT EXISTS idx_vcard_data_message_id
    ON message_vcard_data(message_id);
"""

# -- call_record -----------------------------------------------------------

INDEXES["idx_call_record_contact_id"] = """
-- Call history for a specific contact.
CREATE INDEX IF NOT EXISTS idx_call_record_contact_id
    ON call_record(contact_id);
"""

INDEXES["idx_call_record_timestamp"] = """
-- Chronological call log.
CREATE INDEX IF NOT EXISTS idx_call_record_timestamp
    ON call_record(timestamp);
"""

# -- call_participant ------------------------------------------------------

INDEXES["idx_call_participant_call_id"] = """
-- All participants in a specific call.
CREATE INDEX IF NOT EXISTS idx_call_participant_call_id
    ON call_participant(call_id);
"""

# -- scheduled_event -------------------------------------------------------

INDEXES["idx_scheduled_event_conversation"] = """CREATE INDEX IF NOT EXISTS idx_scheduled_event_conversation ON scheduled_event(conversation_id)"""

INDEXES["idx_scheduled_event_start_time"] = """CREATE INDEX IF NOT EXISTS idx_scheduled_event_start_time ON scheduled_event(start_time)"""

INDEXES["idx_scheduled_event_message"] = """CREATE INDEX IF NOT EXISTS idx_scheduled_event_message ON scheduled_event(message_id)"""

# -- system_event ----------------------------------------------------------

INDEXES["idx_system_event_conversation_timestamp"] = """
-- Chronological system events within a conversation.
CREATE INDEX IF NOT EXISTS idx_system_event_conversation_timestamp
    ON system_event(conversation_id, timestamp);
"""

INDEXES["idx_system_event_event_type"] = """
-- Filter system events by type (e.g. all 'participant_added' events).
CREATE INDEX IF NOT EXISTS idx_system_event_event_type
    ON system_event(event_type);
"""

INDEXES["idx_system_event_actor_id"] = """
-- All system events performed by a specific contact (admin actions audit).
CREATE INDEX IF NOT EXISTS idx_system_event_actor_id
    ON system_event(actor_id);
"""

# -- group_metadata_change --------------------------------------------------

INDEXES["idx_group_metadata_change_conv_ts"] = """
-- Chronological group metadata changes per conversation.
CREATE INDEX IF NOT EXISTS idx_group_metadata_change_conv_ts
    ON group_metadata_change(conversation_id, timestamp);
"""

INDEXES["idx_group_metadata_change_type"] = """
-- Filter group metadata changes by change type.
CREATE INDEX IF NOT EXISTS idx_group_metadata_change_type
    ON group_metadata_change(change_type);
"""

INDEXES["idx_group_metadata_change_changed_by"] = """
-- All group changes made by a specific contact.
CREATE INDEX IF NOT EXISTS idx_group_metadata_change_changed_by
    ON group_metadata_change(changed_by_id);
"""

# -- ghost_message ---------------------------------------------------------

INDEXES["idx_ghost_message_conversation_id"] = """
-- All recovered ghost messages in a conversation.
CREATE INDEX IF NOT EXISTS idx_ghost_message_conversation_id
    ON ghost_message(conversation_id);
"""

INDEXES["idx_ghost_message_original_sender_id"] = """
-- Ghost messages by original sender (who deleted what).
CREATE INDEX IF NOT EXISTS idx_ghost_message_original_sender_id
    ON ghost_message(original_sender_id);
"""

# -- edit_history ----------------------------------------------------------

INDEXES["idx_edit_history_message_id"] = """
-- All edit versions for a specific message.
CREATE INDEX IF NOT EXISTS idx_edit_history_message_id
    ON edit_history(message_id);
"""

# -- stats_daily_activity --------------------------------------------------

INDEXES["idx_stats_daily_activity_conversation_id"] = """
-- Daily stats for a specific conversation (timeline charts).
CREATE INDEX IF NOT EXISTS idx_stats_daily_activity_conversation_id
    ON stats_daily_activity(conversation_id);
"""

INDEXES["idx_stats_daily_activity_date_str"] = """
-- Cross-conversation daily totals (global activity timeline).
CREATE INDEX IF NOT EXISTS idx_stats_daily_activity_date_str
    ON stats_daily_activity(date_str);
"""

# -- stats_contact_activity ------------------------------------------------

INDEXES["idx_stats_contact_activity_contact_id"] = """
-- All conversation stats for a specific contact.
CREATE INDEX IF NOT EXISTS idx_stats_contact_activity_contact_id
    ON stats_contact_activity(contact_id);
"""

INDEXES["idx_stats_contact_activity_conversation_id"] = """
-- All contact stats within a specific conversation (group leaderboard).
CREATE INDEX IF NOT EXISTS idx_stats_contact_activity_conversation_id
    ON stats_contact_activity(conversation_id);
"""

# -- stats_network_edge ----------------------------------------------------

INDEXES["idx_stats_network_edge_source_id"] = """
-- Outgoing edges from a contact (who do they talk to).
CREATE INDEX IF NOT EXISTS idx_stats_network_edge_source_id
    ON stats_network_edge(source_id);
"""

INDEXES["idx_stats_network_edge_target_id"] = """
-- Incoming edges to a contact (who talks to them).
CREATE INDEX IF NOT EXISTS idx_stats_network_edge_target_id
    ON stats_network_edge(target_id);
"""

# -- status_post -----------------------------------------------------------

INDEXES["idx_call_record_category"] = """
-- Filter calls by category (voice_chat, group_call, multi_person, personal).
CREATE INDEX IF NOT EXISTS idx_call_record_category
    ON call_record(call_category);
"""

INDEXES["idx_status_post_contact_id"] = """
-- Accelerates per-contact status grouping and count queries.
CREATE INDEX IF NOT EXISTS idx_status_post_contact_id
    ON status_post(contact_id);
"""

INDEXES["idx_status_post_timestamp"] = """
-- Sort status posts by recency.
CREATE INDEX IF NOT EXISTS idx_status_post_timestamp
    ON status_post(timestamp DESC);
"""

INDEXES["idx_status_post_message_id"] = """
-- Join status posts to messages for media/thumbnail lookup.
CREATE INDEX IF NOT EXISTS idx_status_post_message_id
    ON status_post(message_id);
"""

# ---------------------------------------------------------------------------
# FTS5 Virtual Tables
# ---------------------------------------------------------------------------

FTS_TABLES: dict[str, str] = {}

FTS_TABLES["message_fts"] = """
-- Full-text search index over message text and quoted text.
-- Uses an external-content table backed by the message table to avoid
-- storing duplicate text.  The porter stemmer handles English morphology;
-- unicode61 with remove_diacritics=2 handles accented characters.
--
-- Rebuild with:  INSERT INTO message_fts(message_fts) VALUES('rebuild');
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
    text_content,
    quoted_text,
    content='message',
    content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);
"""

# ---------------------------------------------------------------------------
# FTS5 Triggers -- keep the FTS index in sync with the message table
# ---------------------------------------------------------------------------

FTS_TRIGGERS: dict[str, str] = {}

FTS_TRIGGERS["message_fts_insert"] = """
-- Automatically index new messages in the FTS table.
CREATE TRIGGER IF NOT EXISTS message_fts_insert AFTER INSERT ON message BEGIN
    INSERT INTO message_fts(rowid, text_content, quoted_text)
    VALUES (new.id, new.text_content, new.quoted_text);
END;
"""

FTS_TRIGGERS["message_fts_delete"] = """
-- Remove deleted messages from the FTS index.
CREATE TRIGGER IF NOT EXISTS message_fts_delete AFTER DELETE ON message BEGIN
    INSERT INTO message_fts(message_fts, rowid, text_content, quoted_text)
    VALUES ('delete', old.id, old.text_content, old.quoted_text);
END;
"""

FTS_TRIGGERS["message_fts_update"] = """
-- Re-index messages when text_content or quoted_text is updated.
CREATE TRIGGER IF NOT EXISTS message_fts_update AFTER UPDATE ON message BEGIN
    INSERT INTO message_fts(message_fts, rowid, text_content, quoted_text)
    VALUES ('delete', old.id, old.text_content, old.quoted_text);
    INSERT INTO message_fts(rowid, text_content, quoted_text)
    VALUES (new.id, new.text_content, new.quoted_text);
END;
"""

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_all_ddl() -> list[str]:
    """Return every DDL statement in dependency-safe execution order.

    The returned list contains, in order:
    1. All ``CREATE TABLE`` statements (topologically ordered so that
       parent tables are created before children that reference them).
    2. All ``CREATE INDEX`` statements.
    3. All ``CREATE VIRTUAL TABLE`` (FTS5) statements.

    FTS sync triggers are *intentionally NOT* included here —
    they are installed separately via
    :func:`create_fts_triggers` AFTER bulk ingestion completes.
    This avoids firing the trigger on every message insert
    during the MESSAGES stage; instead the FTS index is
    populated in a single ``rebuild`` pass and only then are
    triggers attached for incremental GUI-side updates.

    Returns
    -------
    list[str]
        Each element is a single executable SQL string (may contain
        embedded newlines).
    """
    statements: list[str] = []

    # Tables must come first, in dependency order.
    # The TABLES dict is an ordered dict (Python 3.7+) and we populated it
    # in dependency order above, so we can iterate directly.
    for ddl in TABLES.values():
        statements.append(ddl.strip())

    # Indexes depend on their parent tables, which are now created.
    for ddl in INDEXES.values():
        # Strip the leading comment lines so SQLite only sees the CREATE INDEX.
        lines = ddl.strip().splitlines()
        sql_lines = [ln for ln in lines if not ln.strip().startswith("--")]
        statements.append("\n".join(sql_lines).strip())

    # FTS virtual tables depend on the content table (message).
    for ddl in FTS_TABLES.values():
        lines = ddl.strip().splitlines()
        sql_lines = [ln for ln in lines if not ln.strip().startswith("--")]
        statements.append("\n".join(sql_lines).strip())

    # NOTE: FTS_TRIGGERS intentionally omitted — see create_fts_triggers().

    return statements


def create_schema(conn: apsw.Connection) -> None:
    """Execute all DDL statements to create the analysis.db schema.

    This is idempotent: every statement uses ``IF NOT EXISTS`` so it is
    safe to call on an already-initialized database.

    Creates tables, indexes, and FTS virtual tables — but NOT
    FTS sync triggers.  The triggers are installed after bulk
    ingestion via :func:`create_fts_triggers`, because firing a
    tokenising trigger on every bulk message insert is the
    dominant slowdown in the MESSAGES stage (roughly ~5x slower
    end-to-end with triggers live).

    After creating all DDL, this function writes the current
    ``SCHEMA_VERSION`` into the ``case_metadata`` table.

    Parameters
    ----------
    conn:
        An open ``apsw.Connection`` to the analysis database with
        write privileges.
    """
    cursor = conn.cursor()

    # Enable foreign keys for this connection.
    cursor.execute("PRAGMA foreign_keys = ON;")

    # Execute every DDL statement (tables + indexes + FTS virtual tables).
    for ddl in get_all_ddl():
        cursor.execute(ddl)

    # Record the schema version.
    cursor.execute(
        """
        INSERT OR REPLACE INTO case_metadata(key, value)
        VALUES ('schema_version', ?);
        """,
        (str(SCHEMA_VERSION),),
    )

    logger.info(
        "analysis.db schema created/verified (version %d): %d tables, "
        "%d indexes, %d FTS tables (triggers installed later).",
        SCHEMA_VERSION,
        len(TABLES),
        len(INDEXES),
        len(FTS_TABLES),
    )


def create_fts_triggers(conn: apsw.Connection) -> None:
    """Install the FTS5 sync triggers after bulk ingestion.

    These triggers keep ``message_fts`` in sync when rows in
    ``message`` are inserted / updated / deleted by the GUI
    (edits, tag writes, manual re-ingest).  They are NOT
    installed during schema creation because firing them on
    every bulk insert during ingestion costs roughly 5x more
    wall time than rebuilding the FTS index in one pass.

    Idempotent: all triggers use ``IF NOT EXISTS``.

    Parameters
    ----------
    conn:
        An open ``apsw.Connection`` to the analysis database with
        write privileges.
    """
    cursor = conn.cursor()
    for ddl in FTS_TRIGGERS.values():
        lines = ddl.strip().splitlines()
        sql_lines = [ln for ln in lines if not ln.strip().startswith("--")]
        cursor.execute("\n".join(sql_lines).strip())
    logger.info("Installed %d FTS sync triggers.", len(FTS_TRIGGERS))
