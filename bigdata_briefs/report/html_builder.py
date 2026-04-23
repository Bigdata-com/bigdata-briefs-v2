#!/usr/bin/env python3
"""Build HTML brief reports from detailed JSON (Companies_new_details.json).

Detail runs: only **active** bullets appear in the main ``<ol>``. Below that, a
single orange **Discard** expander lists inactive bullets **grouped by**
``discarded.stage``; each discarded row has a nested **Details** expander for
full payload. A bullet is treated as detail-shaped when it has ``is_active`` and
``original_text``; ``final_text`` is optional (passed bullets may publish the
draft line only). Legacy runs (no detail shape) match ``build_brief_html_from_json``
(all bullets in the list + run-level ``discarded_by_*`` when present).
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Final, TypedDict, cast

_DISCARDED_RUN_KEYS: Final[tuple[tuple[str, str], ...]] = (
    ("discarded_by_relevance", "Relevance"),
    ("discarded_by_grounding", "Grounding"),
    ("discarded_by_novelty", "Novelty"),
)

_DISCARD_STAGE_ORDER: Final[tuple[str, ...]] = (
    "relevance_score",
    "grounding",
    "novelty_embedding",
    "novelty_embedding_relevance",
    "novelty_search",
    "novelty_search_relevance",
    "error",
    "unknown",
)

_DIR = Path(__file__).resolve().parent


class CitationJson(TypedDict, total=False):
    id: str
    headline: str
    text: str
    source_name: str


class BulletJson(TypedDict, total=False):
    trace_id: str
    text: str
    citations: list[CitationJson]
    embedding_decision: str
    search_action: str
    not_fully_novel: bool
    theme: str
    original_text: str
    final_text: str
    is_active: bool
    passed: object
    discarded: object


class DetailPassedJson(TypedDict, total=False):
    relevance_score: int
    relevance_reason: str


class RetrievedBulletJson(TypedDict, total=False):
    id: str
    text: str
    score: float
    date: str


class EvaluatorDetailJson(TypedDict, total=False):
    evaluator_name: str
    decision: str
    reason: str
    retrieved_bullets: list[RetrievedBulletJson]


class SearchEvidenceJson(TypedDict, total=False):
    simple_id: str
    original_doc_id: str
    chunk_num: int
    headline: str
    date: str
    text: str


class ClaimVerdictJson(TypedDict, total=False):
    claim_index: int
    claim_text: str
    novelty: str
    reasoning: str
    evidence: list[SearchEvidenceJson]


class DetailDiscardedJson(TypedDict, total=False):
    stage: str
    reason: str
    score: int | float
    citations: list[str]
    evaluator_details: list[EvaluatorDetailJson]
    claim_verdicts: list[ClaimVerdictJson]
    overall_verdict: str


class RunJson(TypedDict, total=False):
    run_id: str
    report_window_start: str
    report_window_end: str
    run_created_at: str
    bullet_count: int
    total_bullets: int
    active_bullets: int
    discarded_bullets: int
    bullets: list[BulletJson]
    discarded_by_relevance: list[str]
    discarded_by_grounding: list[str]
    discarded_by_novelty: list[str]


class EntityJson(TypedDict, total=False):
    entity_id: str
    found: bool
    entity_name: str | None
    total_runs: int
    total_bullets: int
    runs: list[RunJson]
    latest_run: RunJson


class RootJson(TypedDict, total=False):
    results: list[EntityJson]
    total_entities: int
    total_bullets: int


_HTML_DOC_SHELL_HEAD: Final[str] = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
  <style>
    :root {{
      --bg-top: #dce4f0;
      --bg-mid: #e8edf6;
      --bg-bottom: #f0f4fb;
      --surface: #ffffff;
      --text: #0f172a;
      --muted: #64748b;
      --border: #d5dee9;
      --border-soft: #e8eef5;
      --accent: #2563eb;
      --accent-hover: #1d4ed8;
      --pill-bg: #eef2f9;
      --source-border: #b8c5d9;
      --card-shadow: 0 4px 22px rgba(15, 23, 42, 0.07), 0 1px 3px rgba(15, 23, 42, 0.04);
      --discard: #ea580c;
      --discard-hover: #c2410c;
      --discard-border: #fdba74;
      --discard-bg: #fff7ed;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      font-size: 15px;
      line-height: 1.55;
      color: var(--text);
      background-color: var(--bg-mid);
      background-image:
        radial-gradient(ellipse 120% 80% at 50% -20%, rgba(255, 255, 255, 0.5), transparent 55%),
        linear-gradient(168deg, var(--bg-top) 0%, var(--bg-mid) 42%, var(--bg-bottom) 100%);
      background-attachment: fixed;
    }}
    .wrap {{
      max-width: 1196px;
      margin: 0 auto;
      padding: 2rem 1.25rem 3rem;
    }}
    .brief-legend {{
      margin-bottom: 1.75rem;
      padding: 1rem 1.25rem;
      background: var(--surface);
      border-radius: 14px;
      border: 1px solid var(--border);
      box-shadow: var(--card-shadow);
    }}
    .brief-legend h3 {{
      margin: 0 0 0.75rem 0;
      font-size: 0.95rem;
      font-weight: 700;
      color: var(--text);
      letter-spacing: -0.01em;
    }}
    ul.legend-rows {{
      margin: 0;
      padding: 0;
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 0.65rem;
    }}
    .legend-row {{
      display: flex;
      align-items: flex-start;
      gap: 0.65rem;
      font-size: 0.88rem;
      line-height: 1.45;
      color: #334155;
    }}
    .legend-swatch {{
      flex-shrink: 0;
      width: 1.1rem;
      height: 1.1rem;
      border-radius: 4px;
      margin-top: 0.2rem;
      border: 1px solid rgba(15, 23, 42, 0.12);
    }}
    .legend-swatch.fully-novel {{
      background: #166534;
    }}
    .legend-swatch.partially-novel {{
      background: #854d0e;
    }}
    .legend-label-green {{
      color: #166534;
      font-weight: 700;
    }}
    .legend-label-amber {{
      color: #854d0e;
      font-weight: 700;
    }}
    article.entity {{
      background: var(--surface);
      border-radius: 14px;
      border: 1px solid var(--border);
      box-shadow: var(--card-shadow);
      margin-bottom: 2rem;
      overflow: visible;
    }}
    .entity-header {{
      padding: 1.25rem 1.5rem;
      border-bottom: 1px solid var(--border-soft);
      background: linear-gradient(180deg, #ffffff 0%, #f7f9fd 100%);
    }}
    .entity-header h2 {{
      margin: 0;
      font-size: 1.35rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }}
    .entity-id {{
      display: inline-block;
      margin-top: 0.35rem;
      font-size: 0.8rem;
      color: var(--muted);
      font-family: ui-monospace, monospace;
    }}
    .run-block {{
      padding: 1rem 1.5rem 1.25rem;
      border-bottom: 1px solid var(--border-soft);
      background: linear-gradient(180deg, rgba(248, 250, 252, 0.65) 0%, rgba(255, 255, 255, 0.4) 100%);
      overflow: visible;
    }}
    .run-block:last-child {{ border-bottom: none; }}
    .run-day {{
      display: inline-block;
      font-size: 0.8rem;
      font-weight: 600;
      color: #334155;
      background: var(--pill-bg);
      padding: 0.35rem 0.75rem;
      border-radius: 6px;
      margin-bottom: 1rem;
    }}
    .run-empty-day {{
      margin: 0;
      font-size: 0.88rem;
      color: var(--muted);
      font-style: italic;
    }}
    ol.bullets {{
      margin: 0;
      padding-left: 1.35rem;
    }}
    ol.bullets li {{
      margin-bottom: 1.85rem;
      padding-left: 0.35rem;
      position: relative;
      z-index: 0;
    }}
    ol.bullets li:has(.bullet-cite-inline-ref:hover),
    ol.bullets li:has(.bullet-cite-inline-ref:focus-within) {{
      z-index: 40;
    }}
    .bullet-text {{
      margin: 0 0 0.5rem 0;
    }}
    .bullet-text.bullet-fully-novel {{
      color: #166534;
    }}
    .bullet-text.bullet-not-fully-novel {{
      color: #854d0e;
    }}
    .bullet-text.bullet-text-discarded {{
      color: #475569;
    }}
    details.bullet-refs {{
      margin-top: 0.5rem;
      margin-left: 0;
      padding-bottom: 1.1rem;
    }}
    details.bullet-refs > summary {{
      cursor: pointer;
      list-style: none;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      user-select: none;
    }}
    details.bullet-refs > summary::-webkit-details-marker {{ display: none; }}
    details.bullet-refs > summary::marker {{ content: ""; }}
    .ref-trigger {{
      color: var(--accent);
      font-weight: 600;
      text-decoration: underline;
      text-underline-offset: 3px;
      font-size: 0.92rem;
    }}
    details.bullet-refs > summary:hover .ref-trigger {{
      color: var(--accent-hover);
    }}
    .ref-count {{
      color: var(--muted);
      font-size: 0.85rem;
      font-weight: 500;
    }}
    .refs-inner {{
      margin-top: 0.75rem;
      padding-left: 0.75rem;
      border-left: 3px solid var(--source-border);
    }}
    .source-card {{
      margin-bottom: 1rem;
      padding: 0.85rem 1rem;
      background: linear-gradient(145deg, #f4f7fc 0%, #eef2f9 100%);
      border-radius: 10px;
      border: 1px solid var(--border-soft);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.75);
    }}
    .source-line {{
      font-size: 0.88rem;
      color: #475569;
      margin-bottom: 0.65rem;
    }}
    .source-line-stack .source-meta-row {{
      display: block;
      line-height: 1.45;
      margin: 0;
    }}
    .source-line-stack .source-meta-row + .source-meta-row {{
      margin-top: 0.15rem;
    }}
    .source-line-stack .source-meta-title {{
      margin-bottom: 0.25rem;
    }}
    .source-line-stack .source-meta-title strong {{
      color: #1e293b;
    }}
    .source-line strong {{
      color: #1e293b;
    }}
    .hl-label, .tx-label {{
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: #64748b;
      margin-top: 0.5rem;
      margin-bottom: 0.2rem;
    }}
    .hl-body, .tx-body {{
      font-size: 0.88rem;
      color: #334155;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .no-refs {{
      font-size: 0.88rem;
      color: var(--muted);
      font-style: italic;
      margin-top: 0.25rem;
    }}
    details.run-discarded {{
      margin-top: 1rem;
      padding-top: 0.85rem;
      border-top: 1px dashed var(--border-soft);
    }}
    details.run-discarded > summary {{
      cursor: pointer;
      list-style: none;
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      user-select: none;
    }}
    details.run-discarded > summary::-webkit-details-marker {{ display: none; }}
    details.run-discarded > summary::marker {{ content: ""; }}
    .discard-trigger {{
      color: var(--discard);
      font-weight: 600;
      text-decoration: underline;
      text-underline-offset: 3px;
      font-size: 0.92rem;
    }}
    details.run-discarded > summary:hover .discard-trigger {{
      color: var(--discard-hover);
    }}
    .discard-count {{
      color: var(--muted);
      font-size: 0.85rem;
      font-weight: 500;
    }}
    .discard-inner {{
      margin-top: 0.85rem;
      padding: 0.75rem 0 0.25rem 0.75rem;
      border-left: 3px solid var(--discard-border);
      background: var(--discard-bg);
      border-radius: 0 10px 10px 0;
    }}
    .discard-category {{
      margin-bottom: 1rem;
    }}
    .discard-category:last-child {{
      margin-bottom: 0;
    }}
    .discard-cat-title {{
      margin: 0 0 0.45rem 0;
      font-size: 0.8rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #9a3412;
    }}
    .discard-cat-count {{
      color: var(--muted);
      font-weight: 600;
      text-transform: none;
      letter-spacing: normal;
    }}
    ul.discard-ul {{
      margin: 0;
      padding-left: 1.2rem;
    }}
    ul.discard-ul li {{
      margin-bottom: 0.5rem;
      font-size: 0.88rem;
      color: #334155;
      line-height: 1.45;
      position: relative;
      z-index: 0;
    }}
    ul.discard-ul li:has(.bullet-cite-inline-ref:hover),
    ul.discard-ul li:has(.bullet-cite-inline-ref:focus-within) {{
      z-index: 40;
    }}
    .empty-doc {{
      padding: 2rem;
      text-align: center;
      color: var(--muted);
    }}
    .bullet-meta {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.5rem;
      margin: 0.2rem 0 0 0;
    }}
    .bullet-meta.bullet-meta-end {{
      justify-content: flex-end;
    }}
    .bullet-trailing-row {{
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      align-items: center;
      gap: 0.5rem;
      margin: 0.2rem 0 0 0;
    }}
    .bullet-trailing-row .bullet-meta {{
      margin: 0;
    }}
    .bullet-para {{
      margin: 0 0 0.5rem 0;
    }}
    .bullet-ref-inline-cluster {{
      display: inline;
      white-space: normal;
    }}
    .bullet-ref-inline-cluster .bullet-cite-inline-ref {{
      margin-left: 0.28rem;
    }}
    .bullet-ref-inline-cluster .bullet-cite-inline-ref:first-child {{
      margin-left: 0.15rem;
    }}
    .bullet-theme-suffix {{
      display: inline-flex;
      align-items: center;
      margin-left: 0.45rem;
      vertical-align: middle;
      flex-wrap: wrap;
      gap: 0.25rem;
    }}
    .bullet-cite-inline-ref {{
      position: relative;
      display: inline-block;
      vertical-align: baseline;
    }}
    .bullet-cite-ref-bracket {{
      font-size: 0.82rem;
      font-weight: 700;
      font-family: ui-monospace, monospace;
      color: var(--accent);
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      border-radius: 6px;
      padding: 0.15rem 0.4rem;
      line-height: 1.25;
      cursor: default;
      user-select: none;
      white-space: nowrap;
    }}
    .bullet-cite-inline-ref:hover .bullet-cite-ref-bracket,
    .bullet-cite-inline-ref:focus-within .bullet-cite-ref-bracket {{
      background: #dbeafe;
      border-color: #93c5fd;
    }}
    .bullet-cite-ref-pop {{
      display: none;
      position: absolute;
      left: 50%;
      right: auto;
      bottom: 100%;
      transform: translateX(-50%);
      margin-bottom: 8px;
      min-width: min(520px, calc(100vw - 32px));
      max-width: min(760px, calc(100vw - 32px));
      max-height: min(85vh, 780px);
      overflow: auto;
      padding: 1rem 1.1rem;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: var(--card-shadow);
      z-index: 200;
      text-align: left;
      box-sizing: border-box;
    }}
    .bullet-cite-inline-ref:hover .bullet-cite-ref-pop,
    .bullet-cite-inline-ref:focus-within .bullet-cite-ref-pop {{
      display: block;
    }}
    .bullet-cite-ref-pop .source-card {{
      max-width: 100%;
    }}
    .bullet-cite-ref-pop .tx-body {{
      max-height: none;
    }}
    .source-card--inline {{
      display: block;
    }}
    .source-card--inline .source-line,
    .source-card--inline .hl-label,
    .source-card--inline .hl-body,
    .source-card--inline .tx-label,
    .source-card--inline .tx-body {{
      display: block;
    }}
    .source-card--inline .hl-label,
    .source-card--inline .tx-label {{
      margin-top: 0.45rem;
    }}
    .theme-pill {{
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: #4338ca;
      background: #eef2ff;
      border: 1px solid #c7d2fe;
      padding: 0.2rem 0.5rem;
      border-radius: 6px;
    }}
    .status-badge {{
      font-size: 0.72rem;
      font-weight: 700;
      padding: 0.2rem 0.5rem;
      border-radius: 6px;
    }}
    .status-badge.published {{
      color: #14532d;
      background: #dcfce7;
      border: 1px solid #86efac;
    }}
    .status-badge.discarded {{
      color: #9a3412;
      background: #ffedd5;
      border: 1px solid #fdba74;
    }}
    .detail-panel {{
      margin-top: 0.65rem;
      padding: 0.75rem 1rem;
      background: #f8fafc;
      border: 1px solid var(--border-soft);
      border-radius: 10px;
      font-size: 0.88rem;
    }}
    .detail-block {{
      margin-top: 0.65rem;
    }}
    .detail-block:first-child {{
      margin-top: 0;
    }}
    .detail-label {{
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #64748b;
      margin-bottom: 0.25rem;
    }}
    .detail-reason, .detail-body {{
      color: #334155;
      line-height: 1.5;
    }}
    .text-compare {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.75rem;
      margin-top: 0.5rem;
    }}
    @media (max-width: 720px) {{
      .text-compare {{ grid-template-columns: 1fr; }}
    }}
    .text-compare-col {{
      padding: 0.65rem 0.75rem;
      background: var(--surface);
      border: 1px solid var(--border-soft);
      border-radius: 8px;
    }}
    .text-compare-col h5 {{
      margin: 0 0 0.35rem 0;
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      color: #64748b;
    }}
    .mono-list {{
      margin: 0.35rem 0 0 0;
      padding-left: 1.1rem;
      font-family: ui-monospace, monospace;
      font-size: 0.82rem;
      color: #475569;
    }}
    .evaluator-block {{
      margin-top: 0.75rem;
      padding-top: 0.75rem;
      border-top: 1px dashed var(--border-soft);
    }}
    .evaluator-block:first-of-type {{
      margin-top: 0;
      padding-top: 0;
      border-top: none;
    }}
    .retrieved-bullet-card {{
      margin-top: 0.5rem;
      padding: 0.6rem 0.75rem;
      background: var(--surface);
      border-radius: 8px;
      border: 1px solid var(--border-soft);
    }}
    .retrieved-bullet-meta {{
      font-size: 0.8rem;
      color: #475569;
      margin-bottom: 0.35rem;
    }}
    .claim-block {{
      margin-top: 0.85rem;
      padding: 0.75rem;
      background: var(--surface);
      border: 1px solid var(--border-soft);
      border-radius: 10px;
    }}
    .claim-block:first-of-type {{
      margin-top: 0.35rem;
    }}
    .claim-meta {{
      font-size: 0.8rem;
      color: #475569;
      margin-bottom: 0.35rem;
    }}
    .evidence-card {{
      margin-top: 0.5rem;
      padding: 0.65rem 0.75rem;
      background: linear-gradient(145deg, #f4f7fc 0%, #eef2f9 100%);
      border-radius: 8px;
      border: 1px solid var(--border-soft);
    }}
    .score-pill {{
      display: inline-block;
      margin-left: 0.35rem;
      font-size: 0.75rem;
      font-weight: 700;
      color: #1e40af;
      background: #dbeafe;
      padding: 0.1rem 0.4rem;
      border-radius: 4px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <aside class="brief-legend" aria-label="Brief novelty legend">
      <h3>Legend</h3>
      <ul class="legend-rows">
        <li class="legend-row">
          <span class="legend-swatch fully-novel" aria-hidden="true"></span>
          <span>
            <span class="legend-label-green">Green</span> briefs are fully novel: the line is treated as entirely new relative to prior coverage.
          </span>
        </li>
        <li class="legend-row">
          <span class="legend-swatch partially-novel" aria-hidden="true"></span>
          <span>
            <span class="legend-label-amber">Amber</span> briefs are partially new: they add new details, new information, or an evolution compared with an earlier story.
          </span>
        </li>
      </ul>
    </aside>
"""


_HTML_DOC_SHELL_TAIL: Final[str] = """
  </div>
</body>
</html>
"""


def _day_from_report_window_start(start: str) -> str:
    """Calendar day from ISO window start."""
    s = (start or "").strip()
    if not s:
        return ""
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        day = s[:10]
        if day[:4].isdigit() and day[5:7].isdigit() and day[8:10].isdigit():
            return day
    try:
        normalized = s.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        return s


def _document_id_and_chunk_index(raw_id: str) -> tuple[str | None, str | None]:
    """Parse chunk citation ids: ``CQS:<doc_hex>-<chunk>`` → (doc id, chunk index).

    Example: ``CQS:3BD3A9EC3D65DABFE93C4123C20E2F93-5`` →
    (``3BD3A9EC3D65DABFE93C4123C20E2F93``, ``5``).
    """
    s = (raw_id or "").strip()
    if not s:
        return None, None
    tail = s.split(":", 1)[-1].strip() if ":" in s else s
    if "-" in tail:
        doc_part, chunk_part = tail.rsplit("-", 1)
        doc_part, chunk_part = doc_part.strip(), chunk_part.strip()
        if doc_part:
            return doc_part, chunk_part if chunk_part else None
    return tail, None


def _bullet_citation_objects(bullet: BulletJson) -> list[CitationJson]:
    """Citation dicts on the bullet plus optional string IDs under ``discarded.citations`` (deduped by ``id``)."""
    seen: set[str] = set()
    out: list[CitationJson] = []

    def add_from_list(lst: object) -> None:
        if not isinstance(lst, list):
            return
        for x in lst:
            if isinstance(x, dict):
                cid = str(x.get("id") or "").strip()
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                out.append(cast(CitationJson, cast(dict[str, object], x)))
            elif isinstance(x, str) and x.strip():
                cid = x.strip()
                if cid in seen:
                    continue
                seen.add(cid)
                out.append(cast(CitationJson, {"id": cid}))

    add_from_list(bullet.get("citations"))
    discarded_raw = bullet.get("discarded")
    if isinstance(discarded_raw, dict):
        add_from_list(discarded_raw.get("citations"))
    return out


def _source_meta_stack_html(
    source_index: int,
    citation_id: str,
    *,
    as_inline_spans: bool,
    source_name: str = "",
    date: str = "",
) -> str:
    """Source header: ``Source N`` / ``Doc_id`` / ``Chunk`` / ``Date`` / optional ``Source Name``."""
    doc_id, chunk_ix = _document_id_and_chunk_index(citation_id)
    esc = html.escape
    doc_disp = esc(doc_id) if doc_id else "—"
    chunk_disp = esc(str(chunk_ix)) if chunk_ix is not None else "—"
    t = "span" if as_inline_spans else "div"
    sb = ' style="display:block"' if as_inline_spans else ""
    idx_esc = esc(str(source_index))
    name_row = ""
    sn = (source_name or "").strip()
    if sn:
        name_row = f'<{t} class="source-meta-row"{sb}>Source Name: {esc(sn)}</{t}>'
    date_row = ""
    dt = (date or "").strip()
    if dt:
        date_row = f'<{t} class="source-meta-row"{sb}>Date: {esc(dt)}</{t}>'
    return (
        f'<{t} class="source-meta-row source-meta-title"{sb}>'
        f"<strong>Source {idx_esc}</strong></{t}>"
        f'<{t} class="source-meta-row"{sb}>Doc_id: {doc_disp}</{t}>'
        f'<{t} class="source-meta-row"{sb}>Chunk: {chunk_disp}</{t}>'
        f"{date_row}"
        f"{name_row}"
    )


def _nl_to_br(s: str) -> str:
    return html.escape(s or "", quote=False).replace("\n", "<br/>\n")


def _runs_for_entity(entity: EntityJson) -> list[RunJson]:
    """Prefer ``runs`` when non-empty; otherwise a single ``latest_run`` object."""
    raw_runs = entity.get("runs")
    if isinstance(raw_runs, list) and raw_runs:
        return [cast(RunJson, x) for x in raw_runs if isinstance(x, dict)]
    latest = entity.get("latest_run")
    if isinstance(latest, dict):
        return [cast(RunJson, latest)]
    return []


def _is_detail_bullet(bullet: BulletJson) -> bool:
    """Pipeline detail JSON: ``is_active`` + draft line; ``final_text`` may be absent when unchanged."""
    return "is_active" in bullet and "original_text" in bullet


def _theme_sort_key(bullet: BulletJson) -> str:
    """Lowercase theme for stable ordering; empty themes sort last."""
    t = str(bullet.get("theme") or "").strip().lower()
    return t if t else "\uffff"


def _render_text_compare(original: str, final: str) -> str:
    return (
        '<div class="text-compare">'
        '<div class="text-compare-col"><h5>Original</h5><div class="detail-body">'
        f'{_nl_to_br(original.strip() or "—")}</div></div>'
        '<div class="text-compare-col"><h5>After rewrite</h5><div class="detail-body">'
        f'{_nl_to_br(final.strip() or "—")}</div></div>'
        "</div>"
    )


def _render_retrieved_bullet_html(rb_raw: object) -> str:
    if not isinstance(rb_raw, dict):
        return ""
    rb = cast(RetrievedBulletJson, cast(dict[str, object], rb_raw))
    bid = str(rb.get("id") or "").strip()
    txt = str(rb.get("text") or "")
    date = str(rb.get("date") or "").strip()
    score_v = rb.get("score")
    score_s = ""
    if isinstance(score_v, (int, float)):
        score_s = f"{float(score_v):.3f}"
    elif score_v is not None:
        score_s = html.escape(str(score_v), quote=False)
    return (
        f'<div class="retrieved-bullet-card">'
        f'<div class="retrieved-bullet-meta"><strong>{html.escape(bid or "—", quote=False)}</strong>'
        f" · similarity {score_s or '—'} · {html.escape(date or '—', quote=False)}</div>"
        f'<div class="tx-body">{_nl_to_br(txt)}</div>'
        f"</div>"
    )


def _render_search_evidence_html(ev_raw: object) -> str:
    if not isinstance(ev_raw, dict):
        return ""
    ev = cast(SearchEvidenceJson, cast(dict[str, object], ev_raw))
    sid = str(ev.get("simple_id") or "—")
    doc = str(ev.get("original_doc_id") or "")
    cn = ev.get("chunk_num")
    hl = str(ev.get("headline") or "")
    dt = str(ev.get("date") or "")
    tx = str(ev.get("text") or "")
    cn_s = html.escape(str(cn), quote=False) if cn is not None else "—"
    return (
        f'<div class="evidence-card">'
        f'<div class="source-line"><strong>{html.escape(sid, quote=False)}</strong>'
        f" · doc {html.escape(doc or '—', quote=False)} · chunk {cn_s}"
        f" · {html.escape(dt, quote=False)}</div>"
        f'<div class="hl-label">Headline</div><div class="hl-body">{_nl_to_br(hl or "—")}</div>'
        f'<div class="tx-label">Text</div><div class="tx-body">{_nl_to_br(tx)}</div>'
        f"</div>"
    )


def _render_claim_verdict_html(cv_raw: object) -> str:
    if not isinstance(cv_raw, dict):
        return ""
    cv = cast(ClaimVerdictJson, cast(dict[str, object], cv_raw))
    idx = cv.get("claim_index")
    ctext = str(cv.get("claim_text") or "")
    nov = str(cv.get("novelty") or "")
    rsn = str(cv.get("reasoning") or "").strip()
    idx_s = html.escape(str(idx), quote=False) if idx is not None else "—"
    parts: list[str] = [
        '<div class="claim-block">',
        f'<div class="claim-meta">Claim #{idx_s} · novelty: <strong>{html.escape(nov, quote=False)}</strong></div>',
        f'<div class="detail-body">{_nl_to_br(ctext.strip() or "—")}</div>',
    ]
    if rsn:
        parts.append(
            '<div class="detail-block"><div class="detail-label">Reasoning</div>'
            f'<div class="detail-reason">{_nl_to_br(rsn)}</div></div>'
        )
    evs = cv.get("evidence")
    if isinstance(evs, list) and evs:
        parts.append(
            '<div class="detail-label" style="margin-top:0.5rem">Prior coverage</div>'
        )
        for ev in evs:
            parts.append(_render_search_evidence_html(ev))
    parts.append("</div>")
    return "".join(parts)


def _render_discarded_detail_body(
    discarded_raw: object,
    original_text: str,
    final_text: str,
) -> str:
    if not isinstance(discarded_raw, dict):
        return ""
    d = cast(DetailDiscardedJson, cast(dict[str, object], discarded_raw))
    stage = str(d.get("stage") or "").strip()
    reason = str(d.get("reason") or "").strip()
    parts: list[str] = [
        '<div class="detail-panel">',
        '<div class="detail-block"><div class="detail-label">Reason</div>',
        f'<div class="detail-reason">{_nl_to_br(reason or "—")}</div></div>',
    ]
    if stage == "relevance_score":
        score_v = d.get("score")
        score_inner = ""
        if isinstance(score_v, (int, float)):
            score_inner = f'<span class="score-pill">Score {int(score_v)}/5</span>'
        elif score_v is not None:
            score_inner = f'<span class="score-pill">{html.escape(str(score_v), quote=False)}</span>'
        if score_inner:
            parts.append(
                '<div class="detail-block"><div class="detail-label">Score</div>'
                f'<div class="detail-body">{score_inner}</div></div>'
            )
    if stage in ("novelty_embedding_relevance", "novelty_search_relevance"):
        parts.append(
            '<div class="detail-block"><div class="detail-label">Original vs rewritten</div>'
            f"{_render_text_compare(original_text, final_text)}</div>"
        )
    if stage == "grounding":
        cites = d.get("citations")
        if isinstance(cites, list) and cites:
            lis = "".join(
                f"<li>{html.escape(str(c).strip(), quote=False)}</li>"
                for c in cites
                if str(c).strip()
            )
            if lis:
                parts.append(
                    '<div class="detail-block"><div class="detail-label">Source IDs checked</div>'
                    f'<ul class="mono-list">{lis}</ul></div>'
                )
    if stage == "novelty_embedding":
        evals = d.get("evaluator_details")
        if isinstance(evals, list):
            for ev in evals:
                if not isinstance(ev, dict):
                    continue
                ed = cast(EvaluatorDetailJson, cast(dict[str, object], ev))
                ename = str(ed.get("evaluator_name") or "evaluator")
                decision = str(ed.get("decision") or "")
                ev_reason = str(ed.get("reason") or "").strip()
                sub: list[str] = [
                    '<div class="evaluator-block">',
                    f'<div class="detail-label">{html.escape(ename, quote=False)}'
                    f" ({html.escape(decision, quote=False)})</div>",
                ]
                if ev_reason:
                    sub.append(f'<div class="detail-reason">{_nl_to_br(ev_reason)}</div>')
                rbs = ed.get("retrieved_bullets")
                if isinstance(rbs, list) and rbs:
                    sub.append(
                        '<div class="detail-label" style="margin-top:0.5rem">Similar prior bullets</div>'
                    )
                    for rb in rbs:
                        sub.append(_render_retrieved_bullet_html(rb))
                sub.append("</div>")
                parts.append("".join(sub))
    if stage == "novelty_search":
        ov = d.get("overall_verdict")
        if isinstance(ov, str) and ov.strip():
            parts.append(
                '<div class="detail-block"><div class="detail-label">Overall verdict</div>'
                f'<div class="detail-body">{html.escape(ov.strip(), quote=False)}</div></div>'
            )
        cvs = d.get("claim_verdicts")
        if isinstance(cvs, list):
            for cv in cvs:
                parts.append(_render_claim_verdict_html(cv))
    parts.append("</div>")
    return "".join(parts)


def _render_passed_block(passed_raw: object) -> str:
    if not isinstance(passed_raw, dict):
        return ""
    p = cast(DetailPassedJson, passed_raw)
    score = p.get("relevance_score")
    reason = str(p.get("relevance_reason") or "").strip()
    score_html = ""
    if isinstance(score, (int, float)):
        score_html = f'<span class="score-pill">Score {int(score)}/5</span>'
    elif score is not None:
        score_html = f'<span class="score-pill">{html.escape(str(score), quote=False)}</span>'
    return (
        f'<div class="detail-panel">'
        f'<div class="detail-label">Relevance (passed){score_html}</div>'
        f'<div class="detail-reason">{_nl_to_br(reason or "—")}</div>'
        f"</div>"
    )


def _wrap_bullet_details(inner: str, details_id: str, *, count_suffix: str = "") -> str:
    """Extra pipeline content (same ``<details>`` chrome as ``References``)."""
    id_esc = html.escape(details_id, quote=True)
    count_html = (
        f'<span class="ref-count">{html.escape(count_suffix, quote=False)}</span>'
        if count_suffix
        else ""
    )
    return (
        f'<details class="bullet-refs" id="{id_esc}">'
        f'<summary><span class="ref-trigger">Details</span>{count_html}</summary>'
        f'<div class="refs-inner">{inner}</div>'
        "</details>"
    )


def _render_detail_inner_published_extras(
    passed_raw: object,
    original: str,
    final: str,
) -> str:
    """Published-only ``Details`` inner: relevance and optional original draft."""
    parts: list[str] = []
    if isinstance(passed_raw, dict):
        block = _render_passed_block(passed_raw)
        if block:
            parts.append(block)
    if original and final and original != final:
        parts.append(
            '<div class="detail-panel">'
            '<div class="detail-label">Original draft</div>'
            f'<div class="tx-body">{_nl_to_br(original)}</div></div>'
        )
    return "\n".join(parts)


def _render_detail_inner_discarded_extras(
    discarded_raw: object,
    original: str,
    final: str,
) -> str:
    """Discarded reasoning — chunk citations use ``References`` like the base script."""
    parts: list[str] = []
    if final.strip() and final.strip() != (original or "").strip():
        parts.append(
            '<div class="detail-panel">'
            '<div class="detail-label">Rewritten text</div>'
            f'<div class="tx-body">{_nl_to_br(final)}</div></div>'
        )
    parts.append(_render_discarded_detail_body(discarded_raw, original, final))
    return "\n".join(parts)


def _discard_stage_group_heading(stage: str) -> str:
    """Short heading under the main ``Discard`` expander (grouped by stage)."""
    labels: dict[str, str] = {
        "relevance_score": "Relevance",
        "grounding": "Grounding",
        "novelty_embedding": "Novelty (embedding)",
        "novelty_embedding_relevance": "Relevance after embedding rewrite",
        "novelty_search": "Novelty (search)",
        "novelty_search_relevance": "Relevance after search rewrite",
        "error": "Pipeline error",
        "unknown": "Unknown stage",
    }
    return labels.get(stage, stage.replace("_", " ").title())


def _discard_stage_key(bullet: BulletJson) -> str:
    raw = bullet.get("discarded")
    if isinstance(raw, dict):
        s = str(raw.get("stage") or "").strip()
        return s if s else "unknown"
    return "unknown"


def _grouped_discarded_bullets(
    discarded: list[BulletJson],
) -> list[tuple[str, list[BulletJson]]]:
    buckets: dict[str, list[BulletJson]] = {}
    for b in discarded:
        buckets.setdefault(_discard_stage_key(b), []).append(b)

    def order_key(st: str) -> int:
        if st in _DISCARD_STAGE_ORDER:
            return _DISCARD_STAGE_ORDER.index(st)
        return len(_DISCARD_STAGE_ORDER)

    return [
        (st, sorted(buckets[st], key=lambda b: (_theme_sort_key(b),)))
        for st in sorted(buckets.keys(), key=order_key)
    ]


def _render_discarded_bullet_row_html(bullet: BulletJson, details_id: str) -> str:
    theme = str(bullet.get("theme") or "").strip()
    original = str(bullet.get("original_text") or "").strip()
    final = str(bullet.get("final_text") or "").strip()
    citations = _bullet_citation_objects(bullet)
    theme_html = (
        f'<span class="theme-pill">{html.escape(theme, quote=False)}</span>' if theme else ""
    )
    prose = _render_bullet_prose_with_inline_refs(
        original,
        citations,
        f"{details_id}-dcite",
        "",
        theme_html=theme_html,
        paragraph_extra_class="bullet-text-discarded",
    )
    inner_parts: list[str] = [
        _render_detail_inner_discarded_extras(bullet.get("discarded"), original, final),
    ]
    if citations:
        inner_parts.append(
            '<div class="detail-panel">'
            '<div class="detail-label">Chunk citations</div>'
            f"{_render_citation_cards(citations)}"
            "</div>"
        )
    inner = "".join(inner_parts).strip() or '<p class="no-refs">No additional details.</p>'
    nested = _wrap_bullet_details(inner, details_id, count_suffix="")
    return (
        f"<li>"
        f"{prose}"
        f"{nested}</li>"
    )


def _render_grouped_discard_section(discarded: list[BulletJson], section_id: str) -> str:
    """One orange ``Discard`` expander; groups by ``discarded.stage``; each row has nested ``Details``."""
    if not discarded:
        return ""
    total = len(discarded)
    grouped = _grouped_discarded_bullets(discarded)
    blocks: list[str] = []
    for g_idx, (stage, items) in enumerate(grouped, start=1):
        title = html.escape(_discard_stage_group_heading(stage), quote=False)
        n_bp = len(items)
        count_esc = html.escape(str(n_bp), quote=False)
        lis = "".join(
            _render_discarded_bullet_row_html(bul, f"{section_id}-g{g_idx}-i{i_idx}")
            for i_idx, bul in enumerate(items, start=1)
        )
        blocks.append(
            f'<div class="discard-category">'
            f'<h4 class="discard-cat-title">{title}'
            f'<span class="discard-cat-count"> ({count_esc})</span></h4>'
            f'<ul class="discard-ul">{lis}</ul>'
            f"</div>"
        )
    inner = "\n".join(blocks)
    id_esc = html.escape(section_id, quote=True)
    return f"""<details class="run-discarded" id="{id_esc}">
  <summary><span class="discard-trigger">Discard</span><span class="discard-count">({total})</span></summary>
  <div class="discard-inner">
{inner}
  </div>
</details>"""


def _display_name(entity: EntityJson) -> str:
    name = entity.get("entity_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return str(entity.get("entity_id") or "Unknown entity")


def _sorted_runs(runs: list[RunJson]) -> list[RunJson]:
    """Oldest ``report_window_start`` first, then newer runs."""

    def key(r: RunJson) -> str:
        return str(r.get("report_window_start") or "")

    return sorted(runs, key=key)


def _bullet_shows_partial_novelty_style(bullet: BulletJson) -> bool:
    """Amber styling: partial novelty or ``search_action`` rewrite (not ``embedding_decision``)."""
    if bullet.get("not_fully_novel") is True:
        return True
    return str(bullet.get("search_action") or "").strip().lower() == "rewrite"


def _bullets_ordered_for_display(bullets: list[BulletJson]) -> list[BulletJson]:
    """Fully-novel-style bullets first, then partial / rewrite signals; stable within each group."""

    def sort_key(b: BulletJson) -> int:
        return 1 if _bullet_shows_partial_novelty_style(b) else 0

    return sorted(bullets, key=sort_key)


def _run_has_detail_discards(bullets: list[BulletJson]) -> bool:
    return any(_is_detail_bullet(b) and b.get("is_active") is False for b in bullets)


def _split_detail_bullets(
    bullets: list[BulletJson],
) -> tuple[list[BulletJson], list[BulletJson], list[BulletJson]]:
    """``(active_detail, discarded_detail, legacy)`` for a single run."""
    active_detail: list[BulletJson] = []
    discarded_detail: list[BulletJson] = []
    legacy: list[BulletJson] = []
    for b in bullets:
        if not _is_detail_bullet(b):
            legacy.append(b)
            continue
        if b.get("is_active") is True:
            active_detail.append(b)
        elif b.get("is_active") is False:
            discarded_detail.append(b)
        else:
            active_detail.append(b)
    return active_detail, discarded_detail, legacy


def _sort_active_bullets_for_main_list(active: list[BulletJson]) -> list[BulletJson]:
    """Order by ``theme``, then novelty (amber after green within the same theme)."""
    return sorted(
        active,
        key=lambda b: (
            _theme_sort_key(b),
            1 if _bullet_shows_partial_novelty_style(b) else 0,
        ),
    )


def _load_root(path: Path) -> RootJson:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"Expected JSON object at root of {path}"
        raise ValueError(msg)
    return cast(RootJson, raw)


def _render_one_citation_card(
    cit: CitationJson,
    source_index: int,
    *,
    as_inline_spans: bool = False,
) -> str:
    """Single source card (headline + chunk text) for one citation index."""
    cid = str(cit.get("id") or "").strip()
    headline = str(cit.get("headline") or "").strip()
    ctext = str(cit.get("text") or "").strip()
    src_name = str(cit.get("source_name") or "").strip()
    date = str(cit.get("date") or "").strip()
    t = "span" if as_inline_spans else "div"
    root_cls = "source-card" + (" source-card--inline" if as_inline_spans else "")
    sb = ' style="display:block"' if as_inline_spans else ""
    return (
        f'<{t} class="{root_cls}"{sb}>'
        f'<{t} class="source-line source-line-stack"{sb}>'
        f"{_source_meta_stack_html(source_index, cid, as_inline_spans=as_inline_spans, source_name=src_name, date=date)}</{t}>"
        f'<{t} class="hl-label"{sb}>Headline</{t}>'
        f'<{t} class="hl-body"{sb}>{_nl_to_br(headline or "—")}</{t}>'
        f'<{t} class="tx-label"{sb}>Text</{t}>'
        f'<{t} class="tx-body"{sb}>{_nl_to_br(ctext or "—")}</{t}>'
        f"</{t}>"
    )


def _render_citation_cards(citations: list[CitationJson]) -> str:
    return "\n".join(
        _render_one_citation_card(cit, c_idx) for c_idx, cit in enumerate(citations, start=1)
    )


def _render_inline_ref_badges(citations: list[CitationJson], id_prefix: str) -> str:
    """One ``[n]`` marker per citation (inline in prose); hover shows that source only in a large panel."""
    if not citations:
        return ""
    parts: list[str] = []
    for i, cit in enumerate(citations, start=1):
        card = _render_one_citation_card(cit, i, as_inline_spans=True)
        num_esc = html.escape(str(i), quote=False)
        aria = html.escape(f"Reference {i}: full headline and chunk text", quote=True)
        pop_id = html.escape(f"{id_prefix}-pop{i}", quote=True)
        parts.append(
            '<span class="bullet-cite-inline-ref">'
            f'<span class="bullet-cite-ref-bracket" tabindex="0" role="button" aria-label="{aria}">'
            '<span class="bullet-cite-ref-open">[</span>'
            f'<span class="bullet-cite-ref-num">{num_esc}</span>'
            '<span class="bullet-cite-ref-close">]</span></span>'
            f'<span class="bullet-cite-ref-pop" id="{pop_id}" role="region" aria-label="{aria}">{card}</span>'
            "</span>"
        )
    return f'<span class="bullet-ref-inline-cluster" role="group">{"".join(parts)}</span>'


def _render_bullet_prose_with_inline_refs(
    published_plain: str,
    citations: list[CitationJson],
    id_prefix: str,
    novelty_class: str,
    *,
    theme_html: str = "",
    paragraph_extra_class: str = "",
) -> str:
    """Prose + citation markers after the last ``.`` (same flow), then optional theme pill after refs."""
    plain = (published_plain or "").strip() or "—"
    refs = _render_inline_ref_badges(citations, id_prefix)
    if not refs:
        inner = _nl_to_br(plain)
    else:
        dot = plain.rfind(".")
        if dot >= 0:
            before, after = plain[: dot + 1], plain[dot + 1 :]
            inner = _nl_to_br(before) + refs + _nl_to_br(after)
        else:
            inner = _nl_to_br(plain) + refs
    if theme_html:
        inner += f'<span class="bullet-theme-suffix">{theme_html}</span>'
    class_parts = ["bullet-text", "bullet-para"]
    nc = novelty_class.strip()
    if nc:
        class_parts.append(nc)
    pec = paragraph_extra_class.strip()
    if pec:
        class_parts.append(pec)
    class_attr = " ".join(class_parts)
    return f'<div class="{class_attr}" role="paragraph">{inner}</div>'


def _render_detail_bullet_block(bullet: BulletJson, bullet_index: int, details_id: str) -> str:
    theme = str(bullet.get("theme") or "").strip()
    original = str(bullet.get("original_text") or "").strip()
    final = str(bullet.get("final_text") or "").strip()
    published_line = final if final else original
    active = bullet.get("is_active") is True
    passed_raw = bullet.get("passed")
    theme_html = (
        f'<span class="theme-pill">{html.escape(theme, quote=False)}</span>' if theme else ""
    )
    citations = _bullet_citation_objects(bullet)
    if active:
        novelty_class = (
            "bullet-not-fully-novel"
            if _bullet_shows_partial_novelty_style(bullet)
            else "bullet-fully-novel"
        )
        body_block = _render_bullet_prose_with_inline_refs(
            published_line,
            citations,
            f"{details_id}-cite",
            novelty_class,
            theme_html=theme_html,
        )
        chunks_li: list[str] = [
            f'<li value="{bullet_index}">',
            body_block,
        ]
        extras = _render_detail_inner_published_extras(passed_raw, original, final)
        if extras.strip():
            chunks_li.append(_wrap_bullet_details(extras, f"{details_id}-detail", count_suffix=""))
        chunks_li.append("</li>")
        return "".join(chunks_li)
    return ""


def _render_legacy_bullet_block(bullet: BulletJson, bullet_index: int, details_id: str) -> str:
    """Identical structure to ``build_brief_html_from_json._render_bullet_block``."""
    body = str(bullet.get("text") or "").strip()
    theme = str(bullet.get("theme") or "").strip()
    theme_html = (
        f'<span class="theme-pill">{html.escape(theme, quote=False)}</span>' if theme else ""
    )
    citations = _bullet_citation_objects(bullet)
    if _bullet_shows_partial_novelty_style(bullet):
        novelty_class = "bullet-not-fully-novel"
    else:
        novelty_class = "bullet-fully-novel"
    body_block = _render_bullet_prose_with_inline_refs(
        body,
        citations,
        f"{details_id}-cite",
        novelty_class,
        theme_html=theme_html,
    )
    if not citations:
        return (
            f'<li value="{bullet_index}">'
            f"{body_block}"
            '<p class="no-refs">No chunk citations for this bullet.</p>'
            "</li>"
        )
    return (
        f'<li value="{bullet_index}">'
        f"{body_block}"
        "</li>"
    )


def _render_bullet_block(bullet: BulletJson, bullet_index: int, details_id: str) -> str:
    if _is_detail_bullet(bullet):
        return _render_detail_bullet_block(bullet, bullet_index, details_id)
    return _render_legacy_bullet_block(bullet, bullet_index, details_id)


def _run_discarded_sections(run: RunJson) -> list[tuple[str, list[str]]]:
    """Non-empty (label, bullet texts) for each discarded category on this run."""
    out: list[tuple[str, list[str]]] = []
    for key, label in _DISCARDED_RUN_KEYS:
        raw = run.get(key)
        if not isinstance(raw, list):
            continue
        items = [str(x).strip() for x in raw if str(x).strip()]
        if items:
            out.append((label, items))
    return out


def _render_run_discarded(run: RunJson, details_id: str) -> str:
    """Same as ``build_brief_html_from_json._render_run_discarded``."""
    sections = _run_discarded_sections(run)
    if not sections:
        return ""
    total = sum(len(items) for _label, items in sections)
    parts: list[str] = []
    for label, items in sections:
        lis = "".join(f"<li>{_nl_to_br(text)}</li>" for text in items)
        lab_esc = html.escape(label, quote=False)
        parts.append(
            f'<div class="discard-category">'
            f'<h4 class="discard-cat-title">Discarded by {lab_esc}</h4>'
            f'<ul class="discard-ul">{lis}</ul>'
            f"</div>"
        )
    inner = "\n".join(parts)
    id_esc = html.escape(details_id, quote=True)
    return f"""<details class="run-discarded" id="{id_esc}">
  <summary><span class="discard-trigger">Discarded</span><span class="discard-count">({total})</span></summary>
  <div class="discard-inner">
{inner}
  </div>
</details>"""


def build_html(data: dict, page_title: str = "Brief Report") -> str:
    """Generate HTML from a data dict compatible with BatchBulletsDetailResponse.

    Accepts the dict produced by ``BatchBulletsDetailResponse.model_dump()``
    (or any dict with a ``results`` list of entity objects) and returns the
    full HTML document as a string.
    """
    results = data.get("results") or []
    chunks: list[str] = [_HTML_DOC_SHELL_HEAD.format(title=html.escape(page_title, quote=True))]

    any_content = False
    entity_counter = 0
    for entity in results:
        if not entity.get("found"):
            continue
        runs = _runs_for_entity(entity)
        if not runs:
            continue
        bullets_in_entity = sum(len(r.get("bullets") or []) for r in runs)
        has_discarded = any(bool(_run_discarded_sections(r)) for r in runs) or any(
            _run_has_detail_discards(list(r.get("bullets") or [])) for r in runs
        )
        if bullets_in_entity == 0 and not has_discarded:
            continue
        any_content = True
        entity_counter += 1
        eid = str(entity.get("entity_id") or "")
        title = html.escape(_display_name(entity), quote=False)
        eid_esc = html.escape(eid, quote=False)
        chunks.append(
            f'<article class="entity" data-entity="{eid_esc}">'
            '<header class="entity-header">'
            f"<h2>{title}</h2>"
            f'<span class="entity-id">{eid_esc}</span>'
            "</header>"
        )
        for run_idx, run in enumerate(_sorted_runs(list(runs)), start=1):
            bullets = list(run.get("bullets") or [])
            discard_html = _render_run_discarded(run, f"e{entity_counter}-r{run_idx}-discard")
            detail_mode = bool(bullets) and any(_is_detail_bullet(b) for b in bullets)
            discarded_detail: list[BulletJson] = []
            main_bullets = bullets
            if detail_mode:
                active_d, discarded_detail, legacy = _split_detail_bullets(bullets)
                main_bullets = _sort_active_bullets_for_main_list(active_d + legacy)
            grouped_discard_html = (
                _render_grouped_discard_section(
                    discarded_detail,
                    f"e{entity_counter}-r{run_idx}-discgrp",
                )
                if detail_mode and discarded_detail
                else ""
            )
            w0 = str(run.get("report_window_start") or "").strip()
            day_label = _day_from_report_window_start(w0) or "Run"
            day_esc = html.escape(day_label, quote=False)
            if not main_bullets and not discard_html and not grouped_discard_html:
                chunks.append(f'<section class="run-block"><div class="run-day">{day_esc}</div>')
                chunks.append('<p class="run-empty-day">No bullets in this report window.</p>')
                chunks.append("</section>")
                continue
            chunks.append(f'<section class="run-block"><div class="run-day">{day_esc}</div>')
            if main_bullets:
                chunks.append('<ol class="bullets">')
                for idx, bullet in enumerate(main_bullets, start=1):
                    details_id = f"e{entity_counter}-r{run_idx}-b{idx}"
                    chunks.append(_render_bullet_block(bullet, idx, details_id))
                chunks.append("</ol>")
            if grouped_discard_html:
                chunks.append(grouped_discard_html)
            if discard_html:
                chunks.append(discard_html)
            chunks.append("</section>")
        chunks.append("</article>")

    if not any_content:
        chunks.append('<p class="empty-doc">No bullets with data in this file.</p>')

    chunks.append(_HTML_DOC_SHELL_TAIL)
    return "".join(chunks)


def _write_html_from_json(*, input_json: Path, output_html: Path, page_title: str) -> None:
    """Write HTML to a file from a JSON file path (CLI entry point only)."""
    data = _load_root(input_json)
    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(build_html(data, page_title), encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Build HTML from detailed brief JSON (theme, pass/discard reasons).",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=_DIR,
        help="Directory containing JSON inputs (default: this script's directory).",
    )
    parser.add_argument(
        "--companies-json",
        type=Path,
        default=None,
        help=(
            "Path to companies JSON (default: Companies_new_details.json if present, else Companies_new.json, else Companies.json)."
        ),
    )
    parser.add_argument(
        "--etfs-json",
        type=Path,
        default=None,
        help="Override path to ETFs.json.",
    )
    parser.add_argument(
        "--companies-html",
        type=Path,
        default=None,
        help="Output HTML for companies (default: <input-dir>/Companies_brief_with_chunks_details.html).",
    )
    parser.add_argument(
        "--etfs-html",
        type=Path,
        default=None,
        help="Output HTML for ETFs (default: <input-dir>/ETFs_brief_with_chunks.html).",
    )
    parser.add_argument(
        "--companies-only",
        action="store_true",
        help="Generate only the companies HTML; do not read ETFs.json or write the ETFs output.",
    )
    args = parser.parse_args()
    base = args.input_dir.resolve()
    if args.companies_json is not None:
        companies_json = args.companies_json.resolve()
    else:
        details_path = base / "Companies_new_details.json"
        new_path = base / "Companies_new.json"
        if details_path.is_file():
            companies_json = details_path
        elif new_path.is_file():
            companies_json = new_path
        else:
            companies_json = base / "Companies.json"
    etfs_json = args.etfs_json.resolve() if args.etfs_json is not None else base / "ETFs.json"
    companies_html = (
        args.companies_html.resolve()
        if args.companies_html is not None
        else base / "Companies_brief_with_chunks_details.html"
    )
    etfs_html = (
        args.etfs_html.resolve() if args.etfs_html is not None else base / "ETFs_brief_with_chunks.html"
    )

    if not companies_json.is_file():
        print(f"Missing input JSON: {companies_json}", file=sys.stderr)
        raise SystemExit(2)

    _write_html_from_json(
        input_json=companies_json,
        output_html=companies_html,
        page_title="Companies brief",
    )
    logging.info("Wrote %s", companies_html)
    print(f"Companies HTML: {companies_html}")

    if args.companies_only:
        logging.info("Skipped ETFs (--companies-only).")
        return

    if not etfs_json.is_file():
        logging.info("Skipping ETFs: file not found at %s", etfs_json)
        return

    _write_html_from_json(
        input_json=etfs_json,
        output_html=etfs_html,
        page_title="ETFs brief",
    )
    logging.info("Wrote %s", etfs_html)
    print(f"ETFs HTML:     {etfs_html}")


if __name__ == "__main__":
    main()
