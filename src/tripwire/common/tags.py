"""Tag parsing, ignore check, ttl parsing."""

import re
from datetime import UTC, datetime, timedelta

_RELATIVE = re.compile(r"(\d+)([mhdw])")
_ABSOLUTE = re.compile(r"\d{4}-\d{2}-\d{2}(t\d{2}:\d{2}(:\d{2})?z)?")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def tags_to_dict(tag_list: list[dict[str, str]] | None) -> dict[str, str]:
    """Convert AWS [{"Key": .., "Value": ..}] tag lists (EC2, RDS, ELBv2) to a dict."""
    return {t["Key"]: t.get("Value", "") for t in tag_list or []}


def _get(tags: dict[str, str], key: str) -> str | None:
    for k, v in tags.items():
        if k.strip().lower() == key:
            return v
    return None


def is_ignored(tags: dict[str, str]) -> bool:
    return (_get(tags, "tripwire") or "").strip().lower() == "ignore"


def get_ttl_value(tags: dict[str, str]) -> str | None:
    return _get(tags, "ttl")


def _require_aware(*dts: datetime | None) -> None:
    for dt in dts:
        if dt is not None and dt.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")


def parse_ttl(value: str | None, created_at: datetime) -> datetime | None:
    """Return the UTC expiry for a ttl tag value, or None if it is unreadable.

    Relative values (30m, 4h, 2d, 1w) count from created_at. Absolute values are
    UTC: 2026-10-20 (midnight) or 2026-10-20T18:00Z.
    """
    _require_aware(created_at)
    v = (value or "").strip().lower()
    try:
        if m := _RELATIVE.fullmatch(v):
            amount = int(m.group(1))
            if amount == 0:
                return None
            return created_at.astimezone(UTC) + timedelta(**{_UNITS[m.group(2)]: amount})
        if _ABSOLUTE.fullmatch(v):
            dt = datetime.fromisoformat(v.upper())
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, OverflowError):  # 2026-13-45, or a ttl past year 9999
        pass
    return None


def is_expired(expiry: datetime, now: datetime) -> bool:
    _require_aware(expiry, now)
    return now >= expiry


def needs_warning(expiry: datetime, now: datetime, window: timedelta = timedelta(hours=1)) -> bool:
    """True within `window` before expiry (not once expired)."""
    _require_aware(expiry, now)
    return expiry - window <= now < expiry
