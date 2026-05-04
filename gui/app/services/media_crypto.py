"""
WhatsApp media download and decryption service.

Implements the WhatsApp media encryption protocol:
  - HKDF-SHA256 key derivation (RFC 5869)
  - AES-256-CBC decryption with PKCS#7 padding
  - HMAC-SHA256 authentication (10-byte truncated MAC)

References:
  - https://github.com/sigalor/whatsapp-web-reveng (protocol docs)
  - https://github.com/sh4dowb/whatsapp-media-decrypt (Python reference)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# HKDF info strings keyed by WhatsApp media type
_HKDF_INFO = {
    "image": b"WhatsApp Image Keys",
    "video": b"WhatsApp Video Keys",
    "audio": b"WhatsApp Audio Keys",
    "voice": b"WhatsApp Audio Keys",
    "document": b"WhatsApp Document Keys",
    "sticker": b"WhatsApp Image Keys",
    "gif": b"WhatsApp Video Keys",
    "animated_gif": b"WhatsApp Video Keys",
}

# MIME type -> media type mapping
_MIME_TO_TYPE = {
    "image/jpeg": "image",
    "image/png": "image",
    "image/webp": "sticker",
    "video/mp4": "video",
    "video/3gpp": "video",
    "audio/aac": "audio",
    "audio/ogg": "audio",
    "audio/ogg; codecs=opus": "voice",
    "audio/mp4": "audio",
    "audio/mpeg": "audio",
    "application/pdf": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
    "application/zip": "document",
}


def _hkdf_expand(key: bytes, length: int, info: bytes = b"") -> bytes:
    """HKDF-Expand with SHA-256 and all-zeros 32-byte salt (WhatsApp convention)."""
    # Extract phase: PRK = HMAC-SHA256(salt=0x00*32, IKM=key)
    prk = hmac.new(b"\x00" * 32, key, hashlib.sha256).digest()

    # Expand phase
    key_stream = b""
    key_block = b""
    block_index = 1
    while len(key_stream) < length:
        key_block = hmac.new(
            prk,
            msg=key_block + info + bytes([block_index]),
            digestmod=hashlib.sha256,
        ).digest()
        block_index += 1
        key_stream += key_block
    return key_stream[:length]


def _aes_unpad(data: bytes) -> bytes:
    """Remove PKCS#7 padding."""
    if not data:
        return data
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16:
        return data  # invalid padding, return as-is
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        return data  # invalid padding
    return data[:-pad_len]


def get_media_type(type_label: str = "", mime_type: str = "") -> str:
    """Resolve a WhatsApp media type string for HKDF info lookup."""
    if type_label and type_label in _HKDF_INFO:
        return type_label
    if mime_type:
        if mime_type in _MIME_TO_TYPE:
            return _MIME_TO_TYPE[mime_type]
        # Fallback: check prefix
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("audio/"):
            return "audio"
    return "document"  # safe default


def decrypt_media(enc_data: bytes, media_key: bytes,
                  media_type: str = "image") -> bytes:
    """Decrypt a WhatsApp .enc media file.

    Args:
        enc_data: Raw bytes of the encrypted file (ciphertext + 10-byte MAC).
        media_key: 32-byte raw media key from msgstore.db / analysis.db.
        media_type: One of 'image', 'video', 'audio', 'voice', 'document',
                    'sticker', 'gif'.

    Returns:
        Decrypted plaintext bytes.

    Raises:
        ValueError: If MAC verification fails or data is too short.
        ImportError: If pycryptodome is not installed.
    """
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise ImportError(
            "pycryptodome is required for media decryption. "
            "Install with: pip install pycryptodome"
        )

    if len(enc_data) < 10 + 16:
        raise ValueError(f"Encrypted data too short ({len(enc_data)} bytes)")

    if len(media_key) != 32:
        raise ValueError(f"Media key must be 32 bytes, got {len(media_key)}")

    info = _HKDF_INFO.get(media_type, b"WhatsApp Document Keys")

    # Derive 112 bytes: iv(16) + cipherKey(32) + macKey(32) + refKey(32)
    expanded = _hkdf_expand(media_key, 112, info)
    iv = expanded[:16]
    cipher_key = expanded[16:48]
    mac_key = expanded[48:80]

    # Split ciphertext and MAC
    file_data = enc_data[:-10]
    mac = enc_data[-10:]

    # Verify MAC (HMAC-SHA256 truncated to 10 bytes)
    expected_mac = hmac.new(mac_key, iv + file_data, hashlib.sha256).digest()[:10]
    if not hmac.compare_digest(mac, expected_mac):
        raise ValueError("MAC verification failed -- file may be corrupted or wrong key")

    # Decrypt AES-256-CBC
    cipher = AES.new(cipher_key, AES.MODE_CBC, iv)
    plaintext = cipher.decrypt(file_data)
    return _aes_unpad(plaintext)


def verify_file_hash(plaintext: bytes, expected_hash: str) -> bool:
    """Verify decrypted file integrity against the stored file_hash.

    Args:
        plaintext: Decrypted file bytes.
        expected_hash: Base64-encoded SHA-256 hash from analysis.db file_hash column.

    Returns:
        True if hashes match.
    """
    import base64
    actual = base64.b64encode(hashlib.sha256(plaintext).digest()).decode()
    return actual == expected_hash


def download_media(url: str, timeout: int = 30) -> bytes:
    """Download encrypted media from WhatsApp CDN.

    Args:
        url: Full CDN URL (https://mmg.whatsapp.net/...).
        timeout: Request timeout in seconds.

    Returns:
        Raw encrypted bytes.

    Raises:
        Exception: On network/HTTP errors.
    """
    import urllib.request
    import urllib.error

    headers = {
        "User-Agent": "WhatsApp/2.24.6.77 A",
        "Accept": "*/*",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise ValueError(
                "URL signature expired (403) -- the oe= timestamp has passed. "
                "WhatsApp CDN rejects requests with expired signatures."
            )
        elif e.code == 404:
            raise ValueError(
                "Content purged from CDN (404) -- WhatsApp deleted the file "
                "server-side even though the URL signature may still be valid. "
                "CDN content retention is shorter than URL signature validity."
            )
        elif e.code == 410:
            raise ValueError(
                "Media gone (410) -- file has been permanently removed from CDN."
            )
        raise


def check_url_expiry(url: str) -> tuple[bool, str]:
    """Check if a WhatsApp CDN URL has expired based on the oe= parameter.

    Args:
        url: CDN URL containing oe= hex timestamp.

    Returns:
        (is_valid, message): Whether the URL is still valid and a human-readable message.
    """
    import re
    import time
    oe_match = re.search(r'oe=([0-9A-Fa-f]+)', url)
    if not oe_match:
        return True, "No expiry parameter (oe=) found — attempting download"
    exp_ts = int(oe_match.group(1), 16)
    now_ts = int(time.time())
    if exp_ts > now_ts:
        days_left = (exp_ts - now_ts) / 86400
        return True, f"URL signature valid for {days_left:.1f} more days (content may still be purged server-side)"
    else:
        days_expired = (now_ts - exp_ts) / 86400
        return False, f"URL expired {days_expired:.1f} days ago (WhatsApp CDN enforces oe= strictly)"


def download_and_decrypt(
    url: str,
    media_key: bytes,
    media_type: str = "image",
    file_hash: str | None = None,
    save_path: str | None = None,
    timeout: int = 30,
) -> bytes:
    """Download and decrypt a WhatsApp media file in one step.

    Args:
        url: CDN download URL.
        media_key: 32-byte raw encryption key.
        media_type: Media type for HKDF info string selection.
        file_hash: Optional expected hash for verification.
        save_path: Optional path to save the decrypted file.
        timeout: Download timeout in seconds.

    Returns:
        Decrypted plaintext bytes.

    Raises:
        ValueError: If URL has expired (oe= timestamp in the past).
    """
    # Check URL expiry before downloading
    is_valid, expiry_msg = check_url_expiry(url)
    if not is_valid:
        raise ValueError(expiry_msg)

    enc_data = download_media(url, timeout=timeout)
    plaintext = decrypt_media(enc_data, media_key, media_type)

    if file_hash and not verify_file_hash(plaintext, file_hash):
        logger.warning("File hash mismatch after decryption!")

    if save_path:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(plaintext)
        logger.info("Saved decrypted media to %s (%d bytes)", save_path, len(plaintext))

    return plaintext


def get_extension_for_mime(mime_type: str) -> str:
    """Get file extension for a MIME type."""
    ext_map = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/3gpp": ".3gp",
        "audio/aac": ".aac",
        "audio/ogg": ".ogg",
        "audio/ogg; codecs=opus": ".opus",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "application/pdf": ".pdf",
    }
    return ext_map.get(mime_type, ".bin")


def parse_url_expiry(url: str) -> Optional[int]:
    """Extract the expiry epoch from a WhatsApp CDN URL.

    WhatsApp CDN URLs contain an 'oe' query parameter which is a hex-encoded
    Unix epoch timestamp indicating when the URL expires (~30 days after message).

    Returns:
        Unix epoch timestamp, or None if not parseable.
    """
    if not url:
        return None
    try:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        oe = qs.get("oe", [None])[0]
        if oe:
            return int(oe, 16)
    except (ValueError, TypeError):
        pass
    return None


def is_url_likely_valid(url: str) -> bool:
    """Check if a WhatsApp CDN URL is likely still valid (not expired).

    Returns True if the URL's 'oe' expiry is in the future.
    """
    import time
    expiry = parse_url_expiry(url)
    if expiry is None:
        return False  # unknown, assume expired
    return expiry > time.time()
