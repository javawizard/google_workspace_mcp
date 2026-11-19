"""
Account names: short names that stand in for Google account email addresses.

Point ``WORKSPACE_MCP_ACCOUNTS_FILE`` at a JSON file that maps names to emails::

    {
      "work": "me@example.com",
      "personal": "me@example.org"
    }

Tool callers can then pass ``user_google_email="work"`` instead of the address.
Email addresses keep working as before. The file lives outside the repo so the
real addresses never need to be committed anywhere.

Names only apply in legacy OAuth 2.0 mode, where the caller chooses the account.
In OAuth 2.1 and trusted-gateway modes the account comes from the verified
sign-in, so the mapping is ignored.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

ACCOUNTS_FILE_ENV = "WORKSPACE_MCP_ACCOUNTS_FILE"

# Lowercase letters, digits, "-" and "_", starting with a letter or digit. No "@",
# so a value can always be told apart from an email address.
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccountsConfigError(ValueError):
    """The accounts file is missing, unreadable or malformed."""


class UnknownAccountError(ValueError):
    """A caller passed an account name that isn't in the accounts file."""


@dataclass(frozen=True)
class Accounts:
    """Name <-> email mapping loaded from the accounts file."""

    by_name: Dict[str, str]

    @property
    def names(self) -> List[str]:
        return list(self.by_name)

    def resolve(self, value: str) -> str:
        """Return the email for an account name, or ``value`` itself if it's an email."""
        if "@" in value:
            if not _EMAIL_PATTERN.match(value.strip()):
                raise UnknownAccountError(
                    f"{value!r} is neither a configured Google account name nor a "
                    f"valid email address. Configured account names: "
                    f"{', '.join(self.names)}."
                )
            return value
        email = self.by_name.get(value.strip().lower())
        if email is None:
            known = ", ".join(self.names) or "(none configured)"
            raise UnknownAccountError(
                f"Unknown Google account name {value!r}. Configured account names: "
                f"{known}. Pass one of these names or a Google email address, or "
                "call list_google_accounts to see them."
            )
        return email


def parse_accounts(data: object, source: str) -> Accounts:
    """Validate the decoded JSON from an accounts file."""
    if not isinstance(data, dict):
        raise AccountsConfigError(
            f"{source}: expected a JSON object mapping account names to emails"
        )

    by_name: Dict[str, str] = {}
    seen_emails: Dict[str, str] = {}
    for raw_name, raw_email in data.items():
        name = str(raw_name).strip().lower()
        if not _NAME_PATTERN.match(name):
            raise AccountsConfigError(
                f"{source}: invalid account name {raw_name!r}; use lowercase letters, "
                "digits, '-' and '_', starting with a letter or digit"
            )
        if name in by_name:
            raise AccountsConfigError(
                f"{source}: account name {name!r} appears more than once"
            )
        if not isinstance(raw_email, str) or not _EMAIL_PATTERN.match(
            raw_email.strip()
        ):
            raise AccountsConfigError(
                f"{source}: account {name!r} must map to an email address"
            )
        # Google addresses are case-insensitive; credential files and session
        # bindings are keyed by the lowercase form.
        email = raw_email.strip().lower()
        if email in seen_emails:
            raise AccountsConfigError(
                f"{source}: accounts {seen_emails[email]!r} and {name!r} "
                "have the same email address"
            )
        seen_emails[email] = name
        by_name[name] = email

    if not by_name:
        raise AccountsConfigError(f"{source}: no accounts listed")
    return Accounts(by_name=by_name)


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def load_accounts_file(path: str) -> Accounts:
    expanded = os.path.expanduser(path)
    try:
        with open(expanded, encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=_reject_duplicate_keys)
    except FileNotFoundError:
        raise AccountsConfigError(f"{ACCOUNTS_FILE_ENV} file not found: {expanded}")
    except _DuplicateKeyError as exc:
        raise AccountsConfigError(
            f"{expanded}: account name {str(exc)!r} appears more than once"
        )
    # ValueError covers JSONDecodeError and UnicodeDecodeError.
    except (OSError, ValueError) as exc:
        raise AccountsConfigError(f"could not read {expanded}: {exc}")
    return parse_accounts(data, expanded)


_accounts: Optional[Accounts] = None
_load_error: Optional[AccountsConfigError] = None
_loaded = False


def get_accounts() -> Optional[Accounts]:
    """Return the configured accounts, or None if no accounts file is set.

    The file is read once per process, so edits take effect on restart. A bad
    file raises AccountsConfigError, and keeps raising the same error without
    re-reading the file.
    """
    global _accounts, _load_error, _loaded
    if not _loaded:
        _loaded = True
        path = os.getenv(ACCOUNTS_FILE_ENV, "").strip()
        try:
            _accounts = load_accounts_file(path) if path else None
        except AccountsConfigError as exc:
            _load_error = exc
        if _accounts is not None:
            logger.info(
                "Loaded %d Google account name(s) from %s: %s",
                len(_accounts.names),
                path,
                ", ".join(_accounts.names),
            )
    if _load_error is not None:
        raise _load_error
    return _accounts


def reset_accounts_cache() -> None:
    """Forget the loaded accounts (for tests)."""
    global _accounts, _load_error, _loaded
    _accounts = None
    _load_error = None
    _loaded = False
