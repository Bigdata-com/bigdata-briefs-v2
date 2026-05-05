// Portfolio Scan — merged Scan + Update view.
// dateMode "resume": resumes each entity from its last run (yesterday→today if none).
// dateMode "custom": user-specified date range.

const { useState: useStateS, useEffect: useEffectS, useRef: useRefS, useMemo: useMemoS } = React;

function ScanView({ tweaks }) {
  const COMPANIES = (window.DATA?.allScanEntities || window.DATA?.companies || [])
    .slice().sort((a, b) => a.name.localeCompare(b.name));
  const UNIVERSES = window.EXTRAS.universes || [];
  const today   = new Date().toISOString().slice(0, 10);
  const weekAgo = new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10);

  const [dateMode, setDateMode] = useStateS("resume"); // resume | custom
  const [scope,    setScope]    = useStateS("entity");  // entity | universe | all
  const [entity,   setEntity]   = useStateS(COMPANIES[0]);
  const [universe, setUniverse] = useStateS(UNIVERSES[0] || null);
  const [startDate, setStartDate] = useStateS(weekAgo);
  const [endDate,   setEndDate]   = useStateS(today);
  const [sources, setSources] = useStateS(["news_premium"]);

  // Preview (resume mode only)
  const [preview,        setPreview]        = useStateS(null);
  const [previewLoading, setPreviewLoading] = useStateS(false);
  const [previewError,   setPreviewError]   = useStateS(null);

  // Run state
  const [mode,        setMode]        = useStateS("configure"); // configure | preview | running | done
  const [scanParams,  setScanParams]  = useStateS(null);
  const [scanResults, setScanResults] = useStateS(null);
  const [runError,    setRunError]    = useStateS(null);
  const pollRef = useRefS(null);

  function resetToConfig() {
    setMode("configure");
    setPreview(null);
    setScanParams(null);
    setScanResults(null);
    setRunError(null);
    setPreviewError(null);
  }

  // ── Poll ────────────────────────────────────────────────────────
  useEffectS(() => {
    if (mode !== "running" || !scanParams) return;
    const poll = () => {
      const ids = scanParams.entity_ids.join(",");
      fetch(`/api/v1/scan/status?entity_ids=${ids}&start_date=${scanParams.start_date}&end_date=${scanParams.end_date}`)
        .then(r => r.json())
        .then(data => {
          setScanResults(data);
          if (data.completed >= data.total) { clearInterval(pollRef.current); setMode("done"); }
        })
        .catch(console.error);
    };
    poll();
    pollRef.current = setInterval(poll, 3000);
    return () => clearInterval(pollRef.current);
  }, [mode, scanParams]);

  // ── Resume: load preview ────────────────────────────────────────
  function loadPreview() {
    setPreviewError(null);
    setPreviewLoading(true);
    let url = `/api/v1/scan/preview?scope=${scope}`;
    if (scope === "entity"   && entity)   url += `&entity_id=${entity.id}`;
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

  // ── Resume: start run ───────────────────────────────────────────
  function startResume() {
    setRunError(null);
    // Derive entity_ids from preview data (entities that have windows to run)
    const runnableIds = (preview?.entities || [])
      .filter(r => r.est_windows > 0)
      .map(r => r.entity_id);
    if (!runnableIds.length) { setRunError("No entities to run."); return; }

    // Derive date range from preview (earliest resume_date → today)
    const resumeDates = (preview?.entities || [])
      .filter(r => r.est_windows > 0 && r.resume_date)
      .map(r => r.resume_date);
    const startStr = resumeDates.length ? resumeDates.sort()[0] : today;
    const endStr   = today;

    const body = { window_mode: "continuous", categories: sources };
    if (scope === "entity")   body.entity_ids = [entity?.id].filter(Boolean);
    else if (scope === "universe") body.universe = universe?.id;
    else body.entity_ids = runnableIds;

    fetch("/api/v1/batch/run-parallel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(r => r.json())
      .then(data => {
        if (data.detail || data.error) { setRunError(data.detail || data.error); return; }
        setScanParams({ entity_ids: runnableIds, start_date: startStr, end_date: endStr, total_windows: data.total });
        setScanResults(null);
        setMode("running");
      })
      .catch(err => setRunError(String(err)));
  }

  // ── Custom: start scan ──────────────────────────────────────────
  function startCustomScan() {
    setRunError(null);
    if (scope === "entity" && !entity)   { setRunError("Select an entity first."); return; }
    if (scope === "universe" && !universe) { setRunError("Select a universe first."); return; }
    const body = { start_date: startDate, end_date: endDate, source_categories: sources };
    if (scope === "entity")   body.entity_id = entity.id;
    if (scope === "universe") body.universe  = universe.id;
    fetch("/api/v1/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(r => r.json())
      .then(data => {
        if (data.detail || data.error) { setRunError(data.detail || data.error); return; }
        // Core endpoint returns ScanResponse (single) or UniverseScanResponse (universe)
        let entityIds, totalWindows;
        if (data.scans) {
          // UniverseScanResponse: { scans: [{scan_id, entity_id, windows_total, start, end}], total_entities, universe }
          entityIds    = data.scans.map(s => s.entity_id);
          totalWindows = data.scans.reduce((sum, s) => sum + s.windows_total, 0);
        } else {
          // ScanResponse: { scan_id, entity_id, windows_total, start, end }
          entityIds    = [data.entity_id];
          totalWindows = data.windows_total;
        }
        setScanParams({ entity_ids: entityIds, start_date: startDate, end_date: endDate, total_windows: totalWindows });
        setScanResults(null);
        setMode("running");
      })
      .catch(err => setRunError(String(err)));
  }

  // ── Aggregated stats ────────────────────────────────────────────
  const agg = useMemoS(() => {
    const out = { succeeded: 0, failed: 0, skipped: 0, pending: 0, running: 0, saved: 0, discarded: 0 };
    for (const e of scanResults?.entities || []) {
      for (const d of e.days || []) {
        if (d.status === "succeeded") { out.succeeded++; out.saved += d.saved||0; out.discarded += d.discarded||0; }
        else if (d.status === "failed")  out.failed++;
        else if (d.status === "skipped") out.skipped++;
        else if (d.status === "running") out.running++;
        else out.pending++;
      }
    }
    return out;
  }, [scanResults]);

  // ── Day calendar data (custom mode) ────────────────────────────
  const allDays = useMemoS(() => {
    if (!scanResults?.entities?.length) return [];
    const byDate = {};
    for (const e of scanResults.entities) {
      for (const d of e.days || []) {
        if (!byDate[d.date]) byDate[d.date] = [];
        byDate[d.date].push({ ...d, entityName: e.entityName });
      }
    }
    return Object.keys(byDate).sort().map(date => {
      const cells = byDate[date];
      const st = cells.map(c => c.status);
      const any = x => st.some(s => s === x);
      let status = "pending";
      if (any("running")) status = "running";
      else if (any("pending")) status = "pending";
      else if (any("failed")) status = "failed";
      else if (st.every(s => s === "skipped")) status = "skipped";
      else if (st.every(s => s === "succeeded" || s === "skipped")) status = "succeeded";
      const saved     = cells.reduce((s, c) => s + (c.status === "succeeded" ? (c.saved||0) : 0), 0);
      const discarded = cells.reduce((s, c) => s + (c.status === "succeeded" ? (c.discarded||0) : 0), 0);
      const errCell   = cells.find(c => c.status === "failed");
      return { date, status, saved, discarded, empty: status === "succeeded" && saved === 0, error: errCell?.error };
    });
  }, [scanResults]);

  const entities      = scanResults?.entities || [];
  const totalDays     = scanParams?.total_windows || 0;
  const completed     = scanResults?.completed || 0;
  const pct           = totalDays > 0 ? Math.min(100, (completed / totalDays) * 100) : 0;
  const entityRowsDone = useMemoS(() => {
    const terminal = d => ["succeeded","failed","skipped"].includes(d.status);
    return entities.filter(e => e.days?.length > 0 && e.days.every(terminal)).length;
  }, [scanResults]);

  const scopeLabel = scope === "entity" ? (entity?.name || "—") : scope === "universe" ? (universe?.label || "—") : "All entities";
  const isRunning  = mode === "running" || mode === "done";

  // Preview cost estimate (resume mode)
  const estimates = (window.RUN_DATA && typeof window.RUN_DATA.composeEstimates === "object") ? window.RUN_DATA.composeEstimates : {};
  function parseCost(eid) {
    const est = estimates[eid];
    if (!est?.costDisplay) return null;
    const n = parseFloat(String(est.costDisplay).replace(/[^0-9.]/g, ""));
    return isNaN(n) ? null : n;
  }
  const previewCost = useMemoS(() => {
    if (!preview) return null;
    let total = 0, covered = 0;
    for (const row of preview.entities) {
      if (row.est_windows === 0) continue;
      const c = parseCost(row.entity_id);
      if (c !== null) { total += c * row.est_windows; covered++; }
    }
    return covered > 0 ? total : null;
  }, [preview]);

  // ── Step numbering ──────────────────────────────────────────────
  // Sections: 01 Date mode, 02 Scope, 03 Entity/Universe (if applicable),
  //           04 Date range (custom only), last Sources
  const entityStepNum = "03";
  const dateStepNum   = scope === "all" ? "03" : "04";
  const srcStepNum    = dateMode === "resume"
    ? (scope === "all" ? "03" : "04")
    : (scope === "all" ? "04" : "05");

  return (
    <div className="scan-layout">

      {/* ── Left: configure ── */}
      <aside className="scan-config">
        <header className="scan-config-head">
          <div className="dateline">Portfolio Scan</div>
          <h1 className="display scan-config-title">Build and maintain <em>coverage</em>.</h1>
          <p className="scan-config-lede">
            Resume each company from its last run, or specify a custom date range.
          </p>
        </header>

        {/* 01 — Date mode */}
        <section className="scan-section">
          <div className="scan-step-num">01</div>
          <h2 className="scan-section-title">Date range</h2>
          <div className="seg seg-mini">
            <button className={"seg-btn" + (dateMode === "resume" ? " active" : "")}
                    onClick={() => { setDateMode("resume"); if (scope === "all" || scope === "universe" || scope === "entity") {}; resetToConfig(); }}>
              <span className="seg-label">Resume</span>
              <span className="seg-sub">From last run to today</span>
            </button>
            <button className={"seg-btn" + (dateMode === "custom" ? " active" : "")}
                    onClick={() => { setDateMode("custom"); if (scope === "all") setScope("entity"); resetToConfig(); }}>
              <span className="seg-label">Custom range</span>
              <span className="seg-sub">Pick start and end dates</span>
            </button>
          </div>
        </section>

        {/* 02 — Scope */}
        <section className="scan-section">
          <div className="scan-step-num">02</div>
          <h2 className="scan-section-title">Scope</h2>
          <div className="seg seg-mini">
            <button className={"seg-btn" + (scope === "entity" ? " active" : "")}
                    onClick={() => { setScope("entity"); resetToConfig(); }}>
              <span className="seg-label">Single entity</span>
              <span className="seg-sub">One company</span>
            </button>
            <button className={"seg-btn" + (scope === "universe" ? " active" : "")}
                    onClick={() => { setScope("universe"); resetToConfig(); }}>
              <span className="seg-label">Universe</span>
              <span className="seg-sub">All in basket</span>
            </button>
            {dateMode === "resume" && (
              <button className={"seg-btn" + (scope === "all" ? " active" : "")}
                      onClick={() => { setScope("all"); resetToConfig(); }}>
                <span className="seg-label">All entities</span>
                <span className="seg-sub">All companies in universe CSV</span>
              </button>
            )}
          </div>
        </section>

        {/* 03 — Entity picker */}
        {scope === "entity" && (
          <section className="scan-section">
            <div className="scan-step-num">{entityStepNum}</div>
            <h2 className="scan-section-title">Entity</h2>
            <select className="scan-select" value={entity?.id}
                    onChange={e => { setEntity(COMPANIES.find(c => c.id === e.target.value)); resetToConfig(); }}>
              {COMPANIES.map(c => <option key={c.id} value={c.id}>{c.name} · {c.id}</option>)}
            </select>
          </section>
        )}

        {/* 03 — Universe picker */}
        {scope === "universe" && (
          <section className="scan-section">
            <div className="scan-step-num">{entityStepNum}</div>
            <h2 className="scan-section-title">Universe</h2>
            <div className="universe-list">
              {UNIVERSES.map(u => (
                <button key={u.id} className={"universe-pick" + (universe?.id === u.id ? " active" : "")}
                        onClick={() => { setUniverse(u); resetToConfig(); }}>
                  <span className="universe-pick-label">{u.label}</span>
                  <span className="universe-pick-count tnum">{u.count}</span>
                  <span className="universe-pick-desc">{u.description}</span>
                </button>
              ))}
            </div>
          </section>
        )}

        {/* Date picker — custom mode only */}
        {dateMode === "custom" && (
          <section className="scan-section">
            <div className="scan-step-num">{dateStepNum}</div>
            <h2 className="scan-section-title">Date range</h2>
            <div className="scan-date-row">
              <label className="scan-date-field">
                <span className="t-cap">From</span>
                <input type="date" value={startDate} max={today}
                       onChange={e => setStartDate(e.target.value)} />
              </label>
              <span className="scan-date-arrow">→</span>
              <label className="scan-date-field">
                <span className="t-cap">To</span>
                <input type="date" value={endDate} max={today}
                       onChange={e => setEndDate(e.target.value)} />
              </label>
            </div>
          </section>
        )}

        {/* Sources */}
        <section className="scan-section">
          <div className="scan-step-num">{srcStepNum}</div>
          <h2 className="scan-section-title">Sources</h2>
          <div className="scan-source-grid">
            {[
              { id: "news",         label: "General news" },
              { id: "news_premium", label: "Premium news" },
              { id: "filings",      label: "SEC filings" },
              { id: "transcripts",  label: "Earnings calls" },
            ].map(s => (
              <label key={s.id} className={"scan-source" + (sources.includes(s.id) ? " active" : "")}>
                <input type="checkbox" checked={sources.includes(s.id)}
                       onChange={() => setSources(c =>
                         c.includes(s.id) ? c.filter(x => x !== s.id) : [...c, s.id]
                       )} />
                <span className="scan-source-box"></span>
                <span className="scan-source-body">
                  <span className="scan-source-label">{s.label}</span>
                  {s.sub && <span className="scan-source-sub">{s.sub}</span>}
                </span>
              </label>
            ))}
          </div>
        </section>

        {(previewError || runError) && (
          <p style={{ color: "var(--discard)", fontFamily: "var(--sans)", fontSize: 12, marginBottom: 8 }}>
            {previewError || runError}
          </p>
        )}

        {/* Cost estimate — custom mode */}
        {dateMode === "custom" && <ScanCostEstimate config={{ scope, entity, universe, startDate, endDate }} />}

        {/* Action buttons */}
        {mode === "configure" && dateMode === "resume" && (
          <button className="launch-btn launch-btn-scan" onClick={loadPreview} disabled={previewLoading}>
            {previewLoading ? "Loading preview…" : "▶  Preview"}
          </button>
        )}
        {mode === "configure" && dateMode === "custom" && (
          <button className="launch-btn launch-btn-scan" onClick={startCustomScan}>
            ▶&nbsp; Start scan
          </button>
        )}
        {mode === "preview" && (
          <>
            <button className="launch-btn launch-btn-scan" onClick={startResume}
                    disabled={!preview || preview.runnable_count === 0}>
              ▶&nbsp; Confirm &amp; start
            </button>
            <button className="btn" style={{ marginTop: 8, width: "100%" }}
                    onClick={() => { setMode("configure"); setPreview(null); }}>
              ← Change scope
            </button>
          </>
        )}
        {isRunning && (
          <button className="btn" style={{ marginTop: 8, width: "100%" }}
                  onClick={() => { setMode("configure"); setScanParams(null); setScanResults(null); setPreview(null); }}>
            New scan
          </button>
        )}
      </aside>

      {/* ── Right: results ── */}
      <main className="scan-main">

        {mode === "configure" && (
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: 300, color: "var(--ink-mute)", fontFamily: "var(--sans)", fontSize: 13 }}>
            <p>Configure on the left and click {dateMode === "resume" ? "Preview" : "Start scan"}.</p>
          </div>
        )}

        {mode === "preview" && preview && (
          <UpdatePreviewPanel preview={preview} previewCost={previewCost} />
        )}

        {isRunning && (
          <>
            <header className="scan-results-head">
              <div className="scan-results-head-row">
                <div>
                  <div className="dateline">{mode === "running" ? "Scan in progress" : "Scan complete"}</div>
                  <h2 className="display scan-results-title">{scopeLabel}</h2>
                  <div className="scan-results-sub">
                    {mode === "running" && <span className="live-dot"></span>}
                    <span>{scanParams?.start_date} → {scanParams?.end_date}</span>
                    <span className="muted"> · </span>
                    <strong className="tnum">{completed}</strong>
                    <span className="muted"> / </span>
                    <strong className="tnum">{totalDays}</strong>
                    <span className="muted"> entity×day cells</span>
                  </div>
                  {entities.length > 1 && (
                    <div className="scan-results-sub" style={{ marginTop: 6 }}>
                      <span className="muted">Entities done · </span>
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
                </div>
                <div className="scan-summary-cell">
                  <dt className="t-cap">Discarded</dt>
                  <dd className="cost-num tnum">{agg.discarded}</dd>
                </div>
              </dl>
            </header>

            {/* Resume → entity list */}
            {dateMode === "resume" && entities.length > 0 && (
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

            {/* Custom → day calendar */}
            {dateMode === "custom" && allDays.length > 0 && (
              <>
                <section className="scan-grid-section">
                  <div className="ops-section-head">
                    <h2>Day-by-day</h2>
                    <span className="ops-section-meta">{allDays.length} calendar days</span>
                  </div>
                  <DayCalendar days={allDays} cursorIdx={completed} />
                </section>
                <section className="scan-list-section">
                  <div className="ops-section-head">
                    <h2>Most recent</h2>
                    <span className="ops-section-meta">last {Math.min(12, allDays.filter(d => d.status !== "pending").length)} days</span>
                  </div>
                  <ul className="scan-day-list">
                    {allDays.filter(d => d.status !== "pending").slice(-12).reverse().map(d => (
                      <ScanDayRow key={d.date} day={d} />
                    ))}
                  </ul>
                </section>
              </>
            )}
          </>
        )}
      </main>
    </div>
  );
}

// ── Preview panel (resume mode) ────────────────────────────────────
function UpdatePreviewPanel({ preview, previewCost }) {
  const runnable  = preview.entities.filter(r => r.est_windows > 0);
  const fallback  = runnable.filter(r => r.fallback);
  const upToDate  = preview.entities.filter(r => !r.fallback && r.has_history && r.est_windows === 0);

  function fmtDt(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", { month: "short", day: "2-digit", year: "numeric", timeZone: "UTC" })
      + " " + d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: "UTC", hour12: false }) + " UTC";
  }
  const fmtCost = v => v == null ? null : (v < 0.01 ? "< $0.01" : `$${v.toFixed(2)}`);

  return (
    <div className="update-preview">
      <div className="update-preview-summary">
        <div className="update-preview-stat">
          <span className="t-cap">Entities to run</span>
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
        {fallback.length > 0 && (
          <div className="update-preview-stat">
            <span className="t-cap">First run (yesterday→today)</span>
            <strong className="tnum update-preview-big update-preview-warn">{fallback.length}</strong>
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
                <tr key={r.entity_id} className={r.fallback ? "update-preview-row-fallback" : ""}>
                  <td>
                    <span className="update-ent-name">{r.name}</span>
                    {r.fallback && <span className="update-preview-first-tag" style={{ marginLeft: 8 }}>first run</span>}
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
    </div>
  );
}

// ── Per-entity live row ────────────────────────────────────────────
function UpdateEntityRow({ ent }) {
  const days      = ent.days || [];
  const succeeded = days.filter(d => d.status === "succeeded").length;
  const failed    = days.filter(d => d.status === "failed").length;
  const running   = days.filter(d => d.status === "running").length;
  const pending   = days.filter(d => ["pending","skipped"].includes(d.status)).length;
  let pill = "pending";
  if (running > 0) pill = "running";
  else if (days.length > 0 && days.every(d => ["succeeded","failed","skipped"].includes(d.status)))
    pill = failed > 0 ? "failed" : "done";
  return (
    <li className={`update-ent-row update-ent-row-${pill}`}>
      <div className="update-ent-row-name">
        <span>{ent.entityName}</span>
        {ent.entityTicker && <span className="update-ent-ticker"> · {ent.entityTicker}</span>}
      </div>
      <div className="update-ent-row-pills">
        {succeeded > 0 && <span className="scan-pill scan-pill-ok">✓ {succeeded}</span>}
        {failed    > 0 && <span className="scan-pill scan-pill-fail">✕ {failed}</span>}
        {running   > 0 && <span className="scan-pill scan-pill-run">● {running}</span>}
        {pending   > 0 && <span className="scan-pill" style={{ opacity: 0.45 }}>{pending} pending</span>}
      </div>
    </li>
  );
}

// ── Cost estimate (custom mode) ────────────────────────────────────
function ScanCostEstimate({ config }) {
  const estimates = (window.RUN_DATA && typeof window.RUN_DATA.composeEstimates === "object")
    ? window.RUN_DATA.composeEstimates : {};
  function parseCost(eid) {
    const est = estimates[eid];
    if (!est?.costDisplay) return null;
    const n = parseFloat(String(est.costDisplay).replace(/[^0-9.]/g, ""));
    return isNaN(n) ? null : n;
  }
  const days = useMemoS(() => {
    if (!config.startDate || !config.endDate) return 1;
    const ms = new Date(config.endDate) - new Date(config.startDate);
    return Math.max(1, Math.round(ms / 86400000) + 1);
  }, [config.startDate, config.endDate]);

  const estimate = useMemoS(() => {
    if (config.scope === "entity" && config.entity) {
      const c = parseCost(config.entity.id);
      return c != null ? { totalCost: c * days, costPerDay: c, nEntities: 1, coveredEntities: 1, label: config.entity.name } : null;
    }
    if (config.scope === "universe" && config.universe) {
      const ids = Array.isArray(config.universe.entity_ids) ? config.universe.entity_ids : [];
      let totalPerDay = 0, covered = 0;
      for (const eid of ids) { const c = parseCost(eid); if (c != null) { totalPerDay += c; covered++; } }
      return covered > 0 ? { totalCost: totalPerDay * days, costPerDay: totalPerDay, nEntities: ids.length, coveredEntities: covered, label: config.universe.label } : null;
    }
    return null;
  }, [config, days]);

  if (!estimate) return null;
  const fmt = v => v < 0.01 ? "< $0.01" : `$${v.toFixed(2)}`;
  const partial = estimate.coveredEntities < estimate.nEntities
    ? ` · ${estimate.coveredEntities}/${estimate.nEntities} entities with data` : "";
  return (
    <div className="scan-estimate">
      <div className="scan-estimate-row">
        <span className="t-cap">Est. cost</span>
        <span className="scan-estimate-val tnum">{fmt(estimate.totalCost)}</span>
      </div>
      <div className="scan-estimate-foot">
        {fmt(estimate.costPerDay)}/day · {days} day{days !== 1 ? "s" : ""}
        {estimate.nEntities > 1 && ` · ${estimate.nEntities} entities`}{partial}
      </div>
    </div>
  );
}

// ── Day calendar (custom mode) ────────────────────────────────────
function DayCalendar({ days, cursorIdx }) {
  const weeks = [];
  let week = [];
  days.forEach(d => {
    const date = new Date(d.date + "T00:00:00");
    const wd = date.getDay();
    if (week.length === 0 && wd !== 1) { const pad = (wd + 6) % 7; for (let p = 0; p < pad; p++) week.push(null); }
    week.push(d);
    if (wd === 0) { weeks.push(week); week = []; }
  });
  if (week.length) weeks.push(week);
  return (
    <div className="scan-cal">
      <div className="scan-cal-header">
        {["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].map(d => <div key={d} className="scan-cal-dow">{d}</div>)}
      </div>
      {weeks.map((w, i) => (
        <div key={i} className="scan-cal-week">
          {Array.from({ length: 7 }).map((_, j) => {
            const cell = w[j];
            if (!cell) return <div key={j} className="scan-cal-cell scan-cal-empty"></div>;
            const date = new Date(cell.date + "T00:00:00");
            const dayNum = date.getDate();
            const showMonth = dayNum <= 7;
            const cls = `scan-cal-cell scan-cal-${cell.status}${cell.empty ? " scan-cal-no-news" : ""}`;
            const tip = `${cell.date} · ${cell.status}${cell.saved !== undefined ? ` · ${cell.saved} saved` : ""}`;
            return (
              <button key={j} className={cls} title={tip}>
                <span className="scan-cal-num tnum">{String(dayNum).padStart(2, "0")}</span>
                {showMonth && <span className="scan-cal-month">{date.toLocaleDateString("en-US", { month: "short" })}</span>}
                {cell.status === "succeeded" && !cell.empty && <span className="scan-cal-saved tnum">{cell.saved}</span>}
                {cell.status === "succeeded" && cell.empty  && <span className="scan-cal-dash">—</span>}
                {cell.status === "running"   && <span className="scan-cal-spin"></span>}
                {cell.status === "failed"    && <span className="scan-cal-x">✕</span>}
                {cell.status === "skipped"   && <span className="scan-cal-dash">·</span>}
              </button>
            );
          })}
        </div>
      ))}
      <div className="scan-cal-legend">
        <span className="scan-cal-leg-cell scan-cal-succeeded"></span><span>Bullets found</span>
        <span className="scan-cal-leg-cell scan-cal-succeeded scan-cal-no-news"></span><span>No news</span>
        <span className="scan-cal-leg-cell scan-cal-running"></span><span>Running</span>
        <span className="scan-cal-leg-cell scan-cal-failed"></span><span>Failed</span>
        <span className="scan-cal-leg-cell scan-cal-skipped"></span><span>Skipped</span>
        <span className="scan-cal-leg-cell scan-cal-pending"></span><span>Pending</span>
      </div>
    </div>
  );
}

// ── Day row (custom mode) ─────────────────────────────────────────
function ScanDayRow({ day }) {
  const date = new Date(day.date + "T00:00:00");
  return (
    <li className={"scan-day-row scan-day-row-" + day.status}>
      <div className="scan-day-row-date">
        <div className="archive-day-num">{String(date.getDate()).padStart(2, "0")}</div>
        <div className="archive-day-month">{date.toLocaleDateString("en-US", { month: "short" })}</div>
        <div className="archive-day-weekday">{date.toLocaleDateString("en-US", { weekday: "short" })}</div>
      </div>
      <div className="scan-day-row-body">
        {day.status === "succeeded" && !day.empty && (
          <React.Fragment>
            <div className="scan-day-row-headline"><strong className="tnum">{day.saved}</strong> bullets saved <span className="muted">· {day.discarded} discarded</span></div>
          </React.Fragment>
        )}
        {day.status === "succeeded" && day.empty && <div className="scan-day-row-headline scan-day-row-empty">No material developments</div>}
        {day.status === "skipped"   && <div className="scan-day-row-headline scan-day-row-skipped">Skipped</div>}
        {day.status === "failed"    && <><div className="scan-day-row-headline scan-day-row-failed">Failed</div><div className="scan-day-row-meta">{day.error}</div></>}
        {day.status === "running"   && <div className="scan-day-row-headline"><span className="live-dot"></span> Running…</div>}
      </div>
      <div className="scan-day-row-status">
        {day.status === "succeeded" && !day.empty && <span className="scan-pill scan-pill-ok">✓ {day.saved}</span>}
        {day.status === "succeeded" && day.empty  && <span className="scan-pill scan-pill-empty">— empty</span>}
        {day.status === "skipped"   && <span className="scan-pill scan-pill-skip">skipped</span>}
        {day.status === "failed"    && <span className="scan-pill scan-pill-fail">✕ failed</span>}
        {day.status === "running"   && <span className="scan-pill scan-pill-run">● live</span>}
      </div>
    </li>
  );
}

window.ScanView = ScanView;
