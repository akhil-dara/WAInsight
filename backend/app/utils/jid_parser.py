"""
WhatsApp JID (Jabber ID) parsing and classification utilities.

JID format follows XMPP conventions used by WhatsApp:
- Individual: phone@s.whatsapp.net (e.g., 15551234567@s.whatsapp.net)
- Group: creatorphone-timestamp@g.us (e.g., 15551234567-1445147498@g.us)
- Broadcast: timestamp@broadcast (e.g., 1692511874@broadcast)
- Status: status@broadcast
- Newsletter: channelid@newsletter
- LID: localid@lid (e.g., 22621592797324@lid)
- Bot: botid@bot
- Device: user.agent:device@server

Confirmed via JADX decompile of WhatsApp APK (Jid.java, DeviceJid.java).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.models.enums import JidServer, JidType, ChatType


@dataclass(frozen=True)
class ParsedJid:
    """Parsed WhatsApp JID with extracted components."""

    raw: str
    """Full JID string as stored in database."""

    user: str
    """User/identifier part before @."""

    server: str
    """Server part after @."""

    agent: int = 0
    """Agent number (0=primary, 1+=linked devices)."""

    device: int = 0
    """Device number within agent."""

    @property
    def is_individual(self) -> bool:
        """True if this is an individual phone contact."""
        return self.server == JidServer.WHATSAPP

    @property
    def is_group(self) -> bool:
        """True if this is a group conversation."""
        return self.server == JidServer.GROUP

    @property
    def is_broadcast(self) -> bool:
        """True if this is a broadcast list."""
        return self.server == JidServer.BROADCAST

    @property
    def is_status(self) -> bool:
        """True if this is the status broadcast."""
        return self.raw == "status@broadcast"

    @property
    def is_newsletter(self) -> bool:
        """True if this is a newsletter/channel."""
        return self.server == JidServer.NEWSLETTER

    @property
    def is_lid(self) -> bool:
        """True if this is a Local ID (WhatsApp's new privacy system)."""
        return self.server in (JidServer.LID, JidServer.HOSTED_LID)

    @property
    def is_bot(self) -> bool:
        """True if this is a bot account."""
        return self.server == JidServer.BOT

    @property
    def is_device_jid(self) -> bool:
        """True if this represents a specific device, not a user."""
        return self.agent > 0 or self.device > 0

    @property
    def phone_number(self) -> Optional[str]:
        """Extract phone number from individual JID, or None."""
        if self.is_individual:
            return self.user
        return None

    @property
    def chat_type(self) -> ChatType:
        """Determine chat type from JID server."""
        if self.is_individual or self.is_lid:
            return ChatType.PERSONAL
        if self.is_group:
            return ChatType.GROUP
        if self.is_broadcast:
            return ChatType.BROADCAST
        if self.is_newsletter:
            return ChatType.NEWSLETTER
        if self.is_status:
            return ChatType.STATUS
        return ChatType.PERSONAL


def parse_jid(raw_string: str | None) -> Optional[ParsedJid]:
    """Parse a WhatsApp JID string into its components.

    Handles standard JIDs and device JIDs with agent:device notation.

    Args:
        raw_string: Full JID string (e.g., '15551234567@s.whatsapp.net').

    Returns:
        ParsedJid instance or None if input is empty/invalid.

    Examples:
        >>> parse_jid('15551234567@s.whatsapp.net')
        ParsedJid(raw='15551234567@s.whatsapp.net', user='15551234567', server='s.whatsapp.net')

        >>> parse_jid('15551234567-1445147498@g.us')
        ParsedJid(raw='15551234567-1445147498@g.us', user='15551234567-1445147498', server='g.us')

        >>> parse_jid('22621592797324@lid')
        ParsedJid(raw='22621592797324@lid', user='22621592797324', server='lid')
    """
    if not raw_string or "@" not in raw_string:
        return None

    # Split into user@server
    at_idx = raw_string.index("@")
    user_part = raw_string[:at_idx]
    server = raw_string[at_idx + 1:]

    # Handle device JID format: user.agent:device@server
    agent = 0
    device = 0

    if ":" in user_part:
        # Device JID: has agent:device prefix
        base, device_str = user_part.rsplit(":", 1)
        try:
            device = int(device_str)
        except ValueError:
            device = 0
        user_part = base

    if "." in user_part and server in (JidServer.WHATSAPP, JidServer.LID, JidServer.HOSTED_LID):
        # Agent notation: user.agent
        parts = user_part.rsplit(".", 1)
        if len(parts) == 2 and parts[1].isdigit():
            user_part = parts[0]
            agent = int(parts[1])

    return ParsedJid(
        raw=raw_string,
        user=user_part,
        server=server,
        agent=agent,
        device=device,
    )


def extract_phone_number(jid_or_raw: str | None) -> Optional[str]:
    """Extract phone number from a JID string.

    Args:
        jid_or_raw: JID string or raw phone number.

    Returns:
        Phone number string or None.
    """
    if not jid_or_raw:
        return None

    parsed = parse_jid(jid_or_raw)
    if parsed and parsed.phone_number:
        return parsed.phone_number

    # Maybe it's already a raw number
    if jid_or_raw.isdigit():
        return jid_or_raw

    return None


def classify_jid_type(jid_type: int | None, server: str | None) -> str:
    """Classify a JID based on its type and server fields.

    Args:
        jid_type: Integer type from jid.type column.
        server: Server string from jid.server column.

    Returns:
        Human-readable classification string.
    """
    if server == JidServer.WHATSAPP:
        return "individual"
    if server == JidServer.GROUP:
        return "group"
    if server == JidServer.BROADCAST:
        return "broadcast"
    if server == JidServer.NEWSLETTER:
        return "newsletter"
    if server == JidServer.BOT:
        return "bot"
    if server in (JidServer.LID, JidServer.HOSTED_LID):
        if jid_type == JidType.LID_USER:
            return "lid_user"
        if jid_type == JidType.LID_DEVICE:
            return "lid_device"
        return "lid_unknown"
    return "unknown"


def is_user_jid(jid_type: int | None, server: str | None) -> bool:
    """Check if a JID represents a real user (not a device or group).

    Used during contact resolution to identify which JIDs
    should be created as contact records.
    """
    if server == JidServer.WHATSAPP and jid_type == JidType.USER:
        return True
    if server == JidServer.LID and jid_type == JidType.LID_USER:
        return True
    return False


def format_phone_display(phone_number: str | None) -> str:
    """Format a phone number for display.

    Adds the ``+`` prefix and inserts spaces for readability
    (e.g. ``+91 NNNNN NNNNN`` for a 12-digit Indian number).

    Args:
        phone_number: Raw phone number digits.

    Returns:
        Formatted phone string.
    """
    if not phone_number:
        return "Unknown"

    num = phone_number.lstrip("+")

    # Indian numbers: +91 XXXXX XXXXX
    if num.startswith("91") and len(num) == 12:
        return f"+{num[:2]} {num[2:7]} {num[7:]}"

    # US numbers: +1 XXX XXX XXXX
    if num.startswith("1") and len(num) == 11:
        return f"+{num[0]} {num[1:4]} {num[4:7]} {num[7:]}"

    # Generic: +CC XXXXXXXX
    if len(num) > 4:
        return f"+{num[:2]} {num[2:]}"

    return f"+{num}"
