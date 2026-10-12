"""Run the subsidiary pipeline over companies, in-process, in batches.

Same handler path the RabbitMQ receiver uses (search -> extract -> score ->
aggregate -> persist company_keyword_score, Contact.subsidiaryMatches,
company_review).

    python -m scripts.subsidiary_run_all --limit 500 --offset 0     # pending only
    python -m scripts.subsidiary_run_all --all --limit 500 --offset 0
    python -m scripts.subsidiary_run_all --all --limit 2000          # everything

--all ignores the "pending" filter and walks every distinct company, so a
batched re-run can cover already-scored companies (which are cache-fast).

Per company the worker logs a `cache[...]` line; this script prints a running
ETA and a final cache-hit summary via handlers.cache_stats().
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.subsidiary import config, db, handlers, logic  # noqa: E402

log = logging.getLogger("scripts.run_all")


def select(args):
    if args.all:
        companies = db.all_company_names()
    else:
        version = logic.extract_version(
            config.LLM_MODEL, handlers.EXTRACT_PROMPT_VERSION,
            logic.vocabulary_version(db.get_subsidiaries()))
        companies = db.pending_companies(version)
    return companies[args.offset:args.offset + args.limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="every distinct company, not just pending ones")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--eta-every", type=int, default=25)
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    companies = select(args)
    print("running %d companies (all=%s offset=%d)" % (len(companies), args.all, args.offset),
          flush=True)

    handlers.reset_cache_stats()
    started = time.time()
    stats = {"ok": 0, "matched": 0, "no_match": 0, "no_data": 0, "error": 0, "paused": 0}
    for i, company in enumerate(companies, 1):
        try:
            outcome, result = handlers.process_company({"company": company, "hint": {}},
                                                       dry_run=False)
        except Exception as exc:  # noqa: BLE001 - keep the batch alive
            outcome, result = "error", repr(exc)
        stats[outcome] = stats.get(outcome, 0) + 1
        if outcome == "ok" and isinstance(result, dict):
            if result.get("matches"):
                stats["matched"] += 1
            elif result.get("status") == "NO_DATA":
                stats["no_data"] += 1
            else:
                stats["no_match"] += 1
        else:
            log.warning("company %r -> %s %s", company, outcome, result)

        if i % args.eta_every == 0 or i == len(companies):
            done = time.time() - started
            rate = done / i
            eta = rate * (len(companies) - i)
            cs = handlers.cache_stats()
            print("[%d/%d] %.1fs avg  ETA %.1f min  hit=%.1f%% (header %.1f/shared %.1f) "
                  "score_calls=%d  %s"
                  % (i, len(companies), rate, eta / 60, cs["hit_rate"], cs["header_rate"],
                     cs["shared_rate"], cs["score_calls"], stats), flush=True)

    cs = handlers.cache_stats()
    print("DONE in %.1f min  %s" % ((time.time() - started) / 60, stats))
    print("cache: phrases=%d header=%d shared=%d stale=%d hit=%.1f%% score_calls=%d/%d"
          % (cs["phrases"], cs["header"], cs["shared"], cs["stale"], cs["hit_rate"],
             cs["score_calls"], cs["companies"]))
    db.close()


if __name__ == "__main__":
    main()
