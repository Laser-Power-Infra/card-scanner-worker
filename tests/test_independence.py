"""Guards the merge's core promise: the two features stay independent.

Neither feature may import the other. Only ``app.shared`` is common.
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _source_files(package):
    return list((REPO / "app" / package).rglob("*.py"))


@pytest.mark.parametrize("package,forbidden", [
    ("profile", "app.subsidiary"),
    ("subsidiary", "app.profile"),
])
def test_feature_does_not_import_the_other(package, forbidden):
    offenders = []
    for path in _source_files(package):
        text = path.read_text(encoding="utf-8")
        if forbidden in text:
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], "%s imported %s: %s" % (package, forbidden, offenders)


def test_features_share_only_app_shared():
    """app/shared must not depend on either feature."""
    for path in (REPO / "app" / "shared").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "app.profile" not in text
        assert "app.subsidiary" not in text
