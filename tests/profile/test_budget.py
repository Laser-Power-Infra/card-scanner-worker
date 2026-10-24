import json
import os

import pytest

from app.profile import budget


@pytest.fixture
def usage_file(tmp_path):
    p = tmp_path / "usage.json"
    os.environ["GEOAPIFY_USAGE_FILE"] = str(p)
    os.environ["GEOAPIFY_MONTHLY_CAP"] = "3"
    yield p
    os.environ.pop("GEOAPIFY_USAGE_FILE", None)
    os.environ.pop("GEOAPIFY_MONTHLY_CAP", None)


def test_reserve_increments_and_reports_remaining(usage_file):
    assert budget.reserve() is True
    assert budget.remaining() == 2
    assert budget.reserve() is True
    assert budget.reserve() is True
    assert budget.reserve() is False
    assert budget.remaining() == 0


def test_cap_blocks_after_limit(usage_file):
    for _ in range(3):
        assert budget.reserve() is True
    assert budget.reserve() is False
    assert budget.status()["count"] == 3


def test_month_rollover_resets_count(monkeypatch, usage_file):
    assert budget.reserve() is True
    # Force a different month on next read.
    monkeypatch.setattr(budget, "_month", lambda: "2099-01")
    assert budget.remaining() == 3
    assert budget.reserve() is True
    data = json.loads(usage_file.read_text())
    assert data["month"] == "2099-01"
    assert data["count"] == 1


def test_daily_cap_blocks(usage_file, monkeypatch):
    monkeypatch.setenv("GEOAPIFY_DAILY_CAP", "2")
    assert budget.reserve() is True
    assert budget.reserve() is True
    assert budget.reserve() is False
    assert budget.status()["day_count"] == 2


def test_corrupt_file_resets(usage_file):
    usage_file.write_text("{not json")
    assert budget.reserve() is True
    data = json.loads(usage_file.read_text())
    assert data["count"] == 1


def test_explicit_path_overrides_env(tmp_path):
    p = tmp_path / "other.json"
    assert budget.reserve(path=str(p), cap=1) is True
    assert budget.reserve(path=str(p), cap=1) is False
    assert not (tmp_path / "usage.json").exists()
