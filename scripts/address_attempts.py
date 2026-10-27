"""Summarise ADDRESS_ATTEMPT blocks from a captured console log.

Phase 1 keeps attempt records on the console. Redirect a run to a file and
point this script at it to get reject reasons (and failed checks) by count.

    python -m scripts.profile_enrich --pass address --limit 10 --inline *> run.log
    python -m scripts.address_attempts --log run.log
    Get-Content run.log | python -m scripts.address_attempts
"""
import argparse
import re
import sys
from collections import Counter

# Lines carry a logging timestamp prefix, so search rather than match.
_START = re.compile(
    r"ADDRESS_ATTEMPT\s+(\S+)\s+\(contact\s+(.+?),\s*(.*)\)\s*$")
# PowerShell wraps native stderr at console width, which can split the header
# after the attempt id. Such a line has no "(contact ...)" part.
_HEAD_ANY = re.compile(r"ADDRESS_ATTEMPT\s+(\S+)")
_REASON = re.compile(r"^\s+reason\s+:\s*(\S+)")
_OUTCOME = re.compile(r"^\s+outcome\s+:\s*(\S+)")
_CHECK = re.compile(r"^\s+\[(PASS|FAIL)\]\s+([^:\s]+)")
_CANDIDATE = re.compile(r"^\s+candidate\s+\d+/\d+\s*:")


def _read_log(path):
    """Read UTF-8 or UTF-16 logs (Windows PowerShell redirection is UTF-16)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _record_from(start):
    return {
        "id": start.group(1),
        "contact": start.group(2).strip(),
        "company": start.group(3).strip(),
        "outcome": "-",
        "reason": "-",
        "failed": [],
        "candidates": 0,
    }


def parse(lines):
    """Return one dict per attempt: id, contact, company, outcome, reason,
    failed checks."""
    items = list(lines)
    records = []
    current = None
    index = 0
    while index < len(items):
        line = items[index].rstrip("\r\n")
        start = _START.search(line)
        if start:
            current = _record_from(start)
            records.append(current)
            index += 1
            continue

        head = _HEAD_ANY.search(line)
        if head:
            # Re-join a header split by PowerShell's line wrapping. It can break
            # after the id or after "(contact", so try the full header first.
            header = line
            peek = index + 1
            while peek < len(items) and peek <= index + 3:
                if _START.search(header):
                    break
                header += " " + items[peek].strip()
                peek += 1
            joined = _START.search(header)
            if joined:
                current = _record_from(joined)
            else:
                current = {"id": head.group(1), "contact": "?",
                           "company": "?", "outcome": "-", "reason": "-",
                           "failed": [], "candidates": 0}
            records.append(current)
            index = peek
            continue

        if current is not None:
            if _CANDIDATE.match(line):
                current["candidates"] += 1
                index += 1
                continue
            # Candidate sub-blocks repeat their own checks; do not fold them
            # into the attempt-level failed-check counts.
            if current["candidates"]:
                index += 1
                continue
            match = _REASON.match(line)
            if match:
                current["reason"] = match.group(1)
                index += 1
                continue
            match = _OUTCOME.match(line)
            if match:
                current["outcome"] = match.group(1)
                index += 1
                continue
            match = _CHECK.match(line)
            if match and match.group(1) == "FAIL":
                current["failed"].append(match.group(2))
        index += 1
    return records


def _filter(records, contact=None, last=None):
    if contact:
        needle = contact.lower()
        records = [r for r in records
                   if needle in r["contact"].lower()
                   or needle in r["company"].lower()]
    if last is not None:
        records = records[-last:]
    return records


def _print_table(title, counts, left="reason"):
    if not counts:
        return
    width = max(len(left), *(len(k) for k in counts))
    print("%s" % title)
    print("%-*s  %5s" % (width, left, "count"))
    print("%s  %s" % ("-" * width, "-" * 5))
    for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print("%-*s  %5d" % (width, key, count))
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Count address attempt outcomes/reasons from console logs.")
    parser.add_argument("--log", help="log file to read (default: stdin)")
    parser.add_argument("--contact", help="only attempts matching this contact id or company")
    parser.add_argument("--last", type=int, default=None,
                        help="only the last N attempts after filtering")
    args = parser.parse_args(argv)

    if args.log:
        records = parse(_read_log(args.log).splitlines())
    else:
        records = parse(sys.stdin)

    records = _filter(records, args.contact, args.last)
    if not records:
        print("no ADDRESS_ATTEMPT blocks found")
        return 1

    outcomes = Counter(r["outcome"] for r in records)
    reasons = Counter(r["reason"] for r in records)
    failed = Counter(check for r in records for check in r["failed"])

    print("attempts: %d" % len(records))
    print()
    _print_table("outcome", outcomes, left="outcome")
    _print_table("reason", reasons)
    _print_table("failed check", failed, left="check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
