"""Sweep thresholds over a checked-in gold set without external API calls."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.subsidiary import db, logic  # noqa: E402

GOLD = Path(__file__).resolve().parents[1] / "data" / "gold_set.json"


def main():
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    subsidiaries = db.get_subsidiaries()
    for threshold in (0.33, 0.5, 0.66, 1.0):
        tp = fp = fn = 0
        for company, expected in gold.items():
            rows = db.get_phrase_rows(company)
            phrases = [row["keyword"] for row in rows]
            vectors = {row["keyword"]: row.get("scores") or {} for row in rows}
            configured = [dict(s, threshold=threshold) for s in subsidiaries]
            actual = set(logic.aggregate(phrases, vectors, configured))
            expected = set(expected)
            tp += len(actual & expected)
            fp += len(actual - expected)
            fn += len(expected - actual)
        precision = tp / (tp + fp) if tp + fp else 0
        recall = tp / (tp + fn) if tp + fn else 0
        print("threshold=%.2f precision=%.3f recall=%.3f tp=%d fp=%d fn=%d" %
              (threshold, precision, recall, tp, fp, fn))


if __name__ == "__main__":
    main()
