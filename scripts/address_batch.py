"""Process one small slice of a saved contact cohort, then exit.

A scheduled task runs this every few minutes. Each invocation picks the next
eligible contacts straight from the database, so a terminated run loses at
most the batch in flight. The task is a reliability wrapper, not a new
pipeline: it calls the normal profile_enrich entry point with --ids.
"""
import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("ADDRESS_MAX_OUTPUT_TOKENS", "7000")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

LOG = REPO / "data" / "address_batch.log"
LOCK = REPO / "data" / "address_batch.lock"
LOCK_STALE_SECONDS = 1500


def _pid_alive(pid):
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
    except Exception:
        return False
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _log_handle():
    fh = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = fh
    sys.stderr = fh
    return fh


def _locked():
    """True when another batch is still running."""
    try:
        pid = int(LOCK.read_text(encoding="ascii").strip() or 0)
    except FileNotFoundError:
        return False
    except ValueError:
        pid = 0
    if pid and _pid_alive(pid):
        return True
    try:
        age = time.time() - LOCK.stat().st_mtime
    except FileNotFoundError:
        return False
    if age < LOCK_STALE_SECONDS:
        return True
    LOCK.unlink(missing_ok=True)
    return False


def _next_ids(cohort, limit, skip=()):
    from app.profile import config, db

    sql = (
        'SELECT c.id FROM "Contact" c '
        'LEFT JOIN enrichment e ON e.contact_id = c.id '
        "WHERE c.id = ANY(%s) "
        "AND e.address_verified_at IS NULL "
        "AND coalesce(e.address_attempts, 0) < %s "
        'AND NOT EXISTS (SELECT 1 FROM "CompanyLocation" l '
        'WHERE l.contact_id = c.id) '
    )
    params = [cohort, config.ADDRESS_MAX_ATTEMPTS]
    if skip:
        sql += "AND NOT (c.id = ANY(%s)) "
        params.append(list(skip))
    sql += 'ORDER BY c."createdAt" DESC NULLS LAST LIMIT %s'
    params.append(limit)
    with db.get_conn().cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [r["id"] if isinstance(r, dict) else r[0] for r in rows]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cohort-file", default=str(REPO / "data"
                                                    / "run500_ids.txt"))
    parser.add_argument("--batch", type=int, default=25)
    parser.add_argument("--max-batches", type=int, default=0,
                        help="0 = chain until the cohort is drained; a positive "
                             "number stops after that many slices")
    args = parser.parse_args()

    if _locked():
        print("%s skip: batch still running" % datetime.now().isoformat(
            timespec="seconds"))
        return 0

    fh = _log_handle()
    LOCK.write_text(str(os.getpid()), encoding="ascii")
    try:
        from scripts import profile_enrich

        with open(args.cohort_file, encoding="ascii") as cohort_fh:
            cohort = [line.strip() for line in cohort_fh if line.strip()]

        processed = 0
        attempted = []
        while True:
            ids = _next_ids(cohort, args.batch, attempted)
            print("%s batch=%d processed_this_run=%d attempted_this_run=%d" % (
                datetime.now().isoformat(timespec="seconds"), len(ids),
                processed, len(attempted)))
            if not ids:
                print("nothing left this run"
                      if attempted else "nothing left")
                break
            sys.argv = ["profile_enrich", "--pass", "address", "--inline",
                        "--no-cache", "--ids", ",".join(ids),
                        "--limit", str(len(ids))]
            profile_enrich.main()
            processed += len(ids)
            attempted.extend(ids)
            if args.max_batches and processed >= args.max_batches * args.batch:
                break
        print("%s run complete processed_this_run=%d" % (
            datetime.now().isoformat(timespec="seconds"), processed))
        return 0
    finally:
        LOCK.unlink(missing_ok=True)
        fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
