// History Details / Forensics — full pipeline view per company
const { useState: useStateHD, useEffect: useEffectHD } = React;

function HistoryDetailsView({ tweaks, initialEntityId, initialDate }) {
  const companies = window.DATA?.companies || [];
  const initialId = initialEntityId || window.DATA.todaysBrief?.entityId || companies[0]?.id;

  const [selectedId, setSelectedId] = useStateHD(initialId);
  const [search, setSearch] = useStateHD("");
  const [openRunId, setOpenRunId] = useStateHD(null);
  const [expandedRejection, setExpandedRejection] = useStateHD(null);
  const [expandedPubCitation, setExpandedPubCitation] = useStateHD(null);
  const [forensicsData, setForensicsData] = useStateHD(null);
  const [loading, setLoading] = useStateHD(false);
  const summaries = window.DATA.companySummaries || {};

  useEffectHD(() => {
    loadForensics(initialId, initialDate);
  }, []);

  function loadForensics(id, targetDate) {
    setSelectedId(id);
    setLoading(true);
    setOpenRunId(null);
    setExpandedPubCitation(null);
    fetch(`/api/frontend/entity/${id}/forensics`)
      .then(r => r.json())
      .then(d => {
        setForensicsData(d);
        if (!d.days || d.days.length === 0) return;
        // If a target date is provided, open the run for that date; otherwise open the latest
        const targetDay = targetDate
          ? d.days.find(day => day.date === targetDate)
          : d.days[0];
        const day = targetDay || d.days[0];
        setOpenRunId(null);
        // Scroll to the target day after render
        if (targetDate) {
          requestAnimationFrame(() => {
            const el = document.getElementById(`hd-day-${targetDate}`);
            if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
          });
        }
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }

  const filtered = companies.filter(c =>
    c.name.toLowerCase().includes(search.toLowerCase()) ||
    c.ticker.toLowerCase().includes(search.toLowerCase())
  );

  const days = forensicsData?.days || [];
  const entityName = forensicsData?.entityName || companies.find(c => c.id === selectedId)?.name || selectedId;
  const ticker = forensicsData?.ticker || companies.find(c => c.id === selectedId)?.ticker || "";

  return (
    <div className="archive-layout hd-layout">
      <aside className="archive-side">
        <div className="t-cap">Coverage Universe</div>
        <input className="archive-search" type="text"
               placeholder="Search company or ticker…"
               value={search} onChange={(e) => setSearch(e.target.value)} />
        <ul className="archive-companies">
          {filtered.map(c => {
            const s = summaries[c.id] || {};
            const runs = s.bulletsSaved != null ? s.bulletsSaved : "—";
            return (
              <li key={c.id}>
                <button className={"archive-company-btn" + (c.id === selectedId ? " active" : "")}
                        onClick={() => loadForensics(c.id)} disabled={loading}>
                  <span className="ac-runs">{runs}</span>
                  <span className="ac-name">{c.name}</span>
                  <span className="ac-meta">{_tk(c.ticker)} · {c.sector?.split(" ")[0] || ""}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </aside>

      <div className="archive-main">
        <header className="archive-header">
          <div className="t-cap">{_tk(ticker)} · AUDIT</div>
          <h1 className="archive-title display">
            {loading ? <span style={{ color: "var(--ink-faint)", fontStyle: "italic" }}>Loading…</span> : entityName}
          </h1>
          <p className="archive-subtitle">
            Every bullet considered, kept or cut, with the pipeline's full reasoning.
          </p>
          <div className="hd-legend">
            <span className="hd-legend-item"><span className="hd-leg-dot hd-leg-pub"></span>Published</span>
            <span className="hd-legend-item"><span className="hd-leg-dot hd-leg-rew"></span>Rewritten</span>
            <span className="hd-legend-item"><span className="hd-leg-dot hd-leg-cut"></span>Rejected — relevance</span>
            <span className="hd-legend-item"><span className="hd-leg-dot hd-leg-dup"></span>Rejected — novelty</span>
            <span className="hd-legend-item"><span className="hd-leg-dot hd-leg-grd"></span>Rejected — grounding</span>
          </div>
        </header>

        <div className="hd-day-timeline">
          {days.map(d => {
            const dt = new Date(d.date + "T00:00:00Z");
            const dayNum = String(dt.getUTCDate()).padStart(2, "0");
            const month3 = dt.toLocaleDateString("en-US", { month: "short", timeZone: "UTC" });
            const wd = dt.toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" });
            const isMulti = d.runs.length > 1;

            if (!isMulti) {
              // Single run: original single-row layout
              const r = d.runs[0];
              const isOpen = openRunId === r.runId;
              return (
                <article key={d.date} id={`hd-day-${d.date}`} className={"hd-day" + (isOpen ? " hd-day-open" : "")}>
                  <button className="hd-day-head" onClick={() => setOpenRunId(isOpen ? null : r.runId)}>
                    <div className="archive-day-date">
                      <div className="archive-day-num">{dayNum}</div>
                      <div className="archive-day-month">{month3}</div>
                      <div className="archive-day-weekday">{wd}</div>
                    </div>
                    <div className="hd-day-info">
                      <div className="hd-day-counts">
                        <span className="hd-count-pub"><strong className="tnum">{r.published}</strong> published</span>
                        <span className="muted">·</span>
                        <span className="hd-count-rej"><strong className="tnum">{r.rejected}</strong> rejected</span>
                        <span className="muted">·</span>
                        <span className="t-mono">run-{r.runId}</span>
                        {r.windowStart && (
                          <span className="muted" style={{ fontSize: 11 }}>
                            {r.windowStart} → {r.windowEnd}
                          </span>
                        )}
                      </div>
                      <div className="hd-day-stagebar">
                        <PipelineStageBar published={r.published} rejected={r.rejected} groups={r.rejectionGroups} />
                      </div>
                    </div>
                    <div className="hd-day-arrow">{isOpen ? "▴" : "▾"}</div>
                  </button>
                  {isOpen && (
                    <div className="hd-day-body">
                      <RunBody r={r} expandedRejection={expandedRejection} setExpandedRejection={setExpandedRejection}
                               expandedPubCitation={expandedPubCitation} setExpandedPubCitation={setExpandedPubCitation} />
                    </div>
                  )}
                </article>
              );
            }

            // Multiple runs on the same day: static date header + individual run rows
            const totalPub = d.runs.reduce((s, r) => s + r.published, 0);
            const totalRej = d.runs.reduce((s, r) => s + r.rejected, 0);
            return (
              <article key={d.date} className="hd-day hd-day-multi">
                {/* Static date header — not a toggle */}
                <div className="hd-day-multi-header">
                  <div className="archive-day-date">
                    <div className="archive-day-num">{dayNum}</div>
                    <div className="archive-day-month">{month3}</div>
                    <div className="archive-day-weekday">{wd}</div>
                  </div>
                  <div className="hd-day-info">
                    <div className="hd-day-counts">
                      <span className="muted">{d.runs.length} runs</span>
                      <span className="muted">·</span>
                      <span className="hd-count-pub"><strong className="tnum">{totalPub}</strong> published</span>
                      <span className="muted">·</span>
                      <span className="hd-count-rej"><strong className="tnum">{totalRej}</strong> rejected</span>
                    </div>
                  </div>
                </div>
                {/* One expandable row per run */}
                {d.runs.map(r => {
                  const isOpen = openRunId === r.runId;
                  return (
                    <React.Fragment key={r.runId}>
                      <button className={"hd-run-head" + (isOpen ? " hd-run-head-open" : "")}
                              onClick={() => setOpenRunId(isOpen ? null : r.runId)}>
                        <div className="hd-day-info">
                          <div className="hd-day-counts">
                            <span className="hd-count-pub"><strong className="tnum">{r.published}</strong> published</span>
                            <span className="muted">·</span>
                            <span className="hd-count-rej"><strong className="tnum">{r.rejected}</strong> rejected</span>
                            <span className="muted">·</span>
                            <span className="t-mono">run-{r.runId}</span>
                            {r.windowStart && (
                              <span className="muted" style={{ fontSize: 11 }}>
                                {_fmtWindow(r.windowStart, r.windowEnd)}
                              </span>
                            )}
                          </div>
                          <div className="hd-day-stagebar">
                            <PipelineStageBar published={r.published} rejected={r.rejected} groups={r.rejectionGroups} />
                          </div>
                        </div>
                        <div className="hd-day-arrow">{isOpen ? "▴" : "▾"}</div>
                      </button>
                      {isOpen && (
                        <div className="hd-run-body">
                          <RunBody r={r} expandedRejection={expandedRejection} setExpandedRejection={setExpandedRejection}
                                   expandedPubCitation={expandedPubCitation} setExpandedPubCitation={setExpandedPubCitation} />
                        </div>
                      )}
                    </React.Fragment>
                  );
                })}
              </article>
            );
          })}
          {days.length === 0 && !loading && (
            <p style={{ color: "var(--ink-mute)", fontStyle: "italic", marginTop: 32 }}>No runs found for this entity.</p>
          )}
        </div>
      </div>
    </div>
  );
}

/** Shared body content (published bullets + rejection groups) for a single run. */
function RunBody({ r, expandedRejection, setExpandedRejection, expandedPubCitation, setExpandedPubCitation }) {
  return (
    <React.Fragment>
      {r.narrative && (
        <div style={{ padding: "12px 0 16px", marginBottom: 16 }}>
          <span className="t-cap" style={{ color: "var(--accent)", marginBottom: 6, display: "block" }}>Narrative</span>
          <p style={{ fontFamily: "var(--serif)", fontSize: 16, fontStyle: "italic", color: "var(--ink-soft)", margin: 0 }}>{r.narrative}</p>
        </div>
      )}

      {/* Published bullets */}
      <section className="hd-section">
        <header className="hd-section-head hd-section-head-pub">
          <span className="t-cap" style={{ color: "var(--novel)" }}>Published bullets</span>
          <span className="muted t-cap">{r.published} kept</span>
        </header>
        {r.bullets && r.bullets.length > 0 ? (
          <div className="hd-published-list">
            {r.bullets.map((b, i) => (
              <article key={b.id || i} className="hd-pub-bullet">
                <div className="hd-pub-side">
                  <span className="bullet-number tnum">{String(i + 1).padStart(2, "0")}</span>
                  <span className="stream-bullet-theme"><ThemeDot theme={b.theme} />{b.theme}</span>
                  {b.novelty === "rewritten" && <span className="bullet-novelty-tag">rewritten</span>}
                </div>
                <div className="hd-pub-body">
                  <p className="bullet-text t-body-large">{b.text}</p>
                  {b.novelty === "rewritten" && b.rewriteReason && (
                    <div className="rewrite-note">
                      <span className="t-cap" style={{ color: "var(--rewrite)" }}>Why rewritten</span>
                      <span className="rewrite-reason">{b.rewriteReason}</span>
                    </div>
                  )}
                  {b.citations?.length > 0 && (
                    <div className="hd-pub-evidence">
                      <span className="t-cap">Sources</span>
                      <ul>
                        {_groupCitations(b.citations).map((sg, si) => (
                          <li key={si} style={{ marginBottom: 10 }}>
                            <div className="hd-pub-cite-line">
                              <strong>{sg.source}</strong>
                              {sg.date && <span className="muted"> · {sg.date}</span>}
                            </div>
                            {sg.headlineGroups.map((hg, hi) => {
                              const citeKey = `${r.runId}-${i}-${si}-${hi}`;
                              const open = expandedPubCitation === citeKey;
                              return (
                                <div key={hi} style={{ marginTop: 4 }}>
                                  <span>{hg.headline}</span>
                                  {hg.excerpts.length > 0 && (
                                    <>
                                      <button type="button" className="hd-pub-cite-toggle"
                                              onClick={() => setExpandedPubCitation(open ? null : citeKey)}>
                                        {open ? "Hide source text" : "Show source text"}
                                      </button>
                                      {open && (
                                        <div className="hd-pub-cite-excerpt">
                                          {hg.excerpts.length === 1
                                            ? hg.excerpts[0]
                                            : hg.excerpts.map((ex, xi) => (
                                                <div key={xi} style={{ marginBottom: 8 }}>
                                                  <div style={{ fontFamily: "var(--sans)", fontSize: 11, color: "var(--ink-mute)", marginBottom: 2 }}>Text {xi + 1}:</div>
                                                  {ex}
                                                </div>
                                              ))
                                          }
                                        </div>
                                      )}
                                    </>
                                  )}
                                </div>
                              );
                            })}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="muted" style={{ fontFamily: "var(--sans)", fontSize: 12, padding: "12px 0" }}>No active bullets for this day.</p>
        )}
      </section>

      {/* Rejected, grouped by stage */}
      {r.rejectionGroups?.length > 0 && (
        <section className="hd-section">
          <header className="hd-section-head hd-section-head-rej">
            <span className="t-cap" style={{ color: "var(--discard)" }}>Rejected candidates</span>
            <span className="muted t-cap">{r.rejected} discarded across {r.rejectionGroups.length} pipeline stages</span>
          </header>
          {r.rejectionGroups.map(g => (
            <div key={g.stage} className={"hd-rej-group hd-rej-group-" + g.stage}>
              <div className="hd-rej-group-head">
                <div>
                  <div className="hd-rej-stage-num">{stageNumber(g.stage)}</div>
                  <div className="hd-rej-stage-label">{g.stageLabel}</div>
                </div>
                <div className="hd-rej-count tnum">{g.count} cut</div>
              </div>
              <ul className="hd-rej-list">
                {g.items.map(item => {
                  const isExp = expandedRejection === item.id;
                  return (
                    <li key={item.id} className={"hd-rej-item" + (isExp ? " hd-rej-item-open" : "")}>
                      <button className="hd-rej-item-head"
                              onClick={() => setExpandedRejection(isExp ? null : item.id)}>
                        <span className="hd-rej-text">{item.text}</span>
                        <span className="hd-rej-tag">
                          {item.score != null && <span className="hd-rej-score">score <strong className="tnum">{item.score}</strong></span>}
                        </span>
                      </button>
                      {isExp && (
                        <div className="hd-rej-detail">
                          <ForensicsDiscardDetail item={item} />
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </section>
      )}
    </React.Fragment>
  );
}

/** Same order as ``_DISCARD_STAGE_ORDER`` in ``ui.py`` (HTML discarded section). */
const _DISCARD_STAGE_ORDER = [
  "relevance_score", "grounding", "novelty_embedding", "novelty_embedding_relevance",
  "novelty_search", "novelty_search_relevance", "unknown",
];

function stageNumber(stage) {
  const i = _DISCARD_STAGE_ORDER.indexOf(stage);
  return i >= 0 ? String(i + 1).padStart(2, "0") : "··";
}

/** Expanded body — fields mirror ``_load_bullets_for_run`` / ``_render_discarded_detail_body`` (HTML). */
function ForensicsDiscardDetail({ item }) {
  const d = item.discarded;
  if (!d || typeof d !== "object") {
    return (
      <React.Fragment>
        {item.scoreReason && (
          <div className="hd-rej-row"><span className="t-cap">Why filtered</span><p>{item.scoreReason}</p></div>
        )}
        {item.evidence && (
          <div className="hd-rej-row"><span className="t-cap">Pipeline detail</span><p>{item.evidence}</p></div>
        )}
      </React.Fragment>
    );
  }
  const reason = (d.reason || "").trim();
  const claims = Array.isArray(d.claim_verdicts) ? d.claim_verdicts : [];
  const evals = Array.isArray(d.evaluator_details) ? d.evaluator_details : [];
  return (
    <React.Fragment>
      {reason !== "" && (
        <div className="hd-rej-row">
          <span className="t-cap">Why filtered</span>
          <p>{d.reason}</p>
        </div>
      )}
      {d.score != null && d.score !== undefined && (
        <div className="hd-rej-row">
          <span className="t-cap">Relevance score</span>
          <p><strong className="tnum">{d.score}</strong>/5</p>
        </div>
      )}
      {(d.overall_verdict || "").trim() !== "" && (
        <div className="hd-rej-row">
          <span className="t-cap">Novelty verdict</span>
          <p>{d.overall_verdict}</p>
        </div>
      )}
      {claims.length > 0 && (
        <div className="hd-rej-row">
          <span className="t-cap">Claim review</span>
          <ul className="hd-rej-claims" style={{ margin: "8px 0 0", paddingLeft: 18 }}>
            {claims.map((cv, i) => (
              <li key={i} style={{ marginBottom: 10 }}>
                {cv.claim_index != null && <span className="muted tnum" style={{ marginRight: 6 }}>#{cv.claim_index}</span>}
                {cv.novelty && <span className="hd-rej-flag" style={{ marginRight: 6 }}>{cv.novelty}</span>}
                {cv.claim_text && <p style={{ margin: "4px 0" }}>{cv.claim_text}</p>}
                {cv.reasoning && <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>{cv.reasoning}</p>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {evals.length > 0 && (
        <div className="hd-rej-row">
          <span className="t-cap">Embedding check</span>
          <ul className="hd-rej-claims" style={{ margin: "8px 0 0", paddingLeft: 18 }}>
            {evals.map((ev, i) => (
              <li key={i} style={{ marginBottom: 8 }}>
                {ev.evaluator_name && <span className="muted" style={{ fontSize: 12 }}>{ev.evaluator_name}</span>}
                {ev.reason && <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>{ev.reason}</p>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </React.Fragment>
  );
}

function PipelineStageBar({ published, rejected, groups }) {
  const segments = [
    { label: "kept", count: published, cls: "stage-kept" },
    ...(groups || []).map(g => ({ label: g.stage, count: g.count, cls: "stage-cut-" + g.stage })),
  ];
  return (
    <div className="hd-funnel">
      {segments.map((s, i) => (
        <div key={i} className={"hd-funnel-seg " + s.cls} style={{ flex: s.count }} title={`${s.label}: ${s.count}`}>
          {s.count >= 3 && <span className="hd-funnel-num tnum">{s.count}</span>}
        </div>
      ))}
    </div>
  );
}

window.HistoryDetailsView = HistoryDetailsView;
