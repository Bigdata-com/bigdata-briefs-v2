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

function BriefView({ density, showDiscarded, dropcap, setShowDiscarded, setView, view, briefLayout, setBriefLayout, appPortfolioIds, appLandingSummaries, appEvents }) {
  const initialBrief = window.DATA.todaysBrief;
  const initialDates = window.DATA.availableDates || [];
  const initialDate = initialBrief?.windowEnd?.slice(0, 10) || initialDates[initialDates.length - 1] || null;

  const [currentBrief, setCurrentBrief] = React.useState(null);
  // mode derived from view: "overview" → brief, "overview-audit" → audit, "overview-archive" → archive
  const mode = view === "overview-audit" ? "audit" : view === "overview-archive" ? "archive" : "brief";
  const setMode = (m) => setView(m === "audit" ? "overview-audit" : m === "archive" ? "overview-archive" : "overview");
  const [currentPulse, setCurrentPulse] = React.useState(window.DATA.pulse);
  const [availableDates, setAvailableDates] = React.useState(initialDates);
  const [selectedDate, setSelectedDate] = React.useState(initialDate);
  const [companySummaries, setCompanySummaries] = React.useState(window.DATA.companySummaries || {});
  const landingSummaries = appLandingSummaries || window.DATA.companySummaries || {};
  const _summariesDateRef = React.useRef(initialDate); // tracks which date companySummaries currently reflects
  const [loading, setLoading] = React.useState(false);
  const [activeBulletId, setActiveBulletId] = React.useState(null);
  const [filterTheme, setFilterTheme] = React.useState(null);
  const [relatedBriefs, setRelatedBriefs] = React.useState([]);
  const [companySearch, setCompanySearch] = React.useState("");
  const [entitySignals, setEntitySignals] = React.useState(null);
  const [signalsLoading, setSignalsLoading] = React.useState(false);
  const [signalMode, setSignalMode] = React.useState("zscore"); // "zscore" | "raw"
  const portfolioIds = appPortfolioIds; // null = loading, Set = loaded
  const _loadingEntityRef = React.useRef(null); // prevents auto-load from racing with explicit loadEntity calls

  // Reset to landing when The Brief nav is clicked
  React.useEffect(() => {
    if (view === "brief") {
      setCurrentBrief(null);
      setCurrentPulse(window.DATA.pulse || []);
    }
  }, [view]);

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

  const _allCompanies = Array.isArray(window.DATA?.companies) ? window.DATA.companies : [];
  const allCompanies = portfolioIds === null
    ? []
    : portfolioIds.size === 0
      ? _allCompanies
      : _allCompanies.filter(c => portfolioIds.has(c.id));

  // Auto-load top company when navigating to any overview* view with no brief loaded.
  // Skips if loadEntity is already in flight (race-condition guard via _loadingEntityRef).
  React.useEffect(() => {
    const isOverview = view === "overview" || view === "overview-audit" || view === "overview-archive";
    if (!isOverview || currentBrief || !portfolioIds || _loadingEntityRef.current) return;
    const top = [...allCompanies]
      .filter(c => companySummaries[c.id]?.bulletsSaved > 0)
      .sort((a, b) => (companySummaries[b.id]?.bulletsSaved ?? 0) - (companySummaries[a.id]?.bulletsSaved ?? 0))[0];
    if (top) loadEntity(top.id, selectedDate, view);
  }, [view, portfolioIds]);

  const _searchLower = companySearch.toLowerCase();
  const _filterCompany = (c) => !_searchLower ||
    c.name.toLowerCase().includes(_searchLower) ||
    (c.ticker || "").toLowerCase().includes(_searchLower);

  // Landing picker: sorted by bullet count descending
  const landingCompanies = React.useMemo(() =>
    [...allCompanies].sort((a, b) =>
      (landingSummaries[b.id]?.bulletsSaved ?? 0) - (landingSummaries[a.id]?.bulletsSaved ?? 0)
    ),
  [landingSummaries, allCompanies]);

  // Overview left rail: filtered to companies that ran on selectedDate, sorted by bullet count
  const companiesForFrontPage = React.useMemo(() => {
    const bid = brief?.entityId;
    const list = allCompanies.filter(c => {
      if (c.id === bid) return true;
      const s = companySummaries[c.id];
      if (!s) return false;
      if (s.hasRunOnDate === true) return true;
      if (s.hasRunOnDate === false) return false;
      return (s.bulletsSaved ?? 0) > 0;
    });
    return [...list].sort((a, b) =>
      (companySummaries[b.id]?.bulletsSaved ?? 0) - (companySummaries[a.id]?.bulletsSaved ?? 0)
    );
  }, [companySummaries, brief?.entityId, allCompanies]);

  function refreshSidebar(date) {
    const url = `/api/frontend/companies/summaries` + (date ? `?date=${date}` : "");
    fetch(url)
      .then(r => r.json())
      .then(data => {
        if (data.summaries) {
          setCompanySummaries(data.summaries);
          _summariesDateRef.current = date;
        }
      })
      .catch(console.error);
  }

  React.useEffect(() => {
    if (selectedDate && selectedDate !== _summariesDateRef.current) refreshSidebar(selectedDate);
  }, [selectedDate]);

  React.useEffect(() => {
    const entityId = brief?.entityId;
    if (!entityId) {
      setEntitySignals(null);
      return;
    }
    let cancelled = false;
    setSignalsLoading(true);
    const endParam = selectedDate ? `&end_date=${encodeURIComponent(selectedDate)}` : "";
    fetch(`/api/frontend/entity/${encodeURIComponent(entityId)}/signals?days=30${endParam}`)
      .then(r => r.json())
      .then(data => { if (!cancelled) setEntitySignals(data); })
      .catch(console.error)
      .finally(() => { if (!cancelled) setSignalsLoading(false); });
    return () => { cancelled = true; };
  }, [brief?.entityId, selectedDate]);

  function loadEntity(entityId, date, targetView = "overview") {
    const targetDate =
      typeof date === "string" && /^\d{4}-\d{2}-\d{2}/.test(date) ? date.slice(0, 10) : null;
    _loadingEntityRef.current = entityId;
    setView(targetView);
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
      .finally(() => { setLoading(false); _loadingEntityRef.current = null; });
  }

  function navigateDate(direction) {
    const key = (selectedDate || brief?.windowEnd || "").toString().slice(0, 10);
    const idx = availableDates.indexOf(key);
    const nextIdx = idx + direction;
    if (nextIdx < 0 || nextIdx >= availableDates.length) return;
    const newDate = availableDates[nextIdx];
    setSelectedDate(newDate);
    loadEntity(brief?.entityId, newDate, view);
  }

  const navDateKey = (selectedDate || brief?.windowEnd || "").toString().slice(0, 10);
  const dateIdx = availableDates.indexOf(navDateKey);
  const canPrev = dateIdx > 0;
  const canNext = dateIdx >= 0 && dateIdx < availableDates.length - 1;

  if (view === "brief") {
    return <BriefLanding
      loading={loading}
      companies={landingCompanies.filter(_filterCompany)}
      allCompanies={landingCompanies}
      summaries={landingSummaries}
      onPick={loadEntity}
      companySearch={companySearch}
      setCompanySearch={setCompanySearch}
      selectedDate={selectedDate}
      briefLayout={briefLayout}
      setBriefLayout={setBriefLayout}
      upcomingEvents={appEvents}
      eventsLoading={appEvents === null}
    />;
  }

  // view === "overview"
  if (!brief || !Array.isArray(brief.bullets)) {
    return (
      <div style={{ padding: "80px 0", fontFamily: "var(--serif)", fontStyle: "italic", color: "var(--ink-mute)", textAlign: "center", fontSize: 15 }}>
        {loading ? "Loading…" : "No company data available."}
      </div>
    );
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
          {!selectedDate && (
            <div className="t-meta" style={{ color: "var(--ink-faint)", marginBottom: 10, fontSize: 10.5 }}>
              Choose a publication day to load the roster.
            </div>
          )}
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

        {/* Date navigation — shown in brief, audit and archive (buttons hidden in archive) */}
        {(mode === "brief" || mode === "audit" || mode === "archive") && (
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16, paddingBottom: 12, borderBottom: "1px solid var(--rule)" }}>
            <span style={{ fontFamily: "var(--sans)", fontSize: 12, fontWeight: 600, color: "var(--ink-soft)", flex: 1 }}>
              {selectedDate ? (() => {
                const [y, m, d] = selectedDate.split("-").map(Number);
                return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", { weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
              })() : "—"}
            </span>
            <button onClick={() => navigateDate(-1)} disabled={!canPrev || loading}
              style={{ fontFamily: "var(--mono)", fontSize: 12, padding: "3px 8px", border: "1px solid var(--rule)", background: "var(--paper)", color: canPrev ? "var(--ink)" : "var(--ink-faint)", cursor: canPrev ? "pointer" : "default", opacity: canPrev ? 1 : 0.4, visibility: mode === "archive" ? "hidden" : "visible" }}
              title="Previous day">← prev</button>
            <button onClick={() => navigateDate(1)} disabled={!canNext || loading}
              style={{ fontFamily: "var(--mono)", fontSize: 12, padding: "3px 8px", border: "1px solid var(--rule)", background: "var(--paper)", color: canNext ? "var(--ink)" : "var(--ink-faint)", cursor: canNext ? "pointer" : "default", opacity: canNext ? 1 : 0.4, visibility: mode === "archive" ? "hidden" : "visible" }}
              title="Next day">next →</button>
            {loading && <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--ink-faint)" }}>loading…</span>}
          </div>
        )}

        {mode === "archive" && (
          <BriefEntityArchive entityId={brief.entityId} entityName={brief.entityName} ticker={brief.ticker} onOpenDate={(d) => { setMode("brief"); loadEntity(brief.entityId, d); }} />
        )}
        {mode === "audit" && (
          <>
            <header className="brief-hero" style={{ marginBottom: 20 }}>
              <h1 className="brief-headline t-display">
                <span className="brief-eyebrow">{brief?.entityName} — Audit</span>
              </h1>
              <h2 className="t-display" style={{ fontSize: 32, margin: "0 0 6px", letterSpacing: "-0.018em" }}>
                Every bullet, kept or cut for {brief?.ticker || brief?.entityName}
              </h2>
            </header>
            <BriefEntityAudit entityId={brief.entityId} selectedDate={selectedDate} />
          </>
        )}
        {mode === "brief" && (<>
        {/* Hero */}
        <header className="brief-hero">
          <h1 className="brief-headline t-display">
            <span className="brief-eyebrow">{brief?.entityName}</span>
            {brief?.ticker ? `What's new on ` : `What's new at `}<em>{brief?.ticker || brief?.entityName}</em> this morning.
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
            // Build a fixed 14-day window ending on selectedDate, filling missing days with 0
            const _endIso = selectedDate || brief?.windowEnd?.slice(0, 10) || new Date().toISOString().slice(0, 10);
            const _14dates = Array.from({ length: 14 }, (_, i) => {
              const d = new Date(_endIso + "T00:00:00Z");
              d.setUTCDate(d.getUTCDate() - (13 - i));
              return d.toISOString().slice(0, 10);
            });
            const _pulseByDate = Object.fromEntries(currentPulse.map(p => [p.date?.slice(0, 10), p.saved ?? 0]));
            const pulseValues = _14dates.map(d => _pulseByDate[d] ?? 0);
            const avg = pulseValues.reduce((a, b) => a + b, 0) / 14;
            const selectedValue = pulseValues[13]; // last = end date
            const firstDate = _14dates[0].slice(5);
            const lastDate = _14dates[13].slice(5);
            return (
              <>
                <div className="t-cap" style={{ marginBottom: 10 }}>
                  14-day pulse
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
                      <div className="t-cap" style={{ fontSize: 9.5 }}>Current</div>
                      <div className="tnum" style={{ fontSize: 18, fontFamily: "var(--serif-display)", fontWeight: 600 }}>
                        {selectedValue}
                      </div>
                    </div>
                    <div>
                      <div className="t-cap" style={{ fontSize: 9.5 }}>14d avg</div>
                      <div className="tnum" style={{ fontSize: 18, fontFamily: "var(--serif-display)", fontWeight: 600 }}>
                        {avg.toFixed(1)}
                      </div>
                    </div>
                    <div>
                      <div className="t-cap" style={{ fontSize: 9.5 }}>Peak</div>
                      <div className="tnum" style={{ fontSize: 18, fontFamily: "var(--serif-display)", fontWeight: 600 }}>
                        {Math.max(...pulseValues)}
                      </div>
                    </div>
                  </div>
                </div>
              </>
            );
          })()}
        </div>

        {/* Signal history */}
        <div className="rail-section">
          <div className="t-cap" style={{ marginBottom: 10 }}>Signal history</div>
          {signalsLoading ? (
            <div className="t-meta" style={{ color: "var(--ink-faint)", fontSize: 11 }}>Loading…</div>
          ) : entitySignals && entitySignals.signals && entitySignals.signals.length > 0 ? (() => {
            const sigs = entitySignals.signals;
            const last = sigs[sigs.length - 1];
            const chunksVals = sigs.map(s => s.chunks_zscore_mo ?? 0);
            const sentVals   = sigs.map(s => s.sent_ewm_short ?? 0);
            const firstDate  = sigs[0]?.date?.slice(5) || "";
            const lastDate   = last?.date?.slice(5) || "";

            const _fmtN = (v, dec = 4) => v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(dec);
            const _fmtZ = (v, dec = 1) => v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(dec);

            const _interpZ = (v) => {
              if (v == null) return "";
              const az = Math.abs(v), z = _fmtZ(v);
              if (az <= 1.0) return `Normal range (z=${z})`;
              if (az <= 2.0) return `${v < 0 ? "Below" : "Above"} average (z=${z})`;
              return `Well ${v < 0 ? "below" : "above"} average (z=${z})`;
            };
            const _interpMomSent = (v) => {
              if (v == null) return "";
              const f = v.toFixed(1), s = v >= 0 ? "+" : "";
              if (Math.abs(v) < 0.02) return `─ Stable (${s}${f})`;
              return v > 0 ? `↑ Rising (${s}${f})` : `↓ Falling (${s}${f})`;
            };
            const _interpMomPct = (v) => {
              if (v == null) return "";
              const f = v.toFixed(1), s = v >= 0 ? "+" : "";
              if (Math.abs(v) < 5) return `─ Stable (${s}${f}%)`;
              return v > 0 ? `↑ Rising (${s}${f}%)` : `↓ Falling (${s}${f}%)`;
            };

            const MetricRow = ({ label, value, interp, color }) => (
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", padding: "3px 0", borderBottom: "1px solid var(--rule)", gap: 8 }}>
                <span style={{ fontFamily: "var(--sans)", fontSize: 10.5, color: "var(--ink-mute)", whiteSpace: "nowrap" }}>{label}</span>
                <span style={{ display: "flex", gap: 6, alignItems: "baseline", minWidth: 0 }}>
                  <span style={{ fontFamily: "var(--mono)", fontSize: 11, fontWeight: 600, color: color || "var(--ink)", whiteSpace: "nowrap" }}>{value}</span>
                  {interp && <span style={{ fontFamily: "var(--sans)", fontSize: 9.5, color: "var(--ink-mute)", textAlign: "right" }}>{interp}</span>}
                </span>
              </div>
            );

            const _col = (v) => v == null ? "inherit" : v >= 0 ? "var(--novel)" : "var(--discard)";

            return (
              <div className="pulse-card surface">
                {/* Media attention sparkline */}
                <div className="pulse-label">Media attention</div>
                <div className="pulse-spark">
                  <Sparkline data={chunksVals} height={36} width={240} fluid
                    color="var(--ink)" fillColor="color-mix(in srgb, var(--ink) 8%, transparent)" showLast />
                </div>
                <div className="pulse-axis" style={{ marginBottom: 8 }}><span>{firstDate}</span><span>{lastDate}</span></div>
                <MetricRow label="Momentum %" value={last.chunks_momentum_pct != null ? (last.chunks_momentum_pct >= 0 ? "+" : "") + last.chunks_momentum_pct.toFixed(1) + "%" : "—"} interp={_interpMomPct(last.chunks_momentum_pct)} color={_col(last.chunks_momentum_pct)} />
                <MetricRow label="vs. 1-month (z)" value={_fmtZ(last.chunks_zscore_mo)} interp={_interpZ(last.chunks_zscore_mo)} color={_col(last.chunks_zscore_mo)} />
                <MetricRow label="vs. 1-quarter (z)" value={_fmtZ(last.chunks_zscore_qt)} interp={_interpZ(last.chunks_zscore_qt)} color={_col(last.chunks_zscore_qt)} />

                <div style={{ marginTop: 10 }} />

                {/* Sentiment sparkline — diverging area with min/max gutter */}
                <div className="pulse-label">Sentiment</div>
                <div className="pulse-spark">
                  <DivergingSparkline data={sentVals} height={38} width={240} />
                </div>
                {(() => {
                  const sMin = Math.min(...sentVals);
                  const sMax = Math.max(...sentVals);
                  const sNow = sentVals[sentVals.length - 1];
                  const sgn  = (v) => v >= 0 ? "+" : "−";
                  const num  = (v, d = 3) => v == null ? "—" : sgn(v) + Math.abs(v).toFixed(d);
                  return (
                    <div className="pulse-caption">
                      <span>min <b style={{ color: sMin >= 0 ? "var(--novel)" : "var(--discard)" }}>{num(sMin)}</b></span>
                      <span>now <b style={{ color: sNow >= 0 ? "var(--novel)" : "var(--discard)" }}>{num(sNow)}</b></span>
                      <span>max <b style={{ color: sMax >= 0 ? "var(--novel)" : "var(--discard)" }}>{num(sMax)}</b></span>
                    </div>
                  );
                })()}
                <div className="pulse-axis" style={{ marginBottom: 8 }}><span>{firstDate}</span><span>{lastDate}</span></div>
                <MetricRow label="Baseline" value={_fmtN(last.sent_ewm_long)} />
                <MetricRow label="Momentum" value={_fmtN(last.sent_momentum)} interp={_interpMomSent(last.sent_momentum)} color={_col(last.sent_momentum)} />
                <MetricRow label="vs. 1-month (z)" value={_fmtZ(last.sent_zscore_mo)} interp={_interpZ(last.sent_zscore_mo)} color={_col(last.sent_zscore_mo)} />
                <MetricRow label="vs. 1-quarter (z)" value={_fmtZ(last.sent_zscore_qt)} interp={_interpZ(last.sent_zscore_qt)} color={_col(last.sent_zscore_qt)} />
              </div>
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
  const [expandedSources, setExpandedSources] = React.useState(new Set());

  React.useEffect(() => {
    if (!active) setExpandedSources(new Set());
  }, [active]);

  const toggleSource = (src) => {
    setExpandedSources(prev => {
      const next = new Set(prev);
      if (next.has(src)) next.delete(src); else next.add(src);
      return next;
    });
  };

  const groupedCitations = _groupCitations(bullet.citations);

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
          {groupedCitations.map((sg, gi) => {
            const isOpen = active || expandedSources.has(sg.source);
            const excerptCount = sg.headlineGroups.reduce((s, hg) => s + hg.excerpts.length, 0);
            return (
              <button key={gi}
                className={"bullet-source-chip" + (isOpen ? " active" : "")}
                onClick={() => toggleSource(sg.source)}
              >
                <span className="cite-source">
                  {sg.source}{excerptCount > 1 && <span style={{ fontFamily: "var(--sans)", color: "var(--ink-mute)" }}> ({excerptCount})</span>}
                </span>
              </button>
            );
          })}
          <button className="bullet-action" onClick={onActivate}>
            {active ? "Hide" : "View all"}
          </button>
        </div>
        {(active || expandedSources.size > 0) && (
          <div className="bullet-sources-expanded">
            {groupedCitations
              .filter(sg => active || expandedSources.has(sg.source))
              .map((sg, si) => (
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

// ── Archive bullet item — bullet text + collapsible source chips ────
function ArchiveBulletItem({ bullet, index }) {
  const [expandedSources, setExpandedSources] = React.useState(new Set());
  const groupedCitations = _groupCitations(bullet.citations || []);

  const toggleSource = (src) => setExpandedSources(prev => {
    const next = new Set(prev);
    if (next.has(src)) next.delete(src); else next.add(src);
    return next;
  });

  return (
    <div className="archive-bullet-item">
      {bullet.theme && (
        <span className="archive-bullet-theme"><ThemeDot theme={bullet.theme} />&nbsp;{bullet.theme}</span>
      )}
      <p className="archive-bullet-text">{bullet.text}</p>
      {groupedCitations.length > 0 && (
        <div className="bullet-citations-row" style={{ marginTop: 6 }}>
          {groupedCitations.map((sg, gi) => {
            const isOpen = expandedSources.has(sg.source);
            const excerptCount = sg.headlineGroups.reduce((s, hg) => s + hg.excerpts.length, 0);
            return (
              <button key={gi}
                className={"bullet-source-chip" + (isOpen ? " active" : "")}
                onClick={() => toggleSource(sg.source)}
              >
                <span className="cite-source">
                  {sg.source}{excerptCount > 1 && <span style={{ fontFamily: "var(--sans)", color: "var(--ink-mute)" }}> ({excerptCount})</span>}
                </span>
              </button>
            );
          })}
        </div>
      )}
      {expandedSources.size > 0 && (
        <div className="bullet-sources-expanded">
          {groupedCitations.filter(sg => expandedSources.has(sg.source)).map((sg, si) => (
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
  );
}

// ── Brief landing ─────────────────────────────────────────
// Two-column split: Portfolio Brief narrative on the left, company picker on the right.
function BriefLanding({ loading, companies, allCompanies, summaries, onPick, companySearch, setCompanySearch, selectedDate, briefLayout, setBriefLayout, upcomingEvents, eventsLoading }) {
  const [pbView, setPbView] = React.useState("bullets"); // "bullets" | "summary"

  // Stats are always computed from the full (unfiltered) company list for the selected date
  const _statsBase = (allCompanies || companies).filter(c => summaries[c.id]?.hasRunOnDate === true);
  const ranToday = _statsBase;
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

  // Fetch portfolio brief — always uses most recent date (no date param)
  React.useEffect(() => {
    setBriefLoading(true);
    fetch("/api/frontend/portfolio-brief?top_n=5")
      .then(r => r.json())
      .then(data => setPortfolioBrief(data))
      .catch(() => setPortfolioBrief(null))
      .finally(() => setBriefLoading(false));
  }, []);

  function _fmtEventDateTime(iso) {
    if (!iso) return { date: "—", time: "" };
    const d = new Date(iso);
    const zone = _tzIana();
    const date = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: zone });
    const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true, timeZone: zone });
    return { date, time };
  }

  const [showExtraCols, setShowExtraCols] = React.useState(false);
  const leftRef = React.useRef(null);
  const pickListRef = React.useRef(null);

  const _layout = briefLayout || "below";

  // Cap pick-list height to match left panel in both layouts
  React.useLayoutEffect(() => {
    const list = pickListRef.current;
    const left = leftRef.current;
    if (!list || !left) return;
    const leftH = left.getBoundingClientRect().height;
    const header = list.previousElementSibling;
    const headerH = header ? header.getBoundingClientRect().height : 0;
    const available = Math.max(leftH - headerH, 220);
    list.style.maxHeight = available + "px";
    list.style.overflowY = "auto";
  }, [_layout, companies.length, portfolioBrief?.date, briefLoading, upcomingEvents]);

  const narrativeText = narrativeMode === "lead" && portfolioBrief?.narrative_b
    ? portfolioBrief.narrative_b
    : portfolioBrief?.narrative;
  const companiesCount = ranToday.length;
  const events = upcomingEvents?.events || [];

  // Group events by day for calendar strip
  const eventsByDay = React.useMemo(() => {
    const zone = _tzIana();
    const days = {};
    events.forEach(ev => {
      const d = new Date(ev.event_datetime);
      const key = d.toLocaleDateString("en-US", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: zone });
      if (!days[key]) days[key] = { date: d, events: [] };
      days[key].events.push(ev);
    });
    return Object.values(days).sort((a, b) => a.date - b.date);
  }, [events]);

  const eventsInPanelBlock = (
    <>
      <div className="pb-highlight-head">Next closest events</div>
      {eventsLoading ? (
        <p style={{ color: "var(--ink-faint)", fontStyle: "italic", fontSize: 13 }}>Loading events…</p>
      ) : events.length === 0 ? (
        <p style={{ color: "var(--ink-mute)", fontStyle: "italic", fontSize: 13 }}>No upcoming events found.</p>
      ) : (
        <ul className="pb-events-list">
          {events.map((ev, i) => {
            const { date: evDate, time: evTime } = _fmtEventDateTime(ev.event_datetime);
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
    </>
  );

  const eventsBelowBlock = eventsByDay.length > 0 && (
    <div className="brief-events-strip">
      <div className="bes-head">Next closest events</div>
      <div className="bes-track">
        {eventsByDay.map((day, di) => {
          const zone = _tzIana();
          const dow = day.date.toLocaleDateString("en-US", { weekday: "short", timeZone: zone });
          const num = day.date.toLocaleDateString("en-US", { day: "numeric", timeZone: zone });
          const mon = day.date.toLocaleDateString("en-US", { month: "short", timeZone: zone });
          return (
            <div key={di} className="bes-day">
              <div className="bes-day-head">
                <span className="bes-day-dow">{dow}</span>
                <span className="bes-day-num">{num}</span>
                <span className="bes-day-mon">{mon}</span>
              </div>
              <div className="bes-day-events">
                {day.events.map((ev, ei) => {
                  const time = new Date(ev.event_datetime).toLocaleTimeString("en-US", {
                    hour: "numeric", minute: "2-digit", hour12: true, timeZone: zone,
                  });
                  return (
                    <div key={ei} className="bes-event">
                      <div className="bes-event-time">{time} <span className="bes-event-tz">{DISPLAY_TZ}</span></div>
                      <div className="bes-event-body">
                        <span className="bes-event-name">{ev.entity_name}</span>
                        <span className="bes-event-kind" data-kind={ev.category === "conference-call" ? "conference" : "earnings"}>
                          {ev.category === "conference-call" ? "Conference" : "Earnings Call"}
                        </span>
                        {(ev.fiscal_period || ev.fiscal_year) && (
                          <span className="bes-event-quarter">{[ev.fiscal_period, ev.fiscal_year].filter(Boolean).join(" ")}</span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );

  return (
    <div className="brief-landing-page" data-layout={_layout}>
    <div className="brief-landing">
      {/* LEFT: Company picker */}
      <div className="brief-pick-wrap">
        <div className="brief-pick-header">
          <div style={{ marginBottom: 6 }}></div>
          <p className="brief-pick-sub">
            {loading ? "Loading…" : companies.length === 0 ? "Add companies in My Portfolio to see briefs here." : "Choose a company to read its brief."}
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
        <div className="brief-pick-list" ref={pickListRef}>
          <div className="brief-pick-row brief-pick-row-head">
            <span className="brief-pick-col-ticker">Ticker</span>
            <span className="brief-pick-col-name">Company</span>
            <span className="brief-pick-col-bullets">Items</span>
          </div>
          {companies.map(c => {
            const s = summaries[c.id] || {};
            const saved = s.bulletsSaved != null ? s.bulletsSaved : "—";
            return (
              <button key={c.id} className="brief-pick-row brief-pick-row-item"
                      onClick={() => onPick(c.id, null)} disabled={loading}>
                <span className="brief-pick-col-ticker">{_tk(c.ticker)}</span>
                <span className="brief-pick-col-name">{c.name}</span>
                <span className="brief-pick-col-bullets">{saved}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* RIGHT: Portfolio Brief */}
      <section className="portfolio-brief" ref={leftRef}>
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
          <div className="pb-eyebrow">Portfolio Brief{dateLabel ? ` — ${dateLabel}` : ""}</div>
          <div className="pb-view-toggle">
            <button className={"pb-view-btn" + (pbView === "bullets" ? " active" : "")} onClick={() => setPbView("bullets")}>Bullet Points</button>
            <button className={"pb-view-btn" + (pbView === "summary" ? " active" : "")} onClick={() => setPbView("summary")}>Summary</button>
          </div>
        </div>
        <h1 className="pb-title">Where the news moved.</h1>
        <p className="pb-subtitle">The most active names in your portfolio today — short reads, one per company.</p>

        <div className="pb-meta-strip">
          <span className="pb-meta-cell"><strong>{companiesCount}</strong> companies</span>
          <span className="pb-meta-cell"><strong>{totalSaved}</strong> material developments</span>
          <span className="pb-meta-cell"><strong>{activeCount}</strong> active names</span>
        </div>

        <div className="pb-narrative">
          {briefLoading
            ? null
            : pbView === "summary"
              ? (narrativeText
                  ? (() => {
                      const _companyById = {};
                      (portfolioBrief?.companies || []).forEach(c => { _companyById[c.name] = c.entityId; });
                      return narrativeText.split("\n\n").map((section, i) => {
                        const nl = section.indexOf("\n");
                        const company = nl === -1 ? section : section.slice(0, nl);
                        const body = nl === -1 ? "" : section.slice(nl + 1);
                        const entityId = _companyById[company];
                        const _go = entityId ? () => onPick(entityId, null) : null;
                        return (
                          <div key={i} className="pb-narrative-section">
                            <div className={"pb-narrative-company" + (entityId ? " pb-narrative-company--link" : "")} onClick={_go} style={entityId ? { cursor: "pointer" } : {}}>{company}</div>
                            {body && <p className="pb-narrative-bullets" onClick={_go} style={entityId ? { cursor: "pointer" } : {}}>{body}</p>}
                          </div>
                        );
                      });
                    })()
                  : <span style={{ color: "var(--ink-mute)", fontStyle: "italic" }}>No portfolio brief available yet — will be generated after the next run.</span>
                )
              : ((portfolioBrief?.bullets_by_company || []).length > 0
                  ? (portfolioBrief.bullets_by_company).map((c, i) => {
                      const _go = c.entityId ? () => onPick(c.entityId, null) : null;
                      return (
                        <div key={i} className="pb-narrative-section">
                          <div className={"pb-narrative-company" + (c.entityId ? " pb-narrative-company--link" : "")} onClick={_go} style={c.entityId ? { cursor: "pointer" } : {}}>{c.name}</div>
                          <ul className="pb-bullets-list">
                            {c.bullets.map((b, j) => <li key={j} className="pb-bullet-item" onClick={_go} style={c.entityId ? { cursor: "pointer" } : {}}>{b}</li>)}
                          </ul>
                        </div>
                      );
                    })
                  : <span style={{ color: "var(--ink-mute)", fontStyle: "italic" }}>No bullet points available yet.</span>
                )
          }
        </div>

        {_layout === "in-panel" && eventsInPanelBlock}
      </section>
    </div>
    {_layout === "below" && eventsBelowBlock}
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
    <div className="archive-inline" style={{ padding: "0 0 40px" }}>
      <header className="brief-hero" style={{ marginBottom: 20 }}>
        <h1 className="brief-headline t-display">
          <span className="brief-eyebrow">{entityName} — Archive</span>
        </h1>
        <h2 className="t-display" style={{ fontSize: 32, margin: "0 0 6px", letterSpacing: "-0.018em" }}>
          Every brief filed for {ticker || entityName}
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
                    <div className="archive-bullets-list">
                      {entry.bullets.map((b, i) => (
                        <ArchiveBulletItem key={b.id || i} bullet={b} index={i} />
                      ))}
                    </div>
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
