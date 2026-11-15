#!/usr/bin/env python3
"""
VeriPhone Account Manager
=========================

A PyQt6 desktop dashboard that runs a *sequential*, authorized
browser-based account-registration test suite against a staging
environment, using the Browser Use Cloud API (SDK v4).

Configuration is intentionally kept at the very top of this file so the
operator can edit an API key, a target URL or a test password without
touching application logic.

Run with:

    python main.py

Requirements:

    PyQt6
    browser-use-sdk

Scope & safety
--------------
This tool is built for *authorized* testing only: point TARGET_URL at a
staging domain you own and are permitted to test. It never attempts to
defeat CAPTCHAs, e-mail verification, rate limits or any other account
protection. When a human-verification challenge is encountered the task is
parked in ``WAITING_FOR_USER`` and the operator is asked to solve it in the
live browser view.
"""

from __future__ import annotations

# ==========================================================================
#  CONFIGURATION  --  edit this block only
# ==========================================================================

BROWSER_USE_API_KEY = "YOUR_API_KEY"

TARGET_URL = "https://your-authorized-staging-domain/signup"

DEFAULT_PASSWORD = "YOUR_TEST_PASSWORD"

OUTPUT_FILE = "results.csv"
KEY_OUTPUT_FILE = "key.txt"

BROWSER_USE_MAX_COST = 0.50

# --------------------------------------------------------------------------
#  Optional tuning (safe defaults; all values are editable)
# --------------------------------------------------------------------------

# Absolute directory used for results.csv / key.txt.
# Empty string -> the folder that contains main.py.
OUTPUT_DIR = ""

# Browser Use model used for the agent runs. "" -> SDK default.
BROWSER_USE_MODEL = ""

# Hard ceiling for a single email task, in seconds (polling watchdog).
TASK_TIMEOUT_SECONDS = 600

# How often the run status endpoint is polled, in seconds.
POLL_INTERVAL_SECONDS = 2.0

# Pause between two consecutive email tasks, in seconds.
DELAY_BETWEEN_TASKS_SECONDS = 1.5

# Automatic retries for *recoverable* failures (network / transient 5xx).
MAX_TASK_RETRIES = 1

# HTTP timeout (seconds) for individual Browser Use API calls.
API_TIMEOUT_SECONDS = 30.0

# Country the cloud browser is proxied through (ISO-3166 alpha-2). Use a
# region that matches your staging environment; "" disables the proxy.
PROXY_COUNTRY_CODE = "us"

# Log a summary of the files the agent attached to a run (screenshots…).
LOG_ATTACHMENTS = False

# ==========================================================================
#  End of configuration
# ==========================================================================

# Optional local overrides, never committed: a file named
# "_overrides.local.py" next to main.py may contain lines such as
#     BROWSER_USE_API_KEY = "bu_live_..."
# Only names already defined above are accepted, so typos stay visible.
_OVERRIDES_FILE = "_overrides.local.py"

import ast
import csv
import html
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, NoReturn, Sequence

APP_NAME = "VeriPhone Account Manager"
APP_TAGLINE = "Sequential registration test runner  ·  Browser Use Cloud API v4"
APP_VERSION = "1.0.0"

LOGGER = logging.getLogger("veriphone")

# Status strings written to / read from results.csv.
STATUS_PENDING = "PENDING"
STATUS_RUNNING = "RUNNING"
STATUS_WAITING = "WAITING_FOR_USER"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"

CSV_COLUMNS = [
    "email",
    "status",
    "task_id",
    "created_at",
    "error_message",
    "session_id",
    "live_url",
    "account_email",
    "credential",   # masked in CSV; the raw value only goes to key.txt
    "cost_usd",
    "duration_seconds",
    "attempt",
]

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")

SECRET_ALIAS = "staging_test_password"


# ==========================================================================
#  Small helpers
# ==========================================================================

def now_iso() -> str:
    """Timestamp used for CSV rows (local time, second precision)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clock() -> str:
    """Timestamp used by the live activity log."""
    return datetime.now().strftime("%H:%M:%S")


def output_directory() -> Path:
    """Resolve the folder holding results.csv and key.txt."""
    if OUTPUT_DIR.strip():
        base = Path(OUTPUT_DIR).expanduser().resolve()
    else:
        base = app_directory()
    return base


def output_path(name: str) -> Path:
    candidate = Path(name)
    if candidate.is_absolute():
        return candidate
    return output_directory() / candidate


def app_directory() -> Path:
    """Folder containing main.py (used by the local-overrides loader)."""
    return Path(__file__).resolve().parent


def mask_secret(value: str) -> str:
    """Never write a full credential into logs or the CSV."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-2:]}"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(round(seconds)))
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def friendly_api_error(code: int | None) -> str:
    """Translate an HTTP status from the Browser Use API into plain English."""
    mapping = {
        400: "Request rejected by the Browser Use API (400).",
        401: "Invalid or missing Browser Use API key (401).",
        402: "Browser Use spend limit reached — top up or raise the cap.",
        403: "The API key is not authorized for this operation (403).",
        404: "Browser Use resource not found (404).",
        409: "Browser Use reported a conflict (409).",
        429: "Rate limited by Browser Use (429). Slow down and retry.",
        500: "Browser Use internal error (500).",
        502: "Browser Use gateway error (502).",
        503: "Browser Use is busy (503). Retry shortly.",
        504: "Browser Use gateway timeout (504).",
    }
    if code is None:
        return ""
    return mapping.get(code, f"Browser Use API error ({code}).")


def is_recoverable(status_code: int | None) -> bool:
    return status_code in {408, 409, 425, 429, 500, 502, 503, 504}


def is_email_valid(address: str) -> bool:
    return bool(EMAIL_RE.match(address))


def fail_missing_qt(error: BaseException) -> NoReturn:
    """Explain a broken PyQt6 install instead of dumping an import traceback."""
    sys.stderr.write(
        "\n"
        f"{APP_NAME} needs PyQt6, which could not be loaded.\n\n"
        "  Install it with:   pip install PyQt6\n\n"
        "On Linux the underlying GUI libraries may also be missing, e.g.:\n"
        "  sudo apt install libxkbcommon-x11-0 libegl1 libgl1 libxcb-cursor0\n\n"
        f"Details: {type(error).__name__}: {error}\n"
    )
    raise SystemExit(2)


# ==========================================================================
#  Email normalisation / import
# ==========================================================================

@dataclass(slots=True)
class ImportReport:
    """Outcome of parsing a TXT file of e-mail addresses."""

    emails: list[str] = field(default_factory=list)
    duplicates: int = 0
    invalid: list[str] = field(default_factory=list)
    empty_lines: int = 0

    @property
    def total_lines(self) -> int:
        return len(self.emails) + len(self.invalid) + self.duplicates + self.empty_lines


def _decode_email_file(file_path: Path) -> str:
    """Decode a TXT list, tolerating UTF-8 BOM, UTF-16 and cp1252 exports."""
    try:
        head = file_path.open("rb").read(64)
    except OSError as exc:
        raise OSError(f"Cannot open {file_path.name}: {exc.strerror or exc}") from exc

    # Detected encodings first: UTF-16 text decodes "successfully" as UTF-8,
    # so plain UTF-8 must not be tried before the UTF-16 variants.
    encodings: list[str] = []
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings.append("utf-16")          # Windows "Unicode" export
    elif b"\x00" in head:
        # UTF-16 without a BOM: ASCII text leaves NULs in every other byte.
        even_nuls = head[0::2].count(0)
        odd_nuls = head[1::2].count(0)
        encodings.append("utf-16-be" if even_nuls > odd_nuls else "utf-16-le")
    encodings.extend(["utf-8-sig", "cp1252", "latin-1"])
    for encoding in encodings:
        try:
            return file_path.read_text(encoding=encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return file_path.read_text(encoding="utf-8", errors="replace")


def read_email_file(path: str | os.PathLike[str]) -> ImportReport:
    """Read a TXT file: one address per line, whitespace trimmed, order kept.

    Empty lines are ignored, duplicates are skipped (first occurrence wins)
    and malformed addresses are collected separately so the UI can report
    them instead of silently dropping them.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not file_path.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")
    if file_path.stat().st_size == 0:
        raise ValueError("The selected file is empty.")

    raw = _decode_email_file(file_path).replace("\x00", "")

    report = ImportReport()
    seen: set[str] = set()
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            report.empty_lines += 1
            continue
        # Tolerate annoted lines such as "user@x.test  # note" or a trailing comma.
        text = text.split("#", 1)[0].strip().rstrip(",;").strip()
        if not text:
            report.empty_lines += 1
            continue
        key = text.casefold()
        if not is_email_valid(text):
            report.invalid.append(text)
            continue
        if key in seen:
            report.duplicates += 1
            continue
        seen.add(key)
        report.emails.append(text)
    return report


# ==========================================================================
#  Task model
# ==========================================================================

class TaskStatus(str, Enum):
    PENDING = STATUS_PENDING
    RUNNING = STATUS_RUNNING
    WAITING_FOR_USER = STATUS_WAITING
    COMPLETED = STATUS_COMPLETED
    FAILED = STATUS_FAILED
    CANCELLED = STATUS_CANCELLED

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)

    @property
    def is_active(self) -> bool:
        return self in (TaskStatus.RUNNING, TaskStatus.WAITING_FOR_USER)


@dataclass
class EmailTask:
    """One e-mail address and everything we learned while processing it."""

    email: str
    status: TaskStatus = TaskStatus.PENDING
    task_id: str = ""
    session_id: str = ""
    live_url: str = ""
    created_at: str = ""
    finished_at: str = ""
    error_message: str = ""
    account_email: str = ""
    credential: str = ""
    cost_usd: float = 0.0
    duration_seconds: float | None = None
    attempt: int = 0
    notes: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, str]:
        return {
            "email": self.email,
            "status": self.status.value,
            "task_id": self.task_id,
            "created_at": self.created_at or now_iso(),
            "error_message": self.error_message,
            "session_id": self.session_id,
            "live_url": self.live_url,
            "account_email": self.account_email,
            # Credentials live only in key.txt; the CSV keeps a masked copy.
            "credential": mask_secret(self.credential),
            "cost_usd": f"{self.cost_usd:.4f}" if self.cost_usd else "",
            "duration_seconds": (
                f"{self.duration_seconds:.1f}" if self.duration_seconds is not None else ""
            ),
            "attempt": str(self.attempt) if self.attempt else "",
        }

    def copy(self) -> "EmailTask":
        return replace(self, notes=list(self.notes))


# ==========================================================================
#  Result store (results.csv / key.txt)
# ==========================================================================

class OutputStoreError(RuntimeError):
    """Raised when results.csv or key.txt cannot be written."""


class ResultStore:
    """Append-only, duplicate-aware persistence for CSV and credentials.

    Every write takes the process lock and rewrites the file atomically
    (temp file + os.replace), so an interrupted run can never truncate
    previous results. Rows are keyed by lowercase e-mail address: a newer
    result for an address replaces the older row while keeping its
    original position.
    """

    def __init__(self, csv_path: Path | None = None, key_path: Path | None = None) -> None:
        self.csv_path = Path(csv_path) if csv_path else output_path(OUTPUT_FILE)
        self.key_path = Path(key_path) if key_path else output_path(KEY_OUTPUT_FILE)
        self._lock = threading.RLock()

    # -- directories -----------------------------------------------------
    def _ensure_parent(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputStoreError(
                f"Cannot create folder {path.parent}: {exc.strerror or exc}"
            ) from exc

    # -- CSV -------------------------------------------------------------
    def read_rows(self) -> list[dict[str, str]]:
        with self._lock:
            return self._read_rows_locked()

    def _read_rows_locked(self) -> list[dict[str, str]]:
        if not self.csv_path.exists():
            return []
        rows: list[dict[str, str]] = []
        try:
            with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    return []
                for raw in reader:
                    row = {key: (raw.get(key) or "") for key in CSV_COLUMNS}
                    if row["email"]:
                        row["email"] = row["email"].strip()
                        rows.append(row)
        except (OSError, csv.Error, UnicodeDecodeError) as exc:
            raise OutputStoreError(
                f"Cannot read {self.csv_path.name}: {getattr(exc, 'strerror', None) or exc}"
            ) from exc
        return rows

    def _write_atomic(self, path: Path, writer: Callable[[Any], None]) -> None:
        """Write via a temp file in the same folder, then rename into place."""
        self._ensure_parent(path)
        tmp = path.with_name(path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="") as handle:
                writer(handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except PermissionError as exc:
            self._cleanup(tmp)
            raise OutputStoreError(
                f"Permission denied writing {path.name}. Close the file if it is open "
                f"in Excel/another program, then retry."
            ) from exc
        except OSError as exc:
            self._cleanup(tmp)
            raise OutputStoreError(
                f"Cannot write {path.name}: {exc.strerror or exc}"
            ) from exc

    @staticmethod
    def _cleanup(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def append_tasks(self, tasks: Iterable[EmailTask]) -> int:
        """Persist tasks, replacing existing rows for the same e-mail.

        Returns the number of rows written. Existing rows for other
        addresses are preserved verbatim and in their original order.
        """
        incoming = [task for task in tasks if task.email]
        if not incoming:
            return 0
        with self._lock:
            rows = self._read_rows_locked()
            index = {row["email"].casefold(): pos for pos, row in enumerate(rows)}
            written = 0
            for task in incoming:
                key = task.email.casefold()
                row = task.to_row()
                if key in index:
                    if row["status"] == STATUS_PENDING and rows[index[key]]["status"] != STATUS_PENDING:
                        # Never downgrade a finished result back to PENDING.
                        continue
                    rows[index[key]] = row
                else:
                    index[key] = len(rows)
                    rows.append(row)
                written += 1
            if not written:
                return 0

            def _write(handle):
                writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)

            self._write_atomic(self.csv_path, _write)
            return written

    def clear_completed(self) -> int:
        """Remove COMPLETED rows (and their credentials) from both files."""
        with self._lock:
            rows = self._read_rows_locked()
            keep = [row for row in rows if row["status"] != STATUS_COMPLETED]
            removed = [row for row in rows if row["status"] == STATUS_COMPLETED]
            if not removed:
                return 0

            def _write(handle):
                writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for row in keep:
                    writer.writerow(row)

            self._write_atomic(self.csv_path, _write)
            doomed = {row["email"].casefold() for row in removed}
            self._remove_credentials(doomed)
            return len(removed)

    # -- key.txt ---------------------------------------------------------
    def read_credentials(self) -> list[tuple[str, str]]:
        """Return ``(email, credential)`` pairs stored one per line."""
        with self._lock:
            if not self.key_path.exists():
                return []
            try:
                text = self.key_path.read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise OutputStoreError(
                    f"Cannot read {self.key_path.name}: {exc.strerror or exc}"
                ) from exc
            pairs: list[tuple[str, str]] = []
            for line in text.splitlines():
                entry = parse_credential_line(line)
                if entry:
                    pairs.append(entry)
            return pairs

    def append_credential(self, email: str, credential: str) -> bool:
        """Append an approved test credential, skipping duplicate pairs."""
        credential = (credential or "").strip()
        if not credential:
            return False
        with self._lock:
            existing = self.read_credentials()
            if any(pair_email.casefold() == email.casefold() and value == credential
                   for pair_email, value in existing):
                return False
            lines = [f"{email}\t{credential}" for email, credential in existing]
            lines.append(f"{email}\t{credential}")

            def _write(handle):
                handle.write(
                    "# VeriPhone Account Manager — authorized staging credentials\n"
                    "# email<TAB>credential — keep this file private\n"
                )
                for line in lines:
                    handle.write(line + "\n")

            self._write_atomic(self.key_path, _write)
            self._restrict_permissions(self.key_path)
            return True

    def _remove_credentials(self, emails: set[str]) -> int:
        if not emails:
            return 0
        current = self.read_credentials()
        kept = [(email, value) for email, value in current if email.casefold() not in emails]
        if len(kept) == len(current):
            return 0

        def _write(handle):
            handle.write(
                "# VeriPhone Account Manager — authorized staging credentials\n"
                "# email<TAB>credential — keep this file private\n"
            )
            for email, value in kept:
                handle.write(f"{email}\t{value}\n")

        self._write_atomic(self.key_path, _write)
        self._restrict_permissions(self.key_path)
        return len(current) - len(kept)

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        """Best-effort owner-only permissions for the credential file."""
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows / exotic filesystems: nothing sensible to do.

    # -- export ----------------------------------------------------------
    def export_csv(self, destination: str | os.PathLike[str]) -> tuple[int, Path]:
        """Copy the accumulated results to a user-chosen location."""
        dest = Path(destination)
        with self._lock:
            rows = self._read_rows_locked()

        def _write(handle):
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

        if rows:
            self._write_atomic(dest, _write)
        else:
            self._ensure_parent(dest)
            try:
                with dest.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
                    writer.writeheader()
            except PermissionError as exc:
                raise OutputStoreError(
                    f"Permission denied writing {dest.name}."
                ) from exc
            except OSError as exc:
                raise OutputStoreError(
                    f"Cannot write {dest.name}: {exc.strerror or exc}"
                ) from exc
        return len(rows), dest

    def stats(self) -> dict[str, int]:
        rows = self.read_rows()
        counts = {status: 0 for status in (
            STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_PENDING, STATUS_RUNNING
        )}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts


def parse_credential_line(line: str) -> tuple[str, str] | None:
    """Parse ``email<TAB>credential`` (also tolerates ``,`` and ``:``)."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    for separator in ("\t", ",", ":"):
        if separator in text:
            email, _, value = text.partition(separator)
            email, value = email.strip(), value.strip()
            if email and value:
                return email, value
            return None
    return None


def _apply_local_overrides() -> list[str]:
    """Load optional ``name = value`` pairs from _overrides.local.py.

    Duplicated (rather than imported) so the file must be valid, plain
    ``KEY = "value"`` assignments. Only names that already exist in the
    configuration block are accepted, which keeps typos visible.
    """
    path = app_directory() / _OVERRIDES_FILE
    if not path.exists():
        return []
    module_globals = globals()
    applied: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("Cannot read %s: %s", _OVERRIDES_FILE, exc)
        return []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, raw = stripped.partition("=")
        name, raw = name.strip(), raw.strip()
        if name not in module_globals or name.startswith("_"):
            continue
        try:
            module_globals[name] = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            continue
        applied.append(name)
    return applied


APPLIED_OVERRIDES = _apply_local_overrides()

# ==========================================================================
#  Browser Use Cloud API (SDK v4) service layer
# ==========================================================================

try:  # pragma: no cover - import guard keeps the GUI launchable without SDK
    from browser_use_sdk.v4 import BrowserUse as BrowserUseV4Client
    from browser_use_sdk.v4 import APIKeySpendLimitError, BrowserUseError
    from browser_use_sdk.v4 import RunBrowserSettings

    BROWSER_USE_AVAILABLE = True
    BROWSER_USE_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover
    BrowserUseV4Client = None  # type: ignore[assignment]
    RunBrowserSettings = None  # type: ignore[assignment]

    class BrowserUseError(Exception):  # type: ignore[no-redef]
        status_code: int = 0
        message: str = ""

    class APIKeySpendLimitError(BrowserUseError):  # type: ignore[no-redef]
        pass

    BROWSER_USE_AVAILABLE = False
    BROWSER_USE_IMPORT_ERROR = str(exc)


class ConfigurationError(RuntimeError):
    """Raised when the editable configuration block is still incomplete."""


# Structured contract for the agent's final answer.
TASK_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "signup_completed": {
            "type": "boolean",
            "description": "True only if the registration form was accepted.",
        },
        "dashboard_reached": {
            "type": "boolean",
            "description": "True if the authenticated dashboard is visible.",
        },
        "account_email": {
            "type": "string",
            "description": "The e-mail address shown for the authenticated test account.",
        },
        "test_credential": {
            "type": "string",
            "description": (
                "The approved test credential displayed on the dashboard test panel "
                "(an API test key or fixture value). Empty when not displayed."
            ),
        },
        "human_verification_required": {
            "type": "boolean",
            "description": "True if a CAPTCHA/verification step blocked progress.",
        },
        "blocker": {
            "type": "string",
            "description": (
                "What blocked the run when human_verification_required is true. One of: "
                "captcha, email_verification, phone_verification, rate_limit, other. "
                "Empty otherwise."
            ),
        },
        "failure_reason": {
            "type": "string",
            "description": "Short, human-readable reason when a check failed.",
        },
        "checks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "One line per validated element or check.",
        },
    },
    "required": ["signup_completed", "dashboard_reached"],
}


@dataclass
class TaskOutcome:
    """Normalised result of one Browser Use run."""

    status: TaskStatus
    ok: bool = False
    error: str = ""
    account_email: str = ""
    credential: str = ""
    live_url: str = ""
    session_id: str = ""
    task_id: str = ""
    cost_usd: float = 0.0
    duration_seconds: float | None = None
    recoverable: bool = False
    notes: list[str] = field(default_factory=list)


# Guidance shown when a run parks as WAITING_FOR_USER. Deliberately specific:
# "solve the challenge in the live view" is useless advice for an inbox check.
_WAITING_GUIDANCE: dict[str, str] = {
    "captcha": (
        "A human-verification challenge blocked the run. Open the live browser view, "
        "solve it there if you are authorized to, then re-run this address."
    ),
    "email_verification": (
        "Registration requires e-mail verification. Nothing was bypassed — confirm the "
        "test inbox/mail fixture for this address, then re-run it."
    ),
    "phone_verification": (
        "Registration requires phone verification. Nothing was bypassed — use the "
        "approved test number or fixture, then re-run this address."
    ),
    "rate_limit": (
        "The target rate-limited this run. Nothing was bypassed — wait for the limit to "
        "reset, then re-run this address."
    ),
}

_DEFAULT_WAITING_GUIDANCE = (
    "An account-protection step blocked the run and was not bypassed. Review the notice, "
    "resolve it through the approved test path, then re-run this address."
)

_BLOCKER_ALIASES = {
    "email": "email_verification",
    "email_verify": "email_verification",
    "verify_email": "email_verification",
    "email_not_verified": "email_verification",
    "phone": "phone_verification",
    "phone_verify": "phone_verification",
    "verify_phone": "phone_verification",
    "sms": "phone_verification",
    "otp": "other",
    "too many": "rate_limit",
    "2fa": "other",
    "two_factor": "other",
    "recaptcha": "captcha",
    "hcaptcha": "captcha",
    "human_verification": "captcha",
    "human": "captcha",
    "throttle": "rate_limit",
    "throttled": "rate_limit",
    "rate_limit": "rate_limit",
    "rate_limited": "rate_limit",
    "too_many_requests": "rate_limit",
}


def normalize_blocker(blocker: str, failure_reason: str = "") -> str:
    """Map the agent's blocker (or free text) onto a known blocker key."""
    # The agent's explicit blocker wins; free text is only a fallback.
    raw = blocker.strip().lower() or failure_reason.lower()
    for key, value in _BLOCKER_ALIASES.items():
        if key in raw.replace("-", "_"):
            return value
    for key in _WAITING_GUIDANCE:
        if key in raw:
            return key
    return ""


def waiting_message(blocker: str, failure_reason: str = "") -> str:
    """Explain a WAITING_FOR_USER stop in terms of what actually blocked it."""
    key = normalize_blocker(blocker, failure_reason)
    guidance = _WAITING_GUIDANCE.get(key, _DEFAULT_WAITING_GUIDANCE)
    reason = (failure_reason or "").strip()
    if reason and reason.lower() not in guidance.lower():
        reason = reason.splitlines()[0][:180]
        return f"{reason} — {guidance}"
    return guidance


@dataclass
class ConnectionProbe:
    ok: bool
    message: str
    detail: str = ""


def build_task_prompt(email: str, target_url: str) -> str:
    """Compose the browser-agent instruction for one authorized test account.

    The password is never inlined here: the agent receives it through the
    ``secret`` binding instead, so it stays out of task text and logs.
    """
    return f"""You are running an AUTHORIZED automated QA test of a staging
registration flow that the operator owns. Only use the pages described below;
never touch production systems, real payment details or third-party accounts.

Target signup page: {target_url}

Test account (created for this test run only)
  e-mail:   {email}
  password: {{secret:{SECRET_ALIAS}}}

Steps
1. Open the signup page. Confirm the page loads and the signup form is visible.
2. Verify email-field validation: the e-mail input must accept "{email}".
3. Verify password-field validation: enter the test password in the password
   field (it is injected from the secret binding — never echo it in your text
   output) and confirm the field is accepted.
4. Submit the form.
5. If a CAPTCHA, e-mail verification, phone verification, rate limit or any
   other account protection appears: STOP immediately. Do NOT attempt to solve,
   bypass or retry it. Set human_verification_required = true, set blocker to
   exactly one of captcha / email_verification / phone_verification /
   rate_limit / other, and describe the notice you saw in failure_reason.
6. If registration succeeds, confirm the app lands on the expected dashboard and
   that the test account is authenticated (account menu / e-mail visible).
7. Locate the dashboard's test-credential panel and read the approved test
   credential displayed there (test API key or fixture value). Do not collect
   production tokens, cookies or third-party credentials.
8. Stop as soon as the dashboard is confirmed. Do not log out, do not change
   settings, do not invite users.

Report the result using the provided output schema. Set signup_completed and
dashboard_reached honestly; put a short reason in failure_reason when a step
failed, and list the checks you performed in "checks".
"""


def parse_agent_output(run: Any) -> dict[str, Any]:
    """Extract the structured result from a RunSummary, tolerating plain text."""
    output = getattr(run, "output", None)
    if isinstance(output, dict):
        return output
    if isinstance(output, str) and output.strip():
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    text = getattr(run, "result", None) or ""
    if text.strip():
        # Agents occasionally wrap JSON in prose or a fenced block.
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else None
        if candidate is None:
            start, end = text.find("{"), text.rfind("}")
            if 0 <= start < end:
                candidate = text[start:end + 1]
        if candidate:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, TypeError):
                pass
    return {}


def _boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1"}:
            return True
        if lowered in {"false", "no", "n", "0", ""}:
            return False
    return None


class BrowserUseService:
    """Thin, testable wrapper around the Browser Use v4 SDK.

    The GUI never talks to the SDK directly: everything goes through this
    class, which keeps the network concerns (retries, error translation,
    cancellation, event flattening) in one place.
    """

    def __init__(
        self,
        api_key: str,
        target_url: str,
        password: str,
        *,
        max_cost: float = BROWSER_USE_MAX_COST,
        model: str = BROWSER_USE_MODEL,
        task_timeout: float = TASK_TIMEOUT_SECONDS,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        proxy_country: str = PROXY_COUNTRY_CODE,
        api_timeout: float = API_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.target_url = (target_url or "").strip()
        self.password = password or ""
        self.max_cost = float(max_cost)
        self.model = model.strip()
        self.task_timeout = float(task_timeout)
        self.poll_interval = max(0.25, float(poll_interval))
        self.proxy_country = (proxy_country or "").strip().lower()
        self.api_timeout = float(api_timeout)
        self._client: Any = None
        self._client_lock = threading.Lock()

    # -- configuration ---------------------------------------------------
    def validate_configuration(self, require_password: bool = True) -> None:
        """Raise ConfigurationError with an actionable message."""
        if not BROWSER_USE_AVAILABLE:
            raise ConfigurationError(
                "browser-use-sdk is not installed. Run: pip install browser-use-sdk"
                + (f"  ({BROWSER_USE_IMPORT_ERROR})" if BROWSER_USE_IMPORT_ERROR else "")
            )
        if not self.api_key or self.api_key == "YOUR_API_KEY":
            raise ConfigurationError(
                "BROWSER_USE_API_KEY is not set. Edit the configuration block at the "
                "top of main.py and paste your Browser Use Cloud API key."
            )
        if not self.target_url or "your-authorized-staging-domain" in self.target_url:
            raise ConfigurationError(
                "TARGET_URL still points at the placeholder. Set it to your "
                "authorized staging signup page."
            )
        if not self.target_url.lower().startswith(("http://", "https://")):
            raise ConfigurationError("TARGET_URL must start with http:// or https://")
        if require_password and (not self.password or self.password == "YOUR_TEST_PASSWORD"):
            raise ConfigurationError(
                "No test password configured. Either set DEFAULT_PASSWORD in main.py "
                "or type a password in the Password field."
            )

    # -- client ----------------------------------------------------------
    def client(self) -> Any:
        if BrowserUseV4Client is None:
            raise ConfigurationError(
                "browser-use-sdk is not available. Run: pip install browser-use-sdk"
            )
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = BrowserUseV4Client(
                        api_key=self.api_key, timeout=self.api_timeout
                    )
        return self._client

    def close(self) -> None:
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - closing must never raise
                pass

    # -- connection probe -------------------------------------------------
    def check_connection(self) -> ConnectionProbe:
        """Cheap authenticated call used by the header status indicator."""
        try:
            self.validate_configuration(require_password=False)
        except ConfigurationError as exc:
            return ConnectionProbe(False, "Not configured", str(exc))
        try:
            self.client().browsers.list(page_size=1)
        except APIKeySpendLimitError as exc:  # pragma: no cover - plan specific
            return ConnectionProbe(
                True, "Connected (spend limit reached)", str(getattr(exc, "message", exc))
            )
        except (BrowserUseError, ConfigurationError) as exc:
            if isinstance(exc, ConfigurationError):
                return ConnectionProbe(False, "Not configured", str(exc))
            return ConnectionProbe(False, "Connection failed", self.describe_error(exc))
        except Exception as exc:  # transport / DNS / TLS
            return ConnectionProbe(False, "Connection failed", f"Network error: {exc}")
        return ConnectionProbe(True, "Connected", "Browser Use Cloud API v4 reachable.")

    @staticmethod
    def describe_error(exc: BaseException) -> str:
        if isinstance(exc, ConfigurationError):
            return str(exc)
        if isinstance(exc, APIKeySpendLimitError):
            return (f"Browser Use spend limit reached: {getattr(exc, 'message', exc)}. "
                    "Raise the key's monthly cap or top up.")
        if isinstance(exc, BrowserUseError):
            code = getattr(exc, "status_code", None)
            message = getattr(exc, "message", "") or str(exc)
            friendly = friendly_api_error(code)
            return f"{friendly} {message}".strip()
        if isinstance(exc, TimeoutError):
            return f"Browser Use request timed out: {exc}"
        return f"{type(exc).__name__}: {exc}"

    # -- task execution ---------------------------------------------------
    def _browser_settings(self) -> dict[str, Any]:
        # None disables the Browser Use proxy (direct connection).
        return {"proxyCountryCode": self.proxy_country or None}

    def create_run(self, email: str) -> Any:
        """Create one browser run for an e-mail address."""
        kwargs: dict[str, Any] = {
            "output_schema": TASK_OUTPUT_SCHEMA,
            "max_cost_usd": self.max_cost,
            "browser_settings": self._browser_settings(),
            "secret_bindings": [
                {
                    "alias": SECRET_ALIAS,
                    "source": {"type": "inline", "value": self.password},
                    "allowedDomains": [self._target_host()],
                }
            ],
        }
        if self.model:
            kwargs["model"] = self.model
        return self.client().runs.create(
            build_task_prompt(email, self.target_url), **kwargs
        )

    def _target_host(self) -> str:
        host = re.sub(r"^https?://", "", self.target_url, flags=re.IGNORECASE)
        host = host.split("/", 1)[0].split("?", 1)[0]
        return host.split("@")[-1].split(":", 1)[0] or "localhost"

    def run_status(self, run_id: str) -> str:
        response = self.client().runs.status(run_id)
        status = getattr(response, "status", response)
        return getattr(status, "value", str(status))

    def run_events(self, run_id: str, after: int | None) -> tuple[list[Any], int | None]:
        page = self.client().runs.events(run_id, after=after, limit=50)
        return list(getattr(page, "events", []) or []), getattr(page, "next_after", None)

    def run_summary(self, run_id: str) -> Any:
        return self.client().runs.get(run_id)

    def cancel_run(self, run_id: str) -> None:
        try:
            self.client().runs.cancel(run_id)
        except BrowserUseError as exc:
            LOGGER.warning("Cancel failed for run %s: %s", run_id, exc)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Cancel failed for run %s: %s", run_id, exc)

    def attachment_count(self, run_id: str) -> int:
        try:
            response = self.client().runs.attachments(run_id)
        except Exception:
            return 0
        items = getattr(response, "attachments", None) or []
        return len(items)

    def live_url_for(self, session_id: str) -> str:
        """Best-effort lookup of the live browser view URL."""
        if not session_id:
            return ""
        try:
            view = self.client().browsers.get(session_id)
        except Exception:
            return ""
        return getattr(view, "live_url", "") or ""

    def _pause(self, seconds: float, should_stop: Callable[[], bool]) -> None:
        """Sleep in short slices so Stop is honoured while polling."""
        remaining = float(seconds)
        while remaining > 0:
            if should_stop():
                return
            slice_seconds = min(0.25, remaining)
            time.sleep(slice_seconds)
            remaining -= slice_seconds

    def execute(
        self,
        email: str,
        *,
        emit: Callable[[str, str], None],
        should_stop: Callable[[], bool],
        on_run_created: Callable[[str, str, str], None] | None = None,
    ) -> TaskOutcome:
        """Create, monitor and harvest one run. Blocking; call from a worker.

        ``emit(level, message)`` receives progress lines for the activity log;
        ``should_stop()`` is polled so Stop/Shutdown can interrupt promptly.
        """
        started = time.monotonic()
        try:
            self.validate_configuration()
        except ConfigurationError as exc:
            return TaskOutcome(status=TaskStatus.FAILED, error=str(exc))

        try:
            created = self.create_run(email)
        except Exception as exc:  # noqa: BLE001 - classified below
            code = getattr(exc, "status_code", None)
            return TaskOutcome(
                status=TaskStatus.FAILED,
                error=self.describe_error(exc),
                recoverable=is_recoverable(code) or not isinstance(exc, BrowserUseError),
                duration_seconds=time.monotonic() - started,
            )

        run_id = str(getattr(created, "id", "") or "")
        session_id = str(getattr(created, "session_id", "") or "")
        if not run_id:
            return TaskOutcome(
                status=TaskStatus.FAILED,
                error="Browser Use did not return a run id.",
                duration_seconds=time.monotonic() - started,
            )

        emit("info", f"Browser task created · run {run_id[:8]} (session {session_id[:8]})")
        live_url = self.live_url_for(session_id)
        if live_url:
            emit("info", f"Live view: {live_url}  — open it if a human check appears.")
        if on_run_created:
            on_run_created(run_id, session_id, live_url)

        deadline = started + self.task_timeout
        last_event_id: int | None = None
        last_status = ""
        waiting_announced = False
        notes: list[str] = []

        while True:
            if should_stop():
                self.cancel_run(run_id)
                return TaskOutcome(
                    status=TaskStatus.CANCELLED,
                    error="Stopped by operator.",
                    task_id=run_id,
                    session_id=session_id,
                    live_url=live_url,
                    duration_seconds=time.monotonic() - started,
                    notes=notes,
                )

            try:
                status = self.run_status(run_id)
            except BrowserUseError as exc:
                code = getattr(exc, "status_code", None)
                if is_recoverable(code):
                    if time.monotonic() > deadline:
                        self.cancel_run(run_id)
                        return TaskOutcome(
                            status=TaskStatus.FAILED,
                            error=(f"Browser Use stayed unavailable ({code}) for the whole "
                                   f"{human_duration(self.task_timeout)} budget."),
                            task_id=run_id,
                            session_id=session_id,
                            live_url=live_url,
                            duration_seconds=time.monotonic() - started,
                            recoverable=True,
                            notes=notes,
                        )
                    emit("warn", f"Status poll failed ({code}); retrying…")
                    self._pause(self.poll_interval, should_stop)
                    continue
                return TaskOutcome(
                    status=TaskStatus.FAILED,
                    error=self.describe_error(exc),
                    task_id=run_id,
                    session_id=session_id,
                    live_url=live_url,
                    duration_seconds=time.monotonic() - started,
                    notes=notes,
                )
            except Exception as exc:  # network interruption
                emit("warn", f"Network hiccup while polling: {exc}")
                if time.monotonic() > deadline:
                    return TaskOutcome(
                        status=TaskStatus.FAILED,
                        error=f"Network interruption: {exc}",
                        task_id=run_id,
                        session_id=session_id,
                        live_url=live_url,
                        duration_seconds=time.monotonic() - started,
                        recoverable=True,
                        notes=notes,
                    )
                self._pause(self.poll_interval, should_stop)
                continue

            # Progressive event log (thoughts, navigation, browser status).
            try:
                events, last_event_id = self.run_events(run_id, last_event_id)
            except Exception:
                events = []
            for event in events:
                line = describe_run_event(event)
                if not line:
                    continue
                notes.append(line)
                del notes[:-200]
                emit("step", line)
                blob = json.dumps(getattr(event, "data", {}) or {}, default=str).lower()
                if not waiting_announced and any(
                    keyword in blob
                    for keyword in ("captcha", "human verification", "verify you are human",
                                    "recaptcha", "hcaptcha", "two-factor", "2fa code")
                ):
                    waiting_announced = True
                    emit("warn", "Human verification detected — the agent will stop rather "
                                 "than bypass it. Open the live view to assist.")

            if status in {"completed", "failed", "cancelled"}:
                return self._collect(
                    run_id, status, started, session_id, live_url, notes, emit
                )

            if status != last_status:
                last_status = status
                emit("info", f"Run {run_id[:8]} is {status}…")

            if time.monotonic() > deadline:
                self.cancel_run(run_id)
                return TaskOutcome(
                    status=TaskStatus.FAILED,
                    error=f"Browser task timed out after {human_duration(self.task_timeout)}.",
                    task_id=run_id,
                    session_id=session_id,
                    live_url=live_url,
                    duration_seconds=time.monotonic() - started,
                    recoverable=True,
                    notes=notes,
                )
            self._pause(self.poll_interval, should_stop)

    # -- result harvesting ------------------------------------------------
    def _collect(
        self,
        run_id: str,
        status: str,
        started: float,
        session_id: str,
        live_url: str,
        notes: list[str],
        emit: Callable[[str, str], None],
    ) -> TaskOutcome:
        duration = time.monotonic() - started
        try:
            summary = self.run_summary(run_id)
        except Exception as exc:
            return TaskOutcome(
                status=TaskStatus.FAILED if status != "cancelled" else TaskStatus.CANCELLED,
                error=f"Could not retrieve run result: {self.describe_error(exc)}",
                task_id=run_id,
                session_id=session_id,
                live_url=live_url,
                duration_seconds=duration,
                recoverable=True,
                notes=notes,
            )

        cost = 0.0
        try:
            cost = float(getattr(summary, "total_cost_usd", 0) or 0)
        except (TypeError, ValueError):
            cost = 0.0

        if status == "cancelled":
            return TaskOutcome(
                status=TaskStatus.CANCELLED,
                error="Run cancelled.",
                task_id=run_id,
                session_id=session_id,
                live_url=live_url,
                cost_usd=cost,
                duration_seconds=duration,
                notes=notes,
            )

        output = parse_agent_output(summary)
        run_error = (getattr(summary, "error", "") or "").strip()
        checks = output.get("checks") or []
        if isinstance(checks, list):
            for check in checks[:25]:
                text = str(check).strip()
                if text:
                    notes.append(text)
                    emit("check", text)

        if LOG_ATTACHMENTS:
            attachments = self.attachment_count(run_id)
            if attachments:
                emit("info", f"{attachments} file(s) attached to run {run_id[:8]}.")

        if status == "failed":
            reason = run_error or str(output.get("failure_reason") or "").strip()
            return TaskOutcome(
                status=TaskStatus.FAILED,
                error=reason or "The browser agent reported a failure.",
                task_id=run_id,
                session_id=session_id,
                live_url=live_url,
                cost_usd=cost,
                duration_seconds=duration,
                recoverable=False,
                notes=notes,
            )

        if _boolish(output.get("human_verification_required")):
            blocker = str(output.get("blocker") or "").strip()
            return TaskOutcome(
                status=TaskStatus.WAITING_FOR_USER,
                error=waiting_message(blocker, str(output.get("failure_reason") or "")),
                task_id=run_id,
                session_id=session_id,
                live_url=live_url,
                cost_usd=cost,
                duration_seconds=duration,
                notes=notes,
            )

        signup = _boolish(output.get("signup_completed"))
        dashboard = _boolish(output.get("dashboard_reached"))
        if signup and dashboard:
            account_email = str(output.get("account_email") or "").strip()
            credential = str(output.get("test_credential") or "").strip()
            if credential:
                emit("success", "Test credential captured from the dashboard panel.")
            emit("success", "Dashboard reached — test account authenticated.")
            return TaskOutcome(
                status=TaskStatus.COMPLETED,
                ok=True,
                account_email=account_email,
                credential=credential,
                task_id=run_id,
                session_id=session_id,
                live_url=live_url,
                cost_usd=cost,
                duration_seconds=duration,
                notes=notes,
            )

        reason = str(output.get("failure_reason") or "").strip()
        if not reason:
            detail = (getattr(summary, "result", "") or "").strip()
            reason = detail[:280] if detail else "Signup or dashboard verification failed."
        return TaskOutcome(
            status=TaskStatus.FAILED,
            error=reason,
            task_id=run_id,
            session_id=session_id,
            live_url=live_url,
            cost_usd=cost,
            duration_seconds=duration,
            notes=notes,
        )


_EVENT_LABELS = {
    "browser.ready": "Browser ready",
    "browser.reattached": "Browser re-attached",
    "run.started": "Run started",
    "run.completed": "Run completed",
    "run.failed": "Run failed",
    "run.cancelled": "Run cancelled",
    "run.dispatch_failed": "Run dispatch failed",
    "step.started": "Step started",
    "step.completed": "Step completed",
}


def describe_run_event(event: Any) -> str:
    """Turn a RunEvent into one readable activity-log line."""
    event_type = str(getattr(event, "type", "") or "")
    data = getattr(event, "data", None) or {}
    label = _EVENT_LABELS.get(event_type)
    if label is None:
        if not event_type:
            return ""
        label = event_type.replace("_", " ").replace(".", " · ")
    text = ""
    if isinstance(data, dict):
        for key in ("message", "thought", "text", "description", "detail", "summary",
                    "step", "action", "url"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip().splitlines()[0][:240]
                break
    if text and text.lower() != label.lower():
        return f"{label} · {text}"
    return label


# ==========================================================================
#  Qt worker threads
#
#  The GUI imports Qt below this line; everything above is pure Python and
#  can be unit-tested without a display.
# ==========================================================================

try:
    from PyQt6.QtCore import QObject, QThread, pyqtSignal  # noqa: E402
except ImportError as _qt_core_error:  # pragma: no cover
    fail_missing_qt(_qt_core_error)

LOG_LEVELS = {"info", "step", "check", "success", "warn", "error"}


class ConnectionCheckThread(QThread):
    """Probes the Browser Use API without blocking the event loop."""

    finished_check = pyqtSignal(bool, str, str)  # ok, headline, detail

    def __init__(self, service: BrowserUseService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._service = service

    def run(self) -> None:  # pragma: no cover - thread body
        try:
            probe = self._service.check_connection()
        except Exception as exc:  # noqa: BLE001
            probe = ConnectionProbe(False, "Connection failed", str(exc))
        finally:
            self._service.close()
        self.finished_check.emit(probe.ok, probe.message, probe.detail)


class QueueWorker(QThread):
    """Sequential task queue.

    One e-mail at a time — a new run is never created while a previous run is
    still alive. Emits Qt signals so the GUI stays entirely in the main
    thread.
    """

    task_updated = pyqtSignal(object)             # EmailTask snapshot
    log_message = pyqtSignal(str, str)            # level, message
    queue_state_changed = pyqtSignal(str)         # starting|running|paused|stopping|stopped|finished
    current_email_changed = pyqtSignal(str)       # "" when idle
    summary_ready = pyqtSignal(dict)              # counters for the cards

    def __init__(
        self,
        service: BrowserUseService,
        tasks: Sequence[EmailTask],
        store: ResultStore,
        *,
        max_retries: int = MAX_TASK_RETRIES,
        between_task_delay: float = DELAY_BETWEEN_TASKS_SECONDS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._tasks = [task.copy() for task in tasks]
        self._store = store
        self._max_retries = max(0, int(max_retries))
        self._delay = max(0.0, float(between_task_delay))
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._last_state = ""

    # -- controls (called from the GUI thread) ---------------------------
    def request_stop(self) -> None:
        self._stop.set()
        self._pause.clear()

    def request_pause(self) -> None:
        self._pause.set()

    def request_resume(self) -> None:
        self._pause.clear()

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    # -- helpers ---------------------------------------------------------
    def _emit_log(self, level: str, message: str) -> None:
        self.log_message.emit(level if level in LOG_LEVELS else "info", message)

    def _emit_state(self, state: str) -> None:
        """Emit queue state transitions only once each (no polling churn)."""
        if state == self._last_state:
            return
        self._last_state = state
        self.queue_state_changed.emit(state)

    def _snapshot(self) -> dict[str, int]:
        counts = {
            "total": len(self._tasks),
            "completed": 0,
            "failed": 0,
            "pending": 0,
            "waiting": 0,
            "cancelled": 0,
        }
        for task in self._tasks:
            if task.status is TaskStatus.COMPLETED:
                counts["completed"] += 1
            elif task.status is TaskStatus.FAILED:
                counts["failed"] += 1
            elif task.status is TaskStatus.CANCELLED:
                counts["cancelled"] += 1
            elif task.status is TaskStatus.WAITING_FOR_USER:
                counts["waiting"] += 1
            else:
                counts["pending"] += 1
        return counts

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in slices so Stop is honoured promptly."""
        remaining = float(seconds)
        while remaining > 0 and not self._stop.is_set():
            slice_seconds = min(0.1, remaining)
            self.msleep(int(slice_seconds * 1000))
            remaining -= slice_seconds

    def _persist(self, task: EmailTask) -> None:
        try:
            self._store.append_tasks([task])
        except OutputStoreError as exc:
            self._emit_log("error", f"Results file: {exc}")
        if task.status is TaskStatus.COMPLETED and task.credential:
            try:
                if self._store.append_credential(task.email, task.credential):
                    self._emit_log(
                        "success",
                        f"Approved test credential saved to {self._store.key_path.name} "
                        f"({mask_secret(task.credential)}).",
                    )
            except OutputStoreError as exc:
                self._emit_log("error", f"Credential file: {exc}")

    # -- thread body ------------------------------------------------------
    def run(self) -> None:  # pragma: no cover - exercised via GUI smoke tests
        self._emit_state("running")
        self._emit_log("info", f"Queue started with {len(self._tasks)} address(es).")
        self.summary_ready.emit(self._snapshot())

        for index, task in enumerate(self._tasks, start=1):
            if self._stop.is_set():
                break

            # Honour a pause *before* starting any new browser task.
            while self._pause.is_set() and not self._stop.is_set():
                self._emit_state("paused")
                self.msleep(200)
            if self._stop.is_set():
                break
            self._emit_state("running")

            self.current_email_changed.emit(task.email)
            self._emit_log("info", f"[{index}/{len(self._tasks)}] Processing {task.email}")
            self._process(task)
            self.summary_ready.emit(self._snapshot())

            if self._stop.is_set():
                break
            if self._delay:
                waited = 0.0
                while waited < self._delay and not self._stop.is_set():
                    self.msleep(100)
                    waited += 0.1

        # Anything never started is explicitly marked cancelled on stop.
        if self._stop.is_set():
            self._emit_state("stopping")
            for task in self._tasks:
                if task.status is TaskStatus.PENDING:
                    task.status = TaskStatus.CANCELLED
                    task.error_message = "Stopped before it started."
                    task.created_at = task.created_at or now_iso()
                    self.task_updated.emit(task.copy())
                    self._persist(task)
            self._emit_log("warn", "Queue stopped by operator.")

        self.current_email_changed.emit("")
        self._service.close()
        self.summary_ready.emit(self._snapshot())
        self._emit_state("stopped" if self._stop.is_set() else "finished")
        self._emit_log("info", "Queue finished — worker released.")

    def _process(self, task: EmailTask) -> None:
        attempt = 0
        while True:
            attempt += 1
            task.attempt = attempt
            task.status = TaskStatus.RUNNING
            task.created_at = task.created_at or now_iso()
            task.error_message = ""
            self.task_updated.emit(task.copy())
            self._persist(task)

            def emit(level: str, message: str, _task: EmailTask = task) -> None:
                self._emit_log(level, f"{_task.email} — {message}")

            def on_created(run_id: str, session_id: str, live_url: str) -> None:
                task.task_id = run_id
                task.session_id = session_id
                task.live_url = live_url
                self.task_updated.emit(task.copy())
                self._persist(task)
                if live_url:
                    self._emit_log("info", f"{task.email} — live browser view available.")

            try:
                outcome = self._service.execute(
                    task.email,
                    emit=emit,
                    should_stop=self._stop.is_set,
                    on_run_created=on_created,
                )
            except Exception as exc:  # noqa: BLE001 - a task must never kill the queue
                LOGGER.exception("Unhandled worker error for %s", task.email)
                outcome = TaskOutcome(
                    status=TaskStatus.FAILED,
                    error=f"Unexpected error: {type(exc).__name__}: {exc}",
                    recoverable=False,
                )

            # A pause pressed while the run was in flight is honoured here:
            # the task is parked as WAITING_FOR_USER until the operator resumes,
            # instead of silently starting the next browser task.
            if self._pause.is_set() and not self._stop.is_set() and not outcome.status.is_terminal:
                outcome = replace(
                    outcome,
                    status=TaskStatus.WAITING_FOR_USER,
                    error=outcome.error or "Paused by operator during the run.",
                )

            if outcome.status is TaskStatus.WAITING_FOR_USER:
                task.status = TaskStatus.WAITING_FOR_USER
                task.error_message = outcome.error
            elif outcome.status is TaskStatus.COMPLETED:
                task.status = TaskStatus.COMPLETED
                task.error_message = ""
            elif outcome.status is TaskStatus.CANCELLED:
                task.status = TaskStatus.CANCELLED
                task.error_message = outcome.error or "Stopped by operator."
            else:
                retry_left = self._max_retries - (attempt - 1)
                if outcome.recoverable and retry_left > 0 and not self._stop.is_set():
                    self._emit_log(
                        "warn",
                        f"{task.email} — recoverable failure ({outcome.error}). "
                        f"Retrying ({attempt}/{self._max_retries}).",
                    )
                    self._sleep_interruptible(min(2.0 * attempt, 8.0))
                    if self._stop.is_set():
                        task.status = TaskStatus.CANCELLED
                        task.error_message = "Stopped by operator."
                        break
                    continue
                task.status = TaskStatus.FAILED
                task.error_message = outcome.error or "Unknown failure."

            task.task_id = outcome.task_id or task.task_id
            task.session_id = outcome.session_id or task.session_id
            task.live_url = outcome.live_url or task.live_url
            task.account_email = outcome.account_email or task.account_email
            task.credential = outcome.credential or task.credential
            task.cost_usd = outcome.cost_usd or task.cost_usd
            task.duration_seconds = outcome.duration_seconds
            task.notes = outcome.notes
            task.finished_at = now_iso()
            break

        if task.status is TaskStatus.WAITING_FOR_USER:
            self._emit_log(
                "warn",
                f"{task.email} — parked as WAITING_FOR_USER. Resolve the block through the "
                f"approved test path, then re-run it with START.",
            )
            if task.live_url:
                self._emit_log("info", f"{task.email} — live browser view: {task.live_url}")

        level = {
            TaskStatus.COMPLETED: "success",
            TaskStatus.FAILED: "error",
            TaskStatus.WAITING_FOR_USER: "warn",
            TaskStatus.CANCELLED: "warn",
        }.get(task.status, "info")
        suffix = f" — {task.error_message}" if task.error_message else ""
        self._emit_log(
            level,
            f"{task.email} — {task.status.value} "
            f"({human_duration(task.duration_seconds)}){suffix}",
        )
        self.task_updated.emit(task.copy())
        self._persist(task)

# ==========================================================================
#  Theme + reusable widgets
# ==========================================================================

try:  # noqa: E402
    from PyQt6.QtCore import QSize, Qt, QTimer, QUrl, pyqtSignal
    from PyQt6.QtGui import (
        QColor,
        QDesktopServices,
        QFont,
        QIcon,
        QLinearGradient,
        QPainter,
        QPen,
        QPixmap,
        QTextOption,
    )
    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSizePolicy,
        QTabWidget,
        QTableWidget,
        QTableWidgetItem,
        QToolButton,
        QVBoxLayout,
        QWidget,
    )
except ImportError as _qt_error:  # pragma: no cover
    fail_missing_qt(_qt_error)

COLOR_BG = "#0A0E13"
COLOR_PANEL = "#111821"
COLOR_PANEL_ALT = "#0D141C"
COLOR_BORDER = "#1D2833"
COLOR_TEXT = "#E7EEF7"
COLOR_MUTED = "#8493A6"
COLOR_ACCENT = "#4F7CFF"
COLOR_ACCENT_HOVER = "#648CFE"
COLOR_SUCCESS = "#34D399"
COLOR_WARN = "#F5B851"
COLOR_DANGER = "#F87171"

FONT_STACK = ('"Inter", "Segoe UI Variable", "Segoe UI", "SF Pro Text", '
              '"Helvetica Neue", "DejaVu Sans", Arial, sans-serif')
MONO_STACK = ('"JetBrains Mono", "Cascadia Mono", "SF Mono", "Consolas", '
              '"DejaVu Sans Mono", monospace')

APP_STYLESHEET = f"""
* {{
    font-family: {FONT_STACK};
    outline: none;
}}
QWidget#Root {{
    background: qlineargradient(x1:0, y1:0, x2:0.35, y2:1,
        stop:0 #0B1017, stop:0.55 {COLOR_BG}, stop:1 #080C11);
}}
QFrame#Header {{
    background: transparent;
    border-bottom: 1px solid #182230;
}}
QLabel#AppTitle {{
    color: {COLOR_TEXT};
    font-size: 21px;
    font-weight: 700;
    letter-spacing: 0.2px;
}}
QLabel#AppTagline {{
    color: {COLOR_MUTED};
    font-size: 12px;
}}
QLabel#Version {{
    color: #55657A;
    font-size: 11px;
}}
QFrame#Card {{
    background: {COLOR_PANEL};
    border: 1px solid {COLOR_BORDER};
    border-radius: 12px;
}}
QFrame#StatCard {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #131C26, stop:1 #101720);
    border: 1px solid {COLOR_BORDER};
    border-radius: 12px;
}}
QLabel#CardTitle {{
    color: #9FB0C4;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.1px;
}}
QLabel#StatCaption {{
    color: {COLOR_MUTED};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.8px;
}}
QLabel#StatValue {{
    color: {COLOR_TEXT};
    font-size: 26px;
    font-weight: 700;
}}
QLabel#StatValue[state="success"] {{ color: {COLOR_SUCCESS}; }}
QLabel#StatValue[state="danger"]  {{ color: {COLOR_DANGER}; }}
QLabel#StatValue[state="accent"]  {{ color: {COLOR_ACCENT}; }}
QLabel#StatValue[state="warn"]    {{ color: {COLOR_WARN}; }}
QLabel#StatHint {{
    color: #5E6E82;
    font-size: 11px;
}}
QLabel#FieldLabel {{
    color: {COLOR_MUTED};
    font-size: 12px;
}}
QLabel#Muted {{
    color: {COLOR_MUTED};
    font-size: 11px;
}}
QLabel#FileName {{
    color: #C6D4E4;
    font-size: 12px;
    padding: 8px 10px;
    background: {COLOR_PANEL_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
}}
QLabel#CurrentEmail {{
    color: {COLOR_TEXT};
    font-size: 13px;
    font-weight: 600;
}}
QLabel#MiniStat {{
    color: {COLOR_TEXT};
    font-size: 16px;
    font-weight: 700;
}}
QLabel#MiniCaption {{
    color: {COLOR_MUTED};
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.7px;
}}
QFrame#MiniStatBox {{
    background: {COLOR_PANEL_ALT};
    border: 1px solid #18222E;
    border-radius: 9px;
}}
QFrame#Pill {{
    background: #16202B;
    border: 1px solid #22303E;
    border-radius: 11px;
}}
QFrame#Pill[state="ok"]    {{ background: rgba(52, 211, 153, 26); border-color: rgba(52, 211, 153, 90); }}
QFrame#Pill[state="warn"]  {{ background: rgba(245, 184, 81, 26); border-color: rgba(245, 184, 81, 90); }}
QFrame#Pill[state="error"] {{ background: rgba(248, 113, 113, 26); border-color: rgba(248, 113, 113, 90); }}
QFrame#Pill[state="busy"]  {{ background: rgba(79, 124, 255, 30); border-color: rgba(79, 124, 255, 90); }}
QLabel#PillText {{
    color: #C9D6E5;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
}}
QLabel#PillText[state="ok"]    {{ color: #7FE9C1; }}
QLabel#PillText[state="warn"]  {{ color: #F8CE86; }}
QLabel#PillText[state="error"] {{ color: #FCA5A5; }}
QLabel#PillText[state="busy"]  {{ color: #A9C2FF; }}
QLabel#Dot {{
    font-size: 13px;
    color: #64748B;
}}
QLabel#Dot[state="ok"]    {{ color: {COLOR_SUCCESS}; }}
QLabel#Dot[state="warn"]  {{ color: {COLOR_WARN}; }}
QLabel#Dot[state="error"] {{ color: {COLOR_DANGER}; }}
QLabel#Dot[state="busy"]  {{ color: {COLOR_ACCENT}; }}

QLineEdit {{
    background: {COLOR_PANEL_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    padding: 9px 11px;
    color: {COLOR_TEXT};
    font-size: 13px;
    selection-background-color: {COLOR_ACCENT};
}}
QLineEdit:focus {{
    border: 1px solid {COLOR_ACCENT};
    background: #0F1720;
}}
QLineEdit:disabled {{ color: #5C6B7D; }}

QPushButton {{
    background: #172231;
    border: 1px solid #22303F;
    border-radius: 9px;
    padding: 9px 14px;
    color: #D6E2F0;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton:hover  {{ background: #1C2938; border-color: #2B3B4D; }}
QPushButton:pressed {{ background: #131D29; }}
QPushButton:disabled {{
    background: #10171F;
    border-color: #1A2530;
    color: #4C5A6B;
}}
QPushButton#Primary {{
    background: {COLOR_ACCENT};
    border: 1px solid {COLOR_ACCENT};
    color: #FFFFFF;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.4px;
    padding: 11px 16px;
}}
QPushButton#Primary:hover   {{ background: {COLOR_ACCENT_HOVER}; border-color: {COLOR_ACCENT_HOVER}; }}
QPushButton#Primary:pressed {{ background: #3D68E0; }}
QPushButton#Primary:disabled {{
    background: #1A2432;
    border-color: #22303E;
    color: #4F5E70;
}}
QPushButton#Danger {{
    background: rgba(248, 113, 113, 18);
    border: 1px solid rgba(248, 113, 113, 70);
    color: #FCA5A5;
}}
QPushButton#Danger:hover {{ background: rgba(248, 113, 113, 34); }}
QPushButton#Danger:disabled {{
    background: #10171F;
    border-color: #1A2530;
    color: #4C5A6B;
}}
QPushButton#Ghost {{
    background: transparent;
    border: 1px solid #1C2733;
    color: #93A4B8;
    font-weight: 500;
    padding: 7px 10px;
}}
QPushButton#Ghost:hover {{ background: #141D28; color: #C9D6E5; }}
QPushButton#Link {{
    background: transparent;
    border: none;
    color: #8FB0FF;
    font-size: 11px;
    font-weight: 600;
    padding: 2px 0;
    text-align: left;
}}
QPushButton#Link:hover {{ color: #B7CBFF; }}

QToolButton#Eye {{
    background: {COLOR_PANEL_ALT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    color: #9FB0C4;
    font-size: 11px;
    font-weight: 600;
    padding: 8px 10px;
}}
QToolButton#Eye:hover {{ background: #16202B; color: {COLOR_TEXT}; }}

QCheckBox {{
    color: #9FB0C4;
    font-size: 11px;
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid #2A3A4B;
    background: {COLOR_PANEL_ALT};
}}
QCheckBox::indicator:checked {{
    background: {COLOR_ACCENT};
    border-color: {COLOR_ACCENT};
}}

QPlainTextEdit#LogView {{
    background: {COLOR_PANEL_ALT};
    border: 1px solid #18222E;
    border-radius: 9px;
    color: #C7D4E3;
    font-family: {MONO_STACK};
    font-size: 12px;
    padding: 8px;
    selection-background-color: #253752;
}}
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 12px;
    background: {COLOR_PANEL};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: #7E8FA3;
    padding: 8px 16px;
    margin-right: 4px;
    border: 1px solid transparent;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    font-size: 12px;
    font-weight: 600;
}}
QTabBar::tab:selected {{
    color: {COLOR_TEXT};
    background: {COLOR_PANEL};
    border: 1px solid {COLOR_BORDER};
    border-bottom-color: {COLOR_PANEL};
}}
QTabBar::tab:hover:!selected {{ color: #AFC0D2; }}

QTableWidget#QueueTable {{
    background: {COLOR_PANEL_ALT};
    alternate-background-color: #0F1620;
    border: 1px solid #18222E;
    border-radius: 9px;
    color: #C7D4E3;
    font-size: 12px;
    gridline-color: transparent;
}}
QTableWidget#QueueTable::item {{ padding: 6px 8px; }}
QHeaderView::section {{
    background: #131C26;
    color: #8B9CAF;
    border: none;
    border-bottom: 1px solid #1C2836;
    padding: 7px 8px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.5px;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #26333F;
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: #33424F; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #26333F; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{
    background: #16202B;
    color: #D6E2F0;
    border: 1px solid #26333F;
    border-radius: 6px;
    padding: 6px 8px;
}}
QMessageBox {{ background: {COLOR_PANEL}; }}
QMessageBox QLabel {{ color: {COLOR_TEXT}; font-size: 13px; }}
"""


def repolish(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def build_app_icon() -> QIcon:
    """Small generated icon so the window has an identity without assets."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    gradient = QLinearGradient(0, 0, 64, 64)
    gradient.setColorAt(0.0, QColor("#5B8CFF"))
    gradient.setColorAt(1.0, QColor("#2F4FD8"))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 16, 16)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(34)
    painter.setFont(font)
    painter.setPen(QPen(QColor("#FFFFFF")))
    # PyQt6 wants a plain int for the flags argument of this overload.
    painter.drawText(pixmap.rect(), int(Qt.AlignmentFlag.AlignCenter), "V")
    painter.end()
    return QIcon(pixmap)


class StatusPill(QFrame):
    """Rounded status chip with a coloured dot."""

    clicked = pyqtSignal()

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Pill")
        self.setProperty("state", "idle")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(11, 5, 12, 5)
        layout.setSpacing(7)
        self._dot = QLabel("●")
        self._dot.setObjectName("Dot")
        self._label = QLabel(text)
        self._label.setObjectName("PillText")
        layout.addWidget(self._dot)
        layout.addWidget(self._label)

    def set_state(self, state: str, text: str | None = None) -> None:
        for widget in (self, self._dot, self._label):
            widget.setProperty("state", state)
            repolish(widget)
        if text is not None:
            self._label.setText(text)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class StatCard(QFrame):
    """Compact metric card: caption, big value, small hint."""

    def __init__(
        self,
        caption: str,
        value: str = "0",
        hint: str = "",
        state: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setProperty("state", state)
        self.setMinimumHeight(84)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(2)
        self._caption = QLabel(caption.upper())
        self._caption.setObjectName("StatCaption")
        self._value = QLabel(value)
        self._value.setObjectName("StatValue")
        if state:
            self._value.setProperty("state", state)
        self._hint = QLabel(hint)
        self._hint.setObjectName("StatHint")
        layout.addWidget(self._caption)
        layout.addWidget(self._value)
        layout.addWidget(self._hint)
        layout.addStretch(1)

    def set_value(self, value: str, hint: str | None = None) -> None:
        self._value.setText(value)
        if hint is not None:
            self._hint.setText(hint)

    def set_compact(self, compact: bool) -> None:
        """Smaller type for text-heavy cards such as the current e-mail."""
        size = 15 if compact else 26
        self._value.setStyleSheet(f"font-size: {size}px; font-weight: 700;")


class MiniStat(QFrame):
    """Very small metric block used inside the import card."""

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MiniStatBox")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(1)
        self._value = QLabel("0")
        self._value.setObjectName("MiniStat")
        self._caption = QLabel(caption.upper())
        self._caption.setObjectName("MiniCaption")
        layout.addWidget(self._value)
        layout.addWidget(self._caption)

    def set_value(self, value: int | str) -> None:
        self._value.setText(str(value))


class Card(QFrame):
    """Panel with a heading and an optional header action row."""

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(16, 14, 16, 15)
        self._root.setSpacing(11)

        header = QHBoxLayout()
        header.setSpacing(8)
        title_label = QLabel(title.upper())
        title_label.setObjectName("CardTitle")
        header.addWidget(title_label)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("Muted")
            header.addWidget(sub)
        header.addStretch(1)
        self.header = header
        self._root.addLayout(header)

    def add_widget(self, widget: QWidget, stretch: int = 0) -> None:
        self._root.addWidget(widget, stretch)

    def add_layout(self, layout) -> None:  # noqa: ANN001
        self._root.addLayout(layout)

    def add_stretch(self, stretch: int = 1) -> None:
        self._root.addStretch(stretch)

# ==========================================================================
#  Main window
# ==========================================================================

_STATUS_STATES = {
    "Idle": ("idle", ""),
    "Ready": ("ok", ""),
    "Running": ("busy", ""),
    "Paused": ("warn", ""),
    "Stopping": ("warn", ""),
    "Waiting for user": ("warn", ""),
    "Completed": ("ok", ""),
    "Stopped": ("error", ""),
    "Error": ("error", ""),
}

_LOG_COLORS = {
    "info": "#8FA5BC",
    "step": "#A9BDD4",
    "check": "#7FB7E8",
    "success": COLOR_SUCCESS,
    "warn": COLOR_WARN,
    "error": COLOR_DANGER,
}

_STATUS_TEXT_COLOR = {
    STATUS_COMPLETED: COLOR_SUCCESS,
    STATUS_FAILED: COLOR_DANGER,
    STATUS_RUNNING: COLOR_ACCENT,
    STATUS_WAITING: COLOR_WARN,
    STATUS_CANCELLED: "#94A3B8",
}


class MainWindow(QMainWindow):
    """Dashboard: import → configure → run → export."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  ·  v{APP_VERSION}")
        self.setMinimumSize(1180, 820)
        self.resize(1320, 900)
        self.setWindowIcon(build_app_icon())

        # ---- state ------------------------------------------------------
        self._store = ResultStore()
        self._tasks: list[EmailTask] = []
        self._task_index: dict[str, EmailTask] = {}
        self._import_path: Path | None = None
        self._worker: QueueWorker | None = None
        self._conn_thread: ConnectionCheckThread | None = None
        self._service: BrowserUseService | None = None
        self._run_state = "Idle"
        self._live_view_opened: set[str] = set()

        # ---- widgets ----------------------------------------------------
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 16, 18, 16)
        body_layout.setSpacing(14)
        body_layout.addLayout(self._build_stat_row())
        body_layout.addWidget(self._build_tabs(), 1)
        body_layout.addWidget(self._build_footer())
        outer.addWidget(body, 1)

        # ---- initial wiring ---------------------------------------------
        self._apply_password_default()
        self._refresh_derived_counts()
        self._load_previous_results()
        self._update_controls()
        self._log("info", f"{APP_NAME} v{APP_VERSION} ready.")
        self._log("info", f"Output folder: {self._store.csv_path.parent}")
        if not BROWSER_USE_AVAILABLE:
            self._log("error", "browser-use-sdk is missing — run: pip install browser-use-sdk")
        elif BROWSER_USE_API_KEY == "YOUR_API_KEY":
            self._log("warn", "Configuration placeholder detected: edit BROWSER_USE_API_KEY "
                              "at the top of main.py before starting.")
        else:
            QTimer.singleShot(400, self.check_connection)

    # ==================================================================
    #  Layout construction
    # ==================================================================
    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 15, 20, 15)
        layout.setSpacing(14)

        mark = QLabel()
        mark.setPixmap(build_app_icon().pixmap(QSize(38, 38)))
        mark.setFixedSize(40, 40)
        layout.addWidget(mark)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        title = QLabel(APP_NAME)
        title.setObjectName("AppTitle")
        tagline = QLabel(APP_TAGLINE)
        tagline.setObjectName("AppTagline")
        titles.addWidget(title)
        titles.addWidget(tagline)
        layout.addLayout(titles)
        layout.addStretch(1)

        self.api_pill = StatusPill("API: checking…")
        self.api_pill.set_state("busy", "API: checking…")
        self.api_pill.setToolTip("Click to re-check the Browser Use API connection.")
        self.api_pill.clicked.connect(self.check_connection)
        layout.addWidget(self.api_pill)

        self.app_pill = StatusPill("Idle")
        self.app_pill.set_state("idle", "●  Idle")
        layout.addWidget(self.app_pill)

        version = QLabel(f"v{APP_VERSION}")
        version.setObjectName("Version")
        layout.addWidget(version)
        return header

    def _build_stat_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        self.card_total = StatCard("Total emails", "0", "imported addresses")
        self.card_done = StatCard("Completed", "0", "0% of queue", state="success")
        self.card_failed = StatCard("Failed", "0", "0% of queue", state="danger")
        self.card_pending = StatCard("Pending", "0", "awaiting processing", state="accent")
        self.card_current = StatCard("Current email", "—", "idle", state="warn")
        self.card_current.set_compact(True)
        for card in (self.card_total, self.card_done, self.card_failed,
                     self.card_pending, self.card_current):
            row.addWidget(card, 1)
        return row

    def _build_tabs(self) -> QWidget:
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_setup_tab(), "Setup & controls")
        self.tabs.addTab(self._build_activity_tab(), "Live activity")
        self.tabs.addTab(self._build_queue_tab(), "Task queue")
        return self.tabs

    # -- tab 1 -----------------------------------------------------------
    def _build_setup_tab(self) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setHorizontalSpacing(14)
        layout.setVerticalSpacing(14)

        # Email import ---------------------------------------------------
        import_card = Card("Email import", "TXT · one address per line")
        upload_row = QHBoxLayout()
        upload_row.setSpacing(8)
        self.btn_upload = QPushButton("Upload TXT…")
        self.btn_upload.setObjectName("Primary")
        self.btn_upload.clicked.connect(self.choose_file)
        self.btn_reload = QPushButton("Reload")
        self.btn_reload.setObjectName("Ghost")
        self.btn_reload.clicked.connect(self.reload_file)
        self.btn_reload.setEnabled(False)
        upload_row.addWidget(self.btn_upload)
        upload_row.addWidget(self.btn_reload)
        upload_row.addStretch(1)
        import_card.add_layout(upload_row)

        self.lbl_filename = QLabel("No file selected")
        self.lbl_filename.setObjectName("FileName")
        self.lbl_filename.setWordWrap(False)
        self.lbl_filename.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        import_card.add_widget(self.lbl_filename)

        mini_row = QHBoxLayout()
        mini_row.setSpacing(8)
        self.mini_total = MiniStat("Total")
        self.mini_pending = MiniStat("Pending")
        self.mini_done = MiniStat("Processed")
        for mini in (self.mini_total, self.mini_pending, self.mini_done):
            mini_row.addWidget(mini, 1)
        import_card.add_layout(mini_row)

        self.lbl_import_hint = QLabel("Duplicates and malformed lines are skipped automatically.")
        self.lbl_import_hint.setObjectName("Muted")
        self.lbl_import_hint.setWordWrap(True)
        import_card.add_widget(self.lbl_import_hint)
        import_card.add_stretch(1)
        layout.addWidget(import_card, 0, 0)

        # Password -------------------------------------------------------
        password_card = Card("Test password", "injected as a Browser Use secret")
        password_card.add_widget(self._label("Password used for every test registration"))
        pw_row = QHBoxLayout()
        pw_row.setSpacing(8)
        self.txt_password = QLineEdit()
        self.txt_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_password.setPlaceholderText("Test account password")
        self.txt_password.textChanged.connect(self._on_password_changed)
        self.txt_password.setClearButtonEnabled(False)
        self.btn_eye = QToolButton()
        self.btn_eye.setObjectName("Eye")
        self.btn_eye.setText("Show")
        self.btn_eye.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_eye.setCheckable(True)
        self.btn_eye.setToolTip("Show or hide the password")
        self.btn_eye.toggled.connect(self._toggle_password_visibility)
        pw_row.addWidget(self.txt_password, 1)
        pw_row.addWidget(self.btn_eye)
        password_card.add_layout(pw_row)

        defaults_row = QHBoxLayout()
        self.btn_default_pw = QPushButton("Use configured DEFAULT_PASSWORD")
        self.btn_default_pw.setObjectName("Link")
        self.btn_default_pw.clicked.connect(self._apply_password_default)
        defaults_row.addWidget(self.btn_default_pw)
        defaults_row.addStretch(1)
        password_card.add_layout(defaults_row)

        self.lbl_password_state = QLabel("")
        self.lbl_password_state.setObjectName("Muted")
        self.lbl_password_state.setWordWrap(True)
        password_card.add_widget(self.lbl_password_state)
        password_card.add_stretch(1)
        layout.addWidget(password_card, 0, 1)

        # Controls -------------------------------------------------------
        control_card = Card("Run controls", "one browser task at a time")
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_start = QPushButton("▶  START")
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self.start_processing)
        self.btn_pause = QPushButton("Pause")
        self.btn_pause.clicked.connect(self.pause_processing)
        self.btn_resume = QPushButton("Resume")
        self.btn_resume.clicked.connect(self.resume_processing)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.clicked.connect(self.stop_processing)
        for button in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop):
            btn_row.addWidget(button, 1)
        control_card.add_layout(btn_row)

        options_row = QHBoxLayout()
        self.chk_open_live_view = QCheckBox("Open live browser view when a task starts")
        self.chk_open_live_view.setChecked(False)
        self.chk_open_live_view.setToolTip(
            "Launch the Browser Use live view URL in your default browser as soon as a "
            "run is created."
        )
        options_row.addWidget(self.chk_open_live_view)
        options_row.addStretch(1)
        self.btn_check_api = QPushButton("Test API connection")
        self.btn_check_api.setObjectName("Ghost")
        self.btn_check_api.clicked.connect(self.check_connection)
        options_row.addWidget(self.btn_check_api)
        control_card.add_layout(options_row)

        self.lbl_control_hint = QLabel(
            "Pause is honoured between tasks; Stop cancels the active browser run."
        )
        self.lbl_control_hint.setObjectName("Muted")
        self.lbl_control_hint.setWordWrap(True)
        control_card.add_widget(self.lbl_control_hint)
        control_card.add_stretch(1)
        layout.addWidget(control_card, 0, 2)

        # Export ---------------------------------------------------------
        export_card = Card("Results & export", f"{self._store.csv_path.name} · "
                                               f"{self._store.key_path.name}")
        export_row = QHBoxLayout()
        export_row.setSpacing(8)
        self.btn_export = QPushButton("Export CSV…")
        self.btn_export.clicked.connect(self.export_csv)
        self.btn_folder = QPushButton("Open folder")
        self.btn_folder.clicked.connect(self.open_output_folder)
        self.btn_clear = QPushButton("Clear completed")
        self.btn_clear.setObjectName("Danger")
        self.btn_clear.clicked.connect(self.clear_completed)
        for button in (self.btn_export, self.btn_folder, self.btn_clear):
            export_row.addWidget(button, 1)
        export_card.add_layout(export_row)

        self.lbl_paths = QLabel(
            f"CSV  →  {self._store.csv_path}\nKEY  →  {self._store.key_path}"
        )
        self.lbl_paths.setObjectName("Muted")
        self.lbl_paths.setWordWrap(True)
        self.lbl_paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        export_card.add_widget(self.lbl_paths)
        export_card.add_stretch(1)
        layout.addWidget(export_card, 1, 0)

        # Configuration summary ------------------------------------------
        config_card = Card("Configuration", "editable at the top of main.py")
        self.lbl_config = QLabel(self._config_summary())
        self.lbl_config.setObjectName("Muted")
        self.lbl_config.setWordWrap(True)
        self.lbl_config.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_config.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        config_card.add_widget(self.lbl_config)
        config_card.add_stretch(1)
        layout.addWidget(config_card, 1, 1, 1, 2)

        layout.setColumnStretch(0, 3)
        layout.setColumnStretch(1, 3)
        layout.setColumnStretch(2, 3)
        return page

    # -- tab 2 -----------------------------------------------------------
    def _build_activity_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        label = QLabel("LIVE ACTIVITY LOG")
        label.setObjectName("CardTitle")
        toolbar.addWidget(label)
        toolbar.addStretch(1)
        self.btn_clear_log = QPushButton("Clear log")
        self.btn_clear_log.setObjectName("Ghost")
        self.btn_clear_log.clicked.connect(self.clear_log)
        toolbar.addWidget(self.btn_clear_log)
        layout.addLayout(toolbar)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("LogView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        self.log_view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.addWidget(self.log_view, 1)
        return page

    # -- tab 3 -----------------------------------------------------------
    def _build_queue_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        label = QLabel("TASK QUEUE")
        label.setObjectName("CardTitle")
        toolbar.addWidget(label)
        toolbar.addStretch(1)
        self.lbl_queue_hint = QLabel("Select a row to open its Browser Use live view.")
        self.lbl_queue_hint.setObjectName("Muted")
        toolbar.addWidget(self.lbl_queue_hint)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 5)
        self.table.setObjectName("QueueTable")
        self.table.setHorizontalHeaderLabels(["EMAIL", "STATUS", "RUN ID", "DURATION", "DETAIL"])
        vertical = self.table.verticalHeader()
        if vertical is not None:
            vertical.setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(self._open_live_view_for_row)
        layout.addWidget(self.table, 1)
        return page

    # -- footer ----------------------------------------------------------
    def _build_footer(self) -> QWidget:
        footer = QWidget()
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(10)
        self.lbl_footer = QLabel("Ready.")
        self.lbl_footer.setObjectName("Muted")
        layout.addWidget(self.lbl_footer, 1)
        disclaimer = QLabel(
            "Authorized testing only · no CAPTCHA, verification or rate-limit bypass"
        )
        disclaimer.setObjectName("Muted")
        layout.addWidget(disclaimer)
        return footer

    # ==================================================================
    #  Small builders / helpers
    # ==================================================================
    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    def _config_summary(self) -> str:
        key_state = (
            "configured" if BROWSER_USE_API_KEY and BROWSER_USE_API_KEY != "YOUR_API_KEY"
            else "<span style='color:#F5B851'>placeholder</span>"
        )
        url_state = TARGET_URL
        if "your-authorized-staging-domain" in TARGET_URL:
            url_state = f"<span style='color:#F5B851'>{html.escape(TARGET_URL)}</span>"
        else:
            url_state = html.escape(TARGET_URL)
        rows = [
            f"API key · {key_state}",
            f"Target · {url_state}",
            f"Max cost / task · ${BROWSER_USE_MAX_COST:.2f}",
            f"Task timeout · {human_duration(TASK_TIMEOUT_SECONDS)}",
            f"Model · {BROWSER_USE_MODEL or 'SDK default'}",
            f"Retries · {MAX_TASK_RETRIES} recoverable",
        ]
        return "<br>".join(rows)

    def _apply_password_default(self) -> None:
        if DEFAULT_PASSWORD and DEFAULT_PASSWORD != "YOUR_TEST_PASSWORD":
            self.txt_password.setText(DEFAULT_PASSWORD)
            self.lbl_password_state.setText("Prefilled from DEFAULT_PASSWORD in main.py.")
        else:
            self.lbl_password_state.setText(
                "DEFAULT_PASSWORD is still a placeholder — type a password above."
            )

    def _on_password_changed(self, text: str) -> None:
        if not text:
            self.lbl_password_state.setText("Password is required to start a run.")
        elif DEFAULT_PASSWORD and text == DEFAULT_PASSWORD:
            self.lbl_password_state.setText("Using the configured test password.")
        else:
            self.lbl_password_state.setText(f"Custom password set ({len(text)} characters).")
        self._update_controls()

    def _toggle_password_visibility(self, visible: bool) -> None:
        self.txt_password.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.btn_eye.setText("Hide" if visible else "Show")

    # ==================================================================
    #  Logging
    # ==================================================================
    def _log(self, level: str, message: str) -> None:
        level = level if level in _LOG_COLORS else "info"
        colour = _LOG_COLORS[level]
        tag = {"info": "INFO", "step": "STEP", "check": "CHECK", "success": " OK ",
               "warn": "WARN", "error": "FAIL"}[level]
        safe = html.escape(message)
        self.log_view.appendHtml(
            f"<span style='color:#586A7E'>{clock()}</span>&nbsp;&nbsp;"
            f"<span style='color:{colour};font-weight:600'>[{tag}]</span>&nbsp;&nbsp;"
            f"<span style='color:{'#C7D4E3' if level == 'info' else colour}'>{safe}</span>"
        )
        self.lbl_footer.setText(f"{clock()}  ·  {message[:150]}")
        bar = self.log_view.verticalScrollBar()
        if bar is not None:
            bar.setValue(bar.maximum())

    def clear_log(self) -> None:
        self.log_view.clear()
        self._log("info", "Activity log cleared.")

    # ==================================================================
    #  Status / counter presentation
    # ==================================================================
    def _set_run_state(self, state: str) -> None:
        self._run_state = state
        css_state, _ = _STATUS_STATES.get(state, ("idle", ""))
        self.app_pill.set_state(css_state, f"●  {state}")

    def _refresh_derived_counts(self) -> None:
        total = len(self._tasks)
        completed = sum(1 for t in self._tasks if t.status is TaskStatus.COMPLETED)
        failed = sum(1 for t in self._tasks if t.status is TaskStatus.FAILED)
        cancelled = sum(1 for t in self._tasks if t.status is TaskStatus.CANCELLED)
        waiting = sum(1 for t in self._tasks if t.status is TaskStatus.WAITING_FOR_USER)
        running = sum(1 for t in self._tasks if t.status is TaskStatus.RUNNING)
        pending = total - completed - failed - cancelled - waiting - running

        def pct(value: int) -> str:
            return f"{(value / total * 100):.0f}% of queue" if total else "no queue"

        self.card_total.set_value(str(total), "imported addresses" if total else "no file loaded")
        self.card_done.set_value(str(completed), pct(completed))
        self.card_failed.set_value(str(failed), pct(failed + cancelled) + (
            f" · {cancelled} cancelled" if cancelled else ""))
        self.card_pending.set_value(str(max(0, pending)),
                                    f"{waiting} waiting for user" if waiting else "awaiting processing")
        self.mini_total.set_value(total)
        self.mini_pending.set_value(max(0, pending) + waiting + running)
        self.mini_done.set_value(completed + failed + cancelled)
        self._sync_table()

    def _set_current_email(self, email: str) -> None:
        if not email:
            self.card_current.set_value("—", "idle")
        else:
            display = email if len(email) <= 26 else email[:24] + "…"
            self.card_current.set_value(display, "processing now")
        self.card_current.setToolTip(email)

    # ==================================================================
    #  Queue table
    # ==================================================================
    def _sync_table(self) -> None:
        """Refresh the queue table in place (items are reused when possible)."""
        if self.table.rowCount() != len(self._tasks):
            self.table.setRowCount(len(self._tasks))
        for row, task in enumerate(self._tasks):
            values = [
                task.email,
                task.status.value,
                (task.task_id[:8] if task.task_id else "—"),
                human_duration(task.duration_seconds),
                task.error_message or (", ".join(task.notes[-2:]) if task.notes else "—"),
            ]
            for column, text in enumerate(values):
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem(text)
                    self.table.setItem(row, column, item)
                elif item.text() != text:
                    # Reuse the item so selection/scroll survive live updates.
                    item.setText(text)
                if column == 1:
                    colour = _STATUS_TEXT_COLOR.get(task.status.value)
                    if colour:
                        item.setForeground(QColor(colour))
                if column == 4:
                    item.setToolTip(task.error_message or "\n".join(task.notes[-12:]))
                if column == 0:
                    item.setToolTip(task.email)

    def _open_live_view_for_row(self, item: QTableWidgetItem) -> None:
        task = self._tasks[item.row()] if 0 <= item.row() < len(self._tasks) else None
        if task is None:
            return
        if task.live_url:
            QDesktopServices.openUrl(QUrl(task.live_url))
        else:
            self._log("warn", f"{task.email} — no live view URL recorded for this task.")

    # ==================================================================
    #  Import
    # ==================================================================
    def choose_file(self) -> None:
        start_dir = str(self._import_path.parent) if self._import_path else str(Path.home())
        filename, _ = QFileDialog.getOpenFileName(
            self, "Select email list", start_dir, "Text files (*.txt);;All files (*)"
        )
        if filename:
            self._load_file(Path(filename))

    def reload_file(self) -> None:
        if self._import_path:
            self._load_file(self._import_path)
        else:
            self._log("warn", "No file has been imported yet.")

    def _load_file(self, path: Path) -> None:
        if self._is_busy():
            QMessageBox.warning(self, APP_NAME,
                                "Stop the current run before importing a new list.")
            return
        try:
            report = read_email_file(path)
        except FileNotFoundError:
            self._log("error", f"Missing TXT file: {path}")
            QMessageBox.critical(self, APP_NAME, f"The file does not exist:\n{path}")
            return
        except (ValueError, IsADirectoryError, OSError) as exc:
            self._log("error", f"Could not read the email file: {exc}")
            QMessageBox.critical(self, APP_NAME, f"Could not read the file:\n{exc}")
            return

        if not report.emails:
            self._log("error", f"{path.name} contains no valid e-mail addresses.")
            QMessageBox.warning(
                self, APP_NAME,
                "No valid addresses found.\n\n"
                f"Invalid lines: {len(report.invalid)}\n"
                "Expected one address per line, e.g. test1@example.com",
            )
            return

        self._import_path = path
        self._tasks = [EmailTask(email=email) for email in report.emails]
        self._task_index = {task.email.casefold(): task for task in self._tasks}
        self._merge_previous_results()

        self.lbl_filename.setText(f"{path.name}   ·   {len(self._tasks)} valid address(es)")
        self.lbl_filename.setToolTip(str(path))
        self.btn_reload.setEnabled(True)

        summary = [f"Imported {len(self._tasks)} address(es) from {path.name}."]
        if report.duplicates:
            summary.append(f"{report.duplicates} duplicate(s) skipped")
        if report.invalid:
            summary.append(f"{len(report.invalid)} invalid line(s) ignored")
        self._log("success", " · ".join(summary))
        if report.invalid:
            preview = ", ".join(report.invalid[:5])
            more = "" if len(report.invalid) <= 5 else f" (+{len(report.invalid) - 5} more)"
            self._log("warn", f"Ignored malformed entries: {preview}{more}")
        self.lbl_import_hint.setText(
            f"{len(self._tasks)} unique address(es) loaded in original order."
            + (f" {report.duplicates} duplicate(s) removed." if report.duplicates else "")
        )

        self._refresh_derived_counts()
        self._set_run_state("Ready")
        self._update_controls()

    def _merge_previous_results(self) -> None:
        """Show previous outcomes so a restart does not re-test completed accounts."""
        try:
            rows = {row["email"].casefold(): row for row in self._store.read_rows()}
        except OutputStoreError as exc:
            self._log("error", f"Could not read previous results: {exc}")
            return
        restored = 0
        for key, task in self._task_index.items():
            row = rows.get(key)
            if not row:
                continue
            try:
                status = TaskStatus(row["status"])
            except ValueError:
                continue
            if status is TaskStatus.PENDING:
                continue
            task.status = status
            task.created_at = row["created_at"]
            task.task_id = row["task_id"]
            task.error_message = row["error_message"]
            task.session_id = row["session_id"]
            task.live_url = row["live_url"]
            task.account_email = row["account_email"]
            try:
                task.duration_seconds = float(row["duration_seconds"]) if row["duration_seconds"] else None
                task.cost_usd = float(row["cost_usd"]) if row["cost_usd"] else 0.0
            except ValueError:
                pass
            restored += 1
        if restored:
            self._log("info", f"Restored {restored} previous result(s) from "
                              f"{self._store.csv_path.name} (they are skipped by default).")

    def _load_previous_results(self) -> None:
        try:
            rows = self._store.read_rows()
        except OutputStoreError as exc:
            self._log("error", f"Results file: {exc}")
            return
        if rows:
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
            detail = ", ".join(f"{value} {key.lower()}" for key, value in sorted(counts.items()))
            self._log("info", f"Existing results: {detail} in {self._store.csv_path.name}.")

    # ==================================================================
    #  Controls
    # ==================================================================
    def _is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _pending_tasks(self, include_failed: bool = True) -> list[EmailTask]:
        allowed = {TaskStatus.PENDING}
        if include_failed:
            allowed.update({TaskStatus.FAILED, TaskStatus.WAITING_FOR_USER, TaskStatus.CANCELLED})
        return [task for task in self._tasks if task.status in allowed]

    def _update_controls(self) -> None:
        busy = self._is_busy()
        paused = busy and self._worker is not None and self._worker.is_paused
        pending = len(self._pending_tasks())
        has_password = bool(self.txt_password.text())
        has_queue = bool(self._tasks)

        self.btn_start.setEnabled(not busy and has_queue and pending > 0 and has_password)
        self.btn_pause.setEnabled(busy and not paused)
        self.btn_resume.setEnabled(busy and paused)
        self.btn_stop.setEnabled(busy)
        self.btn_upload.setEnabled(not busy)
        self.btn_reload.setEnabled(not busy and self._import_path is not None)
        self.btn_clear.setEnabled(not busy)
        self.btn_export.setEnabled(True)
        self.btn_check_api.setEnabled(not busy)

        if not has_queue:
            hint = "Import a TXT file to build the queue."
        elif not has_password:
            hint = "A test password is required before starting."
        elif not pending:
            hint = "Every imported address already has a result. Clear completed to re-run."
        elif busy:
            hint = ("Paused — click Resume to continue." if paused else
                    "Processing… Pause is applied between tasks, Stop cancels the active run.")
        else:
            hint = f"{pending} address(es) queued. Pause is honoured between tasks."
        self.lbl_control_hint.setText(hint)

    def start_processing(self) -> None:
        if self._is_busy():
            self._log("warn", "A queue is already running.")
            return
        if self.text_shows_placeholder():
            QMessageBox.warning(
                self, APP_NAME,
                "Configuration is incomplete.\n\n"
                "Edit BROWSER_USE_API_KEY and TARGET_URL at the top of main.py, "
                "then restart the application.",
            )
            return
        if not self._tasks:
            QMessageBox.information(self, APP_NAME, "Import a TXT file of e-mail addresses first.")
            return

        password = self.txt_password.text()
        if not password:
            QMessageBox.warning(self, APP_NAME, "Enter the test account password.")
            self.txt_password.setFocus()
            return

        queue = self._pending_tasks()
        if not queue:
            QMessageBox.information(
                self, APP_NAME,
                "Nothing left to process.\n\nUse “Clear completed” if you want to re-run "
                "accounts that already finished.",
            )
            return

        self._service = BrowserUseService(
            api_key=BROWSER_USE_API_KEY,
            target_url=TARGET_URL,
            password=password,
        )
        try:
            self._service.validate_configuration()
        except ConfigurationError as exc:
            self._log("error", str(exc))
            QMessageBox.critical(self, APP_NAME, f"Configuration error:\n\n{exc}")
            return

        self._worker = QueueWorker(self._service, queue, self._store, parent=self)
        worker = self._worker
        worker.task_updated.connect(self._on_task_updated)
        worker.log_message.connect(self._on_worker_log)
        worker.queue_state_changed.connect(self._on_queue_state)
        worker.current_email_changed.connect(self._set_current_email)
        worker.summary_ready.connect(self._on_summary)
        worker.finished.connect(self._on_worker_finished)

        self._live_view_opened.clear()
        self._log("info", f"Starting {len(queue)} task(s) sequentially.")
        self._set_run_state("Running")
        self._update_controls()
        worker.start()

    def text_shows_placeholder(self) -> bool:
        return (not BROWSER_USE_AVAILABLE
                or BROWSER_USE_API_KEY == "YOUR_API_KEY"
                or "your-authorized-staging-domain" in TARGET_URL)

    def pause_processing(self) -> None:
        if self._worker is None or not self._worker.isRunning():
            return
        self._worker.request_pause()
        self._log("info", "Pause requested — the active browser task will finish first.")
        self._set_run_state("Paused")
        self._update_controls()

    def resume_processing(self) -> None:
        if self._worker is None or not self._worker.isRunning():
            return
        self._worker.request_resume()
        self._log("info", "Resuming queue.")
        self._set_run_state("Running")
        self._update_controls()

    def stop_processing(self) -> None:
        if self._worker is None or not self._worker.isRunning():
            return
        self._worker.request_stop()
        self._log("warn", "Stop requested — cancelling the active browser run…")
        self._set_run_state("Stopping")
        self._update_controls()

    # ==================================================================
    #  Worker signal handlers
    # ==================================================================
    def _on_task_updated(self, task: EmailTask) -> None:
        existing = self._task_index.get(task.email.casefold())
        if existing is not None:
            existing.status = task.status
            existing.task_id = task.task_id
            existing.session_id = task.session_id
            existing.live_url = task.live_url
            existing.created_at = task.created_at
            existing.error_message = task.error_message
            existing.account_email = task.account_email
            existing.credential = task.credential
            existing.cost_usd = task.cost_usd
            existing.duration_seconds = task.duration_seconds
            existing.attempt = task.attempt
            existing.notes = list(task.notes)
            if (task.status is TaskStatus.RUNNING and task.live_url
                    and self.chk_open_live_view.isChecked()
                    and task.email not in self._live_view_opened):
                self._live_view_opened.add(task.email)
                QDesktopServices.openUrl(QUrl(task.live_url))
        self._refresh_derived_counts()
        if task.status is TaskStatus.WAITING_FOR_USER:
            self._set_run_state("Waiting for user")
            self.tabs.setCurrentIndex(1)
        self._update_controls()

    def _on_worker_log(self, level: str, message: str) -> None:
        self._log(level, message)

    def _on_queue_state(self, state: str) -> None:
        mapping = {
            "running": "Running",
            "paused": "Paused",
            "stopping": "Stopping",
            "finished": "Completed",
            "stopped": "Stopped",
        }
        if state in mapping:
            self._set_run_state(mapping[state])
        self._update_controls()

    def _on_summary(self, counts: dict) -> None:
        self._refresh_derived_counts()

    def _on_worker_finished(self) -> None:
        waiting = sum(1 for t in self._tasks if t.status is TaskStatus.WAITING_FOR_USER)
        if self._run_state in {"Stopping", "Stopped", "Paused"}:
            self._set_run_state("Stopped")
        elif waiting:
            self._set_run_state("Waiting for user")
        else:
            self._set_run_state("Completed")
        self._set_current_email("")
        self._worker = None
        self._refresh_derived_counts()
        self._update_controls()
        counts = {"completed": 0, "failed": 0, "waiting": 0, "cancelled": 0}
        for task in self._tasks:
            key = {
                TaskStatus.COMPLETED: "completed",
                TaskStatus.FAILED: "failed",
                TaskStatus.WAITING_FOR_USER: "waiting",
                TaskStatus.CANCELLED: "cancelled",
            }.get(task.status)
            if key:
                counts[key] += 1
        summary = " · ".join(f"{value} {name}" for name, value in counts.items() if value)
        self._log("info", f"Queue finished · {summary or 'nothing to do'}.")
        self.tabs.setCurrentIndex(1)

    # ==================================================================
    #  API connection
    # ==================================================================
    def check_connection(self) -> None:
        if self._conn_thread is not None and self._conn_thread.isRunning():
            return
        if not BROWSER_USE_AVAILABLE:
            self.api_pill.set_state("error", "API: SDK missing")
            self._log("error", "browser-use-sdk is not installed (pip install browser-use-sdk).")
            return
        checker = BrowserUseService(
            api_key=BROWSER_USE_API_KEY,
            target_url=TARGET_URL,
            password=self.txt_password.text() or DEFAULT_PASSWORD,
        )
        self.api_pill.set_state("busy", "API: checking…")
        self._conn_thread = ConnectionCheckThread(checker, self)
        self._conn_thread.finished_check.connect(self._on_connection_result)
        self._conn_thread.finished.connect(self._conn_thread.deleteLater)
        self._conn_thread.start()

    def _on_connection_result(self, ok: bool, headline: str, detail: str) -> None:
        if ok:
            self.api_pill.set_state("ok", f"API: {headline}")
            self._log("success", f"Browser Use API · {headline}. {detail}".strip())
        else:
            self.api_pill.set_state("error", f"API: {headline}")
            self._log("error", f"Browser Use API · {headline}. {detail}".strip())
        self.api_pill.setToolTip(detail or headline)
        self._conn_thread = None

    # ==================================================================
    #  Export / maintenance
    # ==================================================================
    def export_csv(self) -> None:
        if self._is_busy():
            self._log("warn", "Export is available when the queue is idle.")
            return
        default_name = f"veriphone-results-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
        destination, _ = QFileDialog.getSaveFileName(
            self, "Export results", str(self._store.csv_path.parent / default_name),
            "CSV files (*.csv)"
        )
        if not destination:
            return
        try:
            count, path = self._store.export_csv(destination)
        except OutputStoreError as exc:
            self._log("error", f"Export failed: {exc}")
            QMessageBox.critical(self, APP_NAME, f"Export failed:\n\n{exc}")
            return
        self._log("success", f"Exported {count} row(s) to {path}.")
        self.lbl_footer.setText(f"Exported {count} row(s) → {path}")

    def open_output_folder(self) -> None:
        folder = self._store.csv_path.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._log("error", f"Cannot create output folder: {exc}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            self._log("warn", f"Could not open the folder automatically: {folder}")
        else:
            self._log("info", f"Opened {folder}")

    def clear_completed(self) -> None:
        if self._is_busy():
            self._log("warn", "Stop the run before clearing results.")
            return
        completed = [t for t in self._tasks if t.status is TaskStatus.COMPLETED]
        if not completed and not any(
            row["status"] == STATUS_COMPLETED for row in self._safe_rows()
        ):
            QMessageBox.information(self, APP_NAME, "There are no completed results to clear.")
            return
        confirm = QMessageBox.question(
            self, APP_NAME,
            f"Remove {len(completed)} completed result(s) from {self._store.csv_path.name} "
            f"and their credentials from {self._store.key_path.name}?\n\n"
            "Failed and pending rows are kept. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self._store.clear_completed()
        except OutputStoreError as exc:
            self._log("error", f"Could not clear results: {exc}")
            QMessageBox.critical(self, APP_NAME, f"Could not clear results:\n\n{exc}")
            return
        for task in completed:
            task.status = TaskStatus.PENDING
            task.task_id = ""
            task.error_message = ""
            task.credential = ""
            task.duration_seconds = None
            task.notes.clear()
        self._refresh_derived_counts()
        self._update_controls()
        self._log("success", f"Cleared {removed} completed row(s) from the output files.")

    def _safe_rows(self) -> list[dict[str, str]]:
        try:
            return self._store.read_rows()
        except OutputStoreError:
            return []

    # ==================================================================
    #  Shutdown
    # ==================================================================
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._is_busy():
            confirm = QMessageBox.question(
                self, APP_NAME,
                "A run is in progress. Stop it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            assert self._worker is not None
            self._worker.request_stop()
            if not self._worker.wait(8000):
                self._worker.terminate()
                self._worker.wait(2000)
        if self._conn_thread is not None and self._conn_thread.isRunning():
            self._conn_thread.wait(3000)
        event.accept()


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("VeriPhone")
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    app.setWindowIcon(build_app_icon())

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())
