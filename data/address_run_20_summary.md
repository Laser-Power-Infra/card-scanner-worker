# Address run — 20 contacts (2026-10-27)

Command: `python -m scripts.profile_enrich --pass address --limit 20 --inline`
Log: `data/address_run_20.log` — exit 0, 20 attempts, 829 s wall (mean 41.5 s, median 35 s).

## Headline

| Metric | Value |
|---|---|
| Attempts | 20 (18 unique companies; Voltas ×2, Offshore ×2) |
| PASS | 14 (70%) |
| FAIL | 6 (30%) |
| Verified | 1 attempt / 2 rows |
| Approximate | 13 attempts / 22 rows |
| Rows saved | 24 (1.2 per contact) |
| Pin precision | STREET 8, AREA 8, CITY 8 |
| Source | DIRECTORY 13, OWN_SITE 9, REGISTRY 2 |

## Per contact

| # | Company | Card location | Result | Saved city | Pin | Source | Failed checks | s |
|---|---|---|---|---|---|---|---|---|
| 1 | Frontier Technologies | Gurgaon, Haryana | PASS approximate | Mumbai | STREET | DIRECTORY | confidence, state | 40.0 |
| 2 | Voltas | Thane (W) 400601 | FAIL extraction_failed | – | – | – | – | 123.3 |
| 3 | Voltas | Mumbai | FAIL extraction_failed | – | – | – | – | 118.4 |
| 4 | Offshore Infrastructures | Navi Mumbai 400705 | PASS approximate | Mumbai | STREET | OWN_SITE | confidence | 24.1 |
| 5 | OFFSHORE INFRASTRUCTURES | Navi Mumbai | PASS approximate | Mumbai | STREET | OWN_SITE | confidence | 12.8 |
| 6 | Sesa Goa, Vedanta | Goa | PASS approximate | Bicholim | CITY | DIRECTORY | confidence, city, expected_state, state, area_distance | 62.4 |
| 7 | Ordem dos Advogados (OAM) | Maputo | PASS approximate | Maputo | CITY | DIRECTORY | no_results | 45.9 |
| 8 | Transmodal Group | New York; Toronto | PASS approximate | McKinney | AREA | OWN_SITE | – | 44.6 |
| 9 | MyPathGuru | Thiruvananthapuram | PASS approximate | Thiruvananthapuram | AREA | OWN_SITE | street_level, street_present, city, street | 23.7 |
| 10 | Kangen Water (Enagic) | Bangalore | PASS verified | Bengaluru | AREA | DIRECTORY | street | 46.7 |
| 11 | Shreya Enterprises | Nallasopara (E) | FAIL incomplete_extraction | – | – | – | – | 15.4 |
| 12 | Shillong Smart City | Shillong | PASS approximate | Shillong | CITY | REGISTRY | street_level, street_present, street | 34.8 |
| 13 | Grafik Solution | Mamprobi – Accra | FAIL country_mismatch | – | – | – | – | 7.6 |
| 14 | 4P Infra Solutions | – | FAIL incomplete_extraction | – | – | – | – | 10.2 |
| 15 | Taxi Service & Tour | – | PASS approximate | Udaipur | CITY | DIRECTORY | confidence, city, expected_state, street, area_distance | 20.6 |
| 16 | Kerala Water Authority | – | PASS approximate | Thiruvananthapuram | AREA | REGISTRY | – | 16.6 |
| 17 | Tripura State Electricity | Agartala | PASS approximate | Agartala | CITY | DIRECTORY | street_level, street_present, street | 27.0 |
| 18 | Accurate Power Systems | – | PASS approximate | Baramati | CITY | DIRECTORY | street_level, street_present, street | 46.0 |
| 19 | BASEC | Milton Keynes, England | FAIL country_mismatch | – | – | – | – | 74.0 |
| 20 | Excelsource International | Vadodara | PASS approximate | Mumbai | STREET | OWN_SITE | confidence, state | 35.3 |

## Failure causes

| Cause | Count | Root cause |
|---|---|---|
| extraction_failed | 2 | OpenAI output truncated at `ADDRESS_MAX_OUTPUT_TOKENS=3000`; Voltas needs ~25 addresses. Same pages at 7000 tokens parse fine. 6 wasted API calls, 242 s. |
| country_mismatch | 2 | `geo.country_filter_for()` defaults to `in` for unrecognised locations. "Mamprobi – Accra" (GH) and "Milton Keynes … England" (GB) are absent from `_COUNTRY_CODES`, so valid addresses were rejected as non-India. |
| incomplete_extraction | 2 | LLM returned no street line (Shreya: 5 pages, 0 candidates; 4P: 5 pages, 0 candidates, `valid=0 rejected=0`). |

## Data-quality flags

- **OAM wrong country (data corruption).** Saved row is `…, Maputo, India`. Same `country_filter_for` fallback: `address_fields.fill()` stamps `country=India` when the hint is unrecognised. Not merely a rejection — bad data persisted.
- **Kangen status mismatch.** Attempt logged `verified`, but the extracted Barton Centre address saved as `AREA`; the `VERIFIED` row is a different candidate (Murphy Road). Attempt reason reflects the primary merged row, not the extracted address.
- **3 city conflicts saved anyway**: Frontier (card Gurgaon → saved Mumbai), Excelsource (Vadodara → Mumbai), Transmodal (NY → McKinney). `state` failure is a review flag by design, so these reach the DB as APPROXIMATE without blocking.
- **Duplicate work**: Voltas and Offshore each ran twice (duplicate contacts). Page cache removed refetch (run 5 took 12.8 s vs 24.1 s) but the LLM call is never cached, so duplicate contacts cost full extraction.
- **Coarse pins dominate**: 16 of 24 rows are AREA/CITY; only 8 STREET and 2 VERIFIED. Fallback design works, but most rows are not street-accurate.

## Repro evidence for truncation

```
ADDRESS_MAX_OUTPUT_TOKENS=3000 -> Expecting ',' delimiter: line 57 column 6
ADDRESS_MAX_OUTPUT_TOKENS=7000 -> OK, 25 addresses
```

## Suggested fixes

1. Raise `ADDRESS_MAX_OUTPUT_TOKENS` (>=7000) and treat Responses API `status == "incomplete"` as a distinct, retryable reason.
2. Make `country_filter_for()` return `None`/detected country instead of defaulting to India; derive country from the extracted address or geocode, and never write `India` for a foreign hint.
3. Add Ghana/Mozambique/England + major-city aliases to `_COUNTRY_CODES` (Accra, Maputo, Milton Keynes, Buckinghamshire, …).
4. Cache extraction results per (company, address) alongside pages to stop duplicate-contact spend.
