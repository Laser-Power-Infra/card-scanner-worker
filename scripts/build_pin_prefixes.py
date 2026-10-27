"""Build the local India PIN lookup table used by the address pass.

Source: GeoNames postal-code export for India (CC BY 4.0):
    https://download.geonames.org/export/zip/IN.zip

The worker only needs a small, offline lookup so it can:
  * reject impossible PINs (for example 996544);
  * derive state/district when the model omits them;
  * use the PIN centroid as a cheap area anchor.

Run once, commit ``data/india_pins.json``, then re-run only when the source
changes:

    python -m scripts.build_pin_prefixes
"""
import argparse
import io
import json
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

SOURCE_URL = "https://download.geonames.org/export/zip/IN.zip"
SOURCE_LABEL = "GeoNames IN postal codes (CC BY 4.0)"
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "india_pins.json"

# admin3 values that are really neighbourhoods, not cities. The district is a
# safer city fallback for these.
_NEIGHBOURHOOD_HINTS = (
    "sector", "colony", "nagar", "area", "layout", "extension", "extn",
    "g.p.o", "post office", "tehsil", "mandal", "taluk", "ward",
)


def _clean_city(admin3, district):
    """Return a city-ish admin3 value, or the district when admin3 is a locality.

    GeoNames admin3 is inconsistent: sometimes a city ("Vijayawada (Urban)"),
    often a neighbourhood ("Shaikpet", "Sector 62"). Only trust it when it
    explicitly says City/Urban/Rural; otherwise the district is the safer
    fallback for a missing city.
    """
    text = " ".join(str(admin3 or "").split())
    if not text or text.upper() == "NA":
        return district
    explicit_city = any(word in text.lower()
                        for word in (" city", "(urban)", "(rural)", " urban",
                                     " rural"))
    # "Vijayawada (Urban)" -> "Vijayawada"; "Pune City" -> "Pune".
    base = text.split("(")[0].strip()
    for suffix in (" City", " Urban", " Rural"):
        if base.endswith(suffix):
            base = base[:-len(suffix)].strip()
    lowered = base.lower()
    if any(hint in lowered for hint in _NEIGHBOURHOOD_HINTS):
        return district
    if base.lower() in {"na", "n.a.", "unknown"}:
        return district
    return (base or district) if explicit_city else district


def _rows(zip_bytes):
    archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    with archive.open("IN.txt") as handle:
        for line in handle:
            parts = line.decode("utf-8", "replace").rstrip("\n").split("\t")
            # country, postal_code, place_name, admin1, admin1_code, admin2,
            # admin2_code, admin3, admin3_code, lat, lon, accuracy
            if len(parts) >= 11:
                yield parts


def build(zip_bytes):
    # Keep the most accurate row per PIN. GeoNames accuracy is 1 (best) .. 6.
    best = {}
    for parts in _rows(zip_bytes):
        pin = parts[1].strip()
        if len(pin) != 6 or not pin.isdigit() or pin[0] == "0":
            continue
        try:
            accuracy = int(parts[11]) if parts[11].strip().isdigit() else 6
        except IndexError:
            accuracy = 6
        current = best.get(pin)
        if current is None or accuracy < current[0]:
            best[pin] = (accuracy, parts)

    states, districts, cities = [], [], []
    state_index, district_index, city_index = {}, {}, {}
    pins = {}
    for pin in sorted(best):
        _, parts = best[pin]
        state = " ".join(parts[3].split())
        district = " ".join(parts[5].split())
        city = _clean_city(parts[7], district)
        try:
            lat = round(float(parts[9]), 4)
            lon = round(float(parts[10]), 4)
        except (TypeError, ValueError):
            lat = lon = None
        for value, bucket, index in (
                (state, states, state_index),
                (district, districts, district_index),
                (city, cities, city_index)):
            if value not in index:
                index[value] = len(bucket)
                bucket.append(value)
        pins[pin] = [state_index[state], district_index[district],
                     city_index[city], lat, lon]

    return {
        "source": SOURCE_LABEL,
        "source_url": SOURCE_URL,
        "generated": "2026-10-26",
        "states": states,
        "districts": districts,
        "cities": cities,
        "pins": pins,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="local IN.zip path instead of downloading")
    parser.add_argument("--output", default=str(OUTPUT))
    args = parser.parse_args(argv)

    if args.input:
        raw = Path(args.input).read_bytes()
    else:
        request = urllib.request.Request(SOURCE_URL,
                                         headers={"User-Agent": "card-scanner-worker"})
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()

    payload = build(raw)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(",", ":")),
                      encoding="utf-8")
    print("wrote %s (%d pins, %d states, %d districts)"
          % (output, len(payload["pins"]), len(payload["states"]),
             len(payload["districts"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
