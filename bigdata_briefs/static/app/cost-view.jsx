// Cost Breakdown — drill-down for a single run.
const { useState: useStateC, useEffect: useEffectC } = React;

function CostView({ tweaks }) {
  const COMPANIES = window.DATA?.companies || [];
  const initial = window.EXTRAS.cost;
  const defaultEntityId = initial?.entityId || COMPANIES[0]?.id || "";

  const [costData, setCostData] = useStateC(initial);
  const [selectedRun, setSelectedRun] = useStateC(initial?.runId || null);
  const [pickEntityId, setPickEntityId] = useStateC(defaultEntityId);
  const [entityRuns, setEntityRuns] = useStateC([]);
  const [loading, setLoading] = useStateC(false);
  const [loadingRuns, setLoadingRuns] = useStateC(false);
  const [error, setError] = useStateC(null);

  function fetchEntityRuns(entityId, options = {}) {
    const autoLoadFirst = Boolean(options.autoLoadFirst);
    if (!entityId) {
      setEntityRuns([]);
      return Promise.resolve();
    }
    setLoadingRuns(true);
    setError(null);
    return fetch(`/api/frontend/cost/runs-by-entity/${encodeURIComponent(entityId)}`)
      .then(r => r.json())
      .then(data => {
        const runs = Array.isArray(data.runs) ? data.runs : [];
        setEntityRuns(runs);
        if (autoLoadFirst) {
          if (!runs.length) {
            setError("No pipeline runs for this company.");
          } else {
            const pick = runs.find(x => x.hasMetrics) || runs[0];
            if (pick.hasMetrics) {
              return loadBreakdown(pick.runId);
            }
            setError("No cost metrics stored for this company’s runs yet.");
          }
        }
        return undefined;
      })
      .catch(e => {
        setError(String(e));
        setEntityRuns([]);
      })
      .finally(() => setLoadingRuns(false));
  }

  function loadBreakdown(runId) {
    setLoading(true);
    setError(null);
    const q = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
    return fetch(`/api/frontend/cost/breakdown${q}`)
      .then(r => r.json())
      .then(data => {
        if (data.error && !data.breakdown) {
          setError(data.error === "no_metrics" ? "No cost metrics for this run." : data.error);
          setCostData(null);
          return;
        }
        if (data.breakdown) {
          setCostData(data.breakdown);
          setSelectedRun(data.breakdown.runId);
          setPickEntityId(data.breakdown.entityId);
          fetchEntityRuns(data.breakdown.entityId, { autoLoadFirst: false });
        } else {
          setCostData(null);
        }
      })
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false));
  }

  useEffectC(() => {
    loadBreakdown(initial?.runId || null);
  }, []);

  function onCompanyChange(entityId) {
    setPickEntityId(entityId);
    fetchEntityRuns(entityId, { autoLoadFirst: true });
  }

  function onRunChange(runId8) {
    if (runId8) loadBreakdown(runId8);
  }

  const C = costData;
  const recentList = (C && C.recentForBreakdown) ? C.recentForBreakdown : [];

  if (loading && !C) {
    return (
      <div className="cost-layout" style={{ padding: 40, fontFamily: "var(--sans)", color: "var(--ink-mute)" }}>
        Loading cost data…
      </div>
    );
  }

  if (!C) {
    return (
      <div className="cost-layout" style={{ padding: 40, fontFamily: "var(--sans)" }}>
        <p style={{ color: "var(--ink-mute)" }}>No cost metrics available yet. Run the pipeline to populate runs.</p>
        {error && <p style={{ color: "var(--discard)", fontSize: 13, marginTop: 8 }}>{error}</p>}
      </div>
    );
  }

  const llmTotal = C.llmModels.reduce((s, m) => s + m.cost, 0);
  const phaseMax = Math.max(...C.phases.map(p => p.total), 0.0001);

  return (
    <div className="cost-layout">
      <aside className="cost-side">
        <section className="scan-section">
          <div className="scan-step-num">01</div>
          <h2 className="scan-section-title">Company</h2>
          <select
            className="scan-select"
            value={pickEntityId}
            onChange={(e) => onCompanyChange(e.target.value)}
            disabled={loading || loadingRuns || COMPANIES.length === 0}
          >
            {COMPANIES.length === 0 && <option value="">No companies in roster</option>}
            {COMPANIES.map(c => (
              <option key={c.id} value={c.id}>{c.name} · {c.ticker || c.id}</option>
            ))}
          </select>
          <p className="scan-hint">Same roster as Compose — pick an issuer to load its runs.</p>
        </section>

        <section className="scan-section">
          <div className="scan-step-num">02</div>
          <h2 className="scan-section-title">Run · report window</h2>
          {loadingRuns && (
            <p className="scan-hint" style={{ marginBottom: 8 }}>Loading runs…</p>
          )}
          {!loadingRuns && !entityRuns.length && (
            <p className="scan-hint">No runs for this company yet — run the pipeline or pick another ticker.</p>
          )}
          {entityRuns.length > 0 && (
            <div className="universe-list">
              {entityRuns.map(r => (
                <button
                  key={r.runId}
                  type="button"
                  className={"universe-pick" + (r.runId === selectedRun ? " active" : "")}
                  onClick={() => onRunChange(r.runId)}
                  disabled={loading}
                >
                  <span className="universe-pick-label">{r.windowEnd || "—"}</span>
                  <span className="universe-pick-count tnum">
                    {r.hasMetrics ? `$${Number(r.cost).toFixed(4)}` : "—"}
                  </span>
                  <span className="universe-pick-desc">
                    <span className="t-mono">Run {r.runId}</span>
                    {r.duration && r.duration !== "—" ? <> · {r.duration}</> : null}
                    {!r.hasMetrics ? <> · no metrics row</> : null}
                  </span>
                </button>
              ))}
            </div>
          )}
        </section>

        <section className="scan-section">
          <div className="scan-step-num">03</div>
          <h2 className="scan-section-title">Recent · all entities</h2>
        {error && (
          <p style={{ color: "var(--discard)", fontFamily: "var(--sans)", fontSize: 11, marginBottom: 8 }}>{error}</p>
        )}
        <ul className="cost-runs">
          {recentList.map(r => (
            <li key={`${r.runId}-${r.entity}`}>
              <button className={"cost-run" + (r.runId === selectedRun ? " active" : "")}
                      onClick={() => loadBreakdown(r.runId)}
                      disabled={loading}>
                <div className="cost-run-row1">
                  <span className="t-mono cost-run-ticker">{r.ticker}</span>
                  <span className="t-mono cost-run-cost tnum">${r.cost.toFixed(4)}</span>
                </div>
                <div className="cost-run-name">{r.entity}</div>
                <div className="cost-run-meta">
                  <span className="t-mono">{r.runId}</span>
                  <span className="muted"> · </span>
                  <span>{r.duration}</span>
                </div>
              </button>
            </li>
          ))}
        </ul>
        </section>
      </aside>

      <main className="cost-main">
        <header className="cost-header">
          <a href="#" className="back-link">← Activity log</a>
          <div className="dateline">Cost forensics · run {C.runId}</div>
          <h1 className="display cost-title">{C.entityName} <em>·</em> {C.ticker}</h1>
          <div className="cost-meta">
            <span className="t-mono">{C.entityId}</span>
            <span className="muted">·</span>
            <span>{C.windowStart.slice(0, 10)} → {C.windowEnd.slice(0, 10)}</span>
            <span className="muted">·</span>
            <span><StatusBadge status={C.status} /></span>
            <span className="muted">·</span>
            <span className="tnum">{C.durationSec}s</span>
            {loading && <span className="muted"> · updating…</span>}
          </div>
        </header>

        <section className="cost-tiles">
          <div className="cost-tile">
            <div className="t-cap">LLM</div>
            <div className="cost-tile-amt tnum">${C.summary.llm.toFixed(4)}</div>
            <div className="cost-tile-sub">{C.llmModels.length} models · {C.llmModels.reduce((s, m) => s + m.calls, 0)} calls</div>
          </div>
          <div className="cost-tile">
            <div className="t-cap">Embeddings</div>
            <div className="cost-tile-amt tnum">${C.summary.embeddings.toFixed(4)}</div>
            <div className="cost-tile-sub">{C.summary.embeddingTokens.toLocaleString()} tokens · {C.summary.embeddingModel}</div>
          </div>
          <div className="cost-tile">
            <div className="t-cap">API chunks</div>
            <div className="cost-tile-amt tnum">${C.summary.apiChunks.toFixed(4)}</div>
            <div className="cost-tile-sub">{C.summary.chunksTotal} chunks × ${C.summary.chunkRate}</div>
          </div>
          <div className="cost-tile cost-tile-total">
            <div className="t-cap">Total</div>
            <div className="cost-tile-amt tnum">${C.summary.total.toFixed(4)}</div>
            <div className="cost-tile-sub">${(C.summary.total / Math.max(C.durationSec, 1) * 60).toFixed(4)} per minute of runtime</div>
          </div>
        </section>

        <section className="cost-section">
          <div className="ops-section-head">
            <h2>LLM — by model</h2>
            <span className="ops-section-meta">{C.llmModels.length} models · ${llmTotal.toFixed(4)} total</span>
          </div>
          <table className="editorial cost-table">
            <thead>
              <tr>
                <th>Model</th>
                <th>Role</th>
                <th className="num">Calls</th>
                <th className="num">Input tkn</th>
                <th className="num">Output tkn</th>
                <th className="num">Cost</th>
                <th style={{ minWidth: 160 }}>% of LLM</th>
              </tr>
            </thead>
            <tbody>
              {[...C.llmModels].sort((a, b) => b.cost - a.cost).map(m => {
                const pct = llmTotal > 0 ? (m.cost / llmTotal) * 100 : 0;
                return (
                  <tr key={m.model}>
                    <td><span className="model-tag">{m.model}</span></td>
                    <td>{m.role}</td>
                    <td className="num">{m.calls}</td>
                    <td className="num">{m.promptTokens.toLocaleString()}</td>
                    <td className="num">{m.completionTokens.toLocaleString()}</td>
                    <td className="num" style={{ fontWeight: 600 }}>${m.cost.toFixed(4)}</td>
                    <td>
                      <div className="bar-wrap">
                        <div className="bar-bg"><div className="bar-fill bar-llm" style={{ width: pct + "%" }} /></div>
                        <span className="bar-pct tnum">{pct.toFixed(1)}%</span>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>

        <section className="cost-section">
          <div className="ops-section-head">
            <h2>By pipeline phase</h2>
            <span className="ops-section-meta">Where the dollars actually went</span>
          </div>
          <div className="cost-phases">
            {[...C.phases].sort((a, b) => b.total - a.total).map(p => (
              <div key={p.id} className="cost-phase">
                <div className="cost-phase-head">
                  <span className="cost-phase-label">{p.label}</span>
                  <span className="cost-phase-amt tnum">${p.total.toFixed(4)}</span>
                  <span className="cost-phase-pct tnum muted">{p.percent.toFixed(1)}%</span>
                </div>
                <div className="cost-phase-bar">
                  <div className="cost-phase-fill cost-phase-llm" style={{ width: (p.llm / phaseMax * 100) + "%" }} />
                  <div className="cost-phase-fill cost-phase-embed" style={{ width: (p.embed / phaseMax * 100) + "%" }} />
                  <div className="cost-phase-fill cost-phase-api" style={{ width: (p.api / phaseMax * 100) + "%" }} />
                </div>
                <div className="cost-phase-meta">
                  {p.calls > 0 && <span><strong className="tnum">{p.calls}</strong> LLM calls</span>}
                  {p.requests > 0 && <span><strong className="tnum">{p.requests}</strong> API requests</span>}
                  {p.chunks > 0 && <span><strong className="tnum">{p.chunks}</strong> chunks retrieved</span>}
                </div>
              </div>
            ))}
            <div className="cost-phase-key">
              <span><span className="cost-key cost-key-llm" />LLM</span>
              <span><span className="cost-key cost-key-embed" />Embeddings</span>
              <span><span className="cost-key cost-key-api" />API chunks</span>
            </div>
          </div>
        </section>

        <section className="cost-section">
          <div className="ops-section-head">
            <h2>BigData API · search activity</h2>
            <span className="ops-section-meta">{C.summary.chunksTotal} chunks @ ${C.summary.chunkRate} = ${C.summary.apiChunks.toFixed(4)}</span>
          </div>
          <table className="editorial cost-table">
            <thead>
              <tr>
                <th>Phase</th>
                <th className="num">Requests</th>
                <th className="num">Chunks</th>
                <th className="num">Query units</th>
                <th className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {(C.apiPhases && C.apiPhases.length > 0 ? C.apiPhases : []).map(p => (
                <tr key={p.id}>
                  <td>{p.label}</td>
                  <td className="num">{p.requests}</td>
                  <td className="num">{p.chunks.toLocaleString()}</td>
                  <td className="num">{p.queryUnits.toFixed(2)}</td>
                  <td className="num" style={{ fontWeight: 600 }}>${p.cost.toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(!C.apiPhases || C.apiPhases.length === 0) && (
            <p className="muted" style={{ fontFamily: "var(--sans)", fontSize: 12, marginTop: 8 }}>No per-phase API breakdown stored for this run.</p>
          )}
        </section>
      </main>
    </div>
  );
}

window.CostView = CostView;
