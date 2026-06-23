#!/usr/bin/env python3
"""
batch_sentiment.py — Sentiment Intelligence Report Batch Runner
---------------------------------------------------------------
Generates Sentiment Intelligence Reports for a curated watchlist of 15 companies
(5 large-cap, 5 small-cap, 5 private) and saves each as a markdown file in a
timestamped output folder.

Wraps sentiment_tool.py functions directly — no subprocess overhead.
Runs with limited parallelism (default 3 workers) to respect API rate limits.

Usage
    python batch_sentiment.py                        # default watchlist, 3 workers
    python batch_sentiment.py --workers 5            # faster, more API pressure
    python batch_sentiment.py --model gpt-4o-mini    # cheaper model
    python batch_sentiment.py --out-dir my_folder    # custom output directory
    python batch_sentiment.py --chunks 30 --top 15   # lighter per-report

Customise the WATCHLIST below to fit your coverage universe.
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# ── Import core functions from sentiment_tool (no subprocess, no arg parsing) ──
from sentiment_tool import (
    OpenAI,
    resolve_company,
    get_signal_snapshot,
    fetch_chunks,
    aggregate_to_docs,
    generate_narrative,
    build_markdown,
    LOOKBACK,
)

load_dotenv()

# ── Watchlists ────────────────────────────────────────────────────────────────
# Format: (query, category_label)
# query = ticker or full name (private companies need name)

WATCHLIST: list[tuple[str, str]] = [
    # Large-cap
    ("AAPL",    "large-cap"),
    ("MSFT",    "large-cap"),
    ("NVDA",    "large-cap"),
    ("JPM",     "large-cap"),
    ("AMZN",    "large-cap"),
    # Small-cap
    ("Grifols", "small-cap"),   # Grifols SA (name query — ticker ambiguous across exchanges)
    ("RDDT",    "small-cap"),   # Reddit (NYSE)
    ("DOCN",    "small-cap"),   # DigitalOcean
    ("AMPL",    "small-cap"),   # Amplitude
    ("TASK",    "small-cap"),   # TaskUs
    # Private
    ("Playtomic",          "private"),
    ("Databricks",         "private"),
    ("Stripe",             "private"),
    ("OpenAI",             "private"),
    ("Revolut",            "private"),
]

WATCHLIST_2: list[tuple[str, str]] = [
    # Large-cap
    ("GOOGL",   "large-cap"),   # Alphabet
    ("META",    "large-cap"),   # Meta Platforms
    ("TSLA",    "large-cap"),   # Tesla
    ("V",       "large-cap"),   # Visa
    ("UNH",     "large-cap"),   # UnitedHealth Group
    # Small-cap
    ("NET",     "small-cap"),   # Cloudflare
    ("SNOW",    "small-cap"),   # Snowflake
    ("GTLB",    "small-cap"),   # GitLab
    ("MNDY",    "small-cap"),   # monday.com
    ("COUR",    "small-cap"),   # Coursera
    # Private
    ("Anthropic",   "private"),
    ("SpaceX",      "private"),
    ("Klarna",      "private"),
    ("Epic Games",  "private"),
    ("Shein",       "private"),
]

WATCHLISTS: dict[int, list[tuple[str, str]]] = {
    1: WATCHLIST,
    2: WATCHLIST_2,
}

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_WORKERS = 3      # concurrent reports; keep low to respect API rate limits
DEFAULT_MODEL   = "gpt-5.4-nano"
DEFAULT_CHUNKS  = 50
DEFAULT_TOP     = 50
DEFAULT_DAYS    = 90


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Batch Sentiment Intelligence Report runner")
    p.add_argument("--workers",  type=int,  default=DEFAULT_WORKERS,
                   help=f"Parallel workers (default: {DEFAULT_WORKERS})")
    p.add_argument("--model",    type=str,  default=DEFAULT_MODEL,
                   help=f"OpenAI model (default: {DEFAULT_MODEL})")
    p.add_argument("--chunks",   type=int,  default=DEFAULT_CHUNKS,
                   help=f"max_chunks per search (default: {DEFAULT_CHUNKS})")
    p.add_argument("--top",      type=int,  default=DEFAULT_TOP,
                   help=f"Top snippets to LLM (default: {DEFAULT_TOP})")
    p.add_argument("--days",     type=int,  default=DEFAULT_DAYS,
                   help=f"Narrative lookback window in days (default: {DEFAULT_DAYS})")
    p.add_argument("--out-dir",  type=str,  default=None, dest="out_dir",
                   help="Output folder (default: reports_YYYYMMDD_HHMMSS)")
    p.add_argument("--batch",    type=int,  default=1, choices=list(WATCHLISTS),
                   help="Watchlist to run: 1 (default) or 2")
    return p.parse_args()


# ── Single-company report runner ──────────────────────────────────────────────

def run_one(
    query: str,
    category: str,
    client: OpenAI,
    out_dir: Path,
    args: argparse.Namespace,
) -> dict:
    """
    Generate one report and save it as markdown.
    Returns a result dict for the summary table.
    """
    t0  = time.monotonic()
    now = datetime.now(timezone.utc)
    start_ts = (now - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_ts   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    result = {
        "query":    query,
        "category": category,
        "status":   "ok",
        "name":     query,
        "file":     "",
        "snippets": 0,
        "elapsed":  0.0,
        "error":    "",
    }

    try:
        # Resolve
        company = resolve_company(query)
        result["name"] = company["name"]

        # Parallel: signals + chunks
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_signals = ex.submit(get_signal_snapshot, company["id"])
            fut_chunks  = ex.submit(fetch_chunks, company["id"], start_ts, end_ts, args.chunks, args.days)
            signals                        = fut_signals.result()
            raw_chunks, _ch_t, chunk_days  = fut_chunks.result()

        docs = aggregate_to_docs(raw_chunks, top_n=args.top)
        result["snippets"] = sum(d["snippets"] for d in docs)

        s = signals["sentiment"]
        no_coverage = (s["current"] == 0.0 and s["baseline"] == 0.0)
        no_data     = no_coverage and len(docs) == 0

        if no_data:
            narrative = ""
        else:
            narrative = generate_narrative(
                client, company, signals, docs, chunk_days, args.model,
                raw_count=len(raw_chunks),
            )

        md = build_markdown(company, signals, docs, narrative, chunk_days, len(raw_chunks))

        # Safe filename: replace spaces/slashes
        safe_name = company["name"].replace(" ", "_").replace("/", "-")[:40]
        filename  = f"{category}_{safe_name}.md"
        filepath  = out_dir / filename
        filepath.write_text(md, encoding="utf-8")

        result["file"] = filename

    except Exception as e:
        result["status"] = "error"
        result["error"]  = str(e)

    result["elapsed"] = round(time.monotonic() - t0, 1)
    return result


# ── Batch runner ──────────────────────────────────────────────────────────────

def run_batch(args: argparse.Namespace) -> None:
    watchlist = WATCHLISTS[args.batch]

    # Output directory
    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else Path(f"reports_batch{args.batch}_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nSentiment Intelligence — Batch Run  (watchlist {args.batch})")
    print(f"{'─' * 60}")
    print(f"  Companies : {len(watchlist)}  "
          f"({sum(1 for _,c in watchlist if c=='large-cap')} large-cap  "
          f"{sum(1 for _,c in watchlist if c=='small-cap')} small-cap  "
          f"{sum(1 for _,c in watchlist if c=='private')} private)")
    print(f"  Workers   : {args.workers}")
    print(f"  Model     : {args.model}")
    print(f"  Window    : {args.days}d  ·  top {args.top} snippets")
    print(f"  Output    : {out_dir}/")
    print(f"{'─' * 60}\n")

    # Build OpenAI client once; shared across workers (thread-safe)
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

    t_batch = time.monotonic()
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(run_one, query, category, client, out_dir, args): (query, category)
            for query, category in watchlist
        }
        done = 0
        for fut in as_completed(futures):
            res  = fut.result()
            done += 1
            status_icon = "✓" if res["status"] == "ok" else "✗"
            print(
                f"  [{done:>2}/{len(watchlist)}] {status_icon}  "
                f"{res['category']:<10}  "
                f"{res['name'][:35]:<35}  "
                f"{res['snippets']:>3} snippets  "
                f"{res['elapsed']:>5.1f}s"
                + (f"  ⚠ {res['error'][:50]}" if res["error"] else "")
            )
            results.append(res)

    total_elapsed = time.monotonic() - t_batch

    # ── Summary ───────────────────────────────────────────────────────────────
    ok    = [r for r in results if r["status"] == "ok"]
    errors = [r for r in results if r["status"] == "error"]

    print(f"\n{'─' * 60}")
    print(f"  Completed : {len(ok)}/{len(watchlist)}  in {total_elapsed:.1f}s")
    print(f"  Output    : {out_dir}/")
    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for r in errors:
            print(f"    • {r['query']}: {r['error']}")

    # Write an index file
    index_lines = [
        f"# Sentiment Intelligence Batch Report",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  "
        f"| **Model:** {args.model}  | **Window:** {args.days}d",
        "",
        "| Category | Company | Chunks | File | Status |",
        "|----------|---------|--------|------|--------|",
    ]
    for r in sorted(results, key=lambda x: (x["category"], x["name"])):
        file_link = f"[{r['file']}]({r['file']})" if r["file"] else "—"
        status    = "✓" if r["status"] == "ok" else f"✗ {r['error'][:40]}"
        index_lines.append(
            f"| {r['category']} | {r['name']} | {r['snippets']} | {file_link} | {status} |"
        )
    index_lines.append(f"\n*{len(ok)} reports generated in {total_elapsed:.1f}s*")
    (out_dir / "index.md").write_text("\n".join(index_lines), encoding="utf-8")
    print(f"  Index     : {out_dir}/index.md\n")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()
    run_batch(args)
