"""
WhatsApp message types, JID types, and other enumeration constants.

Values confirmed via JADX decompilation of WhatsApp APK sources.

References:
- APK source: IGJ.java (message type constants)
- APK source: Jid.java, DeviceJid.java (JID type system)
- APK source: AbstractC41743IjA.java (mention type constants)
- APK source: messaging_event.proto (SDKPlatform enum)
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class MessageType(IntEnum):
    """WhatsApp message type codes from message.message_type column.

    Confirmed via JADX decompilation of the WhatsApp APK
    (IGJ.java) and cross-checked against real msgstore.db data.
    """

    TEXT = 0                    # Plain text / emoji message
    IMAGE = 1                  # Image attachment
    AUDIO = 2                  # Audio file / voice note
    VIDEO = 3                  # Video file
    VCARD = 4                  # Contact card (single)
    LOCATION = 5               # One-time location share (static)
    SYSTEM = 7                 # System message - group changes, security, etc.
    DOCUMENT = 9               # Document file - PDF, DOCX, etc.
    MISSED_CALL = 10           # Missed / unanswered call log
    PENDING = 11               # Pending / undelivered — "Waiting for this message"
    ANIMATED_GIF = 13          # GIF sent as MP4
    VCARD_LIST = 14            # Multiple contact cards shared
    DELETED = 15               # "This message was deleted" placeholder
    LIVE_LOCATION = 16         # Live location sharing with updates
    STICKER = 20               # Sticker / avatar image (WebP)
    GROUP_INVITE_V2 = 23       # Group invite (variant 2)
    GROUP_INVITE_TEXT = 24     # Group invite with text description
    LIST_MESSAGE = 25          # Interactive list message (business)
    LIST_REPLY = 26            # Reply to a list message
    BUTTON_MESSAGE = 27        # Interactive button message (business)
    WHATSAPP_OFFICIAL = 28     # Official WhatsApp message / product catalog
    GROUP_INVITE = 32          # Group invite link
    EPHEMERAL_SETTING = 36     # Ephemeral timer activation / deactivation
    VIEW_ONCE_IMAGE = 42       # View-once image
    VIEW_ONCE_VIDEO = 43       # View-once video
    INTERACTIVE = 45           # Interactive message
    POLL_VOTE = 46             # Poll vote
    INTERACTIVE_CTA = 49       # Call-to-action interactive
    INTERACTIVE_CAROUSEL = 55  # Carousel message
    EPHEMERAL_SYNC = 57        # Ephemeral sync response
    INTERACTIVE_PRODUCT = 62   # Product message
    ADMIN_REVOKE = 64          # Admin-deleted group message (deleted-for-all by admin)
    POLL = 66                  # Poll question message
    VIDEO_NOTE = 81            # Video note / circular video message
    VIEW_ONCE_VOICE = 82       # View-once voice note
    BOT_FEEDBACK = 88          # Meta AI bot feedback
    CALL_LOG = 90              # Call / video call log entry
    SCHEDULED_EVENT = 92       # Scheduled event in group
    CHANNEL_ADMIN_INVITE = 94  # Channel admin invitation
    ALBUM = 99                 # Multi-image / multi-video album
    STATUS_MENTION = 103       # Status mention notification
    ADVANCED_CHAT_PRIVACY = 112  # Advanced chat privacy activation / deactivation
    STATUS_UPDATE = 116        # Status update message


# Human-readable labels for each message type
MESSAGE_TYPE_LABELS: dict[int, str] = {
    MessageType.TEXT: "text",
    MessageType.IMAGE: "image",
    MessageType.AUDIO: "audio",
    MessageType.VIDEO: "video",
    MessageType.VCARD: "vcard",
    MessageType.LOCATION: "location",
    MessageType.SYSTEM: "system",
    MessageType.DOCUMENT: "document",
    MessageType.MISSED_CALL: "missed_call",
    MessageType.PENDING: "pending",
    MessageType.ANIMATED_GIF: "gif",
    MessageType.VCARD_LIST: "vcard_list",
    MessageType.DELETED: "deleted",
    MessageType.LIVE_LOCATION: "live_location",
    MessageType.STICKER: "sticker",
    MessageType.GROUP_INVITE_V2: "group_invite",
    MessageType.GROUP_INVITE_TEXT: "group_invite",
    MessageType.LIST_MESSAGE: "list_message",
    MessageType.LIST_REPLY: "list_reply",
    MessageType.BUTTON_MESSAGE: "button_message",
    MessageType.WHATSAPP_OFFICIAL: "whatsapp_official",
    MessageType.GROUP_INVITE: "group_invite",
    MessageType.EPHEMERAL_SETTING: "ephemeral_setting",
    MessageType.VIEW_ONCE_IMAGE: "view_once_image",
    MessageType.VIEW_ONCE_VIDEO: "view_once_video",
    MessageType.INTERACTIVE: "interactive",
    MessageType.POLL_VOTE: "poll_vote",
    MessageType.INTERACTIVE_CTA: "interactive_cta",
    MessageType.INTERACTIVE_CAROUSEL: "carousel",
    MessageType.EPHEMERAL_SYNC: "ephemeral_sync",
    MessageType.INTERACTIVE_PRODUCT: "interactive_product",
    MessageType.ADMIN_REVOKE: "admin_revoke",
    MessageType.POLL: "poll",
    MessageType.VIDEO_NOTE: "video_note",
    MessageType.VIEW_ONCE_VOICE: "view_once_voice",
    MessageType.BOT_FEEDBACK: "bot_feedback",
    MessageType.CALL_LOG: "call_log",
    MessageType.SCHEDULED_EVENT: "scheduled_event",
    MessageType.CHANNEL_ADMIN_INVITE: "channel_admin_invite",
    MessageType.ALBUM: "album",
    MessageType.STATUS_MENTION: "status_mention",
    MessageType.ADVANCED_CHAT_PRIVACY: "advanced_chat_privacy",
    MessageType.STATUS_UPDATE: "status_update",
}

# Message types that contain media attachments
MEDIA_MESSAGE_TYPES: frozenset[int] = frozenset({
    MessageType.IMAGE, MessageType.AUDIO, MessageType.VIDEO,
    MessageType.DOCUMENT, MessageType.ANIMATED_GIF,
    MessageType.STICKER, MessageType.ALBUM,
})


def get_type_label(message_type: int) -> str:
    """Get human-readable label for a message type code.

    Returns 'unknown_{code}' for unrecognized types to preserve data.
    """
    return MESSAGE_TYPE_LABELS.get(message_type, f"unknown_{message_type}")


class SystemActionType(IntEnum):
    """WhatsApp system message action types from message_system.action_type.

    Unknown types are preserved with their raw integer values.
    """

    UNKNOWN_1 = 1
    UNKNOWN_2 = 2
    GROUP_CREATED = 4                      # New group created
    GROUP_ICON_CHANGED = 5                 # Group photo changed
    GROUP_DESCRIPTION_CHANGED_V1 = 6       # Group description changed v1
    PARTICIPANT_JOINED = 11                # Member joined group
    PARTICIPANT_LEFT = 12                  # Member left group
    GROUP_SUBJECT_CHANGED = 13             # Group name changed
    GROUP_DESCRIPTION_CHANGED_V2 = 14      # Group description changed v2
    GROUP_ICON_CHANGED_V2 = 15             # Group photo changed v2 with blobs
    SECURITY_CODE_CHANGED = 18             # Contact security code changed
    CONTACT_BLOCKED = 20                   # Contact blocked
    CONTACT_UNBLOCKED = 21                 # Contact unblocked
    GROUP_CALL_ENDED = 27                  # Voice/video call ended in group
    GROUP_CALL_STARTED = 28                # Voice/video call started in group
    PRIVACY_CHANGED = 29                   # Privacy setting changed
    PRIVACY_CHANGED_V2 = 30                # Privacy setting changed v2
    PRIVACY_CHANGED_V3 = 31                # Privacy setting changed v3
    PRIVACY_CHANGED_V4 = 32                # Privacy setting changed v4
    BROADCAST_LIST = 56                    # Broadcast list message
    NOTIFICATION_SETTINGS = 58             # Notification settings changed
    EPHEMERAL_ENABLED = 67                 # Disappearing messages enabled
    EPHEMERAL_DISABLED = 68                # Disappearing messages disabled
    DEVICE_LINKED = 69                     # New device linked to account
    NUMBER_CHANGED = 79                    # Phone number changed
    GROUP_PARTICIPANT_DEMOTED = 81         # Admin demoted
    GROUP_PARTICIPANT_PROMOTED = 83        # Admin promoted
    GROUP_MEMBER_DEMOTED = 84              # Member demoted
    GROUP_MEMBER_PROMOTED = 85             # Member promoted
    REMINDER_SETUP = 91                    # Reminder scheduled
    REMINDER_SENT = 92                     # Reminder notification sent
    SCHEDULED_CALL_START = 99              # Scheduled call started
    BIZ_CATALOG_INFO = 108                 # Business catalog info
    BIZ_CALLBACK_ENABLED = 109             # Business callback enabled
    BIZ_CALLBACK_DISABLED = 110            # Business callback disabled
    BIZ_OPT_OUT = 111                      # Business opt-out
    EPHEMERAL_UNSUPPORTED = 115            # Disappearing not supported
    USERNAME_CHANGED = 116                 # WhatsApp username changed
    DEVICE_CHANGED = 118                   # Device added/removed
    PRIVACY_CHANGED_V5 = 120              # Privacy setting changed v5
    CHAT_ASSIGNMENT = 129                  # Chat assigned to agent
    MESSAGES_TIMEDOUT = 10                 # Messages expired


# Human-readable labels for system action types.
# Verified against JADX-decompiled WhatsApp APK + cross-checked
# with WhatsApp's own phone display.  Each label below describes
# the on-phone UI string the action_type produces.
SYSTEM_ACTION_LABELS: dict[int, str] = {
    1: "group_subject_changed",              # "X changed the group subject from A to B"
    12: "participant_added",
    13: "participant_left",
    14: "participant_removed",
    18: "security_code_changed",             # Personal chats: "Your security code with X changed"
    20: "participant_joined_via_link",        # "X joined using a group link"
    67: "e2e_encrypted",                     # "Messages are end-to-end encrypted"
    83: "membership_approval_request",       # "X requested to join" (value_change='invite_link')
    84: "admin_promoted",                    # "X was made an admin"
    134: "channel_created",                  # "This channel was created"

    5: "participant_left",                   # "X left"
    6: "group_icon_changed",                 # Source has photo in message_system_photo_change; text_data is Unix timestamp.
    10: "number_changed",                    # "X changed to Y"
    11: "community_or_group_created",        # "X created community/group Y"
    15: "you_are_admin",                     # "You're an admin"
    21: "group_link_reset",                  # "X reset the group link"
    27: "group_description_changed",         # "X changed the group description"
    28: "number_changed",                    # "X changed their phone number"
    29: "group_add_member_permission",       # "changed settings to allow X to add others"
    30: "group_edit_permission",             # "changed settings so X can edit group settings"
    32: "group_send_message_permission",     # "allow all members to send messages"
    56: "disappearing_timer_updated",        # "updated the message timer...N days"
    58: "contact_blocked",                   # "You blocked this contact/business"
    59: "disappearing_timer_changed",        # "message timer was updated...24h" (personal chats)
    63: "business_meta_managed",             # "using a secure service from Meta"
    69: "business_meta_managed",             # "using a secure service from Meta"
    79: "participant_joined_community",      # "X joined from the community"
    81: "community_admin_changed",           # Polarity varies — "X is not admin" / "X is now admin"
    85: "group_join_permission",             # "turned off admin permission to join"
    91: "group_add_permission_all",          # "allow all members to add others"
    92: "group_add_permission_admins",       # "allow only admins to add others"
    99: "community_joined",                  # "You joined from the community"
    109: "subgroup_removed",                 # "Group X was removed"
    110: "subgroup_added",                   # "X added the group Y"
    111: "subgroup_removed",                 # "Group X was removed"
    116: "subgroup_unlinked",                # "X removed this group from community Y"
    118: "message_pinned",                   # "X pinned a message"
    120: "participant_added_with_approval",   # "X added Y. Tap to change member permissions"
    126: "community_group_joined",           # "You joined a group via invite in community"
    131: "community_description_changed",    # "X changed the community description"
    132: "channel_privacy_notice",           # "channel has added privacy..."
    133: "channel_deleted",                  # "The channel X was deleted"
    138: "subgroup_removed",                 # "X removed the group Y"
    142: "group_auto_admin_restriction",     # "group has 256+ members, only admins can edit"
    146: "meta_ai_disclaimer",               # "Only messages that mention @AI can be read by Meta"
    149: "community_linked_group_join",      # "Welcome to the group...You joined this group"
    158: "business_meta_managed",             # "using a secure service from Meta"
    167: "community_created",                # "X created community Y"
    169: "event_updated",                    # "X updated EVENT_NAME"
    173: "community_owner_changed",          # "Community owner has changed"
    188: "group_invite_permission",          # "allow all members to add others / group link invites"
    189: "group_link_reset",                 # "X reset the group link"

    2: "unknown_action_2",                   # Not observed in any sample case
    4: "you_were_added",                     # "You were added" (groups/communities)
    31: "default_disappearing_timer",        # Groups only — actor sets group default timer
    68: "disappearing_messages_changed",     # Personal only — contact uses default timer for disappearing messages
    107: "community_add_permission",         # "X changed community settings to allow only admins to add"
    108: "subgroup_added",                   # "X added the group Y" — paired with action_type 110 (carries child group node in message_system_with_group_nodes)
    115: "phone_number_privacy",             # "This chat has added privacy for your phone number"
    123: "community_welcome_joined",         # "Welcome to the community! You joined this community"
    125: "community_group_invite_joined",    # "You joined a group via invite in the community: X"
    128: "subgroup_linked",                 # "X added this group to the community Y"
    129: "contact_card_shown",               # Contact card with Block/Add (new chat intro)
    144: "community_auto_added",             # "You were added because one of your groups was added to this community"
    156: "ai_disclaimer",                    # AI-generated content disclaimer
    194: "unknown_action_194",               # Rare; not consistently observable
}


def get_system_action_label(action_type: int) -> str:
    """Get human-readable label for a system action type."""
    return SYSTEM_ACTION_LABELS.get(action_type, f"unknown_action_{action_type}")


class JidType(IntEnum):
    """WhatsApp JID types from jid.type column.

    Mapping confirmed against msgstore.db jid table:
      type 0  = phone user on s.whatsapp.net
      type 1  = group on g.us
      type 17 = phone device JID on s.whatsapp.net (agent>0 or device>0)
      type 18 = LID user on lid server
      type 19 = LID device on lid server
      type 21 = newsletter channel
      type 25 = hosted LID
      type 26 = bot account
    """

    USER = 0               # Individual phone contact on s.whatsapp.net
    GROUP = 1              # Group conversation on g.us
    PHONE_DEVICE = 17      # Phone device JID (agent/device variant of type 0)
    LID_USER = 18          # LID user entry on lid server
    LID_DEVICE = 19        # LID device entry on lid server
    NEWSLETTER = 21        # Newsletter channel
    HOSTED_LID = 25        # Hosted LID
    BOT = 26               # Bot account


class JidServer(StrEnum):
    """WhatsApp JID server suffixes."""

    WHATSAPP = "s.whatsapp.net"   # Individual users
    GROUP = "g.us"                 # Groups
    BROADCAST = "broadcast"        # Broadcast lists
    LID = "lid"                    # Local IDs
    NEWSLETTER = "newsletter"      # Newsletter channels
    BOT = "bot"                    # Bot accounts
    HOSTED_LID = "hosted.lid"      # Hosted LIDs
    STATUS_ME = "status_me"        # Self status
    LID_ME = "lid_me"             # Self LID


class ChatType(StrEnum):
    """Conversation types in the normalized analysis.db."""

    PERSONAL = "personal"
    GROUP = "group"
    COMMUNITY = "community"
    BROADCAST = "broadcast"
    NEWSLETTER = "newsletter"
    STATUS = "status"


class GroupType(IntEnum):
    """WhatsApp group_type values from chat table."""

    REGULAR = 0            # Regular personal/group chat
    COMMUNITY_ANNOUNCE = 1 # Community announcement group
    COMMUNITY_SUBGROUP = 2 # Community sub-group
    COMMUNITY_META = 3     # Community meta/admin group
    NEWSLETTER = 4         # Newsletter channel
    COMMUNITY_DEFAULT = 6  # Default community "General" sub-group


class MentionType(IntEnum):
    """Mention types from message_mentions.mention_type.

    Confirmed via JADX decompile (AbstractC41743IjA.java, MentionableEntry.java).
    """

    REGULAR = 0   # Regular @mention of a specific person
    GROUP = 1     # Group mention
    ALL = 2       # @all - mention everyone in group


class CallResult(IntEnum):
    """Call result codes from ``call_log.call_result``."""

    CONNECTED = 0        # initial connection / ringing state (often 0 duration)
    UNANSWERED = 2       # no answer / missed
    UNAVAILABLE = 3      # recipient unavailable
    REJECTED = 4         # rejected by all participants
    ANSWERED = 5         # call answered and ended normally (most common for successful calls)
    BUSY = 7             # recipient busy
    JOINED_VOICE_CHAT = 8  # user joined a voice chat


CALL_RESULT_LABELS: dict[int, str] = {
    0: "connected",
    2: "missed",
    3: "unavailable",
    4: "rejected",
    5: "answered",
    7: "busy",
    8: "joined_voice_chat",
}


# Participant-level call_result (call_log_participant_v2.call_result).
PARTICIPANT_CALL_RESULT_LABELS: dict[int, str] = {
    0: "joined",
    2: "rejected_or_no_answer",
    5: "initiated_or_disconnected",
}


def get_call_result_label(result: int) -> str:
    """Get human-readable label for a call result code."""
    return CALL_RESULT_LABELS.get(result, f"unknown_{result}")


def get_participant_call_result_label(result: int) -> str:
    """Get human-readable label for a participant-level call result code."""
    return PARTICIPANT_CALL_RESULT_LABELS.get(result, f"unknown_{result}")


class SDKPlatform(IntEnum):
    """WhatsApp SDK platform identifiers.

    Confirmed from messaging_event.proto in decompiled APK.
    """

    UNKNOWN_OS = 0
    ANDROID = 1
    IOS = 2
    WEB = 3


class EphemeralDuration(IntEnum):
    """Standard disappearing message durations in seconds."""

    OFF = 0
    TWENTY_FOUR_HOURS = 86_400
    SEVEN_DAYS = 604_800
    NINETY_DAYS = 7_776_000


EPHEMERAL_DURATION_LABELS: dict[int, str] = {
    0: "off",
    86_400: "24 hours",
    604_800: "7 days",
    7_776_000: "90 days",
}


class ViewOnceState(IntEnum):
    """View-once media states from message_view_once_media.state."""

    UNSEEN = 0
    SEEN = 1
    EXPIRED = 2


class ReceiptStatus(IntEnum):
    """Message receipt status levels."""

    SENT = 0
    DELIVERED = 1
    READ = 2
    PLAYED = 3
