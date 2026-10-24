"""Monthly + daily guardrail counter for Geoapify geocoding requests.

Backed by a single JSON file on the local host (the worker is not serverless).
Counts every outbound attempt, including retries, so the caps are conservative.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

log = logging.getLogger("card-desk.budget")

_DEFAULT_CAP = 70000
_DEFAULT_DAILY_CAP = 3000
_STALE_LOCK_SECONDS = 30


def _path(path=None):
    return path or os.environ.get(
        "GEOAPIFY_USAGE_FILE", "data/geoapify_usage.json")


def _cap(cap=None):
    if cap is not None:
        return int(cap)
    return int(os.environ.get("GEOAPIFY_MONTHLY_CAP", str(_DEFAULT_CAP)))


def _month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_cap(cap=None):
    if cap is not None:
        return int(cap)
    return int(os.environ.get("GEOAPIFY_DAILY_CAP", str(_DEFAULT_DAILY_CAP)))


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _lock(path):
    lock = path + ".lock"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            return fd, lock
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > _STALE_LOCK_SECONDS:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            time.sleep(0.01)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"month": _month(), "count": 0, "day": _day(), "day_count": 0,
                "updated_at": None}
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        log.warning("geocode usage file corrupt (%s); resetting", exc)
        return {"month": _month(), "count": 0, "day": _day(), "day_count": 0,
                "updated_at": None}
    if not isinstance(data, dict) or data.get("month") != _month():
        return {"month": _month(), "count": 0, "day": _day(), "day_count": 0,
                "updated_at": None}
    if data.get("day") != _day():
        data["day"] = _day()
        data["day_count"] = 0
    for key in ("count", "day_count"):
        try:
            data[key] = int(data.get(key, 0))
        except (TypeError, ValueError):
            data[key] = 0
    return data


def reserve(path=None, cap=None, daily_cap=None):
    """Increment the counter if under both caps. Returns True if allowed."""
    path = _path(path)
    cap = _cap(cap)
    daily_cap = _daily_cap(daily_cap)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, lock = _lock(path)
    try:
        data = _read(path)
        if data["count"] >= cap:
            log.error("geocode monthly cap reached (%d/%d); refusing request",
                      data["count"], cap)
            return False
        if data.get("day_count", 0) >= daily_cap:
            log.error("geocode daily cap reached (%d/%d); refusing request",
                      data["day_count"], daily_cap)
            return False
        data["count"] += 1
        data["day_count"] = int(data.get("day_count", 0)) + 1
        data["updated_at"] = _utc_now()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(lock)
        except OSError:
            pass


def remaining(path=None, cap=None, daily_cap=None):
    path = _path(path)
    data = _read(path)
    monthly = max(0, _cap(cap) - int(data.get("count", 0)))
    daily = max(0, _daily_cap(daily_cap) - int(data.get("day_count", 0)))
    return min(monthly, daily)


def status(path=None, cap=None, daily_cap=None):
    path = _path(path)
    data = _read(path)
    monthly_cap = _cap(cap)
    daily_cap = _daily_cap(daily_cap)
    return {
        "month": data["month"],
        "count": int(data.get("count", 0)),
        "cap": monthly_cap,
        "remaining": max(0, monthly_cap - int(data.get("count", 0))),
        "day": data.get("day"),
        "day_count": int(data.get("day_count", 0)),
        "daily_cap": daily_cap,
        "day_remaining": max(0, daily_cap - int(data.get("day_count", 0))),
    }
