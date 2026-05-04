"""
WhatsApp ``key_id`` platform classifier.

Infers the sender device platform from message ``key_id``
patterns using multiple signals with confidence scoring:

  1. ``key_id`` length and prefix (always available).
  2. ``device_number`` from ``message_details`` (0 = primary,
     >0 = companion).
  3. Contact-level context: the ``is_business_api_bot`` flag.

Every classification returns ``(platform_label, confidence)``.
The caller should always display the confidence alongside the
label.
"""

# Prefix sets validated against real data with cross-referencing
_PRIMARY_IPHONE_PREFIXES_20 = frozenset({"3A", "5E", "4A"})
_COMPANION_PREFIXES_20 = frozenset({"3F", "3E", "3B"})
_MIXED_PREFIX_20 = frozenset({"2A"})  # ~95% primary iPhone, ~5% companion


def classify_keyid(
    key_id: str,
    from_me: bool = False,
    device_number: int | None = None,
    is_business_api_bot: bool = False,
) -> tuple[str, float]:
    """
    Classify a WhatsApp message key_id to determine platform.

    Args:
        key_id: The message key_id string from msgstore.db
        from_me: Whether this is a sent message (from device owner)
        device_number: Device number from message_details.author_device_jid
                       (0=primary phone, >0=companion). None if unavailable.
        is_business_api_bot: True if the contact is known to be a Business
                             Cloud API bot (sends exclusively 18-char key_ids).
                             Determined at contact level, not per-message.

    Returns:
        (platform_label, confidence) where:
        - platform_label: "android", "iphone", "companion", "android_linked",
          "iphone_linked", "business_api", "newsletter", "channel_bot", "unknown"
        - confidence: 0.0 to 1.0
    """
    if not key_id:
        return ("unknown", 0.0)

    length = len(key_id)
    prefix2 = key_id[:2].upper() if length >= 2 else ""
    prefix4 = key_id[:4].upper() if length >= 4 else ""

    # ---- Sent messages (from_me=1): from device owner's phone ----
    if from_me:
        if length == 32:
            return ("android", 0.99)
        if length <= 10 and key_id.isdigit():
            return ("android", 0.90)
        return ("android", 0.70)

    # ---- Newsletter / Channel system messages ----
    if length == 16:
        if prefix4 == "BAE5":
            return ("newsletter", 0.99)
        if prefix4 == "NXR5":
            return ("newsletter", 0.98)
        return ("newsletter", 0.70)

    if length == 22 and key_id.startswith("FTG-"):
        return ("channel_bot", 0.98)

    # ---- 18-char: Business API bot OR older companion format ----
    if length == 18:
        if is_business_api_bot:
            return ("business_api", 0.95)
        if device_number is not None and device_number > 0:
            # 18-char with device>0 = older companion-device key_id format
            return ("companion", 0.85)
        if device_number is not None and device_number == 0:
            # 18-char on device=0: could be business API bot or old format
            # Without contact-level context, we can't be sure
            return ("business_api", 0.60)
        # No device info at all: ambiguous
        return ("unknown", 0.50)

    # ---- 20-char: iPhone primary or companion ----
    if length == 20:
        if device_number is not None:
            if device_number == 0:
                if prefix2 in _PRIMARY_IPHONE_PREFIXES_20:
                    return ("iphone", 0.97)
                if prefix2 in _MIXED_PREFIX_20:
                    return ("iphone", 0.90)
                if prefix2 in _COMPANION_PREFIXES_20:
                    return ("iphone", 0.60)
                return ("iphone", 0.70)
            else:
                if prefix2 in _COMPANION_PREFIXES_20:
                    return ("companion", 0.95)
                if prefix2 in _PRIMARY_IPHONE_PREFIXES_20:
                    return ("iphone_linked", 0.90)
                if prefix2 in _MIXED_PREFIX_20:
                    return ("companion", 0.70)
                return ("companion", 0.65)
        else:
            if prefix2 in _PRIMARY_IPHONE_PREFIXES_20:
                return ("iphone", 0.85)
            if prefix2 in _COMPANION_PREFIXES_20:
                return ("companion", 0.80)
            if prefix2 in _MIXED_PREFIX_20:
                return ("iphone", 0.75)
            return ("unknown", 0.55)

    # ---- 22-char: Companion ----
    if length == 22:
        if prefix4 == "3EB0":
            return ("companion", 0.95)
        if prefix2 == "3E":
            return ("companion", 0.92)
        return ("companion", 0.70)

    # ---- 32-char: Android ----
    if length == 32:
        if device_number is not None and device_number > 0:
            return ("android_linked", 0.90)
        if prefix2 == "AC":
            return ("android", 0.97)
        return ("android", 0.90)

    # ---- 40-char: Companion (short-lived format) ----
    if length == 40:
        return ("companion", 0.75)

    # ---- Very old numeric IDs ----
    if length <= 10 and key_id.isdigit():
        return ("android", 0.70)

    return ("unknown", 0.30)


def detect_business_api_contacts(cursor, batch_key_ids: dict[int, list[str]]) -> set[int]:
    """
    Detect which ``sender_jid_row_id`` values are Business Cloud
    API bots.

    A contact is classified as a business API bot when their
    messages exclusively use 18-character ``key_id`` values
    (no other lengths).  This pattern is observed for every
    sample of ``TIER_2`` Meta Verified business accounts.

    Args:
        cursor: Reserved for future wa.db cross-checks.
        batch_key_ids: Dict mapping ``sender_jid_row_id`` →
            list of observed ``key_id`` strings.

    Returns:
        Set of ``sender_jid_row_id`` values identified as
        business API bots.
    """
    bot_senders = set()
    for sender_id, key_ids in batch_key_ids.items():
        if not key_ids or len(key_ids) < 3:
            continue
        lengths = {len(k) for k in key_ids}
        if lengths == {18}:
            bot_senders.add(sender_id)
    return bot_senders


def platform_label_display(platform: str) -> str:
    """Human-readable label for display in UI."""
    return {
        "android": "Android",
        "iphone": "iPhone",
        "android_linked": "Android (Linked)",
        "iphone_linked": "iPhone (Linked)",
        "companion": "Web/Desktop",
        "business_api": "Business API",
        "newsletter": "Newsletter",
        "channel_bot": "Channel",
        "unknown": "",
    }.get(platform, platform)


def platform_short_label(platform: str) -> str:
    """Short label for chat bubble indicator near timestamp."""
    return {
        "android": "Android",
        "iphone": "iPhone",
        "android_linked": "Android",
        "iphone_linked": "iPhone",
        "companion": "Web",
        "business_api": "Bot",
        "newsletter": "",
        "channel_bot": "",
        "unknown": "",
    }.get(platform, "")
