"""Owner-run Flex reporting setup/probe; never an issuer of live risk authority.

Secrets are accepted only at a terminal and stored in one exact, create-only
Keychain item. The full account identifier is runtime-only, as required by the
existing provider profile. No command mutates trading, policy, or activation.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import warnings

from .broker.ibkr_flex import IbkrFlexError, IbkrFlexQuery, IbkrFlexReportReader
from .calendar import NEW_YORK
from .gmail_oauth_setup import CreateOnlyMacOSKeychain, SetupError as KeychainSetupError
from .provider_clients import KeychainItem, MacOSKeychain
from .provider_profile import IbkrLocalProviderProfile

FLEX_ITEM = KeychainItem(
    "titan-full-live-ibkr-ending-3103-flex-reporting", "ibkr-live-ending-3103"
)
_SCHEMA = "titan_ibkr_flex_reporting_credentials_v1"
_FIELDS = {"schema_version", "account_key", "query_id", "token"}
_ENROLLMENT_INPUTS = {
    "QUERY_ID": (
        r"[0-9]{1,32}",
        "Activity Flex Query ID (hidden; paste the numeric ID, then press Return): ",
        "Enter the numeric Query ID from Titan Daily Accounting's Info panel "
        "(1-32 digits). Paste only that value, without labels, quotes or spaces. "
        "Nothing appears while you type or paste; press Return once when done.",
    ),
    "TOKEN": (
        r"[0-9]{6,128}",
        "Flex Web Service reporting token (hidden; paste Current Token, then press Return): ",
        "Enter the existing Current Token from Flex Web Service Configuration "
        "(6-128 digits). Paste only that value, without labels, quotes or spaces. "
        "Nothing appears while you type or paste; press Return once when done.",
    ),
}


class FlexSetupError(RuntimeError):
    def __init__(self, code: str):
        self.code = "IBKR_FLEX_SETUP_" + code
        super().__init__(self.code)


def _result(ok: bool, code: str, **details) -> dict:
    return {**details, "ok": ok, "code": code, "reporting_only": True,
            "daily_starting_equity_ready": False, "live_cash_flow_complete_through": None}


def _profile(policy):
    try:
        profile = IbkrLocalProviderProfile.from_config(policy.config)
        if (not isinstance(profile, IbkrLocalProviderProfile)
                or profile.account_key != FLEX_ITEM.account or profile.account_last4 != "3103"):
            raise ValueError()
        return profile
    except Exception:
        raise FlexSetupError("PROFILE_INVALID") from None


def _require_terminal() -> None:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise FlexSetupError("OWNER_TERMINAL_REQUIRED")


def _account(profile) -> str:
    _require_terminal()
    value = _hidden("Exact IBKR account ID (hidden; runtime only): ")
    if not re.fullmatch(r"[A-Z]{1,3}[0-9]{4,12}", value) or not value.endswith(profile.account_last4):
        raise FlexSetupError("ACCOUNT_INVALID")
    return value


def _hidden(prompt: str) -> str:
    # isatty alone is insufficient: getpass otherwise falls back to echoed
    # input when terminal echo control fails. Fail before any fallback read.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise FlexSetupError("PRIVATE_INPUT_UNAVAILABLE") from None


def _enrollment_input(field: str) -> str:
    pattern, prompt, _ = _ENROLLMENT_INPUTS[field]
    value = _hidden(prompt)
    if value == "":
        raise FlexSetupError(field + "_EMPTY")
    if type(value) is not str or not re.fullmatch(pattern, value):
        raise FlexSetupError(field + "_FORMAT_INVALID")
    # Preserve the exact input. Do not normalize, echo, fingerprint or retain
    # malformed input; only static field/error names may leave this helper.
    return value


def _enrollment_error(code: str) -> dict:
    for field, (_, _, action) in _ENROLLMENT_INPUTS.items():
        if code in {
            "IBKR_FLEX_SETUP_" + field + "_EMPTY",
            "IBKR_FLEX_SETUP_" + field + "_FORMAT_INVALID",
        }:
            return _result(False, code, action_required=action)
    return _result(False, code)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FlexSetupError("CREDENTIAL_INVALID")
        result[key] = value
    return result


def _credentials(raw: bytes, profile) -> dict:
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= 4096:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
        if (type(value) is not dict or set(value) != _FIELDS
                or value["schema_version"] != _SCHEMA
                or value["account_key"] != profile.account_key
                or type(value["query_id"]) is not str
                or not re.fullmatch(r"[0-9]{1,32}", value["query_id"])
                or type(value["token"]) is not str
                or not re.fullmatch(r"[0-9]{6,128}", value["token"])):
            raise ValueError()
        return value
    except Exception:
        raise FlexSetupError("CREDENTIAL_INVALID") from None


def _read_owner_credentials(profile) -> dict:
    """Read the exact item as the same owner application used at enrollment.

    This attended read lets macOS own its access dialog's lifetime. It neither
    changes ACLs nor qualifies unattended access, and never creates an item.
    """
    _require_terminal()
    try:
        raw = CreateOnlyMacOSKeychain(maximum_bytes=4096).read(FLEX_ITEM)
    except KeychainSetupError as exc:
        codes = {
            "GMAIL_SETUP_KEYCHAIN_READ_UNAVAILABLE": "KEYCHAIN_READ_UNAVAILABLE",
            "GMAIL_SETUP_KEYCHAIN_READ_FAILED": "KEYCHAIN_READ_FAILED",
            "GMAIL_SETUP_KEYCHAIN_ITEM_INVALID": "KEYCHAIN_ITEM_INVALID",
        }
        code = exc.args[0] if len(exc.args) == 1 and type(exc.args[0]) is str else None
        raise FlexSetupError(codes.get(code, "KEYCHAIN_READ_FAILED")) from None
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        raise FlexSetupError("KEYCHAIN_READ_FAILED") from None
    return _credentials(raw, profile)


def setup_status(policy) -> dict:
    """Exact metadata lookup only. Presence is not an authentication test."""
    try:
        _profile(policy)
        state = MacOSKeychain().metadata_status(FLEX_ITEM)
        if state not in {"PRESENT", "MISSING", "UNAVAILABLE"}:
            state = "UNAVAILABLE"
        return _result(state == "PRESENT", "IBKR_FLEX_SETUP_" + state,
                       credential_metadata=state, authentication_tested=False)
    except Exception:
        return _result(False, "IBKR_FLEX_SETUP_STATUS_UNAVAILABLE", authentication_tested=False)


def enroll(policy) -> dict:
    """Create-only owner enrollment; never overwrite custody or change its ACL."""
    try:
        profile = _profile(policy)
        _require_terminal()
        keychain = CreateOnlyMacOSKeychain(maximum_bytes=4096)
        if keychain.metadata_status(FLEX_ITEM) != "MISSING":
            raise FlexSetupError("EXISTING_OR_UNAVAILABLE_CUSTODY")
        payload = {"schema_version": _SCHEMA, "account_key": profile.account_key,
                   "query_id": _enrollment_input("QUERY_ID"),
                   "token": _enrollment_input("TOKEN")}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        _credentials(raw, profile)
        keychain.add(FLEX_ITEM, raw)
        return _result(True, "IBKR_FLEX_SETUP_ENROLLED", authentication_tested=False)
    except FlexSetupError as exc:
        return _enrollment_error(exc.code)
    except (KeyboardInterrupt, EOFError):
        return _result(False, "IBKR_FLEX_SETUP_OWNER_CANCELED")
    except Exception:
        # A failed readback can leave a created item. Never automatically retry
        # enrollment or disclose a platform exception that may contain data.
        return _result(False, "IBKR_FLEX_SETUP_ENROLLMENT_FAILED_REVIEW_CUSTODY")


@contextmanager
def _probe_lock(install_root: Path):
    """One local probe at a time; regular, owner-private file, no symlink hops."""
    root_fd = control_fd = lock_fd = None
    try:
        root_fd = os.open(install_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        control_fd = os.open("control", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        for fd in (root_fd, control_fd):
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise FlexSetupError("LOCK_DIRECTORY_UNSAFE")
        lock_fd = os.open("flex-reporting-probe.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                          0o600, dir_fd=control_fd)
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise FlexSetupError("LOCK_UNSAFE")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FlexSetupError("PROBE_BUSY") from None
        yield
    except OSError:
        raise FlexSetupError("LOCK_UNAVAILABLE") from None
    finally:
        for fd in (lock_fd, control_fd, root_fd):
            if fd is not None:
                os.close(fd)


def probe(policy, *, report_date: date, install_root: Path) -> dict:
    """One generation, at most three same-ticket retrievals on provider 1019.

    This is an attended, read-only reporting diagnostic, not an unattended
    credential qualification or current cash-flow completeness check. The
    native owner Keychain read may wait for a macOS access dialog, just as
    enrollment does; it does not alter access controls or suppress prompts.
    All calls using the token elsewhere must also coordinate per-token pacing.
    """
    try:
        profile = _profile(policy)
        if type(report_date) is not date or report_date >= datetime.now(timezone.utc).astimezone(NEW_YORK).date():
            raise FlexSetupError("HISTORICAL_DATE_REQUIRED")
        account = _account(profile)
        with _probe_lock(Path(install_root)):
            credential = _read_owner_credentials(profile)
            query = IbkrFlexQuery(credential["query_id"], account, report_date, report_date)
            reader = IbkrFlexReportReader(token_reader=lambda: credential["token"])
            # Also space separate CLI invocations without writing a token,
            # ticket, raw report, or private account identifier to a journal.
            time.sleep(6)
            ticket = reader.request_report(query)
            for attempt in range(3):
                time.sleep(6)
                try:
                    report = reader.retrieve_report(ticket)
                    break
                except IbkrFlexError as exc:
                    if exc.code != "IBKR_FLEX_PROVIDER_1019" or attempt == 2:
                        raise
            summary = report.diagnostic_summary()
            # Byte digests are linkable private report identifiers, not redaction.
            summary.pop("response_sha256", None)
            summary.pop("generation_response_sha256", None)
            return _result(True, "IBKR_FLEX_SETUP_REPORT_RECEIVED", **summary)
    except (FlexSetupError, IbkrFlexError) as exc:
        return _result(False, exc.code)
    except (KeyboardInterrupt, EOFError):
        return _result(False, "IBKR_FLEX_SETUP_OWNER_CANCELED")
    except Exception:
        return _result(False, "IBKR_FLEX_SETUP_PROBE_FAILED")
