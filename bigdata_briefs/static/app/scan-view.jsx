// Historical Scan view — bulk-run a date range, day by day.
// Layout: left = config form, right = progress + results.

const { useState: useStateS, useEffect: useEffectS, useRef: useRefS, useMemo: useMemoS } = React;

function ScanView({ tweaks }) {
  const COMPANIES = window.DATA?.companies || [];
  const UNIVERSES = window.EXTRAS.universes || [];
  const today = new Date().toISOString().slice(0, 10);
  const weekAgo = new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10);

  const [mode, setMode] = useStateS("configure"); // configure | running | done
  const [config, setConfig] = useStateS({
    scope: "entity",
    entity: COMPANIES[0],
    universe: UNIVERSES[0] || null,
    startDate: weekAgo,
    endDate: today,
    sources: ["news"],
  });

  // Scan state: active scan params + polling results
  const [scanParams, setScanParams] = useStateS(null); // {entity_ids, start_date, end_date}
  const [scanResults, setScanResults] = useStateS(null); // from /api/frontend/scan/status
  const [error, setError] = useStateS(null);
  const pollRef = useRefS(null);

  // Poll scan status every 3s while running
  useEffectS(() => {
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

  function startScan() {
    setError(null);
    const body = {
      start_date: config.startDate,
      end_date: config.endDate,
      source_categories: config.sources,
    };
    if (config.scope === "entity" && config.entity) {
      body.entity_id = config.entity.id;
    } else if (config.scope === "universe" && config.universe) {
      body.universe = config.universe.id;
    } else {
      setError("Select an entity or universe first.");
      return;
    }
    fetch("/api/frontend/scan/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(r => r.json())
      .then(data => {
        if (data.error) { setError(data.error); return; }
        setScanParams({
          entity_ids: data.entity_ids,
          start_date: config.startDate,
          end_date: config.endDate,
          total_windows: data.total_windows,
        });
        setScanResults(null);
        setMode("running");
      })
      .catch(err => setError(String(err)));
  }

  // Per-calendar-day row for charts (universe: merge all entities for that date)
  const allDays = useMemoS(() => {
    if (!scanResults?.entities?.length) return [];
    const byDate = {};
    for (const e of scanResults.entities) {
      for (const d of e.days || []) {
        if (!byDate[d.date]) byDate[d.date] = [];
        byDate[d.date].push({ ...d, entityName: e.entityName, entityId: e.entityId });
      }
    }
    return Object.keys(byDate).sort().map(date => {
      const cells = byDate[date];
      const st = cells.map(c => c.status);
      const any = x => st.some(s => s === x);
      const every = x => st.every(s => s === x);
      let status = "pending";
      if (any("running")) status = "running";
      else if (any("pending")) status = "pending";
      else if (any("failed")) status = "failed";
      else if (every("skipped")) {
        status = "skipped";
      } else if (st.every(s => s === "succeeded" || s === "skipped")) {
        status = "succeeded";
      } else status = "pending";
      const saved = cells.reduce((s, c) => s + (c.status === "succeeded" ? (c.saved || 0) : 0), 0);
      const discarded = cells.reduce((s, c) => s + (c.status === "succeeded" ? (c.discarded || 0) : 0), 0);
      const errCell = cells.find(c => c.status === "failed");
      const skippedOnly = every("skipped");
      return {
        date,
        status,
        saved,
        discarded,
        empty: status === "succeeded" && saved === 0,
        error: errCell?.error,
        reason: skippedOnly ? "No run for this calendar day (before resume or out of range)" : undefined,
      };
    });
  }, [scanResults]);

  // True counts: every entity × day (must match API completed semantics)
  const agg = useMemoS(() => {
    const out = { succeeded: 0, failed: 0, skipped: 0, pending: 0, running: 0, saved: 0, discarded: 0 };
    for (const e of scanResults?.entities || []) {
      for (const d of e.days || []) {
        if (d.status === "succeeded") {
          out.succeeded++;
          out.saved += d.saved || 0;
          out.discarded += d.discarded || 0;
        } else if (d.status === "failed") out.failed++;
        else if (d.status === "skipped") out.skipped++;
        else if (d.status === "running") out.running++;
        else out.pending++;
      }
    }
    return out;
  }, [scanResults]);

  const entities = scanResults?.entities || [];
  const entityRowsDone = useMemoS(() => {
    const ents = scanResults?.entities || [];
    const terminal = d => d.status === "succeeded" || d.status === "failed" || d.status === "skipped";
    return ents.filter(ent => {
      const days = ent.days || [];
      return days.length > 0 && days.every(terminal);
    }).length;
  }, [scanResults]);

  const totalDays = scanParams?.total_windows || 0;
  const completed = scanResults?.completed || 0;
  const allDaysList = allDays;
  const pct = totalDays > 0 ? Math.min(100, (completed / totalDays) * 100) : 0;

  const displayName = scanParams
    ? (config.scope === "entity" ? config.entity?.name : config.universe?.label) || ""
    : "";
  const displayTicker = config.scope === "entity" ? (config.entity?.ticker || "") : "";

  return (
    <div className="scan-layout">
      {/* ── Left: configure ── */}
      <aside className="scan-config">
        <header className="scan-config-head">
          <div className="dateline">Historical Scan</div>
          <h1 className="display scan-config-title">Build and maintain <em>coverage</em>.</h1>
          <p className="scan-config-lede">
            Runs the pipeline day-by-day across the chosen window. Use it to create initial
            history for a new company or to keep a company or universe up to date over time.
          </p>
        </header>

        <section className="scan-section">
          <div className="scan-step-num">01</div>
          <h2 className="scan-section-title">Scope</h2>
          <div className="seg seg-mini">
            <button className={"seg-btn" + (config.scope === "entity" ? " active" : "")}
                    onClick={() => setConfig({ ...config, scope: "entity" })}>
              <span className="seg-label">Single entity</span>
              <span className="seg-sub">One company</span>
            </button>
            <button className={"seg-btn" + (config.scope === "universe" ? " active" : "")}
                    onClick={() => setConfig({ ...config, scope: "universe" })}>
              <span className="seg-label">Universe</span>
              <span className="seg-sub">All companies in basket</span>
            </button>
          </div>
        </section>

        {config.scope === "entity" && (
          <section className="scan-section">
            <div className="scan-step-num">02</div>
            <h2 className="scan-section-title">Entity</h2>
            <select className="scan-select" value={config.entity?.id}
                    onChange={(e) => setConfig({ ...config, entity: COMPANIES.find(c => c.id === e.target.value) })}>
              {COMPANIES.map(c => <option key={c.id} value={c.id}>{c.name} · {c.ticker}</option>)}
            </select>
          </section>
        )}

        {config.scope === "universe" && (
          <section className="scan-section">
            <div className="scan-step-num">02</div>
            <h2 className="scan-section-title">Universe</h2>
            <div className="universe-list">
              {UNIVERSES.map(u => (
                <button key={u.id} className={"universe-pick" + (config.universe?.id === u.id ? " active" : "")}
                        onClick={() => setConfig({ ...config, universe: u })}>
                  <span className="universe-pick-label">{u.label}</span>
                  <span className="universe-pick-count tnum">{u.count}</span>
                  <span className="universe-pick-desc">{u.description}</span>
                </button>
              ))}
            </div>
          </section>
        )}

        <section className="scan-section">
          <div className="scan-step-num">03</div>
          <h2 className="scan-section-title">Date range</h2>
          <div className="scan-date-row">
            <label className="scan-date-field">
              <span className="t-cap">From</span>
              <input type="date" value={config.startDate} max={today}
                     onChange={(e) => setConfig({ ...config, startDate: e.target.value })} />
            </label>
            <span className="scan-date-arrow">→</span>
            <label className="scan-date-field">
              <span className="t-cap">To</span>
              <input type="date" value={config.endDate} max={today}
                     onChange={(e) => setConfig({ ...config, endDate: e.target.value })} />
            </label>
          </div>
        </section>

        <section className="scan-section">
          <div className="scan-step-num">04</div>
          <h2 className="scan-section-title">Sources</h2>
          <div className="scan-source-grid">
            {[
              { id: "news", label: "General news", sub: "Default · always recommended" },
              { id: "news_premium", label: "Premium news", sub: "Reuters, Bloomberg, FT, WSJ" },
              { id: "filings", label: "SEC filings", sub: "10-K, 10-Q, 8-K, proxy" },
              { id: "transcripts", label: "Earnings calls", sub: "Quarterly transcripts" },
            ].map(s => (
              <label key={s.id} className={"scan-source" + (config.sources.includes(s.id) ? " active" : "")}>
                <input type="checkbox" checked={config.sources.includes(s.id)}
                       onChange={() => setConfig(c => ({
                         ...c, sources: c.sources.includes(s.id)
                           ? c.sources.filter(x => x !== s.id)
                           : [...c.sources, s.id]
                       }))} />
                <span className="scan-source-box"></span>
                <span className="scan-source-body">
                  <span className="scan-source-label">{s.label}</span>
                  <span className="scan-source-sub">{s.sub}</span>
                </span>
              </label>
            ))}
          </div>
        </section>

        {error && <p style={{ color: "var(--discard)", fontFamily: "var(--sans)", fontSize: 12, marginBottom: 8 }}>{error}</p>}

        <ScanCostEstimate config={config} />

        <button
          className="launch-btn launch-btn-scan"
          onClick={startScan}
          disabled
          title="Start scan is disabled."
        >
          ▶&nbsp; {mode === "running" ? "Scan running…" : "Start scan (disabled)"}
        </button>
        {mode === "done" && (
          <button className="btn" style={{ marginTop: 8, width: "100%" }} onClick={() => { setMode("configure"); setScanParams(null); setScanResults(null); }}>
            New scan
          </button>
        )}
      </aside>

      {/* ── Right: progress + results ── */}
      <main className="scan-main">
        {mode === "configure" && (
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: 300, color: "var(--ink-mute)", fontFamily: "var(--sans)", fontSize: 13 }}>
            <p>Configure a scan on the left and click Start scan.</p>
          </div>
        )}

        {mode !== "configure" && (
          <>
            <header className="scan-results-head">
              <div className="scan-results-head-row">
                <div>
                  <div className="dateline">{mode === "running" ? "Scan in progress" : "Scan complete"}</div>
                  <h2 className="display scan-results-title">
                    {displayName} {displayTicker && <><em>·</em> {displayTicker}</>}
                  </h2>
                  <div className="scan-results-sub">
                    {mode === "running" && <span className="live-dot"></span>}
                    <span>{config.startDate} → {config.endDate}</span>
                    <span className="muted">·</span>
                    <span>
                      <strong className="tnum">{completed}</strong> / <strong className="tnum">{totalDays}</strong>
                      <span className="muted"> entity×day cells resolved</span>
                      <span className="muted"> · </span>
                      <span style={{ fontSize: 12 }}>skipped only when no pipeline run exists for that day and it is before the resume cursor</span>
                    </span>
                  </div>
                  {entities.length > 1 && (
                    <div className="scan-results-sub" style={{ marginTop: 6 }}>
                      <span className="muted">Entities fully done</span>
                      <span className="muted"> · </span>
                      <strong className="tnum">{entityRowsDone}</strong>
                      <span> / </span>
                      <strong className="tnum">{entities.length}</strong>
                      <span className="muted"> · </span>
                      <span style={{ fontSize: 12 }}>Each entity is done when every day is succeeded, failed, or skipped.</span>
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
                  <span>{config.startDate}</span>
                  <span className="tnum">{pct.toFixed(0)}%</span>
                  <span>{config.endDate}</span>
                </div>
              </div>

              <dl className="scan-summary">
                <div className="scan-summary-cell">
                  <dt className="t-cap">Day-cells (entity × day)</dt>
                  <dd className="cost-num tnum">{completed}<span className="compose-estimate-sep">/</span>{totalDays}</dd>
                  <span className="compose-estimate-foot">
                    {agg.succeeded} succeeded · {agg.failed} failed · {agg.skipped} skipped
                    {(agg.pending + agg.running) > 0 && (
                      <span> · {agg.pending} pending{agg.running ? ` · ${agg.running} running` : ""}</span>
                    )}
                  </span>
                </div>
                <div className="scan-summary-cell">
                  <dt className="t-cap">Bullets saved</dt>
                  <dd className="cost-num tnum">{agg.saved}</dd>
                  <span className="compose-estimate-foot">active bullets summed across succeeded entity-days</span>
                </div>
                <div className="scan-summary-cell">
                  <dt className="t-cap">Discarded</dt>
                  <dd className="cost-num tnum">{agg.discarded}</dd>
                  <span className="compose-estimate-foot">inactive bullet rows on those runs (funnel rejects and superseded)</span>
                </div>
              </dl>
            </header>

            {allDaysList.length > 0 && (
              <>
                <section className="scan-grid-section">
                  <div className="ops-section-head">
                    <h2>Day-by-day</h2>
                    <span className="ops-section-meta">{allDaysList.length} calendar days</span>
                  </div>
                  <DayCalendar days={allDaysList} cursorIdx={completed} />
                </section>

                <section className="scan-list-section">
                  <div className="ops-section-head">
                    <h2>Most recent</h2>
                    <span className="ops-section-meta">
                      last {Math.min(12, allDaysList.filter(d => d.status !== "pending").length)} calendar days (merged)
                    </span>
                  </div>
                  <ul className="scan-day-list">
                    {allDaysList.filter(d => d.status !== "pending").slice(-12).reverse().map(d => (
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

// ── Scan cost estimate ─────────────────────────────────────────
function ScanCostEstimate({ config }) {
  const estimates = (window.RUN_DATA && typeof window.RUN_DATA.composeEstimates === "object")
    ? window.RUN_DATA.composeEstimates : {};

  function parseCost(entityId) {
    const est = estimates[entityId];
    if (!est || !est.costDisplay) return null;
    const n = parseFloat(String(est.costDisplay).replace(/[^0-9.]/g, ""));
    return isNaN(n) ? null : n;
  }

  // Calendar days in the selected range (inclusive)
  const days = useMemoS(() => {
    if (!config.startDate || !config.endDate) return 1;
    const ms = new Date(config.endDate) - new Date(config.startDate);
    return Math.max(1, Math.round(ms / 86400000) + 1);
  }, [config.startDate, config.endDate]);

  const estimate = useMemoS(() => {
    if (config.scope === "entity" && config.entity) {
      const costPerDay = parseCost(config.entity.id);
      if (costPerDay === null) return null;
      return {
        totalCost: costPerDay * days,
        costPerDay,
        nEntities: 1,
        coveredEntities: 1,
        label: config.entity.name,
      };
    }
    if (config.scope === "universe" && config.universe) {
      const ids = Array.isArray(config.universe.entity_ids) ? config.universe.entity_ids : [];
      let totalPerDay = 0;
      let covered = 0;
      for (const eid of ids) {
        const c = parseCost(eid);
        if (c !== null) { totalPerDay += c; covered++; }
      }
      if (covered === 0) return null;
      return {
        totalCost: totalPerDay * days,
        costPerDay: totalPerDay,
        nEntities: ids.length,
        coveredEntities: covered,
        label: config.universe.label,
      };
    }
    return null;
  }, [config, days]);

  if (!estimate) return null;

  const fmt = (v) => v < 0.01 ? "< $0.01" : `$${v.toFixed(2)}`;
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
        {estimate.nEntities > 1 && ` · ${estimate.nEntities} entities`}
        {partial}
      </div>
    </div>
  );
}


// ── Day calendar grid ──────────────────────────────────────────
function DayCalendar({ days, cursorIdx }) {
  // Group into ISO weeks for a calendar feel
  const weeks = [];
  let week = [];
  days.forEach((d, i) => {
    const date = new Date(d.date + "T00:00:00");
    const wd = date.getDay(); // 0=Sun..6=Sat
    if (week.length === 0 && wd !== 1) {
      // pad to align weeks Mon-first
      const pad = (wd + 6) % 7;
      for (let p = 0; p < pad; p++) week.push(null);
    }
    week.push({ ...d, idx: i });
    if (wd === 0) {
      weeks.push(week);
      week = [];
    }
  });
  if (week.length) weeks.push(week);

  return (
    <div className="scan-cal">
      <div className="scan-cal-header">
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map(d => (
          <div key={d} className="scan-cal-dow">{d}</div>
        ))}
      </div>
      {weeks.map((w, i) => (
        <div key={i} className="scan-cal-week">
          {Array.from({ length: 7 }).map((_, j) => {
            const cell = w[j];
            if (!cell) return <div key={j} className="scan-cal-cell scan-cal-empty"></div>;
            const date = new Date(cell.date + "T00:00:00");
            const dayNum = date.getDate();
            const monthNum = date.getMonth() + 1;
            const showMonth = dayNum <= 7;
            const cls = `scan-cal-cell scan-cal-${cell.status}${cell.empty ? " scan-cal-no-news" : ""}`;
            const tip = cell.reason
              ? `${cell.date} · ${cell.status} · ${cell.reason}`
              : `${cell.date} · ${cell.status}${cell.saved !== undefined ? ` · ${cell.saved} saved` : ""}`;
            return (
              <button key={j} className={cls} title={tip}>
                <span className="scan-cal-num tnum">{String(dayNum).padStart(2, "0")}</span>
                {showMonth && <span className="scan-cal-month">{date.toLocaleDateString("en-US", { month: "short" })}</span>}
                {cell.status === "succeeded" && !cell.empty && (
                  <span className="scan-cal-saved tnum">{cell.saved}</span>
                )}
                {cell.status === "succeeded" && cell.empty && <span className="scan-cal-dash">—</span>}
                {cell.status === "running" && <span className="scan-cal-spin"></span>}
                {cell.status === "failed" && <span className="scan-cal-x">✕</span>}
                {cell.status === "skipped" && <span className="scan-cal-dash">·</span>}
              </button>
            );
          })}
        </div>
      ))}
      <div className="scan-cal-legend">
        <span className="scan-cal-leg-cell scan-cal-succeeded"></span><span>Bullets found</span>
        <span className="scan-cal-leg-cell scan-cal-succeeded scan-cal-no-news"></span><span>No news that day</span>
        <span className="scan-cal-leg-cell scan-cal-running"></span><span>Running</span>
        <span className="scan-cal-leg-cell scan-cal-failed"></span><span>Failed</span>
        <span className="scan-cal-leg-cell scan-cal-skipped"></span><span>Skipped / not in scan window</span>
        <span className="scan-cal-leg-cell scan-cal-pending"></span><span>Pending</span>
      </div>
    </div>
  );
}

// ── Per-day row in the recent list ──────────────────────────────
function ScanDayRow({ day }) {
  const date = new Date(day.date + "T00:00:00");
  const dayNum = date.getDate();
  const month3 = date.toLocaleDateString("en-US", { month: "short" });
  const wd = date.toLocaleDateString("en-US", { weekday: "short" });

  return (
    <li className={"scan-day-row scan-day-row-" + day.status}>
      <div className="scan-day-row-date">
        <div className="archive-day-num">{String(dayNum).padStart(2, "0")}</div>
        <div className="archive-day-month">{month3}</div>
        <div className="archive-day-weekday">{wd}</div>
      </div>
      <div className="scan-day-row-body">
        {day.status === "succeeded" && !day.empty && (
          <React.Fragment>
            <div className="scan-day-row-headline">
              <strong className="tnum">{day.saved}</strong> bullets saved
              <span className="muted"> · </span>
              <span>{day.discarded} discarded</span>
            </div>
            <div className="scan-day-row-meta">
              {day.durationSec != null ? (
                <span>Brief composed in {day.durationSec}s · ready to view</span>
              ) : (
                <span>Aggregated across entities for this date · ready to view</span>
              )}
            </div>
          </React.Fragment>
        )}
        {day.status === "succeeded" && day.empty && (
          <React.Fragment>
            <div className="scan-day-row-headline scan-day-row-empty">
              No material developments
            </div>
            <div className="scan-day-row-meta">
              {day.discarded} candidates considered, all rejected
              {day.durationSec != null && <span> · {day.durationSec}s</span>}
            </div>
          </React.Fragment>
        )}
        {day.status === "skipped" && (
          <React.Fragment>
            <div className="scan-day-row-headline scan-day-row-skipped">Skipped</div>
            <div className="scan-day-row-meta">{day.reason}</div>
          </React.Fragment>
        )}
        {day.status === "failed" && (
          <React.Fragment>
            <div className="scan-day-row-headline scan-day-row-failed">Failed</div>
            <div className="scan-day-row-meta">{day.error}</div>
          </React.Fragment>
        )}
        {day.status === "running" && (
          <React.Fragment>
            <div className="scan-day-row-headline">
              <span className="live-dot"></span> Running…
            </div>
            <div className="scan-day-row-meta">Pipeline in progress for {day.date}</div>
          </React.Fragment>
        )}
      </div>
      <div className="scan-day-row-status">
        {day.status === "succeeded" && !day.empty && <span className="scan-pill scan-pill-ok">✓ {day.saved}</span>}
        {day.status === "succeeded" && day.empty && <span className="scan-pill scan-pill-empty">— empty</span>}
        {day.status === "skipped" && <span className="scan-pill scan-pill-skip">skipped</span>}
        {day.status === "failed" && <span className="scan-pill scan-pill-fail">✕ failed</span>}
        {day.status === "running" && <span className="scan-pill scan-pill-run">● live</span>}
      </div>
    </li>
  );
}

window.ScanView = ScanView;
