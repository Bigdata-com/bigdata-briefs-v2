// History / Archive view — calendar-style timeline of past briefs
const { useState: useStateH, useMemo: useMemoH, useEffect: useEffectH, useCallback: useCallbackH } = React;

function ArchiveView({ tweaks }) {
  const companies = window.DATA?.companies || [];
  const initialId = window.DATA.todaysBrief?.entityId || companies[0]?.id;

  const [selectedId, setSelectedId] = useStateH(initialId);
  const [search, setSearch] = useStateH("");
  const [sortMode, setSortMode] = useStateH("recent");
  const [expandedRunId, setExpandedRunId] = useStateH(null);
  const [historyData, setHistoryData] = useStateH({
    entityId: initialId,
    entityName: window.DATA.todaysBrief?.entityName || "",
    ticker: window.DATA.todaysBrief?.ticker || "",
    history: window.DATA.history || [],
    pulse: window.DATA.pulse || [],
  });
  const [loading, setLoading] = useStateH(false);
  // Cache runs count per company from companySummaries
  const summaries = window.DATA.companySummaries || {};

  function loadCompany(id) {
    if (id === historyData.entityId && !loading) return;
    setSelectedId(id);
    setLoading(true);
    fetch(`/api/frontend/entity/${id}/history`)
      .then(r => r.json())
      .then(d => {
        setHistoryData({
          entityId: d.entityId,
          entityName: d.entityName,
          ticker: d.ticker,
          history: d.history || [],
          pulse: d.pulse || [],
        });
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }

  const filtered = companies.filter(c =>
    c.name.toLowerCase().includes(search.toLowerCase()) ||
    c.ticker.toLowerCase().includes(search.toLowerCase())
  );

  const history = sortMode === "activity"
    ? [...historyData.history].sort((a, b) => b.saved - a.saved)
    : historyData.history;

  const grouped = useMemoH(() => {
    // Group by date first, then by month
    const byDate = {};
    history.forEach(h => { (byDate[h.date] = byDate[h.date] || []).push(h); });

    const out = {};
    Object.entries(byDate).forEach(([date, runs]) => {
      const d = new Date(date + "T00:00:00Z");
      const monthKey = d.toLocaleDateString("en-US", { month: "long", year: "numeric", timeZone: "UTC" });
      (out[monthKey] = out[monthKey] || []).push({ date, runs });
    });
    return out;
  }, [history]);

  return (
    <div className="archive-layout">
      <aside className="archive-side">
        <div className="t-cap">Coverage Universe</div>
        <input
          className="archive-search"
          type="text"
          placeholder="Search company or ticker…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <ul className="archive-companies">
          {filtered.map(c => {
            const s = summaries[c.id] || {};
            const runs = c.id === historyData.entityId
              ? historyData.history.length
              : s.totalRuns ?? "—";
            return (
              <li key={c.id}>
                <button
                  className={"archive-company-btn" + (c.id === selectedId ? " active" : "")}
                  onClick={() => loadCompany(c.id)}
                  disabled={loading}
                >
                  <span className="ac-runs">{runs !== "—" ? `${runs} ${runs === 1 ? "day" : "days"}` : "—"}</span>
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
          <div className="t-cap">{_tk(historyData.ticker)} · {companies.find(c => c.id === historyData.entityId)?.industry || ""}</div>
          <h1 className="archive-title display">
            {loading ? <span style={{ color: "var(--ink-faint)", fontStyle: "italic" }}>Loading…</span> : historyData.entityName}
          </h1>
          <p className="archive-subtitle">
            All briefs filed for this entity. {history.length} runs across the last {history.length} days.
          </p>
        </header>

        <div className="archive-toolbar">
          <span><strong style={{ color: "var(--ink)", fontWeight: 600 }}>{history.length}</strong> briefs</span>
          <span>·</span>
          <span><strong style={{ color: "var(--ink)", fontWeight: 600 }}>{history.reduce((s, h) => s + h.saved, 0)}</strong> bullets saved</span>
          <span>·</span>
          <span><strong style={{ color: "var(--ink)", fontWeight: 600 }}>{history.reduce((s, h) => s + h.discarded, 0)}</strong> discarded</span>
          <span className="toolbar-spacer"></span>
          <span className="toolbar-toggle">
            <button className={sortMode === "recent" ? "active" : ""} onClick={() => setSortMode("recent")}>Recent</button>
            <button className={sortMode === "activity" ? "active" : ""} onClick={() => setSortMode("activity")}>By Activity</button>
          </span>
        </div>

        <div className="archive-timeline">
          {Object.entries(grouped).map(([month, items]) => (
            <React.Fragment key={month}>
              <div style={{ padding: "20px 0 8px", display: "flex", alignItems: "baseline", gap: 12, borderTop: "1px solid var(--rule)", marginTop: 8 }}>
                <span className="t-cap" style={{ fontSize: 11 }}>{month}</span>
                <span style={{ flex: 1, height: 1, background: "var(--rule)" }}></span>
                <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-faint)" }}>
                  {items.length} briefs · {items.reduce((s, i) => s + i.saved, 0)} saved
                </span>
              </div>
              {items.map(({ date, runs }) => {
                const d = new Date(date + "T00:00:00Z");
                const day = d.getUTCDate();
                const month3 = d.toLocaleDateString("en-US", { month: "short", timeZone: "UTC" });
                const wd = d.toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" });
                const isMulti = runs.length > 1;
                const totalSaved     = runs.reduce((s, r) => s + r.saved, 0);
                const totalDiscarded = runs.reduce((s, r) => s + r.discarded, 0);

                return (
                  <article key={date} className={"archive-day" + (isMulti ? " archive-day-multi" : "")}>
                    <div className="archive-day-date">
                      <div className="archive-day-num">{String(day).padStart(2, "0")}</div>
                      <div className="archive-day-month">{month3}</div>
                      <div className="archive-day-weekday">{wd}</div>
                    </div>
                    <div className="archive-day-content">
                      {isMulti && (
                        <div className="archive-meta archive-meta-day-total">
                          <span>{runs.length} runs</span>
                          <span>·</span>
                          <span>{totalSaved} saved</span>
                          <span>·</span>
                          <span>{totalDiscarded} discarded</span>
                        </div>
                      )}
                      {runs.map(entry => (
                        <div key={entry.runId} className={"archive-run" + (isMulti ? " archive-run-sub" : "")}>
                          <h2 className="archive-headline">
                            {entry.narrative || entry.bullets?.[0]?.text || "No material developments"}
                          </h2>
                          <div className="archive-meta">
                            <span style={{ fontFamily: "var(--mono)", textTransform: "none", letterSpacing: 0 }}>run-{entry.runId}</span>
                            <span>·</span>
                            <span>{entry.saved} saved</span>
                            <span>·</span>
                            <span>{entry.discarded} discarded</span>
                            {entry.themes?.length > 0 && (
                              <>
                                <span>·</span>
                                <span className="meta-themes">
                                  {entry.themes.map(t => (
                                    <span key={t} className="meta-theme"><ThemeDot theme={t} />&nbsp;{t}</span>
                                  ))}
                                </span>
                              </>
                            )}
                          </div>
                          {entry.bullets?.length > 0 && (
                            <button
                              className="archive-expand-btn"
                              onClick={() => setExpandedRunId(expandedRunId === entry.runId ? null : entry.runId)}
                            >
                              {expandedRunId === entry.runId ? `▴ hide bullets` : `▾ ${entry.bullets.length} bullet${entry.bullets.length !== 1 ? "s" : ""}`}
                            </button>
                          )}
                          {expandedRunId === entry.runId && entry.bullets?.length > 0 && (
                            <ol className="archive-bullets-list">
                              {entry.bullets.map((b, i) => (
                                <li key={i} className="archive-bullet-item">
                                  {b.theme && <span className="archive-bullet-theme"><ThemeDot theme={b.theme} />&nbsp;{b.theme}</span>}
                                  <p className="archive-bullet-text">{b.text}</p>
                                </li>
                              ))}
                            </ol>
                          )}
                        </div>
                      ))}
                    </div>
                  </article>
                );
              })}
            </React.Fragment>
          ))}
          {history.length === 0 && !loading && (
            <p style={{ color: "var(--ink-mute)", fontStyle: "italic", marginTop: 32 }}>No briefs found for this entity.</p>
          )}
        </div>
      </div>
    </div>
  );
}

window.ArchiveView = ArchiveView;
