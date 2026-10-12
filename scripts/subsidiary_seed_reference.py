"""Idempotently seed subsidiary keywords from data/company_keywords (1).json."""
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.subsidiary import config  # noqa: E402
from app.subsidiary.db import _run  # noqa: E402
from app.subsidiary.logic import normalize_phrase  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1] / "data" / "company_keywords.json"
NEGATIVE_VETO = {
    "LASER POWER & INFRA": {"Automotive cable", "Optical fibre"},
    "GM DALUI & SONS / GMD": {"Tyre valve", "Automotive valve", "Engine valve",
                              "Heart valve", "Medical valve"},
    "UIC UDYOG LIMITED": {"Jewellery wire", "Music wire", "Medical wire",
                          "Decorative wire"},
    "CEEBUILD": {"Building construction", "Furniture fabrication",
                 "Automobile fabrication"},
}
PROCUREMENT = {"TENDER / COMMERCIAL"}
TECHNICAL = {"TECHNICAL", "TECHNICAL / STANDARDS"}
DEMAND = {"APPLICATIONS", "INDUSTRY", "INDUSTRIAL APPLICATIONS",
          "WATER / INFRASTRUCTURE", "CUSTOMER / PROJECT"}
OWN = {
    "CORE BUSINESS", "CORE PRODUCTS", "CABLES", "CONDUCTORS", "MANUFACTURING",
    "WIRE MANUFACTURING", "SLUICE / GATE VALVES", "BUTTERFLY VALVES",
    "CHECK / NON RETURN", "AIR / PRESSURE VALVES", "OTHER VALVES",
    "ACTUATION / ACCESSORIES", "FABRICATION", "DISTRIBUTION STRUCTURES",
    "STAY / EARTHING", "SAFETY / UTILITY HARDWARE",
}


def alias_group(phrase):
    key = normalize_phrase(phrase)
    groups = {
        "acsr": {"acsr", "acsr conductor"},
        "aac": {"aac", "aac conductor"},
        "aaac": {"aaac", "aaac conductor"},
        "galvanized-steel-wire": {"gs wire", "gi wire", "galvanized steel wire"},
        "nrv": {"nrv", "non return valve", "non-return valve"},
        "prv": {"prv", "pressure reducing valve"},
        "mov": {"mov", "motor operated valve"},
    }
    for group, phrases in groups.items():
        if key in {normalize_phrase(p) for p in phrases}:
            return group
    return None


def role_for(category):
    if category == "NEGATIVE KEYWORDS":
        return None
    if category in PROCUREMENT:
        return "PROCUREMENT"
    if category in TECHNICAL:
        return "TECHNICAL"
    if category in DEMAND:
        return "DEMAND_SIDE"
    if category in OWN:
        return "OWN_BUSINESS"
    raise ValueError("Unmapped subsidiary keyword category: %s" % category)


def seed(path=SOURCE):
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    seen = set()
    for subsidiary, categories in data.items():
        for category, phrases in categories.items():
            for original in phrases:
                phrase = normalize_phrase(original)
                identity = (subsidiary, phrase)
                negative = category == "NEGATIVE KEYWORDS"
                strength = None
                if negative:
                    strength = "VETO" if original in NEGATIVE_VETO.get(subsidiary, set()) else "PENALTY"
                    role = "OWN_BUSINESS"
                else:
                    role = role_for(category)
                alias = alias_group(original)
                if identity in seen:
                    _run(
                        'UPDATE subsidiary_keyword SET '
                        '"negativeStrength" = COALESCE("negativeStrength", %s::"NegativeStrength"), '
                        '"aliasGroup" = COALESCE("aliasGroup", %s) '
                        'WHERE "subsidiary" = %s AND "phrase" = %s',
                        (strength, alias, subsidiary, phrase))
                    continue
                seen.add(identity)
                _run(
                    'INSERT INTO subsidiary_keyword '
                    '("id", "subsidiary", "threshold", "phrase", "category", "role", '
                    '"negativeStrength", "aliasGroup") '
                    'VALUES (%s, %s, %s, %s, %s, %s::"KeywordRole", %s::"NegativeStrength", %s) '
                    'ON CONFLICT ("subsidiary", "phrase") DO UPDATE SET '
                    '"threshold" = EXCLUDED."threshold", "category" = EXCLUDED."category", '
                    '"role" = EXCLUDED."role", "negativeStrength" = EXCLUDED."negativeStrength", '
                    '"aliasGroup" = EXCLUDED."aliasGroup"',
                    ("sk_" + uuid4().hex, subsidiary, config.MATCH_THRESHOLD_DEFAULT,
                     phrase, category, role, strength, alias))
    print("Seeded %s unique subsidiary phrases from %s" % (len(seen), path))


if __name__ == "__main__":
    seed()
