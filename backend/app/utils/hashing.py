"""
Cryptographic hashing utilities for forensic evidence chain.

Computes SHA-256 hashes of source database files to establish
an evidence chain of custody. All hashes are computed before
any analysis begins and stored in case_metadata.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional


def sha256_file(file_path: Path, chunk_size: int = 65536) -> str:
    """Compute SHA-256 hash of a file.

    Reads in chunks so multi-GB databases don't have to fit in
    memory all at once.

    Args:
        file_path: Path to the file to hash.
        chunk_size: Read buffer size in bytes. 64KB default balances
                    memory usage with I/O efficiency.

    Returns:
        Lowercase hex digest string (64 characters).

    Raises:
        FileNotFoundError: If the file doesn't exist.
        PermissionError: If the file can't be read.
    """
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Compute SHA-256 hash of raw bytes.

    Args:
        data: Bytes to hash.

    Returns:
        Lowercase hex digest string.
    """
    return hashlib.sha256(data).hexdigest()


def sha256_string(text: str) -> str:
    """Compute SHA-256 hash of a UTF-8 string.

    Args:
        text: String to hash.

    Returns:
        Lowercase hex digest string.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_evidence_hashes(
    databases_path: Path,
    *,
    only_files: Optional[list[Path]] = None,
) -> dict[str, str]:
    """Compute SHA-256 hashes for source database files.

    Used to establish the forensic evidence chain.  Must be called BEFORE
    any analysis or modification of source files.

    Parameters
    ----------
    databases_path:
        Directory containing WhatsApp database files.  Only consulted when
        ``only_files`` is ``None`` (legacy "hash everything" mode).
    only_files:
        Explicit list of files to hash (each a full ``Path``).  When
        provided, **only these files are hashed** — no globbing of the
        databases directory.  This is the preferred call pattern: the
        caller knows exactly which databases the pipeline will actually
        open, so hashing stays scoped to evidence-of-interest.  For every
        file in the list, the sidecar ``.db-wal`` is also hashed when it
        exists next to it.

    Returns
    -------
    dict[str, str]
        Dictionary mapping filename to SHA-256 hex digest.  Only includes
        files that exist and are readable; unreadable files are recorded
        with the sentinel value ``"ERROR_UNREADABLE"``.

    Example
    -------
        >>> hashes = compute_evidence_hashes(
        ...     Path("/path/to/databases"),
        ...     only_files=[Path("/path/to/msgstore.db"), Path("/path/to/wa.db")],
        ... )
        >>> hashes['msgstore.db']
        'a1b2c3d4e5f6...'
    """
    hashes: dict[str, str] = {}

    if only_files is not None:
        # Targeted mode — hash only the caller-supplied files (+ their WAL
        # sidecars if present).  Never glob the source directory.
        for f in only_files:
            if not f or not f.exists():
                continue
            try:
                hashes[f.name] = sha256_file(f)
            except (PermissionError, OSError):
                hashes[f.name] = "ERROR_UNREADABLE"
            # Sidecar WAL / journal files are part of the evidence.
            for suffix in (".db-wal", ".db-shm", ".db-journal"):
                sidecar = f.with_suffix(f.suffix + suffix) if not f.suffix.endswith(suffix) else None
                # Correct sidecar path is "<same basename>-wal" — i.e. for
                # msgstore.db → msgstore.db-wal.
                sidecar = f.parent / (f.name + suffix)
                if sidecar.exists():
                    try:
                        hashes[sidecar.name] = sha256_file(sidecar)
                    except (PermissionError, OSError):
                        hashes[sidecar.name] = "ERROR_UNREADABLE"
        return hashes

    # Legacy fallback: hash every .db and .db-wal in the directory.  Kept
    # for callers that don't yet pass ``only_files``; new callers should
    # always supply it so unrelated databases don't end up in the
    # evidence log.
    if not databases_path.exists():
        return hashes

    for db_file in sorted(databases_path.glob("*.db")):
        try:
            hashes[db_file.name] = sha256_file(db_file)
        except (PermissionError, OSError):
            hashes[db_file.name] = "ERROR_UNREADABLE"

    for wal_file in sorted(databases_path.glob("*.db-wal")):
        try:
            hashes[wal_file.name] = sha256_file(wal_file)
        except (PermissionError, OSError):
            hashes[wal_file.name] = "ERROR_UNREADABLE"

    return hashes


def verify_file_hash(file_path: Path, expected_hash: str) -> bool:
    """Verify a file matches its expected SHA-256 hash.

    Used to verify evidence integrity hasn't been compromised.

    Args:
        file_path: Path to the file to verify.
        expected_hash: Expected SHA-256 hex digest.

    Returns:
        True if hash matches, False otherwise.
    """
    if not file_path.exists():
        return False
    try:
        actual = sha256_file(file_path)
        return actual.lower() == expected_hash.lower()
    except (PermissionError, OSError):
        return False


def format_hash_short(hex_digest: str, length: int = 12) -> str:
    """Truncate a hash digest for display purposes.

    Args:
        hex_digest: Full hex digest string.
        length: Number of characters to show.

    Returns:
        Truncated hash with '...' suffix.

    Example:
        >>> format_hash_short('a1b2c3d4e5f6a7b8c9d0e1f2')
        'a1b2c3d4e5f6...'
    """
    if len(hex_digest) <= length:
        return hex_digest
    return hex_digest[:length] + "..."
