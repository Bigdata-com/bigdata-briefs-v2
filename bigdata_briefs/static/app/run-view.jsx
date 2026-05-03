// Run view — Compose, Launch, and Watch a brief pipeline run.
// Layout: 3 columns — recent runs sidebar (left), compose form (center), live console (right).
// On launch, the form collapses and the console expands to fill.

const { useState: useStateR, useEffect: useEffectR, useRef: useRefR, useMemo: useMemoR } = React;

/**
 * Single definition for Compose strip + live console (synthetic until API exposes real steps).
 * Each step: strip label === console stage name (≤11 chars for column), same startSec for active strip.
 */
const BRIEF_RUN_STEPS = [
  {
    id: "setup",
    label: "Setup",
    startSec: 0,
    events: [
      { minSec: 0, ts: "00.0", msg: (c) => `Run queued for ${c.entity.name}.` },
      { minSec: 1, ts: "01.0", msg: (c) => `Company context loaded from the knowledge graph.` },
    ],
  },
  {
    id: "search",
    label: "Search",
    startSec: 3,
    events: [
      { minSec: 3, ts: "03.0", msg: (c) => `Wide search across sources: ${c.sources.join(", ")}.` },
      { minSec: 8, ts: "08.0", msg: () => `Thematic queries running in parallel.` },
    ],
  },
  {
    id: "draft",
    label: "Draft",
    startSec: 15,
    events: [{ minSec: 15, ts: "15.0", msg: () => `Drafting candidate bullet points.` }],
  },
  {
    id: "facts",
    label: "Fact check",
    startSec: 25,
    events: [{ minSec: 25, ts: "25.0", msg: () => `Checking citations and quotes against the sources.` }],
  },
  {
    id: "similar",
    label: "Similarity",
    startSec: 35,
    events: [{ minSec: 35, ts: "35.0", msg: () => `Comparing to recent work so obvious repeats are caught early.` }],
  },
  {
    id: "web",
    label: "Web check",
    startSec: 45,
    events: [
      { minSec: 45, ts: "45.0", msg: () => `Fresh web evidence — planning queries and fetching results.` },
      { minSec: 52, ts: "52.0", msg: () => `Deciding what to keep, tighten, or drop from that evidence.` },
    ],
  },
  {
    id: "finish",
    label: "Wrap up",
    startSec: 58,
    events: [{ minSec: 58, ts: "58.0", msg: () => `Merging themes and building the report.` }],
  },
];

function briefRunStripLabels() {
  return BRIEF_RUN_STEPS.map((s) => s.label);
}

function briefRunActiveStripIndex(elapsedSec, mode) {
  const n = BRIEF_RUN_STEPS.length;
  if (mode === "done") return n;
  let idx = 0;
  for (let i = n - 1; i >= 0; i--) {
    if (elapsedSec >= BRIEF_RUN_STEPS[i].startSec) {
      idx = i;
      break;
    }
  }
  return idx;
}

/** Console stage column: fixed width 11. */
function briefRunStageColumn(label) {
  const s = label.length > 11 ? label.slice(0, 11) : label;
  return s.padEnd(11, " ");
}

/** Matches POST /api/frontend/run: incremental vs explicit reporting dates. */
function composeWindowSummary(cfg) {
  if (cfg.window === "custom") return `${cfg.customStart} → ${cfg.customEnd}`;
  return "Today / since last run";
}

/**
 * Optional `window.RUN_DATA.composeEstimates` from `/api/frontend/run-data.json`:
 * - costDisplay, costFoot (strings)
 * - latencyDisplay (string, e.g. "4–5m") OR latencyMin + latencyMax (numbers, minutes)
 * - latencyFoot (string)
 * When fields are missing, Compose falls back to model + window heuristics.
 */
function resolveComposeEstimates(RD, model, config, cost, minTime, maxTime) {
  const allEst = RD.composeEstimates && typeof RD.composeEstimates === "object" ? RD.composeEstimates : {};
  const entityId = config && config.entity && config.entity.id;
  // allEst is keyed by entity_id; fall back to allEst itself for backward compat
  const est = (entityId && allEst[entityId] && typeof allEst[entityId] === "object")
    ? allEst[entityId]
    : (allEst.costDisplay != null ? allEst : {});
  const displayCost =
    est.costDisplay != null && String(est.costDisplay).trim() !== ""
      ? String(est.costDisplay)
      : `$${cost}`;
  const displayCostFoot =
    est.costFoot != null && String(est.costFoot).trim() !== ""
      ? String(est.costFoot)
      : `${model.label.toLowerCase()} · ${config.sources.length} src`;
  const displayLatencyFoot =
    est.latencyFoot != null && String(est.latencyFoot).trim() !== ""
      ? String(est.latencyFoot)
      : composeWindowSummary(config);

  let latencyMainForHero = null;
  let latencyLaunchStr = null;
  if (est.latencyDisplay != null && String(est.latencyDisplay).trim() !== "") {
    const ld = String(est.latencyDisplay);
    latencyMainForHero = <dd className="cost-num tnum">{ld}</dd>;
    latencyLaunchStr = ld;
  } else if (est.latencyMin != null && est.latencyMax != null) {
    const lmin = Math.round(Number(est.latencyMin));
    const lmax = Math.round(Number(est.latencyMax));
    if (!Number.isNaN(lmin) && !Number.isNaN(lmax)) {
      latencyMainForHero = (
        <dd className="cost-num tnum">
          {lmin}
          <span className="compose-estimate-sep">–</span>
          {lmax}
          <span className="compose-estimate-unit">m</span>
        </dd>
      );
      latencyLaunchStr = `${lmin}–${lmax}m`;
    }
  }
  if (!latencyMainForHero) {
    latencyMainForHero = (
      <dd className="cost-num tnum">
        1
        <span className="compose-estimate-sep">–</span>
        2
        <span className="compose-estimate-unit">m</span>
      </dd>
    );
    latencyLaunchStr = "1–2m";
  }

  return {
    displayCost,
    displayCostFoot,
    displayLatencyFoot,
    latencyMainForHero,
    latencyLaunchStr,
  };
}

function buildBriefRunConsoleLines(now, mode, config, result, error) {
  const l = [];
  if (mode === "compose") return l;
  for (const step of BRIEF_RUN_STEPS) {
    for (const ev of step.events) {
      if (now >= ev.minSec) {
        l.push({
          ts: ev.ts,
          stage: step.id,
          stageLabel: step.label,
          msg: typeof ev.msg === "function" ? ev.msg(config) : ev.msg,
        });
      }
    }
  }
  if (result) {
    l.push({
      ts: "--.-",
      stage: "brief",
      stageLabel: "Brief",
      msg: `Brief saved · ${result.bullets_saved} bullet${result.bullets_saved === 1 ? "" : "s"}.`,
    });
    l.push({
      ts: "--.-",
      stage: "story",
      stageLabel: "Story",
      msg: result.narrative
        ? `Opening story line · ${result.narrative.split(" ").length} words.`
        : "Opening story line skipped (no bullets).",
    });
    l.push({
      ts: "--.-",
      stage: "done",
      stageLabel: "Done",
      msg: `Finished in ${result.duration_sec}s.`,
    });
  }
  if (error) {
    l.push({ ts: "--.-", stage: "error", stageLabel: "Error", msg: error });
  }
  return l;
}

// ── Mode tabs ─────────────────────────────────────────────────────
function RunView({ tweaks }) {
  const RD = window.RUN_DATA && typeof window.RUN_DATA === "object" ? window.RUN_DATA : {};
  const composePicklist = useMemoR(() => {
    const d = window.DATA && typeof window.DATA === "object" ? window.DATA : {};
    if (Array.isArray(d.composeEntities)) return d.composeEntities;
    return Array.isArray(d.companies) ? d.companies : [];
  }, []);
  const firstEntity = composePicklist[0] || {
    id: "",
    name: "No companies loaded",
    ticker: "—",
    industry: "",
    country: "",
  };
  const defaultSources = (Array.isArray(RD.sources) ? RD.sources : []).filter(s => s.checked).map(s => s.id);
  const sources0 = defaultSources.length > 0 ? defaultSources : ["news"];
  const modelsList = Array.isArray(RD.models) ? RD.models : [];
  const defaultModelId = modelsList.find(m => m.id === "balanced")?.id || modelsList[0]?.id || "balanced";

  const today = new Date().toISOString().slice(0, 10);
  const [mode, setMode] = useStateR("compose"); // compose | running | done
  const [config, setConfig] = useStateR({
    entity: firstEntity,
    model: defaultModelId,
    themes: [],
    themesAuto: true,
    window: "24h",
    customStart: today,
    customEnd: today,
    sources: sources0,
    novelty: window.DATA?.noveltyDays ?? 30,
  });
  const [runId, setRunId] = useStateR(null);
  const [runResult, setRunResult] = useStateR(null);
  const [runError, setRunError] = useStateR(null);
  const [now, setNow] = useStateR(0); // elapsed seconds (for log animation)
  const pollRef = useRefR(null);

  // Elapsed timer while running
  useEffectR(() => {
    if (mode !== "running") { setNow(0); return; }
    const t0 = performance.now();
    const iv = setInterval(() => setNow((performance.now() - t0) / 1000), 500);
    return () => clearInterval(iv);
  }, [mode]);

  // Poll run status every 2s
  useEffectR(() => {
    if (!runId || mode !== "running") return;
    pollRef.current = setInterval(() => {
      fetch(`/api/frontend/run/${runId}`)
        .then(r => r.json())
        .then(data => {
          if (data.status === "succeeded" || data.status === "no_data") {
            clearInterval(pollRef.current);
            setRunResult(data);
            setMode("done");
          } else if (data.status === "failed") {
            clearInterval(pollRef.current);
            setRunError(data.error || "Run failed");
            setMode("done");
          }
          // "running" or "queued" → keep polling
        })
        .catch(() => {});
    }, 2000);
    return () => clearInterval(pollRef.current);
  }, [runId, mode]);

  const launch = () => {
    setRunId(null);
    setRunResult(null);
    setRunError(null);
    setMode("running");
    fetch("/api/frontend/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        entity_id: config.entity.id,
        window: config.window,
        custom_start: config.window === "custom" ? config.customStart : undefined,
        custom_end:   config.window === "custom" ? config.customEnd   : undefined,
        source_categories: config.sources,
      }),
    })
      .then(r => r.json())
      .then(data => { if (data.run_id) setRunId(data.run_id); })
      .catch(err => { setRunError(String(err)); setMode("done"); });
  };

  const reset = () => {
    setMode("compose");
    setRunId(null);
    setRunResult(null);
    setRunError(null);
  };

  if (!composePicklist.length) {
    return (
      <div className="run-layout" style={{ padding: "32px 24px", fontFamily: "var(--sans)" }}>
        <p style={{ color: "var(--ink-mute)", maxWidth: 520 }}>
          Compose needs at least one entity that appears in the Top US 100 universe (first nine in list order).
          Seed orchestration for those tickers, then reload.
        </p>
      </div>
    );
  }

  return (
    <div className="run-layout">
      {/* ── Left: recent runs ── */}
      <aside className="run-side">
        <RecentRuns mode={mode} liveElapsed={now} config={config} result={runResult} />
      </aside>

      {/* ── Center: compose / running header ── */}
      <main className="run-main">
        {mode === "compose" && (
          <ComposeForm
            config={config}
            setConfig={setConfig}
            onLaunch={launch}
            entityPicklist={composePicklist}
          />
        )}
        {mode !== "compose" && (
          <RunHeader
            config={config}
            now={now}
            mode={mode}
            result={runResult}
            error={runError}
            onReset={reset}
          />
        )}
      </main>

      {/* ── Right: live console ── */}
      <section className="run-console">
        <LiveConsole
          mode={mode}
          now={now}
          config={config}
          result={runResult}
          error={runError}
        />
      </section>
    </div>
  );
}

// ── Compose form ──────────────────────────────────────────────────
function formatComposeHeroDateline() {
  const now = new Date();
  const day = now.getUTCDate();
  const month = now.toLocaleDateString("en-US", { month: "long", timeZone: "UTC" });
  const year = now.getUTCFullYear();
  const timePart = now.toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  });
  return `Compose · ${day} ${month} ${year} · ${timePart} UTC`;
}

function ComposeForm({ config, setConfig, onLaunch, entityPicklist }) {
  const RD = window.RUN_DATA && typeof window.RUN_DATA === "object" ? window.RUN_DATA : {};
  const picks = Array.isArray(entityPicklist) ? entityPicklist : [];
  const [composeDateline, setComposeDateline] = useStateR(formatComposeHeroDateline);
  const [entitySearch, setEntitySearch] = useStateR("");
  useEffectR(() => {
    const tick = () => setComposeDateline(formatComposeHeroDateline());
    tick();
    const id = setInterval(tick, 15_000);
    return () => clearInterval(id);
  }, []);
  const composeSearchIdSet = useMemoR(() => {
    const raw = window.DATA?.composeSearchEntityIds;
    if (!Array.isArray(raw) || raw.length === 0) return null;
    return new Set(raw);
  }, []);
  const filteredEntities = useMemoR(() => {
    const q = entitySearch.trim().toLowerCase();
    const all = Array.isArray(window.DATA?.companies) ? window.DATA.companies : [];
    if (!q) return picks;
    const pool = composeSearchIdSet
      ? all.filter(c => composeSearchIdSet.has(c.id))
      : all;
    return pool
      .filter(
        c => c.name.toLowerCase().includes(q) || (c.ticker && c.ticker.toLowerCase().includes(q))
      )
      .sort((a, b) => (a.name || "").localeCompare(b.name || "", undefined, { sensitivity: "base" }));
  }, [entitySearch, picks, composeSearchIdSet]);

  const models = Array.isArray(RD.models) ? RD.models : [];
  const model = models.find(m => m.id === config.model) || models[0] || {
    id: "balanced", label: "Balanced", desc: "", cost: 0.42, time: "4–5m",
  };
  // Cost preview: depends on model + window + concurrency + sources
  const winMult = config.window === "custom" ? 5.5 : 1;
  const srcMult = 0.6 + 0.05 * (config.sources?.length || 0);
  const themeMult = config.themesAuto ? 1 : Math.max(0.4, (config.themes?.length || 0) / 5);
  const cost = (model.cost * winMult * srcMult * themeMult).toFixed(2);
  const timeStr = String(model.time || "4–5m");
  const timeParts = timeStr.split(/[-–]/);
  const minT = parseInt(timeParts[0], 10) || 4;
  const maxT = parseInt(timeParts[1] || String(minT), 10) || minT;
  const minTime = Math.round(minT * winMult * themeMult);
  const maxTime = Math.round(maxT * winMult * themeMult);

  const {
    displayCost,
    displayCostFoot,
    displayLatencyFoot,
    latencyMainForHero,
    latencyLaunchStr,
  } = resolveComposeEstimates(RD, model, config, cost, minTime, maxTime);

  const toggleTheme = (t) => {
    setConfig(c => ({
      ...c,
      themes: c.themes.includes(t) ? c.themes.filter(x => x !== t) : [...c.themes, t]
    }));
  };
  const toggleSource = (id) => {
    setConfig(c => ({
      ...c,
      sources: c.sources.includes(id) ? c.sources.filter(x => x !== id) : [...c.sources, id]
    }));
  };

  return (
    <div className="compose-wrap">
      <header className="compose-hero">
        <div className="dateline">{composeDateline}</div>
        <h1 className="display compose-title">
          New <em>Brief</em>.
        </h1>
        <p className="compose-standfirst">
          Configure a single-entity run. It will search sources, draft bullets, check facts,
          compare to the last <strong>{config.novelty} days</strong> for repetition, verify on the web,
          then wrap up your morning note.
        </p>
        <dl className="compose-estimate" aria-label="Live estimate">
          <div className="compose-estimate-cell">
            <dt className="t-cap">Est. cost</dt>
            <dd className="cost-num tnum">{displayCost}</dd>
            <span className="compose-estimate-foot">{displayCostFoot}</span>
          </div>
          <div className="compose-estimate-cell">
            <dt className="t-cap">Est. latency</dt>
            {latencyMainForHero}
            <span className="compose-estimate-foot">{displayLatencyFoot}</span>
          </div>
        </dl>
      </header>

      <div className="compose-form">
        {/* Entity */}
        <section className="compose-section">
          <div className="compose-section-head">
            <span className="compose-step-num">01</span>
            <div>
              <h2 className="compose-section-title">Entity</h2>
              <p className="compose-section-desc">Pick a company to analyze.</p>
            </div>
          </div>
          <div className="compose-section-body">
            <input
              type="text"
              className="compose-input"
              placeholder="Search companies (all universes on the desk)…"
              value={entitySearch}
              onChange={(e) => setEntitySearch(e.target.value)}
            />
            <div className="entity-grid">
              {filteredEntities.map(c => (
                <button
                  key={c.id}
                  className={"entity-pick" + (c.id === config.entity.id ? " active" : "")}
                  onClick={() => setConfig({ ...config, entity: c })}
                >
                  <span className="entity-pick-ticker">{c.ticker}</span>
                  <span className="entity-pick-name">{c.name}</span>
                  <span className="entity-pick-meta">{c.industry} · {c.country}</span>
                </button>
              ))}
            </div>
          </div>
        </section>

        {/* Window */}
        <section className="compose-section">
          <div className="compose-section-head">
            <span className="compose-step-num">02</span>
            <div>
              <h2 className="compose-section-title">Window</h2>
              <p className="compose-section-desc">
                Default follows today on the desk. Use custom dates when you want a specific day or range.
              </p>
            </div>
          </div>
          <div className="compose-section-body">
            <div className="seg">
              {[
                {
                  id: "24h",
                  label: "Today / since last run",
                  sub: "Automatic — the usual daily slice for this company",
                },
                { id: "custom", label: "Custom dates", sub: "You pick the calendar days" },
              ].map(o => (
                <button
                  key={o.id}
                  className={"seg-btn" + (config.window === o.id ? " active" : "")}
                  onClick={() => setConfig({ ...config, window: o.id })}
                >
                  <span className="seg-label">{o.label}</span>
                  <span className="seg-sub">{o.sub}</span>
                </button>
              ))}
            </div>
            {config.window === "custom" && (
              <div className="custom-range">
                <label>From <input type="date" value={config.customStart} onChange={(e) => setConfig({ ...config, customStart: e.target.value })} /></label>
                <label>To <input type="date" value={config.customEnd} onChange={(e) => setConfig({ ...config, customEnd: e.target.value })} /></label>
              </div>
            )}
          </div>
        </section>

        {/* Themes */}
        {/* Sources */}
        <section className="compose-section">
          <div className="compose-section-head">
            <span className="compose-step-num">03</span>
            <div>
              <h2 className="compose-section-title">Sources</h2>
              <p className="compose-section-desc">Where the retriever should look. Off by default: social and blogs.</p>
            </div>
          </div>
          <div className="compose-section-body">
            <div className="source-grid">
              {(Array.isArray(RD.sources) ? RD.sources : []).map(s => (
                <label key={s.id} className={"source-check" + (config.sources.includes(s.id) ? " active" : "")}>
                  <input
                    type="checkbox"
                    checked={config.sources.includes(s.id)}
                    onChange={() => toggleSource(s.id)}
                  />
                  <span className="source-check-box"></span>
                  <span>{s.label}</span>
                </label>
              ))}
            </div>
          </div>
        </section>

      </div>

      {/* ── Sticky launch bar ── */}
      <div className="launch-bar">
        <div className="launch-summary">
          <div className="launch-summary-line">
            <span className="t-cap">Brief</span>
            <strong>{config.entity.name}</strong>
            <span className="muted">·</span>
            <span>{config.entity.ticker}</span>
            <span className="muted">·</span>
            <span>{composeWindowSummary(config)}</span>
            <span className="muted">·</span>
            <span>{config.sources.length} sources</span>
          </div>
        </div>
        <div className="launch-cost">
          <div>
            <span className="t-cap">Est. cost</span>
            <span className="cost-num tnum">{displayCost}</span>
          </div>
          <div>
            <span className="t-cap">Est. time</span>
            <span className="cost-num tnum">{latencyLaunchStr}</span>
          </div>
        </div>
        <button className="launch-btn" onClick={onLaunch} disabled title="Launch from Compose is disabled.">
          ▶&nbsp; Launch run (disabled)
        </button>
      </div>
    </div>
  );
}

// ── Run header (during/after run) ────────────────────────────────
function RunHeader({ config, now, mode, result, error, onReset }) {
  const stages = briefRunStripLabels();
  const activeStage = briefRunActiveStripIndex(now, mode);

  const bullets = (result && result.bullets) || [];
  const narrative = result && result.narrative;

  return (
    <div className="runhead-wrap">
      <header className="runhead-hero">
        <div className="runhead-meta">
          <span className="dateline">{mode === "running" ? "Running · live" : error ? "Run failed" : "Run complete"}</span>
          {result && <span className="runhead-runid t-mono">run-{result.run_id?.slice(0, 8)}</span>}
        </div>
        <h1 className="display runhead-title">
          {config.entity.name} <em>·</em> {config.entity.ticker}
        </h1>
        <div className="runhead-sub">
          {mode === "running" && (
            <React.Fragment>
              <span className="live-dot"></span>
              <span><strong className="tnum">{now.toFixed(0)}s</strong> elapsed</span>
              <span className="muted">· pipeline running</span>
            </React.Fragment>
          )}
          {mode === "done" && result && !error && (
            <React.Fragment>
              <span style={{ color: "var(--novel)" }}>✓</span>
              <span><strong className="tnum">{result.duration_sec}s</strong></span>
              <span className="muted">·</span>
              <span><strong className="tnum">{result.bullets_saved}</strong> saved · <strong className="tnum">{result.bullets_discarded}</strong> discarded</span>
            </React.Fragment>
          )}
          {mode === "done" && error && (
            <span style={{ color: "var(--discard)" }}>✕ {error}</span>
          )}
        </div>
      </header>

      {/* Stage progress strip */}
      <div className="stage-strip">
        {stages.map((label, i) => {
          const state = mode === "done" && !error ? "done" : i < activeStage ? "done" : i === activeStage ? "running" : "pending";
          return (
            <div key={i} className={"stage-step state-" + state}>
              <div className="stage-step-num">{String(i + 1).padStart(2, "0")}</div>
              <div className="stage-step-label">{label}</div>
              <div className="stage-step-bar"><div className="stage-step-fill"></div></div>
            </div>
          );
        })}
      </div>

      {/* Narrative + bullets when done */}
      <div className="stream-section">
        <div className="ops-section-head">
          <h2>{mode === "running" ? "Bullets · live" : "Results"}</h2>
          {result && <span className="ops-section-meta">{result.bullets_saved} saved · {result.bullets_discarded} discarded</span>}
        </div>
        <div className="stream-list">
          {mode === "running" && (
            <div className="stream-empty">
              <span className="t-cap">Pipeline running</span>
              <p>Results will appear here once the run completes.</p>
            </div>
          )}
          {mode === "done" && narrative && (
            <div style={{ padding: "12px 0 20px", borderBottom: "1px solid var(--rule)", marginBottom: 16 }}>
              <div className="t-cap" style={{ marginBottom: 8, color: "var(--accent)" }}>Today's narrative</div>
              <p style={{ fontFamily: "var(--serif)", fontSize: 17, fontStyle: "italic", color: "var(--ink-soft)", margin: 0 }}>
                {narrative}
              </p>
            </div>
          )}
          {mode === "done" && bullets.map((b, i) => (
            <article key={b.id || i} className={"stream-bullet stream-bullet-" + b.novelty}>
              <div className="stream-bullet-side">
                <span className="stream-bullet-num tnum">{String(i + 1).padStart(2, "0")}</span>
                <span className="stream-bullet-theme"><ThemeDot theme={b.theme} />{b.theme}</span>
                {b.novelty === "rewritten" && <span className="bullet-novelty-tag">rewritten</span>}
              </div>
              <div className="stream-bullet-body">
                <p>{b.text}</p>
                {b.rewriteReason && <div className="rewrite-note"><span className="t-cap" style={{ color: "var(--rewrite)" }}>note</span><span className="rewrite-reason">{b.rewriteReason}</span></div>}
              </div>
            </article>
          ))}
        </div>
      </div>

      {/* Result CTA */}
      {mode === "done" && (
        <div className="run-done-cta">
          <button className="btn btn-primary">Open the brief →</button>
          <button className="btn" onClick={onReset}>Compose another</button>
        </div>
      )}
    </div>
  );
}

// ── Live console (right column) ──────────────────────────────────
function LiveConsole({ mode, now, config, result, error }) {
  const consoleRef = useRefR(null);

  const lines = useMemoR(
    () => buildBriefRunConsoleLines(now, mode, config, result, error),
    [
      mode,
      Math.floor(now),
      result,
      error,
      config.entity.id,
      config.entity.name,
      config.sources,
      config.window,
      config.customStart,
      config.customEnd,
    ]
  );

  const stageColor = {
    setup: "var(--ink-mute)",
    search: "var(--running)",
    draft: "var(--accent)",
    facts: "var(--rewrite)",
    similar: "var(--novel)",
    web: "var(--novel)",
    finish: "var(--ink)",
    brief: "var(--ink)",
    story: "var(--accent)",
    done: "var(--novel)",
    error: "var(--discard)",
  };

  useEffectR(() => {
    if (consoleRef.current) consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
  }, [lines.length]);

  if (mode === "compose") {
    return (
      <div className="console-shell">
        <div className="console-head">
          <span className="console-led led-idle"></span>
          <span className="t-cap">Console</span>
          <span className="muted t-cap" style={{ marginLeft: "auto" }}>idle</span>
        </div>
        <div className="console-body console-body-empty">
          <pre className="console-art">{`
        ┌─────────────────────┐
        │  brief.run() ready  │
        └─────────────────────┘

  configure → launch when ready
`}</pre>
        </div>
        <div className="console-foot"><span className="t-mono muted">awaiting launch</span></div>
      </div>
    );
  }

  return (
    <div className="console-shell">
      <div className="console-head">
        <span className={"console-led " + (mode === "running" ? "led-running" : error ? "led-error" : "led-done")}></span>
        <span className="t-cap">Console · {config.entity.ticker}</span>
        <span className="muted t-mono" style={{ marginLeft: "auto", fontSize: 10 }}>
          {result ? `run-${result.run_id?.slice(0, 8)}` : "queued"}
        </span>
      </div>
      <div className="console-body" ref={consoleRef}>
        {lines.map((e, i) => (
          <div key={i} className="console-line">
            <span className="console-ts tnum">{e.ts}</span>
            <span className="console-stage" style={{ color: stageColor[e.stage] || "var(--ink-mute)" }}>
              {briefRunStageColumn(e.stageLabel)}
            </span>
            <span className="console-msg">{e.msg}</span>
          </div>
        ))}
        {mode === "running" && (
          <div className="console-line console-cursor">
            <span className="console-ts tnum">{now.toFixed(0).padStart(4, "0")}</span>
            <span className="console-blink">▊</span>
          </div>
        )}
      </div>
      <div className="console-foot">
        <span className="t-mono muted">{lines.length} events · {now.toFixed(0)}s</span>
        <span className="t-mono muted" style={{ marginLeft: "auto" }}>
          {mode === "done" ? (error ? "exit 1 · failed" : "exit 0 · ok") : "live"}
        </span>
      </div>
    </div>
  );
}

// ── Recent runs sidebar ──────────────────────────────────────────
function RecentRuns({ mode, liveElapsed, config, result }) {
  const recent = Array.isArray(window.RUN_DATA?.recent) ? window.RUN_DATA.recent : [];
  return (
    <div>
      <div className="t-cap" style={{ marginBottom: 14 }}>Recent runs</div>

      {/* Currently composing/running placeholder */}
      <div className={"recent-active " + (mode === "running" ? "is-running" : mode === "done" ? "is-done" : "is-composing")}>
        <div className="recent-active-head">
          <span className="t-cap" style={{ color: "var(--accent)" }}>
            {mode === "compose" ? "Drafting" : mode === "running" ? "Live" : "Just finished"}
          </span>
          {mode === "running" && <span className="live-dot"></span>}
          {mode === "done" && <span style={{ color: "var(--novel)" }}>✓</span>}
        </div>
        <div className="recent-active-name">{config.entity.name}</div>
        <div className="recent-active-meta">
          <span className="t-mono">{config.entity.ticker}</span>
          <span> · </span>
          <span>{composeWindowSummary(config)}</span>
          {mode === "running" && <span> · {liveElapsed.toFixed(0)}s</span>}
          {mode === "done" && result && <span> · {result.duration_sec}s · {result.bullets_saved} saved</span>}
          {mode === "done" && !result && <span> · done</span>}
        </div>
      </div>

      <div className="recent-list">
        {recent.map(r => (
          <button key={r.id} className={"recent-item recent-item-" + r.status}>
            <div className="recent-item-row1">
              <span className={"recent-item-status status-" + r.status}>
                {r.status === "running" ? "●" : r.status === "succeeded" ? "✓" : "✕"}
              </span>
              <span className="recent-item-ticker">{r.ticker}</span>
              <span className="recent-item-time">{r.started}</span>
            </div>
            <div className="recent-item-name">{r.entity}</div>
            <div className="recent-item-meta">
              {r.status === "failed" ? (
                <span className="recent-item-err">{r.error}</span>
              ) : (
                <React.Fragment>
                  <span>{Math.round(r.elapsed)}s</span>
                  <span> · </span>
                  <span>{r.saved} saved</span>
                  {r.discarded > 0 && (
                    <React.Fragment>
                      <span> · </span>
                      <span className="muted">{r.discarded} cut</span>
                    </React.Fragment>
                  )}
                </React.Fragment>
              )}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

window.RunView = RunView;
