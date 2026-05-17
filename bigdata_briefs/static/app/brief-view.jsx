// ── Brief reading view ──────────────────────────────────────────────
// The morning desk note. Hero brief with a left rail of "Today's
// front page" — companies that produced material updates today.

/** When false, earnings release labels and earnings-based roster ordering are off. */
const BRIEF_SHOW_EARNINGS_RELEASE_INFO = false;

function _parseWindowParts(iso) {
  if (!iso) return { weekday: "—", monShort: "—", day: "—", time: "—" };
  const zone = _tzIana();
  const d = new Date(iso);
  return {
    weekday:  d.toLocaleDateString("en-US", { weekday: "short", timeZone: zone }),
    monShort: d.toLocaleDateString("en-US", { month: "short", timeZone: zone }),
    day:      parseInt(d.toLocaleDateString("en-US", { day: "numeric", timeZone: zone }), 10),
    time:     d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: zone, hour12: true }),
  };
}

function _fmtDur(start, end) {
  if (!start || !end) return "—";
  const ms = new Date(end) - new Date(start);
  if (ms < 0) return "—";
  const mins = Math.round(ms / 60000);
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h < 1) return `${m}m`;
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function BriefWindowBand({ start, end }) {
  const s = _parseWindowParts(start);
  const e = _parseWindowParts(end);
  return (
    <div className="cw-v6">
      <div className="cw-v6-plate">
        <div className="cw-v6-plate-main">Timespan</div>
        <div className="cw-v6-plate-sub">{_tzLong()}</div>
      </div>
      <div className="cw-v6-content">
        <div className="cw-v6-stamp">
          <div className="cw-v6-stamp-row">
            <span className="cw-v6-stamp-date">{s.weekday}, {s.monShort} {s.day}</span>
            <span className="cw-v6-stamp-time tnum">{s.time}</span>
          </div>
        </div>
        <div className="cw-v6-spine">
          <span className="cw-v6-spine-line" />
          <span className="cw-v6-spine-meta tnum">{_fmtDur(start, end)}</span>
          <span className="cw-v6-spine-line" />
          <span className="cw-v6-spine-arrow">▸</span>
        </div>
        <div className="cw-v6-stamp cw-v6-stamp-end">
          <div className="cw-v6-stamp-row">
            <span className="cw-v6-stamp-date">{e.weekday}, {e.monShort} {e.day}</span>
            <span className="cw-v6-stamp-time tnum">{e.time}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function BriefView({ density, showDiscarded, dropcap, setShowDiscarded, setView }) {
  const initialBrief = window.DATA.todaysBrief;
  const initialDates = window.DATA.availableDates || [];
  const initialDate = initialBrief?.windowEnd?.slice(0, 10) || initialDates[initialDates.length - 1] || null;

  const [currentBrief, setCurrentBrief] = React.useState(null);
  const [mode, setMode] = React.useState("brief"); // "brief" | "archive" | "audit"
  const [currentPulse, setCurrentPulse] = React.useState(window.DATA.pulse);
  const [availableDates, setAvailableDates] = React.useState(initialDates);
  const [selectedDate, setSelectedDate] = React.useState(initialDate);
  const [companySummaries, setCompanySummaries] = React.useState(window.DATA.companySummaries || {});
  const [loading, setLoading] = React.useState(false);
  const [activeBulletId, setActiveBulletId] = React.useState(null);
  const [filterTheme, setFilterTheme] = React.useState(null);
  const [relatedBriefs, setRelatedBriefs] = React.useState([]);
  const [companySearch, setCompanySearch] = React.useState("");
  const [entitySignals, setEntitySignals] = React.useState(null);
  const [signalsLoading, setSignalsLoading] = React.useState(false);
  const [signalMode, setSignalMode] = React.useState("zscore"); // "zscore" | "raw"

  const brief = currentBrief;

  React.useEffect(() => {
    const entityId = brief?.entityId;
    const date =
      selectedDate ||
      (brief?.windowEnd && brief.windowEnd.slice(0, 10)) ||
      null;
    if (!entityId || !date) {
      setRelatedBriefs([]);
      return;
    }
    let cancelled = false;
    fetch(
      `/api/frontend/brief/related?entity_id=${encodeURIComponent(entityId)}&date=${encodeURIComponent(date)}`
    )
      .then(r => r.json())
      .then(data => {
        if (cancelled) return;
        const list = Array.isArray(data.related) ? data.related : [];
        const shuffled = [...list];
        for (let i = shuffled.length - 1; i > 0; i--) {
          const j = Math.floor(Math.random() * (i + 1));
          const t = shuffled[i];
          shuffled[i] = shuffled[j];
          shuffled[j] = t;
        }
        setRelatedBriefs(shuffled.slice(0, 3));
      })
      .catch(() => { if (!cancelled) setRelatedBriefs([]); });
    return () => { cancelled = true; };
  }, [brief?.entityId, brief?.windowEnd, selectedDate]);

  const allCompanies = Array.isArray(window.DATA?.companies) ? window.DATA.companies : [];

  const _searchLower = companySearch.toLowerCase();
  const _filterCompany = (c) => !_searchLower ||
    c.name.toLowerCase().includes(_searchLower) ||
    (c.ticker || "").toLowerCase().includes(_searchLower);

  const companiesForFrontPage = React.useMemo(() => {
    if (!brief) {
      return [...allCompanies].sort((a, b) => {
        const ba = companySummaries[a.id]?.bulletsSaved ?? -1;
        const bb = companySummaries[b.id]?.bulletsSaved ?? -1;
        if (bb !== ba) return bb - ba;
        return (a.name || "").localeCompare(b.name || "", undefined, { sensitivity: "base" });
      });
    }
    const bid = brief.entityId;
    const list = allCompanies.filter(c => {
      if (c.id === bid) return true;
      const s = companySummaries[c.id];
      if (!s) return false;
      if (s.hasRunOnDate === true) return true;
      if (s.hasRunOnDate === false) return false;
      return (s.bulletsSaved ?? 0) > 0;
    });
    list.sort((a, b) => {
      const sa = companySummaries[a.id] || {};
      const sb = companySummaries[b.id] || {};
      const ea = BRIEF_SHOW_EARNINGS_RELEASE_INFO && sa.earningsOnDate ? 1 : 0;
      const eb = BRIEF_SHOW_EARNINGS_RELEASE_INFO && sb.earningsOnDate ? 1 : 0;
      if (eb !== ea) return eb - ea;
      const ba = sa.bulletsSaved ?? 0;
      const bb = sb.bulletsSaved ?? 0;
      if (bb !== ba) return bb - ba;
      return (a.name || "").localeCompare(b.name || "", undefined, { sensitivity: "base" });
    });
    return list;
  }, [companySummaries, brief?.entityId, allCompanies]);

  function refreshSidebar(date) {
    const url = `/api/frontend/companies/summaries` + (date ? `?date=${date}` : "");
    fetch(url)
      .then(r => r.json())
      .then(data => { if (data.summaries) setCompanySummaries(data.summaries); })
      .catch(console.error);
  }

  React.useEffect(() => {
    if (selectedDate) refreshSidebar(selectedDate);
  }, [selectedDate]);

  React.useEffect(() => {
    const entityId = brief?.entityId;
    if (!entityId) {
      setEntitySignals(null);
      return;
    }
    let cancelled = false;
    setSignalsLoading(true);
    fetch(`/api/frontend/entity/${encodeURIComponent(entityId)}/signals?days=30`)
      .then(r => r.json())
      .then(data => { if (!cancelled) setEntitySignals(data); })
      .catch(console.error)
      .finally(() => { if (!cancelled) setSignalsLoading(false); });
    return () => { cancelled = true; };
  }, [brief?.entityId]);

  function loadEntity(entityId, date) {
    const targetDate =
      typeof date === "string" && /^\d{4}-\d{2}-\d{2}/.test(date) ? date.slice(0, 10) : null;
    setLoading(true);
    setFilterTheme(null);
    setActiveBulletId(null);
    const url = `/api/frontend/entity/${entityId}/brief` + (targetDate ? `?date=${encodeURIComponent(targetDate)}` : "");
    fetch(url)
      .then(r => r.json())
      .then(data => {
        if (data.availableDates) setAvailableDates(data.availableDates);
        if (data.brief) {
          setCurrentBrief(data.brief);
          setCurrentPulse(data.pulse || []);
          // Keep sidebar counts in sync: brief already aggregates all day's runs
          setCompanySummaries(prev => ({
            ...prev,
            [entityId]: {
              ...(prev[entityId] || {}),
              bulletsSaved:     data.brief.bulletsSaved,
              bulletsDiscarded: data.brief.bulletsDiscarded,
              lastRunDate:      data.brief.coverageEnd || data.brief.windowEnd,
            },
          }));
          const asked = targetDate && /^\d{4}-\d{2}-\d{2}$/.test(targetDate) ? targetDate.slice(0, 10) : null;
          if (asked) {
            setSelectedDate(asked);
          } else {
            const rd = (data.selectedDate || data.brief.windowEnd || "").toString().slice(0, 10);
            if (/^\d{4}-\d{2}-\d{2}$/.test(rd)) setSelectedDate(rd);
          }
        }
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }

  function navigateDate(direction) {
    const key = (selectedDate || brief?.windowEnd || "").toString().slice(0, 10);
    const idx = availableDates.indexOf(key);
    const nextIdx = idx + direction;
    if (nextIdx < 0 || nextIdx >= availableDates.length) return;
    const newDate = availableDates[nextIdx];
    setSelectedDate(newDate);
    loadEntity(brief?.entityId, newDate);
  }

  const navDateKey = (selectedDate || brief?.windowEnd || "").toString().slice(0, 10);
  const dateIdx = availableDates.indexOf(navDateKey);
  const canPrev = dateIdx > 0;
  const canNext = dateIdx >= 0 && dateIdx < availableDates.length - 1;

  const briefOk = brief != null && Array.isArray(brief.bullets);
  if (!briefOk) {
    return <BriefLanding
      loading={loading}
      companies={companiesForFrontPage.filter(_filterCompany)}
      summaries={companySummaries}
      onPick={loadEntity}
      companySearch={companySearch}
      setCompanySearch={setCompanySearch}
      selectedDate={selectedDate}
    />;
  }

  const allBullets = brief.bullets;
  const themesList = Array.isArray(brief.themes) ? brief.themes : [];
  const discardedList = Array.isArray(brief.discarded) ? brief.discarded : [];

  // Assign maximally-separated hues using golden angle so sibling themes never clash
  const _dark = document.documentElement.dataset.theme === "dark";
  const _themeColorList = themesList.map((_, i) =>
    `hsl(${(i * 137.508 + 30) % 360}, 70%, ${_dark ? 62 : 38}%)`
  );
  const themeColors = {};
  themesList.forEach((t, i) => { themeColors[t.name] = _themeColorList[i]; });

  const bullets = filterTheme
    ? allBullets.filter(b => b.theme === filterTheme)
    : allBullets;

  const novelCount = allBullets.filter(b => b.novelty === "novel").length;
  const rewrittenCount = allBullets.filter(b => b.novelty === "rewritten").length;

  return (
    <div className="brief-layout" data-density={density}>
      {/* ── Left rail: today's front page ── */}
      <aside className="brief-rail">
        <div className="rail-section">
          <input
            className="archive-search"
            type="text"
            placeholder="Search company or ticker…"
            value={companySearch}
            onChange={e => setCompanySearch(e.target.value)}
            style={{ marginBottom: 10 }}
          />
          <div className="t-meta" style={{ color: "var(--ink-faint)", marginBottom: 10, fontSize: 10.5 }}>
            {selectedDate
              ? "Companies on the desk for this date"
              : "Choose a publication day to load the roster."}
          </div>
          <div className="frontpage-scroll">
          <ol className="frontpage-list">
            {companiesForFrontPage.filter(_filterCompany).map(c => {
              const isActive = c.id === brief?.entityId;
              const summary = companySummaries?.[c.id] || {};
              const pulse7 = (summary.pulse7 || []).map((p, i, arr) => ({
                value: p.saved,
                muted: i < arr.length - 1,
              }));
              const todaysSaved = isActive
                ? (brief?.bulletsSaved ?? summary.bulletsSaved ?? 0)
                : (summary.bulletsSaved ?? "—");
              return (
                <li key={c.id} className={`frontpage-item ${isActive ? "active" : ""}`}>
                  <button
                    className="frontpage-btn"
                    onClick={() => loadEntity(c.id, selectedDate)}
                    disabled={loading}
                    style={{ cursor: loading ? "wait" : "pointer" }}
                  >
                    <div className="frontpage-row1">
                      <span className="ticker">{_tk(c.ticker)}</span>
                      <span className="saved-count tnum">{todaysSaved}</span>
                    </div>
                    {BRIEF_SHOW_EARNINGS_RELEASE_INFO && summary.earningsOnDate && (
                      <div
                        className="t-cap"
                        style={{
                          fontSize: 9,
                          lineHeight: 1.25,
                          marginTop: 2,
                          letterSpacing: "0.04em",
                          color: "var(--accent)",
                        }}
                        title={summary.earningsSessionTitle || ""}
                      >
                        {summary.earningsSessionTitle
                          ? `Earnings · ${summary.earningsSessionTitle}`
                          : "Earnings call"}
                      </div>
                    )}
                    <div className="frontpage-row2">
                      <span className="company-name">{c.name}</span>
                    </div>
                    <div className="frontpage-row3">
                      {pulse7.length > 0 && (
                        <MiniBars
                          data={pulse7}
                          height={16} barWidth={4} gap={1.5}
                          color={isActive ? "var(--ink)" : "var(--ink-faint)"}
                          mutedColor="var(--rule)"
                        />
                      )}
                    </div>
                  </button>
                </li>
              );
            })}
          </ol>
          </div>
        </div>
      </aside>

      {/* ── Main column ── */}
      <main className="brief-main">
        {/* Top row: date + prev/next on the left — mode tabs on the right */}
        <div className="brief-mode-bar">
          <div className="brief-mode-bar-left">
            {(mode === "brief" || mode === "audit") ? (
              <>
                <div className="dateline" style={{ marginBottom: 0 }}>
                  {selectedDate
                    ? (() => {
                        const [y, m, d] = selectedDate.split("-").map(Number);
                        const dt = new Date(Date.UTC(y, m - 1, d));
                        return dt.toLocaleDateString("en-US", { weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
                      })()
                    : "—"}
                </div>
                <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                  <button
                    onClick={() => navigateDate(-1)}
                    disabled={!canPrev || loading}
                    style={{
                      fontFamily: "var(--mono)", fontSize: 12, padding: "3px 8px",
                      border: "1px solid var(--rule)", background: "var(--paper)",
                      color: canPrev ? "var(--ink)" : "var(--ink-faint)",
                      cursor: canPrev ? "pointer" : "default",
                      opacity: canPrev ? 1 : 0.4,
                    }}
                    title="Previous day"
                  >← prev</button>
                  <button
                    onClick={() => navigateDate(1)}
                    disabled={!canNext || loading}
                    style={{
                      fontFamily: "var(--mono)", fontSize: 12, padding: "3px 8px",
                      border: "1px solid var(--rule)", background: "var(--paper)",
                      color: canNext ? "var(--ink)" : "var(--ink-faint)",
                      cursor: canNext ? "pointer" : "default",
                      opacity: canNext ? 1 : 0.4,
                    }}
                    title="Next day"
                  >next →</button>
                  {loading && <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-faint)" }}>loading…</span>}
                </div>
              </>
            ) : null}
          </div>
          <div className="brief-mode-tabs" role="tablist">
            <button className={"brief-mode-tab" + (mode === "brief" ? " active" : "")}
                    onClick={() => setMode("brief")}>The Brief</button>
            <button className={"brief-mode-tab" + (mode === "audit" ? " active" : "")}
                    onClick={() => setMode("audit")}>Audit</button>
            <button className={"brief-mode-tab" + (mode === "archive" ? " active" : "")}
                    onClick={() => setMode("archive")}>Archive</button>
          </div>
        </div>

        {mode === "archive" && (
          <BriefEntityArchive entityId={brief.entityId} entityName={brief.entityName} ticker={brief.ticker} onOpenDate={(d) => { setMode("brief"); loadEntity(brief.entityId, d); }} />
        )}
        {mode === "audit" && (
          <BriefEntityAudit entityId={brief.entityId} selectedDate={selectedDate} />
        )}
        {mode === "brief" && (<>
        {/* Hero */}
        <header className="brief-hero">
          <h1 className="brief-headline t-display">
            <span className="brief-eyebrow">{brief?.entityName}</span>
            What's new on <em>{_tk(brief?.ticker)}</em> this morning.
          </h1>
          {loading
            ? <p className="brief-standfirst" style={{ color: "var(--ink-faint)", fontStyle: "italic" }}>Loading…</p>
            : brief?.noRunForWindow
              ? <p className="brief-standfirst" style={{ color: "var(--ink-mute)", fontStyle: "italic" }}>
                  No pipeline run for this calendar day. Use prev/next to move along the coverage range, or run the scanner for this window.
                </p>
              : brief?.bulletsSaved === 0
                ? <p className="brief-standfirst" style={{ color: "var(--ink-mute)", fontStyle: "italic" }}>No material developments for this period.</p>
                : brief?.narrative
                  ? <p className="brief-standfirst">{brief.narrative}</p>
                  : <p className="brief-standfirst" style={{ color: "var(--ink-faint)", fontStyle: "italic" }}>Narrative not available.</p>
          }

          <div className="brief-stats">
            <div className="brief-stat">
              <div className="brief-stat-num tnum">{brief.bulletsSaved}</div>
              <div className="brief-stat-label">Material<br />developments</div>
            </div>
            <div
              className="brief-stat"
              title="Unique documents in the retrieval pool for this company on this run — search results that mention or concern this company and were available to the pipeline."
            >
              <div className="brief-stat-num tnum">{brief.sourcesScanned}</div>
              <div className="brief-stat-label">Available<br />sources</div>
            </div>
            <div className="brief-stat">
              <div className="brief-stat-num tnum">{brief.chunksReviewed}</div>
              <div className="brief-stat-label">Excerpts<br />reviewed</div>
            </div>
            <div className="brief-stat">
              <div className="brief-stat-num tnum">{brief.bulletsDiscarded}</div>
              <div className="brief-stat-label">Filtered<br />out</div>
            </div>
            <div className="brief-stat">
              <div className="brief-stat-num tnum">{Math.round(brief.durationSec / 60)}<span className="brief-stat-unit">m</span></div>
              <div className="brief-stat-label">Pipeline<br />runtime</div>
            </div>
          </div>

          {!brief.noRunForWindow && (brief.coverageStart || brief.windowStart) && (
            <BriefWindowBand
              start={brief.coverageStart || brief.windowStart}
              end={brief.coverageEnd   || brief.windowEnd}
            />
          )}
        </header>

        <hr className="rule-thick" />

        {/* Theme filter row */}
        <div className="theme-filter">
          <span className="t-cap" style={{ marginRight: 14 }}>Sections</span>
          <button className={`theme-chip ${filterTheme === null ? "active" : ""}`} onClick={() => setFilterTheme(null)}>
            All <span className="muted tnum">{allBullets.length}</span>
          </button>
          {themesList.map(t => (
            <button key={t.name} className={`theme-chip ${filterTheme === t.name ? "active" : ""}`} onClick={() => setFilterTheme(t.name)}>
              <ThemeDot theme={t.name} color={themeColors[t.name]} />
              {t.name} <span className="muted tnum">{t.count}</span>
            </button>
          ))}
        </div>

        {/* Bullets, grouped by theme when no filter */}
        <div className="bullets-stream">
          {bullets.map((b, i) => (
            <BulletItem
              key={b.id}
              bullet={b}
              index={i}
              isFirst={i === 0 && dropcap}
              active={activeBulletId === b.id}
              onActivate={() => setActiveBulletId(activeBulletId === b.id ? null : b.id)}
              themeColor={themeColors[b.theme]}
            />
          ))}
        </div>

        {/* Discarded reveal */}
        <div className="discarded-section">
          <button className="discarded-toggle" onClick={() => setShowDiscarded(!showDiscarded)}>
            <span className="t-cap">Editor's Cut · {discardedList.length} items filtered</span>
            <span className="discarded-toggle-arrow">{showDiscarded ? "▴ hide" : "▾ show"}</span>
          </button>
          {showDiscarded && <DiscardedList items={discardedList} />}
        </div>

        {/* Footer dateline */}
        <footer className="brief-footer">
          <hr className="rule-double" />
          <div className="brief-footer-grid">
            <div>
              <div className="t-cap">Pipeline</div>
              <div className="soft">Run <span className="t-mono">{brief.runId}</span> · 5-stage novelty filter · gpt-4.1 + text-embedding-3-large</div>
            </div>
            <div>
              <div className="t-cap">Window</div>
              <div className="soft">{_fmtWindow(brief?.windowStart, brief?.windowEnd)}</div>
            </div>
            <div>
              <div className="t-cap">Coverage</div>
              <div className="soft">DOW 30 universe · 14-day novelty lookback</div>
            </div>
          </div>
        </footer>
        </>)}
      </main>

      {/* ── Right rail: meta ── */}
      <aside className="brief-rail brief-rail-right">
        <div className="rail-section">
          <div className="t-cap" style={{ marginBottom: 12 }}>About this brief</div>
          <div className="entity-card surface">
            <div className="entity-card-name t-h3">{brief.entityName}</div>
            <div className="entity-card-meta">
              <span className="entity-card-ticker">{_tk(brief.ticker)}</span>
              {brief.exchange && <span className="entity-card-exchange">· {brief.exchange}</span>}
            </div>
            <hr className="rule" style={{ margin: "12px 0" }} />
            <dl className="entity-dl">
              {brief.sector   && <><dt>Sector</dt><dd>{brief.sector}</dd></>}
              {brief.industry && <><dt>Industry</dt><dd>{brief.industry}</dd></>}
              {brief.country  && <><dt>Country</dt><dd>{brief.country}</dd></>}
              <dt>Entity ID</dt><dd className="t-mono">{brief.entityId}</dd>
              {brief.webpage  && <><dt>Web</dt><dd><a href={brief.webpage} target="_blank" rel="noreferrer" style={{ fontSize: 11 }}>{brief.webpage.replace(/^https?:\/\//, "").replace(/\/$/, "")}</a></dd></>}
            </dl>
          </div>
        </div>

        <div className="rail-section">
          {(() => {
            const pulseValues = currentPulse.map(p => p.saved);
            const n = pulseValues.length;
            const avg = n ? (pulseValues.reduce((a, b) => a + b, 0) / n) : 0;
            // Selected day value = last entry in pulse (pulse ends on selectedDate)
            const selectedValue = n ? pulseValues[n - 1] : (brief?.bulletsSaved ?? 0);
            // vs prev 7d: avg of last 7 vs avg of the 7 before that
            const last7 = pulseValues.slice(-7);
            const prev7 = pulseValues.slice(-14, -7);
            const last7avg = last7.length ? last7.reduce((a, b) => a + b, 0) / last7.length : 0;
            const prev7avg = prev7.length ? prev7.reduce((a, b) => a + b, 0) / prev7.length : null;
            const vs7pct = prev7avg !== null && prev7avg > 0
              ? Math.round((last7avg - prev7avg) / prev7avg * 100)
              : null;
            const vs7color = vs7pct === null ? "var(--ink-mute)" : vs7pct >= 0 ? "var(--novel)" : "var(--discard)";
            const vs7label = vs7pct === null ? "—" : (vs7pct >= 0 ? `+${vs7pct}%` : `${vs7pct}%`);
            const firstDate = currentPulse[0]?.date?.slice(5) || "";
            const lastDate = currentPulse[n - 1]?.date?.slice(5) || selectedDate?.slice(5) || "";
            return (
              <>
                <div className="t-cap" style={{ marginBottom: 10 }}>
                  {n}-day pulse
                </div>
                <div className="pulse-card surface">
                  <div className="pulse-label">Material developments per day</div>
                  <div className="pulse-spark">
                    <Sparkline
                      data={pulseValues}
                      height={48}
                      width={240}
                      fluid
                      color="var(--ink)"
                      fillColor="color-mix(in srgb, var(--ink) 8%, transparent)"
                      showLast
                    />
                  </div>
                  <div className="pulse-axis">
                    <span>{firstDate}</span>
                    <span>{lastDate}</span>
                  </div>
                  <hr className="rule" style={{ margin: "10px 0" }} />
                  <div className="pulse-summary">
                    <div>
                      <div className="t-cap" style={{ fontSize: 9.5 }}>Selected</div>
                      <div className="tnum" style={{ fontSize: 18, fontFamily: "var(--serif-display)", fontWeight: 600 }}>
                        {selectedValue}
                      </div>
                    </div>
                    <div>
                      <div className="t-cap" style={{ fontSize: 9.5 }}>{n}d avg</div>
                      <div className="tnum" style={{ fontSize: 18, fontFamily: "var(--serif-display)", fontWeight: 600 }}>
                        {avg.toFixed(1)}
                      </div>
                    </div>
                    <div>
                      <div className="t-cap" style={{ fontSize: 9.5 }}>Peak</div>
                      <div className="tnum" style={{ fontSize: 18, fontFamily: "var(--serif-display)", fontWeight: 600 }}>
                        {n ? Math.max(...pulseValues) : "—"}
                      </div>
                    </div>
                  </div>
                </div>
              </>
            );
          })()}
        </div>

        {/* Signal sparklines with Z-score / Raw toggle */}
        <div className="rail-section">
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <div className="t-cap">Signal history</div>
            <div style={{ display: "flex", gap: 4 }}>
              <button
                className={"theme-chip" + (signalMode === "zscore" ? " active" : "")}
                onClick={() => setSignalMode("zscore")}
                style={{ fontSize: 10, padding: "2px 7px" }}
              >Z-score</button>
              <button
                className={"theme-chip" + (signalMode === "raw" ? " active" : "")}
                onClick={() => setSignalMode("raw")}
                style={{ fontSize: 10, padding: "2px 7px" }}
              >Raw</button>
            </div>
          </div>
          {signalsLoading ? (
            <div className="t-meta" style={{ color: "var(--ink-faint)", fontSize: 11 }}>Loading…</div>
          ) : entitySignals && entitySignals.signals && entitySignals.signals.length > 0 ? (() => {
            const sigs = entitySignals.signals;
            const chunksKey = signalMode === "zscore" ? "chunks_zscore_mo" : "chunks_ewm_short";
            const sentKey   = signalMode === "zscore" ? "sent_zscore_mo"   : "sent_ewm_short";
            const chunksVals = sigs.map(s => s[chunksKey] ?? 0);
            const sentVals   = sigs.map(s => Math.min(1, Math.max(-1, s[sentKey] ?? 0)));
            return (
              <>
                <div style={{ marginBottom: 8 }}>
                  <div className="t-cap" style={{ fontSize: 9.5, marginBottom: 4 }}>Media attention</div>
                  <Sparkline
                    data={chunksVals}
                    height={36}
                    width={240}
                    fluid
                    color="var(--ink)"
                    fillColor="color-mix(in srgb, var(--ink) 8%, transparent)"
                    showLast
                  />
                </div>
                <div>
                  <div className="t-cap" style={{ fontSize: 9.5, marginBottom: 4 }}>Sentiment</div>
                  <Sparkline
                    data={sentVals}
                    height={36}
                    width={240}
                    fluid
                    color="var(--ink)"
                    fillColor="color-mix(in srgb, var(--ink) 8%, transparent)"
                    showLast
                  />
                </div>
              </>
            );
          })() : (
            <div className="t-meta" style={{ color: "var(--ink-faint)", fontSize: 11 }}>No signal data available.</div>
          )}
        </div>

        {/* Audit link removed — Audit is now an inline tab at top of brief */}

        {relatedBriefs.length > 0 && (
          <div className="rail-section">
            <div className="t-cap" style={{ marginBottom: 10 }}>Read also</div>
            <ul className="related-list">
              {relatedBriefs.map(r => (
                <li key={r.entityId}>
                  <a
                    href="#"
                    onClick={(e) => {
                      e.preventDefault();
                      loadEntity(r.entityId, r.date);
                    }}
                  >
                    {r.entityName}
                    {r.ticker ? ` · ${r.ticker}` : ""}
                  </a>
                  <div className="muted t-cap" style={{ fontSize: 10, marginTop: 2 }}>
                    {r.date} · {r.bulletsSaved} items
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </aside>
    </div>
  );
}

// ── Single bullet ───────────────────────────────────────────────────
function BulletItem({ bullet, index, isFirst, active, onActivate, themeColor }) {
  const [noteOpen, setNoteOpen] = React.useState(false);

  return (
    <article className={`bullet ${isFirst ? "bullet-first" : ""} ${active ? "bullet-active" : ""}`}>
      <div className="bullet-side">
        <span className="bullet-number tnum">{String(index + 1).padStart(2, "0")}</span>
        <span className="bullet-theme-label">
          <ThemeDot theme={bullet.theme} color={themeColor} />
          <span>{bullet.theme}</span>
        </span>
      </div>
      <div className="bullet-body">
        <p className={`bullet-text t-body-large ${isFirst ? "dropcap" : ""}`}>
          {bullet.text}
          {bullet.citations.map((c, i) => <CitationRef key={c.id} citation={c} idx={i} />)}
        </p>
        {bullet.novelty === "rewritten" && (
          <div className="rewrite-note">
            <button className="rewrite-note-toggle" onClick={() => setNoteOpen(o => !o)}>
              <span className="t-cap" style={{ color: "var(--rewrite)" }}>Editor's note</span>
              <span className="rewrite-note-arrow">{noteOpen ? "▴" : "▾"}</span>
            </button>
            {noteOpen && <span className="rewrite-reason">{bullet.rewriteReason}</span>}
          </div>
        )}
        <div className="bullet-citations-row">
          {(() => {
            // Group chips by source name, combine citation numbers
            // Group by source, count unique excerpts
            const grouped = [];
            const seen = new Map();
            bullet.citations.forEach((c) => {
              const src = c.source || "—";
              if (!seen.has(src)) { seen.set(src, { source: src, excerpts: new Set() }); grouped.push(seen.get(src)); }
              const ex = String(c.excerpt != null ? c.excerpt : (c.text != null ? c.text : "")).trim();
              if (ex) seen.get(src).excerpts.add(ex);
            });
            return grouped.map((g, gi) => (
              <span key={gi} className="bullet-source-chip">
                <span className="cite-source">
                  {g.source}{g.excerpts.size > 1 && <span style={{ fontFamily: "var(--sans)", color: "var(--ink-mute)" }}> ({g.excerpts.size})</span>}
                </span>
              </span>
            ));
          })()}
          <button className="bullet-action" onClick={onActivate}>
            {active ? "Hide" : "View"}
          </button>
        </div>
        {active && (
          <div className="bullet-sources-expanded">
            {_groupCitations(bullet.citations).map((sg, si) => (
              <div key={si} className="source-block">
                <div className="source-block-source" style={{ fontWeight: 600 }}>
                  {sg.source}{sg.date ? <span className="muted" style={{ fontWeight: 400 }}> · {sg.date}</span> : null}
                </div>
                {sg.headlineGroups.map((hg, hi) => (
                  <div key={hi} style={{ marginTop: 8 }}>
                    <div className="source-block-headline">{hg.headline}</div>
                    {hg.excerpts.length === 1
                      ? <p className="source-block-excerpt">"{hg.excerpts[0]}"</p>
                      : hg.excerpts.map((ex, xi) => (
                          <div key={xi} style={{ marginTop: 6 }}>
                            <div style={{ fontFamily: "var(--sans)", fontSize: 11, color: "var(--ink-mute)", marginBottom: 2 }}>Text {xi + 1}:</div>
                            <p className="source-block-excerpt">"{ex}"</p>
                          </div>
                        ))
                    }
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </article>
  );
}

// ── Discarded list ──────────────────────────────────────────────────
function DiscardedList({ items }) {
  const grouped = items.reduce((acc, item) => {
    if (!acc[item.stage]) acc[item.stage] = [];
    acc[item.stage].push(item);
    return acc;
  }, {});
  const stageLabels = {
    relevance_score: "Relevance score",
    grounding: "Grounding",
    novelty_embedding: "Novelty (embedding)",
    novelty_search: "Novelty (search)",
  };
  return (
    <div className="discarded-list">
      <p className="discarded-intro soft">
        Items the pipeline considered and rejected, with the reason. Helps audit what the brief did <em>not</em> tell you.
      </p>
      {Object.entries(grouped).map(([stage, list]) => (
        <div key={stage} className="discarded-group">
          <div className="discarded-group-head">
            <span className="t-cap" style={{ color: "var(--discard)" }}>{stageLabels[stage] || stage}</span>
            <span className="muted t-cap" style={{ fontSize: 10 }}>{list.length} item{list.length > 1 ? "s" : ""}</span>
          </div>
          <ul>
            {list.map(item => (
              <li key={item.id}>
                <span className="discarded-text">{item.text}</span>
                <span className="discarded-reason muted">— {item.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

window.BriefView = BriefView;

// ── Brief landing ─────────────────────────────────────────
// Two-column split: Portfolio Brief narrative on the left, company picker on the right.
function BriefLanding({ loading, companies, summaries, onPick, companySearch, setCompanySearch, selectedDate }) {
  // Only count companies that actually ran on the selected date
  const ranToday = companies.filter(c => summaries[c.id]?.hasRunOnDate === true);
  const totalSaved     = ranToday.reduce((s, c) => s + (summaries[c.id]?.bulletsSaved     || 0), 0);
  const totalDiscarded = ranToday.reduce((s, c) => s + (summaries[c.id]?.bulletsDiscarded || 0), 0);
  const moversAll = ranToday
    .map(c => ({ ...c, saved: summaries[c.id]?.bulletsSaved || 0, discarded: summaries[c.id]?.bulletsDiscarded || 0 }))
    .filter(c => c.saved > 0)
    .sort((a, b) => b.saved - a.saved);
  const movers = moversAll.slice(0, 5); // top 5 for display list
  const activeCount = moversAll.length; // actual count of active companies

  // Portfolio brief state
  const [portfolioBrief, setPortfolioBrief] = React.useState(null);
  const [briefLoading, setBriefLoading] = React.useState(false);
  const [narrativeMode, setNarrativeMode] = React.useState("thematic"); // "thematic" | "lead"

  const dateLabel = React.useMemo(() => {
    const iso = portfolioBrief?.date;
    if (!iso) return null;
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", {
      weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC",
    });
  }, [portfolioBrief?.date]);

  // Upcoming events state
  const [upcomingEvents, setUpcomingEvents] = React.useState(null);
  const [eventsLoading, setEventsLoading] = React.useState(false);

  // Fetch portfolio brief — always uses most recent date (no date param)
  React.useEffect(() => {
    setBriefLoading(true);
    fetch("/api/frontend/portfolio-brief?top_n=5")
      .then(r => r.json())
      .then(data => setPortfolioBrief(data))
      .catch(() => setPortfolioBrief(null))
      .finally(() => setBriefLoading(false));
  }, []);

  // Fetch upcoming events
  React.useEffect(() => {
    setEventsLoading(true);
    const params = selectedDate ? `?date=${encodeURIComponent(selectedDate)}&limit=8` : "?limit=8";
    fetch(`/api/frontend/upcoming-events${params}`)
      .then(r => r.json())
      .then(data => setUpcomingEvents(data))
      .catch(() => setUpcomingEvents(null))
      .finally(() => setEventsLoading(false));
  }, [selectedDate]);

  function _fmtEventDateTime(iso) {
    if (!iso) return { date: "—", time: "" };
    const d = new Date(iso);
    const zone = _tzIana();
    const date = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: zone });
    const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true, timeZone: zone });
    return { date, time };
  }

  const narrativeText = narrativeMode === "lead" && portfolioBrief?.narrative_b
    ? portfolioBrief.narrative_b
    : portfolioBrief?.narrative;
  const companiesCount = ranToday.length;
  const events = upcomingEvents?.events || [];

  return (
    <div className="brief-landing">
      {/* LEFT: Portfolio Brief */}
      <section className="portfolio-brief">
        <div className="pb-eyebrow">Portfolio Brief{dateLabel ? ` — ${dateLabel}` : ""}</div>
        <h1 className="pb-title">The day, told as one story.</h1>
        <p className="pb-subtitle">A single editorial synthesis of every material development across your coverage today.</p>

        <div className="pb-meta-strip">
          <span className="pb-meta-cell"><strong>{companiesCount}</strong> companies</span>
          <span className="pb-meta-cell"><strong>{totalSaved}</strong> material developments</span>
          <span className="pb-meta-cell"><strong>{totalDiscarded}</strong> filtered out</span>
          <span className="pb-meta-cell"><strong>{activeCount}</strong> active names</span>
        </div>

        {portfolioBrief?.narrative && (
          <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
            <button
              className={"theme-chip" + (narrativeMode === "thematic" ? " active" : "")}
              onClick={() => setNarrativeMode("thematic")}
              title="Identifies dominant cross-cutting themes"
            >Thematic</button>
            {portfolioBrief?.narrative_b && (
              <button
                className={"theme-chip" + (narrativeMode === "lead" ? " active" : "")}
                onClick={() => setNarrativeMode("lead")}
                title="One strong theme sentence + concrete examples"
              >Lead + Support</button>
            )}
          </div>
        )}

        <p className="pb-narrative">
          {briefLoading
            ? null
            : narrativeText
              ? <><span className="dropcap">{narrativeText.charAt(0)}</span>{narrativeText.slice(1)}</>
              : <span style={{ color: "var(--ink-mute)", fontStyle: "italic" }}>No portfolio brief available yet — will be generated after the next run.</span>
          }
        </p>

        <div className="pb-highlight-head">Next closest events</div>
        {eventsLoading ? (
          <p style={{ color: "var(--ink-faint)", fontStyle: "italic", fontSize: 13 }}>Loading events…</p>
        ) : events.length === 0 ? (
          <p style={{ color: "var(--ink-mute)", fontStyle: "italic", fontSize: 13 }}>No upcoming events found.</p>
        ) : (
          <ul className="pb-events-list">
            {events.map((ev, i) => {
              const { date: evDate, time: evTime } = _fmtEventDateTime(ev.event_datetime);
              const ticker = ev.ticker || ev.entity_id;
              return (
                <li key={i} className="pb-event">
                  <div className="pb-event-when">
                    <span className="pb-event-date">{evDate}</span>
                    <span className="pb-event-time tnum">{evTime}</span>
                    <span className="pb-event-tz">{DISPLAY_TZ}</span>
                  </div>
                  <div className="pb-event-meta" style={{ flexDirection: "column", alignItems: "flex-start", gap: 3 }}>
                    <span className="pb-event-detail">{ev.entity_name}{ev.fiscal_period ? ` · ${ev.fiscal_period}` : ""}{ev.fiscal_year ? ` ${ev.fiscal_year}` : ""}</span>
                    <span className="pb-event-kind" data-kind={ev.category === "conference-call" ? "conference" : "earnings"}>
                      {ev.category === "conference-call" ? "Conference" : "Earnings Call"}
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {/* RIGHT: Company picker */}
      <div className="brief-pick-wrap">
        <div className="brief-pick-header">
          <div className="dateline" style={{ marginBottom: 6 }}>The Brief</div>
          <p className="brief-pick-sub">
            {loading ? "Loading…" : "Choose a company to read its brief."}
          </p>
          <input
            className="archive-search"
            type="text"
            placeholder="Search company or ticker…"
            value={companySearch}
            onChange={e => setCompanySearch(e.target.value)}
            style={{ marginTop: 12 }}
          />
        </div>
        <div className="brief-pick-list">
          <div className="brief-pick-row brief-pick-row-head">
            <span className="brief-pick-col-ticker">Ticker</span>
            <span className="brief-pick-col-name">Company</span>
            <span className="brief-pick-col-date">Last run</span>
            <span className="brief-pick-col-bullets">Published</span>
            <span className="brief-pick-col-discarded">Discarded</span>
            <span className="brief-pick-col-delta">Med. Att. Δ</span>
            <span className="brief-pick-col-delta">Sent. Δ</span>
          </div>
          {companies.map(c => {
            const s = summaries[c.id] || {};
            const saved = s.bulletsSaved != null ? s.bulletsSaved : "—";
            const discarded = s.bulletsDiscarded != null ? s.bulletsDiscarded : "—";
            const rawDate = s.lastRunDate || (s.pulse7?.length > 0 ? s.pulse7[s.pulse7.length - 1].date : null);
            const date = _fmtRunDate(rawDate);
            const fmtDelta = (v) => {
              if (v == null) return { label: "—", color: "inherit" };
              const fixed = v.toFixed(1);
              const label = v >= 0 ? `+${fixed}%` : `${fixed}%`;
              const color = v >= 0 ? "var(--novel)" : "var(--discard)";
              return { label, color };
            };
            const chunksDelta = fmtDelta(s.deltaChunksPct);
            const sentDelta = fmtDelta(s.deltaSentPct);
            return (
              <button key={c.id} className="brief-pick-row brief-pick-row-item"
                      onClick={() => onPick(c.id, null)} disabled={loading}>
                <span className="brief-pick-col-ticker">{_tk(c.ticker)}</span>
                <span className="brief-pick-col-name">{c.name}</span>
                <span className="brief-pick-col-date">{date}</span>
                <span className="brief-pick-col-bullets">{saved}</span>
                <span className="brief-pick-col-discarded">{discarded}</span>
                <span className="brief-pick-col-delta tnum" style={{ color: chunksDelta.color }}>{chunksDelta.label}</span>
                <span className="brief-pick-col-delta tnum" style={{ color: sentDelta.color }}>{sentDelta.label}</span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Inline Archive for the selected entity (Change 4) ───────────────
function BriefEntityArchive({ entityId, entityName, ticker, onOpenDate }) {
  const [data, setData] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [expandedRunId, setExpandedRunId] = React.useState(null);

  React.useEffect(() => {
    setLoading(true);
    fetch(`/api/frontend/entity/${entityId}/history`)
      .then(r => r.json())
      .then(d => setData(d))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [entityId]);

  if (loading || !data) {
    return <p style={{ padding: "40px 8px", color: "var(--ink-mute)", fontStyle: "italic", fontFamily: "var(--serif)" }}>Loading archive…</p>;
  }

  const history = data.history || [];

  return (
    <div className="archive-inline" style={{ padding: "12px 4px 40px" }}>
      <header style={{ marginBottom: 20 }}>
        <div className="dateline" style={{ marginBottom: 6 }}>{_tk(ticker)} · Archive</div>
        <h2 className="t-display" style={{ fontSize: 32, margin: "0 0 6px", letterSpacing: "-0.018em" }}>
          Every brief filed for {entityName}.
        </h2>
        <p style={{ fontFamily: "var(--serif)", fontStyle: "italic", color: "var(--ink-mute)", margin: 0, fontSize: 14 }}>
          {history.length} runs · {history.reduce((s, h) => s + h.saved, 0)} bullets saved · {history.reduce((s, h) => s + h.discarded, 0)} discarded
        </p>
      </header>

      <div>
        {history.map(entry => {
          const d = new Date(entry.date + "T00:00:00Z");
          const day = d.getUTCDate();
          const month3 = d.toLocaleDateString("en-US", { month: "short", timeZone: "UTC" });
          const wd = d.toLocaleDateString("en-US", { weekday: "short", timeZone: "UTC" });
          return (
            <article key={entry.runId} className="archive-day">
              <div className="archive-day-date">
                <div className="archive-day-num">{String(day).padStart(2, "0")}</div>
                <div className="archive-day-month">{month3}</div>
                <div className="archive-day-weekday">{wd}</div>
              </div>
              <div className="archive-day-content">
                <div className="archive-run">
                  <h3 className="archive-headline" style={{ cursor: "pointer" }} onClick={() => onOpenDate(entry.date)}>
                    {entry.narrative || entry.bullets?.[0]?.text || "No material developments"}
                  </h3>
                  <div className="archive-meta">
                    <span style={{ fontFamily: "var(--mono)", textTransform: "none", letterSpacing: 0 }}>run-{entry.runId}</span>
                    <span>·</span>
                    <span>{entry.saved} saved</span>
                    <span>·</span>
                    <span>{entry.discarded} discarded</span>
                  </div>
                  {entry.bullets?.length > 0 && (
                    <button className="archive-expand-btn"
                            onClick={() => setExpandedRunId(expandedRunId === entry.runId ? null : entry.runId)}>
                      {expandedRunId === entry.runId ? "▴ hide bullets" : `▾ ${entry.bullets.length} bullet${entry.bullets.length !== 1 ? "s" : ""}`}
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
              </div>
            </article>
          );
        })}
        {history.length === 0 && (
          <p style={{ color: "var(--ink-mute)", fontStyle: "italic", marginTop: 32 }}>No briefs found for this entity.</p>
        )}
      </div>
    </div>
  );
}

// ── Inline Audit (forensic) for the selected entity (Change 4) ──────
// ── Inline Audit — renders identical content to HistoryDetailsView (no sidebar) ──
function BriefEntityAudit({ entityId, selectedDate }) {
  const [forensicsData, setForensicsData] = React.useState(null);
  const [loading, setLoading] = React.useState(true);
  const [openRunId, setOpenRunId] = React.useState(null);
  const [expandedRejection, setExpandedRejection] = React.useState(null);
  const [expandedPubCitation, setExpandedPubCitation] = React.useState(null);

  React.useEffect(() => {
    if (!entityId) return;
    setLoading(true);
    fetch(`/api/frontend/entity/${entityId}/forensics`)
      .then(r => r.json())
      .then(d => {
        setForensicsData(d);
        // Auto-open the run matching selectedDate, else open the first run
        if (d.days && d.days.length > 0) {
          const targetDay = selectedDate
            ? d.days.find(day => day.date === selectedDate)
            : d.days[0];
          const day = targetDay || d.days[0];
          if (day && day.runs && day.runs.length > 0) {
            setOpenRunId(day.runs[0].runId);
          }
        }
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [entityId, selectedDate]);

  if (loading) return (
    <div style={{ padding: 40, color: "var(--ink-mute)", fontStyle: "italic", fontFamily: "var(--sans)", fontSize: 13 }}>Loading audit…</div>
  );

  const allDays = forensicsData?.days || [];
  // Show only the day matching the selected date
  const days = selectedDate
    ? allDays.filter(d => d.date === selectedDate)
    : allDays.slice(0, 1);

  if (!days.length) return (
    <div style={{ padding: 40, color: "var(--ink-mute)", fontStyle: "italic", fontFamily: "var(--sans)", fontSize: 13 }}>No audit data for this date.</div>
  );

  return (
    <div style={{ paddingTop: 8 }}>
      {days.map(d => {
        const isMulti = d.runs.length > 1;

        // Static header — no collapse, always expanded
        const RunHeader = ({ r }) => (
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px 12px", padding: "10px 0", borderBottom: "1px solid var(--rule)", marginBottom: 16, fontFamily: "var(--sans)", fontSize: 12 }}>
            <span className="hd-count-pub"><strong className="tnum">{r.published}</strong> published</span>
            <span className="muted">·</span>
            <span className="hd-count-rej"><strong className="tnum">{r.rejected}</strong> rejected</span>
            <span className="muted">·</span>
            <span className="t-mono" style={{ color: "var(--ink-mute)" }}>run-{r.runId}</span>
            {r.windowStart && (
              <span className="muted" style={{ fontSize: 11 }}>{_fmtWindow(r.windowStart, r.windowEnd)}</span>
            )}
          </div>
        );

        if (!isMulti) {
          const r = d.runs[0];
          return (
            <article key={d.date}>
              <RunHeader r={r} />
              <RunBody r={r} expandedRejection={expandedRejection} setExpandedRejection={setExpandedRejection}
                       expandedPubCitation={expandedPubCitation} setExpandedPubCitation={setExpandedPubCitation} />
            </article>
          );
        }

        const totalPub = d.runs.reduce((s, r) => s + r.published, 0);
        const totalRej = d.runs.reduce((s, r) => s + r.rejected, 0);
        return (
          <article key={d.date}>
            <div style={{ fontFamily: "var(--sans)", fontSize: 12, color: "var(--ink-mute)", marginBottom: 12 }}>
              {d.runs.length} runs · <strong className="tnum">{totalPub}</strong> published · <strong className="tnum">{totalRej}</strong> rejected
            </div>
            {d.runs.map(r => (
              <React.Fragment key={r.runId}>
                <RunHeader r={r} />
                <RunBody r={r} expandedRejection={expandedRejection} setExpandedRejection={setExpandedRejection}
                         expandedPubCitation={expandedPubCitation} setExpandedPubCitation={setExpandedPubCitation} />
              </React.Fragment>
            ))}
          </article>
        );
      })}
    </div>
  );
}
