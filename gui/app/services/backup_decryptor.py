"""
WhatsApp backup decryption for .crypt14 and .crypt15 files.

Supports:
  - crypt14: Standard encryption using device key file (/data/data/com.whatsapp/files/key)
  - crypt15: End-to-end encrypted backup using encrypted_backup.key or user password

Key sources:
  - 158-byte Android key file (key at offset 126, 32 bytes)
  - 32-byte raw key file
  - 64-character hex string

Uses the ``wa-crypt-tools`` library (``pip install
wa-crypt-tools``) as the primary decryption engine, falling
back to manual AES-256-GCM decryption when that package is not
available.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _load_key(key_text: str) -> bytes:
    """Parse a key from hex string or key file path."""
    key_text = key_text.strip()

    # Check if it's a file path
    if os.path.isfile(key_text):
        raw = Path(key_text).read_bytes()
        if len(raw) == 32:
            return raw
        # Android key file format (158 bytes): key at offset 126, 32 bytes
        if len(raw) == 158:
            return raw[126:158]
        # Newer key files may vary — try offset 30 (after Java serialization header)
        if len(raw) >= 62:
            return raw[30:62]
        # Try first 32 bytes as last resort
        if len(raw) >= 32:
            return raw[:32]
        raise ValueError(
            f"Key file has unexpected size ({len(raw)} bytes). "
            f"Expected 32 bytes (raw key) or 158 bytes (Android key file)."
        )

    # Try hex string
    key_text = key_text.replace(" ", "").replace("-", "")
    if len(key_text) == 64:
        try:
            return bytes.fromhex(key_text)
        except ValueError:
            pass

    raise ValueError(
        "Invalid key format. Provide a 64-character hex string or "
        "a path to a 32-byte key file."
    )


def decrypt_backup(crypt_file: str, key_text: str, output_path: str) -> None:
    """Decrypt a WhatsApp .crypt14/.crypt15 backup file.

    Tries wa-crypt-tools first (handles all protobuf header variants).
    Falls back to manual AES-GCM if wa-crypt-tools is unavailable.

    Args:
        crypt_file: Path to the encrypted backup.
        key_text: 64-char hex key or path to key file.
        output_path: Where to save the decrypted msgstore.db.
    """
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    # Try wa-crypt-tools first (most reliable — handles all format variants)
    try:
        _decrypt_with_wa_crypt_tools(crypt_file, key_text, output_path)
        _verify_output(output_path)
        logger.info("Decrypted via wa-crypt-tools: %s", Path(output_path).name)
        return
    except ImportError:
        logger.info("wa-crypt-tools not installed, falling back to manual decryption")
    except Exception as e:
        logger.warning("wa-crypt-tools failed: %s — trying manual decryption", e)

    # Fallback: manual AES-GCM decryption
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise ImportError(
            "Neither wa-crypt-tools nor pycryptodome is installed.\n"
            "Install with: pip install wa-crypt-tools pycryptodome"
        )

    key = _load_key(key_text)
    data = Path(crypt_file).read_bytes()
    name_lower = Path(crypt_file).name.lower()

    if name_lower.endswith(".crypt15"):
        plaintext = _decrypt_crypt15(data, key, AES)
    elif name_lower.endswith(".crypt14"):
        plaintext = _decrypt_crypt14(data, key, AES)
    else:
        raise ValueError(f"Unsupported file type: {Path(crypt_file).name}")

    # Verify and decompress if needed
    if not plaintext[:16].startswith(b"SQLite format 3"):
        import zlib
        try:
            plaintext = zlib.decompress(plaintext)
        except zlib.error:
            pass
        if not plaintext[:16].startswith(b"SQLite format 3"):
            raise ValueError(
                "Decryption produced invalid output (not a SQLite database). "
                "The key may be incorrect."
            )

    Path(output_path).write_bytes(plaintext)
    logger.info("Decrypted via manual AES-GCM: %s", Path(output_path).name)


def _decrypt_with_wa_crypt_tools(crypt_file: str, key_text: str, output_path: str) -> None:
    """Use wa-crypt-tools library for decryption."""
    import subprocess
    import sys

    key_text = key_text.strip()

    # wa-crypt-tools expects: python -m wa_crypt_tools.wadecrypt <key_file> <crypt_file> <output>
    # If key_text is a hex string, write it to a temp file
    key_path = key_text
    temp_key = None
    if not os.path.isfile(key_text):
        import tempfile
        temp_key = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
        temp_key.write(bytes.fromhex(key_text))
        temp_key.close()
        key_path = temp_key.name

    try:
        result = subprocess.run(
            [sys.executable, "-m", "wa_crypt_tools.wadecrypt",
             key_path, crypt_file, output_path],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            raise RuntimeError(f"wa-crypt-tools failed: {error_msg}")
    finally:
        if temp_key:
            try:
                os.unlink(temp_key.name)
            except OSError:
                pass


def _verify_output(output_path: str) -> None:
    """Verify the decrypted file is a valid SQLite database."""
    with open(output_path, "rb") as f:
        header = f.read(16)
    if not header.startswith(b"SQLite format 3"):
        # Try zlib decompression
        import zlib
        data = Path(output_path).read_bytes()
        try:
            decompressed = zlib.decompress(data)
            if decompressed[:16].startswith(b"SQLite format 3"):
                Path(output_path).write_bytes(decompressed)
                return
        except zlib.error:
            pass
        raise ValueError("Decrypted file is not a valid SQLite database")


def _decrypt_crypt15(data: bytes, key: bytes, AES) -> bytes:
    """Decrypt crypt15 format (67-byte header)."""
    if len(data) < 67 + 16 + 16:
        raise ValueError(f"crypt15 file too small ({len(data)} bytes)")

    header = data[:67]
    iv = data[67:67 + 16]
    encrypted = data[67 + 16:]
    ciphertext = encrypted[:-16]
    tag = encrypted[-16:]

    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    cipher.update(header)

    try:
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError:
        cipher2 = AES.new(key, AES.MODE_GCM, nonce=iv)
        try:
            plaintext = cipher2.decrypt_and_verify(ciphertext, tag)
        except ValueError:
            raise ValueError(
                "GCM authentication failed. The key may be incorrect "
                "or the file may be corrupted."
            )

    return plaintext


def _decrypt_crypt14(data: bytes, key: bytes, AES) -> bytes:
    """Decrypt crypt14 format (variable-length protobuf header).

    Tries multiple header sizes since the format varies by WhatsApp version.
    """
    # Try common header sizes
    for hdr_size in [99, 67, 86, 110, 120, 130, 140, 148]:
        if hdr_size + 32 > len(data):
            continue
        header = data[:hdr_size]
        iv = data[hdr_size:hdr_size + 16]
        encrypted = data[hdr_size + 16:]
        ciphertext = encrypted[:-16]
        tag = encrypted[-16:]

        # Try with AAD (header as additional authenticated data)
        try:
            cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
            cipher.update(header)
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)
            return plaintext
        except ValueError:
            pass

        # Try without AAD
        try:
            cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)
            return plaintext
        except ValueError:
            pass

    raise ValueError(
        "Failed to decrypt crypt14 with any known header size. "
        "Try installing wa-crypt-tools: pip install wa-crypt-tools"
    )
