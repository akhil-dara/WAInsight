"""Application-wide configuration and constants."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QDate, Signal

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Windows + Python 3.14 ships zoneinfo but NOT the IANA database, so
# `ZoneInfo("UTC")` raises ZoneInfoNotFoundError unless the user has
# `pip install tzdata`.  Provide a robust fallback that:
#   1. Always uses datetime.timezone.utc for "UTC" / "Etc/UTC".
#   2. Tries ZoneInfo(name) for everything else.
#   3. Falls back to a fixed UTC offset built from a hand-coded table
#      of the IANA names we surface in the timezone picker.
#   4. Last resort: datetime.timezone.utc with a warning.
def _safe_zoneinfo(name: str):
    if not name or name in ("UTC", "Etc/UTC", "Universal", "GMT"):
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Hand-coded fallback for the cities we expose in IANA_TIMEZONES.
        # Maps name -> minutes-east-of-UTC.  Doesn't honour DST (would
        # require the actual tzdata for that), but gives the user a
        # working clock until they `pip install tzdata`.
        fixed_offsets = {
            "Asia/Kolkata": 330, "Asia/Calcutta": 330,
            "Asia/Karachi": 300, "Asia/Dubai": 240, "Asia/Tehran": 210,
            "Asia/Kabul": 270, "Asia/Dhaka": 360, "Asia/Yangon": 390,
            "Asia/Bangkok": 420, "Asia/Singapore": 480, "Asia/Hong_Kong": 480,
            "Asia/Shanghai": 480, "Asia/Tokyo": 540, "Asia/Seoul": 540,
            "Asia/Manila": 480, "Asia/Jakarta": 420, "Asia/Riyadh": 180,
            "Asia/Jerusalem": 120, "Asia/Tashkent": 300, "Asia/Almaty": 360,
            "Australia/Sydney": 600, "Australia/Melbourne": 600,
            "Australia/Brisbane": 600, "Australia/Perth": 480,
            "Pacific/Auckland": 720, "Pacific/Honolulu": -600,
            "Europe/London": 0, "Europe/Dublin": 0, "Europe/Lisbon": 0,
            "Europe/Paris": 60, "Europe/Berlin": 60, "Europe/Madrid": 60,
            "Europe/Rome": 60, "Europe/Amsterdam": 60, "Europe/Stockholm": 60,
            "Europe/Vienna": 60, "Europe/Prague": 60, "Europe/Warsaw": 60,
            "Europe/Zurich": 60, "Europe/Brussels": 60, "Europe/Athens": 120,
            "Europe/Helsinki": 120, "Europe/Bucharest": 120, "Europe/Istanbul": 180,
            "Europe/Moscow": 180, "Africa/Cairo": 120, "Africa/Lagos": 60,
            "Africa/Johannesburg": 120, "Africa/Nairobi": 180,
            "Africa/Casablanca": 60,
            "America/New_York": -300, "America/Chicago": -360,
            "America/Denver": -420, "America/Los_Angeles": -480,
            "America/Phoenix": -420, "America/Anchorage": -540,
            "America/Toronto": -300, "America/Vancouver": -480,
            "America/Halifax": -240, "America/St_Johns": -210,
            "America/Mexico_City": -360, "America/Sao_Paulo": -180,
            "America/Buenos_Aires": -180, "America/Bogota": -300,
            "America/Lima": -300, "America/Caracas": -240,
            "US/Eastern": -300, "US/Central": -360, "US/Mountain": -420,
            "US/Pacific": -480, "US/Alaska": -540, "US/Hawaii": -600,
        }
        mins = fixed_offsets.get(name)
        if mins is not None:
            return timezone(timedelta(minutes=mins), name=name)
        # Unknown name - degrade to UTC quietly.  Better than crashing.
        return timezone.utc


# --- Timezone support (IANA-based) ---

# Common IANA timezone definitions: (iana_name, abbreviation)
# Used by settings page and View menu for display.
IANA_TIMEZONES: list[tuple[str, str]] = [
    ("UTC", "UTC"),
    # Americas
    ("US/Eastern", "EST/EDT"),
    ("US/Central", "CST/CDT"),
    ("US/Mountain", "MST/MDT"),
    ("US/Pacific", "PST/PDT"),
    ("US/Alaska", "AKST/AKDT"),
    ("US/Hawaii", "HST"),
    ("America/New_York", "EST/EDT"),
    ("America/Chicago", "CST/CDT"),
    ("America/Denver", "MST/MDT"),
    ("America/Los_Angeles", "PST/PDT"),
    ("America/Anchorage", "AKST/AKDT"),
    ("America/Phoenix", "MST"),
    ("America/Toronto", "EST/EDT"),
    ("America/Vancouver", "PST/PDT"),
    ("America/Mexico_City", "CST"),
    ("America/Sao_Paulo", "BRT"),
    ("America/Argentina/Buenos_Aires", "ART"),
    ("America/Bogota", "COT"),
    ("America/Lima", "PET"),
    ("America/Santiago", "CLT"),
    # Europe
    ("Europe/London", "GMT/BST"),
    ("Europe/Paris", "CET/CEST"),
    ("Europe/Berlin", "CET/CEST"),
    ("Europe/Madrid", "CET/CEST"),
    ("Europe/Rome", "CET/CEST"),
    ("Europe/Amsterdam", "CET/CEST"),
    ("Europe/Zurich", "CET/CEST"),
    ("Europe/Stockholm", "CET/CEST"),
    ("Europe/Athens", "EET/EEST"),
    ("Europe/Istanbul", "TRT"),
    ("Europe/Moscow", "MSK"),
    ("Europe/Kiev", "EET/EEST"),
    ("Europe/Warsaw", "CET/CEST"),
    # Asia
    ("Asia/Kolkata", "IST"),
    ("Asia/Dubai", "GST"),
    ("Asia/Karachi", "PKT"),
    ("Asia/Dhaka", "BST"),
    ("Asia/Kathmandu", "NPT"),
    ("Asia/Bangkok", "ICT"),
    ("Asia/Ho_Chi_Minh", "ICT"),
    ("Asia/Jakarta", "WIB"),
    ("Asia/Singapore", "SGT"),
    ("Asia/Shanghai", "CST"),
    ("Asia/Hong_Kong", "HKT"),
    ("Asia/Taipei", "CST"),
    ("Asia/Tokyo", "JST"),
    ("Asia/Seoul", "KST"),
    ("Asia/Riyadh", "AST"),
    ("Asia/Tehran", "IRST"),
    # Oceania
    ("Australia/Sydney", "AEST/AEDT"),
    ("Australia/Melbourne", "AEST/AEDT"),
    ("Australia/Brisbane", "AEST"),
    ("Australia/Perth", "AWST"),
    ("Australia/Adelaide", "ACST/ACDT"),
    ("Pacific/Auckland", "NZST/NZDT"),
    ("Pacific/Fiji", "FJT"),
    # Africa
    ("Africa/Cairo", "EET"),
    ("Africa/Johannesburg", "SAST"),
    ("Africa/Lagos", "WAT"),
    ("Africa/Nairobi", "EAT"),
]


def _detect_system_timezone() -> str:
    """Detect the system's IANA timezone name. Falls back to UTC."""
    try:
        import time as _time
        # Python 3.9+: try tzname approach via datetime
        local_tz = datetime.now().astimezone().tzinfo
        # ZoneInfo objects have a .key attribute
        if hasattr(local_tz, "key"):
            return local_tz.key
        # On Windows, try tzlocal or manual mapping
        try:
            import tzlocal  # type: ignore
            return str(tzlocal.get_localzone())
        except ImportError:
            pass
        # Last resort: match offset
        offset_sec = -_time.timezone if _time.daylight == 0 else -_time.altzone
        for iana_name, _ in IANA_TIMEZONES:
            try:
                zi = _safe_zoneinfo(iana_name)
                dt_now = datetime.now(tz=zi)
                if dt_now.utcoffset() and dt_now.utcoffset().total_seconds() == offset_sec:
                    return iana_name
            except Exception:
                continue
    except Exception:
        pass
    return "UTC"


class _TimezoneNotifier(QObject):
    timezone_changed = Signal(str)


_tz_iana_name: str = _detect_system_timezone()
_timezone_notifier = _TimezoneNotifier()


def get_timezone_notifier() -> _TimezoneNotifier:
    return _timezone_notifier


def set_timezone(iana_name: str) -> None:
    """Set the analysis timezone by IANA name (e.g. 'Asia/Kolkata')."""
    global _tz_iana_name
    if not iana_name or iana_name == _tz_iana_name:
        return
    _tz_iana_name = iana_name
    _timezone_notifier.timezone_changed.emit(iana_name)


def get_timezone_name() -> str:
    """Return the current IANA timezone name."""
    return _tz_iana_name


def get_tz():
    """Get the current analysis timezone as a ZoneInfo (or fixed-offset
    fallback when the IANA db is unavailable - e.g. Python 3.14 on
    Windows without `pip install tzdata`)."""
    return _safe_zoneinfo(_tz_iana_name)


def get_timezone_display(iana_name: str) -> str:
    """Build a display string like 'Asia/Kolkata (IST, UTC+5:30)' for a given IANA name."""
    try:
        zi = _safe_zoneinfo(iana_name)
        dt_now = datetime.now(tz=zi)
        utc_off = dt_now.utcoffset()
        total_sec = int(utc_off.total_seconds()) if utc_off else 0
        sign = "+" if total_sec >= 0 else "-"
        h, rem = divmod(abs(total_sec), 3600)
        m = rem // 60
        offset_str = f"UTC{sign}{h}" + (f":{m:02d}" if m else "")

        # Find abbreviation from our list
        abbr = dt_now.strftime("%Z") or ""
        for entry_name, entry_abbr in IANA_TIMEZONES:
            if entry_name == iana_name:
                abbr = entry_abbr
                break

        return f"{iana_name} ({abbr}, {offset_str})"
    except Exception:
        return iana_name


def get_current_timezone_display() -> str:
    return get_timezone_display(get_timezone_name())


def get_timezone_abbreviation(iana_name: str | None = None) -> str:
    try:
        zi = _safe_zoneinfo(iana_name or _tz_iana_name)
        return datetime.now(tz=zi).strftime("%Z") or "LOCAL"
    except Exception:
        return "LOCAL"


def timestamp_to_local_datetime(ts_ms: int | float):
    ts_val = int(ts_ms)
    return datetime.fromtimestamp(ts_val / 1000, tz=get_tz())


def timestamp_to_utc_datetime(ts_ms: int | float):
    ts_val = int(ts_ms)
    return datetime.fromtimestamp(ts_val / 1000, tz=timezone.utc)


def timestamp_to_qdate(ts_ms: int | float) -> QDate:
    dt = timestamp_to_local_datetime(ts_ms)
    return QDate(dt.year, dt.month, dt.day)


def date_range_to_timestamps(date_from, date_to) -> tuple[int | None, int | None]:
    if not date_from or not date_to:
        return None, None
    start_dt = datetime(date_from.year, date_from.month, date_from.day, tzinfo=get_tz())
    end_dt = datetime(date_to.year, date_to.month, date_to.day, 23, 59, 59, 999000, tzinfo=get_tz())
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


def qdate_range_to_timestamps(date_from: QDate, date_to: QDate) -> tuple[int | None, int | None]:
    return date_range_to_timestamps(date_from.toPython(), date_to.toPython())


def sqlite_localtime_modifiers() -> tuple[str, ...]:
    dt_now = datetime.now(tz=get_tz())
    offset = dt_now.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return ("unixepoch", f"{sign}{hours:02d}:{minutes:02d}")


# --- Legacy compatibility shims ---
def set_timezone_offset(hours: float) -> None:
    """Legacy shim: set timezone by UTC offset hours. Maps to nearest IANA zone."""
    # Best-effort: find matching IANA zone
    target_sec = hours * 3600
    for iana_name, _ in IANA_TIMEZONES:
        try:
            zi = _safe_zoneinfo(iana_name)
            dt_now = datetime.now(tz=zi)
            off = dt_now.utcoffset()
            if off and abs(off.total_seconds() - target_sec) < 60:
                set_timezone(iana_name)
                return
        except Exception:
            continue
    # Fallback: store UTC
    set_timezone("UTC")


def get_timezone_offset() -> float:
    """Legacy shim: return the current timezone's UTC offset in hours."""
    try:
        zi = _safe_zoneinfo(_tz_iana_name)
        dt_now = datetime.now(tz=zi)
        off = dt_now.utcoffset()
        return off.total_seconds() / 3600 if off else 0.0
    except Exception:
        return 0.0


def format_timestamp(ts_ms, fmt: str = "full") -> str:
    """Format a millisecond timestamp with the configured timezone.

    fmt options:
        'full'      -> '2025-11-27 14:30:05.123'  (forensic detail)
        'datetime'  -> '2025-11-27 14:30:05'
        'date'      -> '2025-11-27'
        'time'      -> '14:30:05'
        'bubble'    -> 'Nov 27, 2:30 PM'           (chat bubble)
        'system'    -> 'Nov 27, 2025 · 2:30 PM'    (system events)
        'short'     -> '2:30 PM'
    """
    if not ts_ms:
        return ""
    try:
        ts_val = int(ts_ms)
        if ts_val <= 0:
            return ""
        dt = timestamp_to_local_datetime(ts_val)
        ms = ts_val % 1000
        tz_name = dt.strftime("%Z") or "LOCAL"
        if fmt == "full":
            return dt.strftime(f"%Y-%m-%d %H:%M:%S.{ms:03d}")
        if fmt == "datetime":
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        if fmt == "minute":
            return dt.strftime("%Y-%m-%d %H:%M")
        if fmt == "date":
            return dt.strftime("%Y-%m-%d")
        if fmt == "time":
            return dt.strftime("%H:%M:%S")
        if fmt == "iso":
            # Like datetime.isoformat() but in the selected case timezone
            return dt.isoformat()
        if fmt == "bubble":
            return dt.strftime("%b %d, %#I:%M %p")
        if fmt == "system":
            return dt.strftime(f"%b %d, %Y \u00b7 %#I:%M:%S.{ms:03d} %p")
        if fmt == "short":
            return dt.strftime("%#I:%M %p")
        if fmt == "forensic_tz":
            return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{ms:03d} {tz_name}"
        return dt.strftime(fmt)
    except (ValueError, OSError, OverflowError, TypeError):
        return ""


def format_timestamp_with_utc(ts_ms, fmt: str = "full") -> str:
    local_str = format_timestamp(ts_ms, "forensic_tz" if fmt == "full" else fmt)
    if not local_str:
        return ""
    utc_dt = timestamp_to_utc_datetime(ts_ms)
    utc_ms = int(ts_ms) % 1000
    utc_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S") + f".{utc_ms:03d} UTC"
    return f"{local_str} [{utc_str}]"

# Default database path (relative to project root)
DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "backend" / "output" / "analysis.db"

APP_NAME = "WAInsight"
APP_SUBTITLE = "WhatsApp Forensic Suite for Android"
APP_VERSION = "2.2.0"
ORG_NAME = "Device Owner"

# Sidebar page definitions: (id, label, icon, section_header)
# icon names are Material Design icon names for qt-material-icons
PAGES = [
    # Overview
    ("_header_overview", "Overview", None, True),
    ("dashboard", "Dashboard", "dashboard", False),
    ("conversations", "Conversations", "chat", False),
    ("status", "Status Updates", "timeline", False),
    ("contacts", "Contacts", "people", False),
    ("media", "Media Gallery", "image", False),
    ("documents", "Documents", "document", False),
    ("calls", "Calls", "call", False),
    ("events", "Scheduled Events", "event", False),
    ("search", "Search", "search", False),
    ("analytics", "Analytics", "bar_chart", False),

    # Forensics
    ("_header_forensics", "Forensics", None, True),
    ("cross_contact", "Cross-Contact Analysis", "groups", False),
    ("ghost", "Ghost Messages", "shield", False),
    ("edits", "Edit History", "edit", False),
    ("revoked", "Revoked Messages", "delete", False),
    ("system_events", "System Events", "warning", False),
    ("media_recovery", "Media Recovery", "download", False),
    ("image_similarity", "Image Similarity", "search", False),
    ("orphaned_media", "Orphaned Media", "broken_image", False),
    ("starred", "Starred Messages", "star", False),
    ("tagged", "Tagged Messages", "flag", False),

    # More
    ("_header_more", "More", None, True),
    ("locations", "Locations", "location_on", False),
    ("links", "Links", "link", False),
    ("polls", "Polls", "poll", False),
    ("export", "Export", "download", False),

    # Footer
    ("_header_settings", "", None, True),
    ("settings", "Settings", "settings", False),
]

# Message type display labels (message.message_type → human-readable).
MESSAGE_TYPE_LABELS = {
    0: "Text", 1: "Image", 2: "Audio", 3: "Video",
    4: "Contact Card", 5: "Location", 7: "System",
    9: "Document", 10: "Missed Call",
    11: "Waiting", 13: "GIF", 14: "Contact Cards",
    15: "Deleted", 16: "Live Location", 20: "Sticker",
    23: "Group Invite", 24: "Group Invite", 25: "List Message",
    26: "List Reply", 27: "Button Message",
    28: "WhatsApp Official", 32: "Group Invite",
    36: "Ephemeral Setting", 42: "View-Once Image", 43: "View-Once Video",
    45: "Interactive", 46: "Poll Vote", 49: "CTA Button",
    55: "Carousel", 57: "Ephemeral Sync",
    62: "Product", 64: "Admin Revoke",
    66: "Poll", 81: "Video Note", 82: "View-Once Voice",
    88: "Bot Feedback", 90: "Call Log", 92: "Event",
    94: "Channel Invite", 99: "Album",
    103: "Status Mention", 112: "Privacy Setting", 116: "Status",
}

# Call result display labels (maps result_label strings → user-facing text)
# call_result=5 ("answered") is the normal successful call ending (most common).
# call_result=0 ("connected") is a transient ringing state (rare).
CALL_RESULT_LABELS = {
    "answered": "Answered",
    "connected": "Connected",
    "missed": "Missed",  # For voice chats shows as "Not Joined" in chat renderer
    "rejected": "Declined",
    "unavailable": "Unavailable",
    "busy": "Busy",
    "joined_voice_chat": "Joined",
    # Legacy labels (from analysis.db ingested before enum update)
    "completed": "Answered",
    "cancelled": "Cancelled",
    "disconnected": "Answered",
    "ended": "Answered",
    "failed": "Failed",
}

# Chat type display names
CHAT_TYPE_LABELS = {
    "personal": "Personal",
    "group": "Group",
    "community": "Community",
    "broadcast": "Broadcast",
    "newsletter": "Newsletter",
    "status": "Status",
}

# Colors for charts
CHART_COLORS = [
    "#00bcd4", "#26a69a", "#66bb6a", "#ffa726",
    "#ef5350", "#ab47bc", "#42a5f5", "#78909c",
    "#ec407a", "#7e57c2", "#29b6f6", "#ffca28",
]
