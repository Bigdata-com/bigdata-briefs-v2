// Update view — catch-up scan that resumes every entity from its last run to now.

const {
  useState: useStateU,
  useEffect: useEffectU,
  useRef: useRefU,
  useMemo: useMemoU,
} = React;

function UpdateView({ tweaks }) {
  const COMPANIES = window.DATA?.companies || [];
  const UNIVERSES = window.EXTRAS.universes || [];

  const [scope, setScope] = useStateU("entity");
  const [entity, setEntity] = useStateU(COMPANIES[0]);
  const [universe, setUniverse] = useStateU(UNIVERSES[0] || null);
  const [sources, setSources] = useStateU(["news"]);

  // Preview state
  const [preview, setPreview] = useStateU(null);   // null | {entities, runnable_count, total_est_windows}
  const [previewLoading, setPreviewLoading] = useStateU(false);
  const [previewError, setPreviewError] = useStateU(null);

  // Run state: mirrors ScanView
  const [mode, setMode] = useStateU("configure"); // configure | preview | running | done
  const [scanParams, setScanParams] = useStateU(null);
  const [scanResults, setScanResults] = useStateU(null);
  const [runError, setRunError] = useStateU(null);
  const pollRef = useRefU(null);

  // ── Poll scan status (reuses /api/frontend/scan/status) ──────────
  useEffectU(() => {
    if (mode !== "running" || !scanParams) return;
    const poll = () => {
      const ids = scanParams.entity_ids.join(",");
      fetch(`/api/frontend/scan/status?entity_ids=${ids}&start_date=${scanParams.start_date}&end_date=${scanParams.end_date}`)
        .then(r => r.json())
        .then(data => {
          setScanResults(data);
          if (data.completed >= data.total) {
            clearInterval(pollRef.current);
            setMode("done");
          }
        })
        .catch(console.error);
    };
    poll();
    pollRef.current = setInterval(poll, 3000);
    return () => clearInterval(pollRef.current);
  }, [mode, scanParams]);

  // ── Load preview ──────────────────────────────────────────────────
  function loadPreview() {
    setPreviewError(null);
    setPreviewLoading(true);
    let url = `/api/frontend/update/preview?scope=${scope}`;
    if (scope === "entity" && entity) url += `&entity_id=${entity.id}`;
    if (scope === "universe" && universe) url += `&universe=${universe.id}`;
    fetch(url)
      .then(r => r.json())
      .then(data => {
        if (data.error) { setPreviewError(data.error); return; }
        setPreview(data);
        setMode("preview");
      })
      .catch(err => setPreviewError(String(err)))
      .finally(() => setPreviewLoading(false));
  }

  // ── Start update run ──────────────────────────────────────────────
  function startUpdate() {
    setRunError(null);
    const body = { scope, source_categories: sources };
    if (scope === "entity") body.entity_id = entity?.id;
    if (scope === "universe") body.universe = universe?.id;

    fetch("/api/frontend/update/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(r => r.json())
      .then(data => {
        if (data.error) { setRunError(data.error); return; }
        setScanParams({
          entity_ids: data.entity_ids,
          start_date: data.start_date,
          end_date: data.end_date,
          total_windows: data.total_windows,
        });
        setScanResults(null);
        setMode("running");
      })
      .catch(err => setRunError(String(err)));
  }

  // ── Aggregated progress stats ────────────────────────────────────
  const agg = useMemoU(() => {
    const out = { succeeded: 0, failed: 0, skipped: 0, pending: 0, running: 0, saved: 0, discarded: 0 };
    for (const e of scanResults?.entities || []) {
      for (const d of e.days || []) {
        if (d.status === "succeeded") { out.succeeded++; out.saved += d.saved || 0; out.discarded += d.discarded || 0; }
        else if (d.status === "failed") out.failed++;
        else if (d.status === "skipped") out.skipped++;
        else if (d.status === "running") out.running++;
        else out.pending++;
      }
    }
    return out;
  }, [scanResults]);

  const totalDays = scanParams?.total_windows || 0;
  const completed = scanResults?.completed || 0;
  const pct = totalDays > 0 ? Math.min(100, (completed / totalDays) * 100) : 0;
  const entities = scanResults?.entities || [];

  const entityRowsDone = useMemoU(() => {
    const ents = scanResults?.entities || [];
    const terminal = d => d.status === "succeeded" || d.status === "failed" || d.status === "skipped";
    return ents.filter(ent => {
      const days = ent.days || [];
      return days.length > 0 && days.every(terminal);
    }).length;
  }, [scanResults]);

  // ── Display helpers ──────────────────────────────────────────────
  const scopeLabel = scope === "entity"
    ? (entity?.name || "—")
    : scope === "universe"
      ? (universe?.label || "—")
      : "All entities";

  const estimates = (window.RUN_DATA && typeof window.RUN_DATA.composeEstimates === "object")
    ? window.RUN_DATA.composeEstimates : {};

  function parseCost(eid) {
    const est = estimates[eid];
    if (!est || !est.costDisplay) return null;
    const n = parseFloat(String(est.costDisplay).replace(/[^0-9.]/g, ""));
    return isNaN(n) ? null : n;
  }

  const previewCost = useMemoU(() => {
    if (!preview) return null;
    let total = 0;
    let covered = 0;
    for (const row of preview.entities) {
      if (!row.has_history || row.est_windows === 0) continue;
      const c = parseCost(row.entity_id);
      if (c !== null) { total += c * row.est_windows; covered++; }
    }
    return covered > 0 ? total : null;
  }, [preview]);

  return (
    <div className="scan-layout">
      {/* ── Left: configure ── */}
      <aside className="scan-config">
        <header className="scan-config-head">
          <div className="dateline">Update</div>
          <h1 className="display scan-config-title">Catch up to <em>now</em>.</h1>
          <p className="scan-config-lede">
            Resumes each entity from its last completed run and covers every remaining day
            up to the current moment. No date selection needed.
          </p>
        </header>

        {/* Step 01 — Scope */}
        <section className="scan-section">
          <div className="scan-step-num">01</div>
          <h2 className="scan-section-title">Scope</h2>
          <div className="seg seg-mini">
            <button className={"seg-btn" + (scope === "entity" ? " active" : "")}
                    onClick={() => { setScope("entity"); setPreview(null); setMode("configure"); }}>
              <span className="seg-label">Single entity</span>
              <span className="seg-sub">One company</span>
            </button>
            <button className={"seg-btn" + (scope === "universe" ? " active" : "")}
                    onClick={() => { setScope("universe"); setPreview(null); setMode("configure"); }}>
              <span className="seg-label">Universe</span>
              <span className="seg-sub">All in basket</span>
            </button>
            <button className={"seg-btn" + (scope === "all" ? " active" : "")}
                    onClick={() => { setScope("all"); setPreview(null); setMode("configure"); }}>
              <span className="seg-label">All entities</span>
              <span className="seg-sub">Every entity in DB</span>
            </button>
          </div>
        </section>

        {/* Step 02 — Entity / Universe selector */}
        {scope === "entity" && (
          <section className="scan-section">
            <div className="scan-step-num">02</div>
            <h2 className="scan-section-title">Entity</h2>
            <select className="scan-select" value={entity?.id}
                    onChange={e => { setEntity(COMPANIES.find(c => c.id === e.target.value)); setPreview(null); setMode("configure"); }}>
              {COMPANIES.map(c => <option key={c.id} value={c.id}>{c.name} · {c.ticker}</option>)}
            </select>
          </section>
        )}

        {scope === "universe" && (
          <section className="scan-section">
            <div className="scan-step-num">02</div>
            <h2 className="scan-section-title">Universe</h2>
            <div className="universe-list">
              {UNIVERSES.map(u => (
                <button key={u.id}
                        className={"universe-pick" + (universe?.id === u.id ? " active" : "")}
                        onClick={() => { setUniverse(u); setPreview(null); setMode("configure"); }}>
                  <span className="universe-pick-label">{u.label}</span>
                  <span className="universe-pick-count tnum">{u.count}</span>
                  <span className="universe-pick-desc">{u.description}</span>
                </button>
              ))}
            </div>
          </section>
        )}

        {/* Step 03 — Sources */}
        <section className="scan-section">
          <div className="scan-step-num">{scope === "all" ? "02" : "03"}</div>
          <h2 className="scan-section-title">Sources</h2>
          <div className="scan-source-grid">
            {[
              { id: "news",         label: "Web News",   sub: "Default · always recommended" },
              { id: "news_premium", label: "Premium News",   sub: "Reuters, Bloomberg, FT, WSJ" },
              { id: "filings",      label: "SEC filings",    sub: "10-K, 10-Q, 8-K, proxy" },
              { id: "transcripts",  label: "Earnings calls", sub: "Quarterly transcripts" },
            ].map(s => (
              <label key={s.id} className={"scan-source" + (sources.includes(s.id) ? " active" : "")}>
                <input type="checkbox" checked={sources.includes(s.id)}
                       onChange={() => setSources(c =>
                         c.includes(s.id) ? c.filter(x => x !== s.id) : [...c, s.id]
                       )} />
                <span className="scan-source-box"></span>
                <span className="scan-source-body">
                  <span className="scan-source-label">{s.label}</span>
                  <span className="scan-source-sub">{s.sub}</span>
                </span>
              </label>
            ))}
          </div>
        </section>

        {previewError && (
          <p style={{ color: "var(--discard)", fontFamily: "var(--sans)", fontSize: 12, marginBottom: 8 }}>
            {previewError}
          </p>
        )}
        {runError && (
          <p style={{ color: "var(--discard)", fontFamily: "var(--sans)", fontSize: 12, marginBottom: 8 }}>
            {runError}
          </p>
        )}

        {mode === "configure" && (
          <button className="launch-btn launch-btn-scan" onClick={loadPreview} disabled={previewLoading}>
            {previewLoading ? "Loading preview…" : "▶\u00a0 Preview"}
          </button>
        )}

        {mode === "preview" && (
          <>
            <button className="launch-btn launch-btn-scan" onClick={startUpdate}
                    disabled={!preview || preview.runnable_count === 0}>
              ▶&nbsp; Confirm &amp; start update
            </button>
            <button className="btn" style={{ marginTop: 8, width: "100%" }}
                    onClick={() => { setMode("configure"); setPreview(null); }}>
              ← Change scope
            </button>
          </>
        )}

        {(mode === "running" || mode === "done") && (
          <button className="btn" style={{ marginTop: 8, width: "100%" }}
                  onClick={() => { setMode("configure"); setScanParams(null); setScanResults(null); setPreview(null); }}>
            New update
          </button>
        )}
      </aside>

      {/* ── Right: preview / progress ── */}
      <main className="scan-main">

        {/* Configure placeholder */}
        {mode === "configure" && (
          <div className="update-empty-state">
            <p>Select a scope on the left and click <strong>Preview</strong> to see what will be updated.</p>
          </div>
        )}

        {/* Preview table */}
        {mode === "preview" && preview && (
          <UpdatePreviewPanel preview={preview} previewCost={previewCost} />
        )}

        {/* Running / done */}
        {(mode === "running" || mode === "done") && (
          <>
            <header className="scan-results-head">
              <div className="scan-results-head-row">
                <div>
                  <div className="dateline">{mode === "running" ? "Update in progress" : "Update complete"}</div>
                  <h2 className="display scan-results-title">{scopeLabel}</h2>
                  <div className="scan-results-sub">
                    {mode === "running" && <span className="live-dot"></span>}
                    <span>
                      <strong className="tnum">{completed}</strong>
                      <span className="muted"> / </span>
                      <strong className="tnum">{totalDays}</strong>
                      <span className="muted"> entity×day cells resolved</span>
                    </span>
                  </div>
                  {entities.length > 1 && (
                    <div className="scan-results-sub" style={{ marginTop: 6 }}>
                      <span className="muted">Entities fully done · </span>
                      <strong className="tnum">{entityRowsDone}</strong>
                      <span> / </span>
                      <strong className="tnum">{entities.length}</strong>
                    </div>
                  )}
                </div>
              </div>

              <div className="scan-progress">
                <div className="scan-progress-track">
                  <div className="scan-progress-fill" style={{ width: pct + "%" }}></div>
                  <div className="scan-progress-marker" style={{ left: pct + "%" }}></div>
                </div>
                <div className="scan-progress-axis">
                  <span>{scanParams?.start_date}</span>
                  <span className="tnum">{pct.toFixed(0)}%</span>
                  <span>{scanParams?.end_date}</span>
                </div>
              </div>

              <dl className="scan-summary">
                <div className="scan-summary-cell">
                  <dt className="t-cap">Day-cells</dt>
                  <dd className="cost-num tnum">{completed}<span className="compose-estimate-sep">/</span>{totalDays}</dd>
                  <span className="compose-estimate-foot">
                    {agg.succeeded} succeeded · {agg.failed} failed · {agg.skipped} skipped
                    {(agg.pending + agg.running) > 0 && ` · ${agg.pending + agg.running} pending/running`}
                  </span>
                </div>
                <div className="scan-summary-cell">
                  <dt className="t-cap">Bullets saved</dt>
                  <dd className="cost-num tnum">{agg.saved}</dd>
                  <span className="compose-estimate-foot">across succeeded entity-days</span>
                </div>
                <div className="scan-summary-cell">
                  <dt className="t-cap">Discarded</dt>
                  <dd className="cost-num tnum">{agg.discarded}</dd>
                  <span className="compose-estimate-foot">funnel rejects on those runs</span>
                </div>
              </dl>
            </header>

            {/* Per-entity live rows */}
            {entities.length > 0 && (
              <section className="scan-list-section">
                <div className="ops-section-head">
                  <h2>Entities</h2>
                  <span className="ops-section-meta">{entities.length} in scope</span>
                </div>
                <ul className="update-entity-list">
                  {entities.map(e => <UpdateEntityRow key={e.entityId} ent={e} />)}
                </ul>
              </section>
            )}
          </>
        )}
      </main>
    </div>
  );
}


// ── Preview panel ──────────────────────────────────────────────────
function UpdatePreviewPanel({ preview, previewCost }) {
  const runnable = preview.entities.filter(r => r.has_history && r.est_windows > 0);
  const skipped  = preview.entities.filter(r => !r.has_history);
  const upToDate = preview.entities.filter(r => r.has_history && r.est_windows === 0);

  function fmtDt(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    const date = d.toLocaleDateString("en-US", { month: "short", day: "2-digit", year: "numeric" });
    const time = d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: "UTC", hour12: false });
    return `${date} ${time} UTC`;
  }

  const fmtCost = v => v == null ? null : (v < 0.01 ? "< $0.01" : `$${v.toFixed(2)}`);

  return (
    <div className="update-preview">
      <div className="update-preview-summary">
        <div className="update-preview-stat">
          <span className="t-cap">Entities to update</span>
          <strong className="tnum update-preview-big">{preview.runnable_count}</strong>
        </div>
        <div className="update-preview-stat">
          <span className="t-cap">Est. day-windows</span>
          <strong className="tnum update-preview-big">{preview.total_est_windows}</strong>
        </div>
        {previewCost != null && (
          <div className="update-preview-stat">
            <span className="t-cap">Est. cost</span>
            <strong className="tnum update-preview-big">{fmtCost(previewCost)}</strong>
          </div>
        )}
        {skipped.length > 0 && (
          <div className="update-preview-stat">
            <span className="t-cap">No history (skipped)</span>
            <strong className="tnum update-preview-big update-preview-warn">{skipped.length}</strong>
          </div>
        )}
        {upToDate.length > 0 && (
          <div className="update-preview-stat">
            <span className="t-cap">Already up to date</span>
            <strong className="tnum update-preview-big update-preview-ok">{upToDate.length}</strong>
          </div>
        )}
      </div>

      {runnable.length === 0 && (
        <p className="update-preview-empty">All entities are already up to date — nothing to run.</p>
      )}

      {runnable.length > 0 && (
        <div className="update-preview-table-wrap">
          <table className="update-preview-table">
            <thead>
              <tr>
                <th>Entity</th>
                <th>Last run</th>
                <th>Resume from</th>
                <th className="tnum" style={{ textAlign: "right" }}>Days</th>
              </tr>
            </thead>
            <tbody>
              {runnable.map(r => (
                <tr key={r.entity_id}>
                  <td>
                    <span className="update-ent-name">{r.name}</span>
                    {r.ticker && <span className="update-ent-ticker"> · {r.ticker}</span>}
                  </td>
                  <td className="update-td-mono">{fmtDt(r.last_run_at)}</td>
                  <td className="update-td-mono">{fmtDt(r.resume_from)}</td>
                  <td className="tnum" style={{ textAlign: "right" }}>{r.est_windows}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {skipped.length > 0 && (
        <details className="update-preview-skipped">
          <summary>{skipped.length} entit{skipped.length === 1 ? "y" : "ies"} with no history — use Scan to create initial coverage</summary>
          <ul>
            {skipped.map(r => <li key={r.entity_id}>{r.name}{r.ticker ? ` · ${r.ticker}` : ""}</li>)}
          </ul>
        </details>
      )}
    </div>
  );
}


// ── Per-entity live row during run ────────────────────────────────
function UpdateEntityRow({ ent }) {
  const days = ent.days || [];
  const succeeded = days.filter(d => d.status === "succeeded").length;
  const failed    = days.filter(d => d.status === "failed").length;
  const running   = days.filter(d => d.status === "running").length;
  const pending   = days.filter(d => d.status === "pending" || d.status === "skipped").length;

  let pill = "pending";
  if (running > 0) pill = "running";
  else if (days.length > 0 && days.every(d => ["succeeded","failed","skipped"].includes(d.status))) {
    pill = failed > 0 ? "failed" : "done";
  }

  return (
    <li className={`update-ent-row update-ent-row-${pill}`}>
      <div className="update-ent-row-name">
        <span>{ent.entityName}</span>
        {ent.entityTicker && <span className="update-ent-ticker"> · {ent.entityTicker}</span>}
      </div>
      <div className="update-ent-row-pills">
        {succeeded > 0 && <span className="scan-pill scan-pill-ok">✓ {succeeded}</span>}
        {failed > 0    && <span className="scan-pill scan-pill-fail">✕ {failed}</span>}
        {running > 0   && <span className="scan-pill scan-pill-run">● {running}</span>}
        {pending > 0   && <span className="scan-pill" style={{ opacity: 0.45 }}>{pending} pending</span>}
      </div>
    </li>
  );
}

window.UpdateView = UpdateView;
