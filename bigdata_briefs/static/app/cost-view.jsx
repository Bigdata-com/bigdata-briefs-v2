// Cost Breakdown — drill-down for a single run.
const { useState: useStateC, useEffect: useEffectC } = React;

// ── Phase grouping ─────────────────────────────────────────────────
// Phases that share a common prefix are collapsed into a group row.
const PHASE_GROUP_PREFIXES = [
  "Bullets Generation",
  "Novelty Embedding Evaluation",
  "Novelty Search",
  "Entity Grounding Check",
  "Relevance Score",
  "Concept Search",
];

function getGroupKey(label) {
  for (const prefix of PHASE_GROUP_PREFIXES) {
    if (label.startsWith(prefix) && label.length > prefix.length) return prefix;
  }
  return null;
}

function buildPhaseGroups(phases) {
  const order = [];
  const byKey = {};

  phases.forEach(p => {
    const key = getGroupKey(p.label);
    if (key) {
      if (!byKey[key]) {
        byKey[key] = { type: "group", key, label: key, items: [], total: 0, llm: 0, embed: 0, api: 0, calls: 0, chunks: 0, requests: 0 };
        order.push(byKey[key]);
      }
      const g = byKey[key];
      g.items.push(p);
      g.total     += p.total;
      g.llm       += p.llm;
      g.embed     += p.embed;
      g.api       += p.api;
      g.calls     += p.calls;
      g.chunks    += p.chunks;
      g.requests  += p.requests;
    } else {
      order.push({ type: "single", data: p });
    }
  });

  const grand = order.reduce((s, x) => s + (x.type === "group" ? x.total : x.data.total), 0);
  order.forEach(x => {
    if (x.type === "group") x.percent = grand > 0 ? (x.total / grand) * 100 : 0;
  });

  return { rows: order, grand };
}

function PhaseRow({ p, phaseMax, indent = false }) {
  return (
    <div className={"cost-phase" + (indent ? " cost-phase-indent" : "")}>
      <div className="cost-phase-head">
        <span className="cost-phase-label">{p.label}</span>
        <span className="cost-phase-amt tnum">${p.total.toFixed(4)}</span>
        <span className="cost-phase-pct tnum muted">{p.percent.toFixed(1)}%</span>
      </div>
      <div className="cost-phase-bar">
        <div className="cost-phase-fill cost-phase-llm"   style={{ width: (p.llm   / phaseMax * 100) + "%" }} />
        <div className="cost-phase-fill cost-phase-embed" style={{ width: (p.embed / phaseMax * 100) + "%" }} />
        <div className="cost-phase-fill cost-phase-api"   style={{ width: (p.api   / phaseMax * 100) + "%" }} />
      </div>
      <div className="cost-phase-meta">
        {p.calls    > 0 && <span><strong className="tnum">{p.calls}</strong> LLM calls</span>}
        {p.requests > 0 && <span><strong className="tnum">{p.requests}</strong> API requests</span>}
        {p.chunks   > 0 && <span><strong className="tnum">{p.chunks}</strong> chunks retrieved</span>}
      </div>
    </div>
  );
}

// Extract the sub-operation name from a phase label given its group prefix.
// e.g. prefix="Novelty Search", label="Novelty Search Parse 9 Attempt0" → "Parse"
// Returns null if the remainder is only a number (no meaningful sub-type).
function getSubKey(prefix, label) {
  const rest = label.slice(prefix.length).trim();
  const m = rest.match(/^([A-Za-z][A-Za-z\s]*?)(?=\s+\d|\s*$)/);
  const key = m ? m[1].trim() : "";
  return key || null;
}

function buildSubGroups(groupPrefix, items) {
  const order = [];
  const byKey = {};
  let hasSubKeys = false;

  items.forEach(p => {
    const key = getSubKey(groupPrefix, p.label);
    if (key) {
      hasSubKeys = true;
      if (!byKey[key]) {
        byKey[key] = { key, label: key, items: [], total: 0, llm: 0, embed: 0, api: 0, calls: 0, chunks: 0, requests: 0, percent: 0 };
        order.push(byKey[key]);
      }
      const sg = byKey[key];
      sg.items.push(p);
      sg.total    += p.total;
      sg.llm      += p.llm;
      sg.embed    += p.embed;
      sg.api      += p.api;
      sg.calls    += p.calls;
      sg.chunks   += p.chunks;
      sg.requests += p.requests;
    } else {
      order.push({ key: p.id, single: p });
    }
  });

  if (!hasSubKeys) return null; // no sub-grouping needed

  const grand = order.reduce((s, x) => s + (x.single ? x.single.total : x.total), 0);
  order.forEach(x => { if (!x.single) x.percent = grand > 0 ? (x.total / grand) * 100 : 0; });

  return order;
}

function SubGroupRow({ sg, phaseMax }) {
  const [open, setOpen] = useStateC(false);
  const isSingle = sg.items.length === 1;
  return (
    <div className="cost-phase-group">
      <div className={"cost-phase cost-phase-indent" + (open ? " cost-phase-open" : "")}>
        <div className="cost-phase-head">
          <button className="cost-phase-expand" onClick={() => setOpen(v => !v)}>
            <span className="cost-phase-expand-icon">{open ? "▾" : "▸"}</span>
            <span className="cost-phase-label">{sg.label}</span>
            {!isSingle && <span className="cost-phase-label-sub">{sg.items.length}×</span>}
          </button>
          <span className="cost-phase-amt tnum">${sg.total.toFixed(4)}</span>
          <span className="cost-phase-pct tnum muted">{sg.percent.toFixed(1)}%</span>
        </div>
        <div className="cost-phase-meta">
          {sg.calls    > 0 && <span><strong className="tnum">{sg.calls}</strong> LLM calls</span>}
          {sg.requests > 0 && <span><strong className="tnum">{sg.requests}</strong> API requests</span>}
          {sg.chunks   > 0 && <span><strong className="tnum">{sg.chunks}</strong> chunks</span>}
        </div>
      </div>
      {open && (
        <div className="cost-phase-children" style={{ marginLeft: 32 }}>
          {sg.items.map(p => <PhaseRow key={p.id} p={p} phaseMax={phaseMax} indent />)}
        </div>
      )}
    </div>
  );
}

function GroupRow({ g, phaseMax }) {
  const [open, setOpen] = useStateC(false);
  const subGroups = React.useMemo(() => buildSubGroups(g.key, g.items), [g.key, g.items]);

  return (
    <div className="cost-phase-group">
      <div className={"cost-phase" + (open ? " cost-phase-open" : "")}>
        <div className="cost-phase-head">
          <button className="cost-phase-expand" onClick={() => setOpen(v => !v)}>
            <span className="cost-phase-expand-icon">{open ? "▾" : "▸"}</span>
            <span className="cost-phase-label">{g.label}</span>
            <span className="cost-phase-label-sub">{g.items.length} steps</span>
          </button>
          <span className="cost-phase-amt tnum">${g.total.toFixed(4)}</span>
          <span className="cost-phase-pct tnum muted">{g.percent.toFixed(1)}%</span>
        </div>
        <div className="cost-phase-bar">
          <div className="cost-phase-fill cost-phase-llm"   style={{ width: (g.llm   / phaseMax * 100) + "%" }} />
          <div className="cost-phase-fill cost-phase-embed" style={{ width: (g.embed / phaseMax * 100) + "%" }} />
          <div className="cost-phase-fill cost-phase-api"   style={{ width: (g.api   / phaseMax * 100) + "%" }} />
        </div>
        <div className="cost-phase-meta">
          {g.calls    > 0 && <span><strong className="tnum">{g.calls}</strong> LLM calls</span>}
          {g.requests > 0 && <span><strong className="tnum">{g.requests}</strong> API requests</span>}
          {g.chunks   > 0 && <span><strong className="tnum">{g.chunks}</strong> chunks retrieved</span>}
        </div>
      </div>
      {open && (
        <div className="cost-phase-children">
          {subGroups
            ? subGroups.map(x =>
                x.single
                  ? <PhaseRow key={x.key} p={x.single} phaseMax={phaseMax} indent />
                  : <SubGroupRow key={x.key} sg={x} phaseMax={phaseMax} />
              )
            : g.items.map(p => <PhaseRow key={p.id} p={p} phaseMax={phaseMax} indent />)
          }
        </div>
      )}
    </div>
  );
}

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
  const { rows: phaseRows } = buildPhaseGroups([...C.phases].sort((a, b) => b.total - a.total));

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
            <div className="t-cap">Compute tokens</div>
            <div className="cost-tile-amt tnum">${C.summary.llm.toFixed(4)}</div>
            <div className="cost-tile-sub">{C.llmModels.length} models · {C.llmModels.reduce((s, m) => s + m.calls, 0)} calls</div>
          </div>
          <div className="cost-tile">
            <div className="t-cap">Embeddings</div>
            <div className="cost-tile-amt tnum">${C.summary.embeddings.toFixed(4)}</div>
            <div className="cost-tile-sub">{C.summary.embeddingTokens.toLocaleString()} tokens · {C.summary.embeddingModel}</div>
          </div>
          <div className="cost-tile">
            <div className="t-cap">Grounding tokens</div>
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
            <h2>Compute tokens — by model</h2>
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
                <th style={{ minWidth: 160 }}>% of Compute</th>
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
            {phaseRows.map((row, i) =>
              row.type === "group"
                ? <GroupRow key={row.key} g={row} phaseMax={phaseMax} />
                : <PhaseRow key={row.data.id} p={row.data} phaseMax={phaseMax} />
            )}
            <div className="cost-phase-key">
              <span><span className="cost-key cost-key-llm" />Compute tokens</span>
              <span><span className="cost-key cost-key-embed" />Embeddings</span>
              <span><span className="cost-key cost-key-api" />Grounding tokens</span>
            </div>
          </div>
        </section>

      </main>
    </div>
  );
}

window.CostView = CostView;
