"""Human review list of approximate company locations (read-only).

    python -m scripts.address_review
    python -m scripts.address_review --limit 50 --company "Fab"
"""
import argparse

from app.profile import db


def _address(row):
    parts = (row.get("address_line1"), row.get("address_line2"),
             row.get("city"), row.get("state"), row.get("postal_code"),
             row.get("country"))
    return ", ".join(str(part).strip() for part in parts
                     if str(part or "").strip())


def format_row(index, row):
    return "%3d | %-28s | %-52s | %-9s | %-6s | %s" % (
        index,
        str(row.get("company") or "-")[:28],
        _address(row)[:52],
        row.get("source_type") or "-",
        row.get("precision") or "-",
        row.get("found_at") or "-",
    )


def fetch(limit=25, company=None, precision=None):
    sql = (
        'SELECT c.company, c."companyLocation", l.address_line1, '
        'l.address_line2, l.city, l.state, l.postal_code, l.country, '
        'l.source_type, l.precision, l.found_count, l.found_at '
        'FROM "CompanyLocation" l JOIN "Contact" c ON c.id = l.contact_id '
        'WHERE l.status = %s'
    )
    params = ["APPROXIMATE"]
    if company:
        sql += ' AND c.company ILIKE %s'
        params.append("%" + company + "%")
    if precision:
        sql += ' AND l.precision = %s'
        params.append(precision.upper())
    sql += " ORDER BY l.found_at DESC LIMIT %s"
    params.append(limit)
    with db.get_conn().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="List approximate company locations for human review.")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--company", help="filter by company name fragment")
    parser.add_argument("--precision", choices=("street", "area", "city"),
                        help="only locations with this pin precision")
    args = parser.parse_args(argv)

    rows = fetch(args.limit, args.company, args.precision)
    print("approximate locations: %d" % len(rows))
    if not rows:
        return 0
    print("%3s | %-28s | %-52s | %-9s | %-6s | %s"
          % ("#", "COMPANY", "ADDRESS", "SOURCE", "PIN", "FOUND"))
    print("%s-+-%s-+-%s-+-%s-+-%s-+-%s"
          % ("-" * 3, "-" * 28, "-" * 52, "-" * 9, "-" * 6, "-" * 19))
    for index, row in enumerate(rows, 1):
        print(format_row(index, row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
