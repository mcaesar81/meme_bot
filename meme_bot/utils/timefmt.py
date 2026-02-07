from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Chicago")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ensure_aware_utc(parsed)


def iso_utc(dt: datetime | None = None) -> str:
    value = ensure_aware_utc(dt or now_utc())
    return value.astimezone(timezone.utc).isoformat()


def iso_local(dt: datetime | None = None) -> str:
    value = ensure_aware_utc(dt or now_utc())
    return value.astimezone(LOCAL_TZ).isoformat()


def local_prefix(dt: datetime | None = None) -> str:
    value = ensure_aware_utc(dt or now_utc())
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %z")
