#!/usr/bin/env python3
# Requirements: pip install requests pandas numpy openai python-dotenv
"""
sentiment_tool.py — Company Sentiment Intelligence Report
----------------------------------------------------------
Combines 90-day quantitative signals (EWM sentiment + media attention) with
an AI-synthesised executive narrative from the top chunks for a company.

Speed design
  • API keys are validated before any real work (fail fast).
  • 90-day signal calculation and chunk search run in PARALLEL threads.
  • Narrative generation fires only after both results are ready.
  • Single direct /v1/search call — no batch, no polling, no semantic text.
  • Chunks ranked purely by entity relevance; irrelevant chunks filtered out
    before the LLM context is assembled.

Usage
    python sentiment_tool.py                         # AAPL, 7-day window
    python sentiment_tool.py TSLA
    python sentiment_tool.py "RavenPack"             # search by name (private OK)
    python sentiment_tool.py NVDA --days 14
    python sentiment_tool.py MSFT --chunks 100 --top 30
    python sentiment_tool.py AMZN --min-rel 0.3 --out amzn.md
    python sentiment_tool.py JPM  --model gpt-4o-mini
"""

import argparse
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
from openai import OpenAI, AuthenticationError, BadRequestError

load_dotenv()

# ── API config ────────────────────────────────────────────────────────────────
BIGDATA_API_KEY = os.environ.get("BIGDATA_API_KEY", "")
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
BASE_URL        = "https://api.bigdata.com"
HEADERS         = {"X-API-KEY": BIGDATA_API_KEY, "Content-Type": "application/json"}

# ── HTTP session — one per thread, keeps TLS alive across sequential calls ────
_session_local = threading.local()

def _bd_session() -> requests.Session:
    """Return a thread-local requests.Session with the Bigdata headers baked in.
    Connection keep-alive eliminates repeated TLS handshakes within a thread."""
    if not hasattr(_session_local, "s"):
        sess = requests.Session()
        sess.headers.update(HEADERS)
        _session_local.s = sess
    return _session_local.s

# ── Signal parameters (matching company_signal.py) ───────────────────────────
LOOKBACK      = 90
HL_SHORT      = 5
HL_LONG       = 21
WIN_MONTHLY   = 30
WIN_QUARTERLY = 90


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Company Sentiment Intelligence Report")
    p.add_argument("query",      nargs="?",  default="AAPL",
                   help="Ticker or company name — public or private (default: AAPL)")
    p.add_argument("--days",     type=int,   default=90,
                   help="Narrative lookback window in days (default: 90)")
    p.add_argument("--chunks",   type=int,   default=50,
                   help="max_chunks for direct search (default: 50)")
    p.add_argument("--top",      type=int,   default=50,
                   help="Max documents sent to LLM after aggregation (default: 50)")
    p.add_argument("--model",    type=str,   default="gpt-5.4-nano",
                   help="OpenAI model (default: gpt-5.4-nano)")
    p.add_argument("--out",            type=str,   default=None,
                   help="Write markdown report to this path")
    p.add_argument("--validate-keys",  action="store_true", default=False,
                   help="Test API keys before running (default: off)")
    return p.parse_args()


# ── Step 0: API key validation ────────────────────────────────────────────────

def validate_api_keys() -> OpenAI:
    """Test both API keys immediately. Raises SystemExit on failure."""
    errors = []

    # Bigdata
    if not BIGDATA_API_KEY:
        errors.append("BIGDATA_API_KEY is not set")
    else:
        try:
            r = _bd_session().post(
                f"{BASE_URL}/v1/knowledge-graph/companies",
                json={"query": "test"},
                timeout=10,
            )
            if r.status_code in (401, 403):
                errors.append(f"Bigdata API key rejected (HTTP {r.status_code})")
            else:
                print("  [✓] Bigdata API key OK")
        except requests.RequestException as e:
            errors.append(f"Bigdata API unreachable: {e}")

    # OpenAI
    if not OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY is not set")
        client = None
    else:
        client = OpenAI(api_key=OPENAI_API_KEY)
        try:
            client.models.list()
            print("  [✓] OpenAI API key OK")
        except AuthenticationError:
            errors.append("OpenAI API key rejected (AuthenticationError)")
        except Exception as e:
            errors.append(f"OpenAI unreachable: {e}")

    if errors:
        for err in errors:
            print(f"  [✗] {err}", file=sys.stderr)
        sys.exit(1)

    return client


# ── Step 1: Resolve company query → enriched company info ────────────────────

def resolve_company(query: str) -> dict:
    """
    Search the Knowledge Graph by ticker or name (public or private).
    Returns a dict with id, name, description, industry, sector, country, type.
    No types filter — finds PUBLIC and PRIVATE companies alike.
    """
    resp = _bd_session().post(
        f"{BASE_URL}/v1/knowledge-graph/companies",
        json={"query": query},
    )
    resp.raise_for_status()
    companies = resp.json().get("results", [])
    if not companies:
        raise ValueError(f"Could not resolve '{query}' — no companies found")
    top = companies[0]
    return {
        "query":       query,
        "id":          top["id"],
        "name":        top.get("name", query),
        "description": top.get("description", ""),
        "industry":    top.get("industry", ""),
        "sector":      top.get("sector", ""),
        "country":     top.get("country", ""),
        "type":        top.get("type", ""),
    }


# ── Step 2a: 90-day quantitative signals ─────────────────────────────────────

def _fetch_volume(entity_id: str) -> pd.DataFrame:
    end_d   = date.today()
    start_d = end_d - timedelta(days=LOOKBACK)
    body = {
        "query": {
            "auto_enrich_filters": False,
            "filters": {
                "timestamp": {
                    "start": f"{start_d.isoformat()}T00:00:00Z",
                    "end":   f"{end_d.isoformat()}T23:59:59Z",
                },
                "entity": {"any_of": [entity_id], "all_of": [], "none_of": []},
                "category": {"mode": "EXCLUDE", "values": ["my_files"]},
            },
        }
    }
    resp = _bd_session().post(f"{BASE_URL}/v1/search/volume", json=body)
    resp.raise_for_status()
    volume = resp.json().get("results", {}).get("volume", [])

    rows = []
    for e in volume:
        d = e.get("date") or e.get("day")
        if d is None:
            continue
        rows.append({
            "date":      pd.Timestamp(d),
            "chunks":    e.get("chunks", 0),
            "documents": e.get("documents", 0),
            "sentiment": e.get("sentiment") or 0.0,
        })

    full_idx = pd.date_range(start_d, end_d, freq="D")
    if not rows:
        # No coverage data for this entity — return a zero-filled frame
        df = pd.DataFrame(
            {"chunks": 0, "documents": 0, "sentiment": 0.0},
            index=full_idx,
        )
        return df

    df = pd.DataFrame(rows).set_index("date").sort_index()
    return df.reindex(full_idx, fill_value=0)


def _compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    df["sent_ewm_short"] = df["sentiment"].ewm(halflife=HL_SHORT,  adjust=False).mean()
    df["sent_ewm_long"]  = df["sentiment"].ewm(halflife=HL_LONG,   adjust=False).mean()
    df["sent_momentum"]  = df["sent_ewm_short"] - df["sent_ewm_long"]

    roll_s_mo = df["sent_ewm_short"].rolling(WIN_MONTHLY,   min_periods=7)
    roll_s_qt = df["sent_ewm_short"].rolling(WIN_QUARTERLY, min_periods=14)
    df["sent_zscore_mo"] = (df["sent_ewm_short"] - roll_s_mo.mean()) / roll_s_mo.std().replace(0, np.nan)
    df["sent_zscore_qt"] = (df["sent_ewm_short"] - roll_s_qt.mean()) / roll_s_qt.std().replace(0, np.nan)

    df["dow"]             = df.index.dayofweek
    dow_avg               = df.groupby("dow")["chunks"].transform("mean")
    df["chunks_norm"]     = df["chunks"] / dow_avg.replace(0, np.nan).fillna(1)
    df["chunks_ewm_short"]    = df["chunks"].ewm(halflife=HL_SHORT, adjust=False).mean()
    df["chunks_ewm_long"]     = df["chunks"].ewm(halflife=HL_LONG,  adjust=False).mean()
    df["chunks_norm_ewm"]     = df["chunks_norm"].ewm(halflife=HL_SHORT, adjust=False).mean()
    df["chunks_momentum_pct"] = (
        (df["chunks_ewm_short"] / df["chunks_ewm_long"].replace(0, np.nan) - 1) * 100
    )
    roll_c_mo = df["chunks_norm_ewm"].rolling(WIN_MONTHLY,   min_periods=7)
    roll_c_qt = df["chunks_norm_ewm"].rolling(WIN_QUARTERLY, min_periods=14)
    df["chunks_zscore_mo"] = (df["chunks_norm_ewm"] - roll_c_mo.mean()) / roll_c_mo.std().replace(0, np.nan)
    df["chunks_zscore_qt"] = (df["chunks_norm_ewm"] - roll_c_qt.mean()) / roll_c_qt.std().replace(0, np.nan)
    return df


def _safe(v, decimals: int = 2) -> float | None:
    if v is None:
        return None
    f = float(v)
    return None if np.isnan(f) else round(f, decimals)


def get_signal_snapshot(entity_id: str) -> dict:
    """Fetch 90-day volume + compute EWM signals. Returns snapshot dict."""
    t0  = time.monotonic()
    df  = _fetch_volume(entity_id)
    df  = _compute_signals(df)
    row = df.iloc[-1]
    return {
        "_elapsed_s": round(time.monotonic() - t0, 2),
        "sentiment": {
            "current":    _safe(row["sent_ewm_short"], 3),
            "baseline":   _safe(row["sent_ewm_long"],  3),
            "momentum":   _safe(row["sent_momentum"],  3),
            "zscore_1mo": _safe(row["sent_zscore_mo"], 1),
            "zscore_1qt": _safe(row["sent_zscore_qt"], 1),
        },
        "media_attention": {
            "momentum_pct": _safe(row["chunks_momentum_pct"], 1),
            "zscore_1mo":   _safe(row["chunks_zscore_mo"],    1),
            "zscore_1qt":   _safe(row["chunks_zscore_qt"],    1),
        },
    }


# ── Step 2b: Direct search — entity filter only, no semantic text ─────────────

# Fallback windows tried in order when the initial window returns 0 chunks.
# Capped at LOOKBACK (90d) so we never exceed the signal horizon.
_FALLBACK_DAYS = [30, 60, 90]

# Plaintiff-PR detection pattern (applied to doc headlines).
_LAW_FIRM_PATTERNS = re.compile(
    r"investor alert|class action|securities fraud|investor notice|"
    r"investigates potential|announces investigation|breach of fiduciary|"
    r"shareholder rights",
    re.IGNORECASE,
)


def _search_one_window(entity_id: str, start_ts: str, end_ts: str, max_chunks: int) -> list[dict]:
    """Single /v1/search call. Returns flat list of chunk dicts (unsorted)."""
    body = {
        "query": {
            "auto_enrich_filters": False,
            "filters": {
                "timestamp": {"start": start_ts, "end": end_ts},
                "entity": {"any_of": [entity_id], "all_of": [], "none_of": []},
                "category": {"mode": "EXCLUDE", "values": ["my_files"]},
            },
            "ranking_params": {"freshness_boost": 10, "source_boost": 5},
            "max_chunks": max_chunks,
        }
    }
    resp = _bd_session().post(f"{BASE_URL}/v1/search", json=body)
    resp.raise_for_status()

    flat: list[dict] = []
    for doc in resp.json().get("results", []):
        headline  = doc.get("headline", "")
        source    = doc.get("source", {}).get("name", "")
        url       = doc.get("url", "")
        timestamp = doc.get("timestamp", "")[:16].replace("T", " ")
        for ch in doc.get("chunks", []):
            relevance = float(ch.get("relevance", 0.0))
            sentiment = float(ch.get("sentiment", 0.0) or 0.0)
            flat.append({
                "headline":  headline,
                "source":    source,
                "url":       url,
                "timestamp": timestamp,
                "text":      ch.get("text", "")[:400],
                "relevance": round(relevance, 3),
                "sentiment": round(sentiment, 3),
                "score":     round(relevance * abs(sentiment), 4),
            })
    return flat


def fetch_chunks(
    entity_id: str,
    start_ts: str,
    end_ts: str,
    max_chunks: int,
    requested_days: int,
) -> tuple[list[dict], float, int]:
    """
    Fetch chunks for the requested window.  If the result is empty, automatically
    widens to progressively larger windows (30 → 60 → 90 days) until chunks are
    found or all fallbacks are exhausted.

    Returns (flat sorted chunk list, elapsed_seconds, actual_days_used).
    """
    t0  = time.monotonic()
    now = datetime.now(timezone.utc)

    flat = _search_one_window(entity_id, start_ts, end_ts, max_chunks)

    actual_days = requested_days
    if not flat:
        for fb_days in _FALLBACK_DAYS:
            if fb_days <= requested_days:
                continue          # already tried an equal or wider window
            fb_start = (now - timedelta(days=fb_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            fb_end   = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            flat = _search_one_window(entity_id, fb_start, fb_end, max_chunks)
            actual_days = fb_days
            if flat:
                break             # found something — stop widening

    flat.sort(key=lambda c: c["score"], reverse=True)
    return flat, round(time.monotonic() - t0, 2), actual_days


# ── Step 3: Aggregate chunks → document-level entries ────────────────────────

def _trunc_headline(s: str, maxlen: int = 55) -> str:
    """Truncate at word boundary, append ellipsis if shortened."""
    if len(s) <= maxlen:
        return s
    return s[:maxlen].rsplit(" ", 1)[0] + "…"


def aggregate_to_docs(flat: list[dict], top_n: int) -> list[dict]:
    """
    Aggregate a flat sorted chunk list to document-level entries.

    Grouping key: source + "::" + headline[:80]
    Per-document signals:
      relevance = max chunk relevance
      sentiment = relevance-weighted mean chunk sentiment
      doc_score = relevance × |sentiment| × log1p(chunk_count)   ← internal sort key

    Plaintiff-PR docs (law-firm investor-alert headlines) are tagged and
    collapsed: only the highest-ranked plaintiff-PR doc is kept.

    Returns top_n docs sorted by doc_score descending, rank assigned 1..N.
    """
    # ── group chunks by doc ───────────────────────────────────────────────────
    doc_chunks_map: dict[str, list[dict]] = {}
    doc_meta_map:   dict[str, dict]       = {}

    for ch in flat:
        key = ch["source"] + "::" + ch["headline"][:80]
        if key not in doc_chunks_map:
            doc_chunks_map[key] = []
            doc_meta_map[key]   = {
                "headline":  ch["headline"],
                "source":    ch["source"],
                "url":       ch["url"],
                "timestamp": ch["timestamp"],
            }
        doc_chunks_map[key].append(ch)

    # ── compute doc-level signals ─────────────────────────────────────────────
    docs: list[dict] = []
    for key, chunks in doc_chunks_map.items():
        meta          = doc_meta_map[key]
        doc_relevance = max(c["relevance"] for c in chunks)
        total_rel     = sum(c["relevance"] for c in chunks)
        if total_rel > 0:
            doc_sentiment = (
                sum(c["sentiment"] * c["relevance"] for c in chunks) / total_rel
            )
        else:
            doc_sentiment = sum(c["sentiment"] for c in chunks) / len(chunks)
        chunk_count  = len(chunks)
        doc_score    = doc_relevance * abs(doc_sentiment) * math.log1p(chunk_count)
        # best chunk text for LLM context
        best_chunk   = max(chunks, key=lambda c: c["relevance"])

        docs.append({
            "headline":     meta["headline"],
            "source":       meta["source"],
            "url":          meta["url"],
            "timestamp":    meta["timestamp"],
            "text":         best_chunk["text"],   # highest-relevance chunk text
            "snippets":     chunk_count,
            "relevance":    round(doc_relevance, 2),
            "sentiment":    round(doc_sentiment, 3),
            "doc_score":    round(doc_score, 4),
            "plaintiff_pr": bool(_LAW_FIRM_PATTERNS.search(meta["headline"])),
        })

    # ── sort, collapse plaintiff-PR duplicates, cap at top_n ─────────────────
    docs.sort(key=lambda d: d["doc_score"], reverse=True)

    filtered: list[dict] = []
    seen_plaintiff = False
    for d in docs:
        if d["plaintiff_pr"]:
            if not seen_plaintiff:
                seen_plaintiff = True
                filtered.append(d)
            # else: collapse — skip additional plaintiff-PR docs
        else:
            filtered.append(d)

    top_docs = filtered[:top_n]
    for i, d in enumerate(top_docs, start=1):
        d["rank"] = i
    return top_docs


def filter_chunks(raw: list[dict], top_n: int) -> list[dict]:
    """
    Legacy helper — kept for backward compatibility.
    New code should use aggregate_to_docs() instead.
    """
    kept = raw[:top_n]
    for i, c in enumerate(kept, start=1):
        c["rank"] = i
    return kept


# ── Step 4: Build LLM context + generate narrative ────────────────────────────

SYSTEM_PROMPT = """\
Senior Equity Research Analyst. Audience: C-level (CFO, CIO, CEO, Board).

── CORE ─────────────────────────────────────────────────────────────────────────
400-600 words. No filler. Output EXACTLY the four headers below — no extras.
Every section weaves quantitative signals AND evidence. Synthesis is the value.

── LABEL CONSISTENCY ────────────────────────────────────────────────────────────
Match the `current` sentiment score in narrative:
  |score| ≤ 0.05 → Neutral | 0.05–0.15 → Slightly ± | >0.15 → Strongly ±
"Neutral" = never use directional language (no "softening", "slightly negative").

── NOISE FLOOR ──────────────────────────────────────────────────────────────────
Z-scores: |z|>2 unusual · 1<|z|<2 notable · |z|<1 normal.
Momentum = short-EWM minus long-EWM. Sentiment: −1.0 → 0.0 → +1.0.
Normal-range z (|z|<1): one clause, move on. Lead with whatever clears |z|>1.
Nothing clears the noise floor → say so; do not manufacture insight.

── EVIDENCE-ONLY CLAIMS ─────────────────────────────────────────────────────────
Every claim must trace to a supplied evidence row. No fabrication. No exceptions.

── BOILERPLATE AWARENESS ────────────────────────────────────────────────────────
SEC boilerplate ("may adversely affect", "subject to risks", "cyber-attacks may")
is standard legal language, not news. Ignore as a sentiment driver.
NEVER output "No fresh risk signals in current data." — omit Risk/Watchlist
entirely if no fresh content exists.

── SOURCE HIERARCHY ─────────────────────────────────────────────────────────────
Trust: news wire > analyst report > earnings transcript >
       regulatory filing > company PR > plaintiff-PR (lowest).
Plaintiff-PR ("investor alert", "class action", "investigates"): do not let it
drive the sentiment label, Executive Summary, or any directional claim regardless
of rank. If a plaintiff-PR row is present in the evidence, mention it in at most
one Risk/Watchlist sentence. If no plaintiff-PR row is present, say nothing.
Multiple plaintiff-PR rows = ONE signal.
Multiple same-theme PRs from one wire = ONE voice.

── EVIDENCE QUALITY ─────────────────────────────────────────────────────────────
If evidence is thin, source-concentrated, or low-relevance, prefix Executive
Summary: "Evidence base is [thin/source-concentrated/low-relevance]; conclusions
drawn with reduced confidence." Scale certainty to evidence density.

── TAKE A VIEW ──────────────────────────────────────────────────────────────────
Executive Summary opens with a directional claim, not a description.
"Sentiment is mixed" = bad. "NVDA enters Q2 with coverage momentum that flat
sentiment has yet to confirm" = good.

── FALSE BALANCE ────────────────────────────────────────────────────────────────
Overwhelmingly directional evidence → write it with conviction, then:
"No material counter-evidence in the current data."

── CATEGORY VS COMPANY ──────────────────────────────────────────────────────────
For each evidence row, verify the company is the primary subject — not merely
a customer, partner, or peripheral mention. Secondary-mention rows = background
context only; do not use them for directional claims. If >2 top rows are about
other entities: "A significant portion of coverage covers partner/sector stories
where the company appears as a secondary mention."

── OUTLOOK SPECIFICITY ──────────────────────────────────────────────────────────
Name the single sharpest unresolved tension (company action vs. market pricing).
No generic advice. If unsupported: "Evidence base insufficient to support a
specific forward view."

── OUTPUT FORMAT ────────────────────────────────────────────────────────────────
Output EXACTLY these four sections:

## Executive Summary
[View in sentence 1. 2-3 sentences anchoring quant regime + evidence. Caveat if warranted.]

## Key Drivers
**Bullish:** [evidence-backed factors, source + date. Omit if absent.]
**Risk / Watchlist:** [evidence-backed concerns. Omit if no fresh signals.]

## Media Attention Signal
[Momentum + z-scores + why per evidence. Flag category vs. company-specific coverage.]

## Outlook
[Single sharpest tension. Company-specific. No generic advice.
If unsupported: "Evidence base insufficient to support a specific forward view."]
"""


def generate_narrative(
    client: OpenAI,
    company: dict,
    signals: dict,
    docs: list[dict],
    days: int,
    model: str,
    raw_count: int = 0,
) -> str:
    context = {
        "company": {
            "name":        company["name"],
            "id":          company["id"],
            "description": company["description"],
            "industry":    company["industry"],
            "sector":      company["sector"],
            "country":     company["country"],
            "type":        company["type"],
        },
        "signal_date":           date.today().isoformat(),
        "signal_lookback_days":  LOOKBACK,
        "narrative_window_days": days,
        "quantitative_signals": {
            k: v for k, v in signals.items() if not k.startswith("_")
        },
        # Strip internal-only fields and trim text before sending to LLM.
        # doc_score / plaintiff_pr / rank / url add tokens without helping the model.
        # Text capped at 250 chars (ample context; 400 was generous).
        "evidence_docs": [
            {
                "headline":  d["headline"],
                "source":    d["source"],
                "timestamp": d["timestamp"],
                "snippets":  d["snippets"],
                "relevance": d["relevance"],
                "sentiment": d["sentiment"],
                "text":      d["text"][:250],
            }
            for d in docs
        ],
        "evidence_quality": {
            "total_raw_chunks":  raw_count,
            "total_docs":        len(docs),
            "typical_threshold": 30,
        },
    }
    # Compact JSON — no indentation whitespace; saves ~400 prompt tokens for a
    # typical 20-doc context which translates directly to lower LLM latency.
    user_msg = (
        f"Generate a Sentiment Intelligence Report for {company['name']} "
        f"using the structured data below.\n\n"
        f"```json\n{json.dumps(context, separators=(',', ':'))}\n```"
    )
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=1200,   # 400-600 word target ≈ 800-900 tokens incl. markdown; 1200 gives safe headroom
    )
    try:
        response = client.chat.completions.create(**kwargs)
    except BadRequestError as e:
        # Newer models (o-series, gpt-5+) require max_completion_tokens
        if "max_tokens" in str(e) and "max_completion_tokens" in str(e):
            del kwargs["max_tokens"]
            kwargs["max_completion_tokens"] = 1200
            response = client.chat.completions.create(**kwargs)
        else:
            raise
    return response.choices[0].message.content.strip()


# ── Render helpers ────────────────────────────────────────────────────────────

def _sent_label(v: float | None) -> str:
    if v is None:
        return "N/A"
    if   v >  0.15: return "Strongly Positive"
    elif v >  0.05: return "Slightly Positive"
    elif v < -0.15: return "Strongly Negative"
    elif v < -0.05: return "Slightly Negative"
    else:           return "Neutral"


def _mom_label(v: float | None, unit: str = "") -> str:
    if v is None:
        return "N/A"
    if   v >  1.5: return f"↑↑ Strong upward   ({v:+.1f}{unit})"
    elif v >  0.5: return f"↑  Mild upward     ({v:+.1f}{unit})"
    elif v < -1.5: return f"↓↓ Strong downward ({v:+.1f}{unit})"
    elif v < -0.5: return f"↓  Mild downward   ({v:+.1f}{unit})"
    else:          return f"─  Stable          ({v:+.1f}{unit})"


def _z_label(z: float | None) -> str:
    if z is None:
        return "insufficient data"
    if   z >  2.0: return f"Unusually HIGH   (z={z:+.1f})"
    elif z >  1.0: return f"Above average    (z={z:+.1f})"
    elif z < -2.0: return f"Unusually LOW    (z={z:+.1f})"
    elif z < -1.0: return f"Below average    (z={z:+.1f})"
    else:          return f"Normal range     (z={z:+.1f})"


def _fmt_date(ts: str) -> str:
    """'2026-04-14 09:32' → 'Apr 14'"""
    try:
        return datetime.strptime(ts[:10], "%Y-%m-%d").strftime("%b %d")
    except ValueError:
        return ts[:6]


def _noneg_zero(v: float) -> float:
    """Normalise negative-zero: -0.000 displays as -0.000 which is visually wrong.
    Return plain 0.0 for any value equal to zero after Python's float comparison."""
    return 0.0 if v == 0.0 else v


# ── Terminal renderer ─────────────────────────────────────────────────────────

def render_terminal(
    company: dict,
    signals: dict,
    docs: list[dict],
    narrative: str,
    days: int,
    raw_count: int,
) -> None:
    name      = company["name"]
    entity_id = company["id"]
    W  = "═" * 70
    HR = "─" * 66
    s  = signals["sentiment"]
    m  = signals["media_attention"]

    print(f"\n{W}")
    print(f"  {name}  [{entity_id}]")
    meta_parts = [p for p in [company.get("industry"), company.get("country"), company.get("type")] if p]
    if meta_parts:
        print(f"  {' · '.join(meta_parts)}")
    print(f"  Sentiment Intelligence Report  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  |  Bigdata.com")
    print(f"{W}")

    no_data = (s["current"] == 0.0 and s["baseline"] == 0.0 and not docs)

    if no_data:
        print(f"\n  No documents found for this company in the last {LOOKBACK} days.")
        print(f"  This lack of media attention suggests limited engagement")
        print(f"  or scrutiny from investors and analysts.")
        print(f"\n{W}")
        return

    print(f"\n  ▌ QUANTITATIVE SIGNALS  (90-day lookback)")
    print(f"  {HR}")
    print(f"\n  SENTIMENT")
    cur = s["current"]
    print(f"    Current            {cur:>+.4f}  —  {_sent_label(cur)}")
    print(f"    Baseline           {s['baseline']:>+.4f}")
    print(f"    Momentum           {_mom_label(s['momentum'])}")
    print(f"    vs. 1-month        {_z_label(s['zscore_1mo'])}")
    print(f"    vs. 1-quarter      {_z_label(s['zscore_1qt'])}")

    print(f"\n  MEDIA ATTENTION")
    print(f"    Momentum           {_mom_label(m['momentum_pct'], '%')}")
    print(f"    vs. 1-month        {_z_label(m['zscore_1mo'])}")
    print(f"    vs. 1-quarter      {_z_label(m['zscore_1qt'])}")

    print(f"\n  {HR}")
    print(f"  ▌ SENTIMENT INTELLIGENCE")
    print(f"  {HR}")
    for line in narrative.splitlines():
        print(f"  {line}")

    total_snippets = sum(d["snippets"] for d in docs)
    total_docs     = len(docs)
    print(f"\n  {HR}")
    print(
        f"  ▌ SOURCE EVIDENCE  "
        f"(last {days}d · {raw_count} raw · {total_snippets} snippets from {total_docs} docs)"
    )
    print(f"  {HR}")
    print(f"  {'Ranking':<8} {'Date':<7} {'Source':<22}  {'Headline':<45}  {'Snippets':>8}  {'Relevance':>9}  Sent")
    print(f"  {HR}")
    for d in docs:
        sent = _noneg_zero(d["sentiment"])
        sign = "▲" if sent > 0.05 else "▼" if sent < -0.05 else "─"
        hl   = _trunc_headline(d["headline"], 45)
        print(
            f"  {d['rank']:<8} {_fmt_date(d['timestamp']):<7} {d['source'][:22]:<22}  "
            f"{hl:<45}  {d['snippets']:>8}  {d['relevance']:>9.2f}  {sent:>+7.3f}{sign}"
        )

    print(f"\n  {HR}")
    print(f"  ▌ METRICS REFERENCE")
    print(f"  {HR}")
    print(f"  Sentiment (−1 to +1)  daily relevance-weighted avg across snippets")
    print(f"    Current     exponential smooth halflife 5d  — current regime")
    print(f"    Baseline    exponential smooth halflife 21d — trend baseline")
    print(f"    Momentum    Current minus Baseline; positive = rising above trend")
    print(f"  Media Attention  daily evidence count, DoW-normalised (removes Mon>Sun")
    print(f"                   seasonality) before smoothing and z-scoring")
    print(f"  Z-score (1mo/1qt)  std devs above/below rolling mean")
    print(f"                     |z|>2 unusual · 1<|z|<2 notable · |z|<1 normal")
    print(f"\n{W}")


def print_timing(timings: dict[str, float], sig_t: float, ch_t: float) -> None:
    HR = "─" * 49
    wall_parallel = max(sig_t, ch_t)
    total = sum(timings.values())

    print(f"\n  ▌ TIMING BREAKDOWN")
    print(f"  {HR}")
    for label, elapsed in timings.items():
        if label == "Signals (parallel)":
            print(f"  {label:<28}  {elapsed:>5.2f}s  ┐ ran concurrently")
        elif label == "Chunk fetch (parallel)":
            print(f"  {label:<28}  {elapsed:>5.2f}s  ┘ wall time: {wall_parallel:.2f}s")
        else:
            print(f"  {label:<28}  {elapsed:>5.2f}s")
    print(f"  {HR}")
    print(f"  {'Total wall time':<28}  {total - sig_t - ch_t + wall_parallel:>5.2f}s")
    print(f"  {'═' * 70}\n")


# ── Markdown renderer ─────────────────────────────────────────────────────────

def build_markdown(
    company: dict,
    signals: dict,
    docs: list[dict],
    narrative: str,
    days: int,
    raw_count: int,
) -> str:
    name      = company["name"]
    entity_id = company["id"]
    s   = signals["sentiment"]
    m   = signals["media_attention"]
    cur = s["current"]

    meta_parts = [p for p in [company.get("industry"), company.get("country"), company.get("type")] if p]
    meta_str   = "  ·  ".join(meta_parts) if meta_parts else ""

    total_snippets = sum(d["snippets"] for d in docs)
    total_docs     = len(docs)

    lines: list[str] = [
        f"# {name} — Sentiment Intelligence Report",
        f"**Entity ID:** `{entity_id}`  |  **Date:** {date.today()}  |  **Powered by:** Bigdata.com",
    ]
    if meta_str:
        lines.append(f"**{meta_str}**")
    if company.get("description"):
        lines.append(f"\n> {company['description']}")
    lines += [
        "",
        "---",
        "",
        "## Quantitative Signals — 90-day Lookback",
        "",
        "### Sentiment",
        "| Metric | Value | Interpretation |",
        "|--------|-------|----------------|",
        f"| Current | `{cur:+.4f}` | {_sent_label(cur)} |",
        f"| Baseline | `{s['baseline']:+.4f}` | |",
        f"| Momentum | `{s['momentum']:+.4f}` | {_mom_label(s['momentum'])} |",
        f"| vs. 1-month (z) | `{s['zscore_1mo']}` | {_z_label(s['zscore_1mo'])} |",
        f"| vs. 1-quarter (z) | `{s['zscore_1qt']}` | {_z_label(s['zscore_1qt'])} |",
        "",
        "### Media Attention",
        "| Metric | Value | Interpretation |",
        "|--------|-------|----------------|",
        f"| Momentum % | `{m['momentum_pct']}%` | {_mom_label(m['momentum_pct'], '%')} |",
        f"| vs. 1-month (z) | `{m['zscore_1mo']}` | {_z_label(m['zscore_1mo'])} |",
        f"| vs. 1-quarter (z) | `{m['zscore_1qt']}` | {_z_label(m['zscore_1qt'])} |",
        "",
        "---",
        "",
        "## Sentiment Intelligence",
        "",
        narrative,
        "",
        "---",
        "",
        f"## Source Evidence — Last {days} Days ({total_snippets} snippets from {total_docs} documents · {raw_count} raw)",
        "",
        "| Ranking | Date | Source | Headline | Snippets | Relevance | Sent | Link |",
        "|---------|------|--------|----------|----------|-----------|------|------|",
    ]
    for d in docs:
        sent      = _noneg_zero(d["sentiment"])
        sign      = "▲" if sent > 0.05 else "▼" if sent < -0.05 else "─"
        hl        = _trunc_headline(d["headline"])
        link_cell = f"[↗]({d['url']})" if d["url"] else "—"
        lines.append(
            f"| {d['rank']} | {_fmt_date(d['timestamp'])} | {d['source']} "
            f"| {hl} | {d['snippets']} | {d['relevance']:.2f} | `{sent:+.3f}`{sign} | {link_cell} |"
        )

    lines += [
        "",
        "---",
        "",
        "## Metrics Reference",
        "",
        "**Sentiment (−1 to +1):** daily relevance-weighted average across snippets.",
        "- **Current:** exponential smooth, halflife 5d — current regime.",
        "- **Baseline:** exponential smooth, halflife 21d — trend baseline.",
        "- **Momentum:** Current minus Baseline. Positive = sentiment rising above trend.",
        "",
        "**Media Attention:** daily evidence count, day-of-week normalised"
        " (removes Mon > Sun seasonality) before smoothing and z-scoring.",
        "",
        "**Z-score (1mo / 1qt):** standard deviations above/below the rolling mean."
        " |z| > 2 unusual · 1 < |z| < 2 notable · |z| < 1 normal.",
    ]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"\n---\n*Generated {ts} — Bigdata.com*")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args        = parse_args()
    t_wall      = time.monotonic()
    timings: dict[str, float] = {}

    now      = datetime.now(timezone.utc)
    start_ts = (now - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_ts   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"\nSentiment Intelligence  |  {args.query}  |  {date.today()}")
    print("─" * 60)

    # Step 0 — validate keys (optional, off by default)
    t0 = time.monotonic()
    if args.validate_keys:
        print("Validating API keys...")
        client = validate_api_keys()
    else:
        client = OpenAI(api_key=OPENAI_API_KEY)
    timings["API key validation"] = round(time.monotonic() - t0, 2)

    # Step 1 — resolve company
    print(f"\n[1/4] Resolving \"{args.query}\"...")
    t0      = time.monotonic()
    company = resolve_company(args.query)
    timings["Ticker resolution"] = round(time.monotonic() - t0, 2)

    meta_parts = [p for p in [company.get("industry"), company.get("country"), company.get("type")] if p]
    print(f"      → {company['name']}  [{company['id']}]" +
          (f"  ·  {' · '.join(meta_parts)}" if meta_parts else ""))
    if company.get("description"):
        desc = company["description"]
        print(f"      → {(desc[:120] + '…') if len(desc) > 120 else desc}")

    # Step 2 — parallel: signals + chunks
    print(f"\n[2/4] Fetching signals + chunks in parallel...")
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_signals = ex.submit(get_signal_snapshot, company["id"])
        fut_chunks  = ex.submit(fetch_chunks, company["id"], start_ts, end_ts, args.chunks, args.days)
        signals                    = fut_signals.result()
        raw_chunks, ch_t, chunk_days = fut_chunks.result()
    sig_t = signals["_elapsed_s"]
    timings["Signals (parallel)"]      = sig_t
    timings["Chunk fetch (parallel)"]  = ch_t

    s = signals["sentiment"]
    m = signals["media_attention"]
    print(f"      signals: {sig_t}s  |  chunks: {ch_t}s  |  wall: {max(sig_t, ch_t):.2f}s")

    no_coverage = (s["current"] == 0.0 and s["baseline"] == 0.0)
    if no_coverage:
        print(f"      ⚠  No volume data found for this entity in the last {LOOKBACK} days.")
        print(f"         Quantitative signals will show as zero / N/A.")
    else:
        print(f"      sentiment {s['current']:+.3f}  "
              f"momentum {s['momentum']:+.3f}  "
              f"z(1mo) {s['zscore_1mo']}")
    if chunk_days != args.days:
        print(f"      ⚠  No chunks in last {args.days}d — widened to {chunk_days}d to find coverage.")
    print(f"      {len(raw_chunks)} raw snippets received (window: last {chunk_days}d)")

    # Step 3 — aggregate chunks to document-level entries
    t0 = time.monotonic()
    print(f"\n[3/4] Aggregating to top {args.top} documents (doc-level)...")
    docs = aggregate_to_docs(raw_chunks, top_n=args.top)
    timings["Doc aggregation"] = round(time.monotonic() - t0, 2)
    total_snippets = sum(d["snippets"] for d in docs)
    print(f"      {len(raw_chunks)} raw → {total_snippets} snippets from {len(docs)} documents")

    # Step 4 — narrative (skipped entirely when no data at all)
    no_data = no_coverage and len(docs) == 0
    print(f"\n[4/4] Generating narrative ({args.model})...")
    t0 = time.monotonic()
    if no_data:
        narrative = ""
        timings["Narrative (LLM)"] = 0.0
        print(f"      skipped — no documents found")
    else:
        narrative = generate_narrative(
            client, company, signals, docs, chunk_days, args.model,
            raw_count=len(raw_chunks),
        )
        timings["Narrative (LLM)"] = round(time.monotonic() - t0, 2)
        print(f"      done in {timings['Narrative (LLM)']}s  ({len(narrative.split())} words)")

    # Render
    render_terminal(company, signals, docs, narrative, chunk_days, len(raw_chunks))
    print_timing(timings, sig_t, ch_t)

    # Optional markdown export
    if args.out:
        md = build_markdown(company, signals, docs, narrative, chunk_days, len(raw_chunks))
        with open(args.out, "w") as f:
            f.write(md)
        print(f"Report saved → {args.out}")


if __name__ == "__main__":
    main()
