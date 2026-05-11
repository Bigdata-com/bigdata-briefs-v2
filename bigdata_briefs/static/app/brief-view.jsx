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
    time:     d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: zone, hour12: false }),
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
  if (h < 24) return `${h}h ${String(m).padStart(2, "0")}m`;
  const days = Math.floor(h / 24);
  return `${days}d ${h % 24}h`;
}

function BriefWindowBand({ start, end }) {
  const s = _parseWindowParts(start);
  const e = _parseWindowParts(end);
  return (
    <div className="cw-v6">
      <div className="cw-v6-plate">
        <div className="cw-v6-plate-main">Coverage</div>
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

function BriefView({ density, showDiscarded, dropcap, setShowDiscarded, setView, setAuditEntityId, setAuditDate }) {
  const initialBrief = window.DATA.todaysBrief;
  const initialDates = window.DATA.availableDates || [];
  const initialDate = initialBrief?.windowEnd?.slice(0, 10) || initialDates[initialDates.length - 1] || null;

  const [currentBrief, setCurrentBrief] = React.useState(null);
  const [currentPulse, setCurrentPulse] = React.useState(window.DATA.pulse);
  const [availableDates, setAvailableDates] = React.useState(initialDates);
  const [selectedDate, setSelectedDate] = React.useState(initialDate);
  const [companySummaries, setCompanySummaries] = React.useState(window.DATA.companySummaries || {});
  const [loading, setLoading] = React.useState(false);
  const [activeBulletId, setActiveBulletId] = React.useState(null);
  const [filterTheme, setFilterTheme] = React.useState(null);
  const [relatedBriefs, setRelatedBriefs] = React.useState([]);
  const [companySearch, setCompanySearch] = React.useState("");

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
    return (
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
          </div>
          {companiesForFrontPage.filter(_filterCompany).map(c => {
            const s = companySummaries[c.id] || {};
            const saved = s.bulletsSaved != null ? s.bulletsSaved : "—";
            const discarded = s.bulletsDiscarded != null ? s.bulletsDiscarded : "—";
            const rawDate = s.lastRunDate || (s.pulse7?.length > 0 ? s.pulse7[s.pulse7.length - 1].date : null);
            const date = _fmtRunDate(rawDate);
            return (
              <button key={c.id} className="brief-pick-row brief-pick-row-item"
                      onClick={() => loadEntity(c.id, null)} disabled={loading}>
                <span className="brief-pick-col-ticker">{c.ticker}</span>
                <span className="brief-pick-col-name">{c.name}</span>
                <span className="brief-pick-col-date">{date}</span>
                <span className="brief-pick-col-bullets">{saved}</span>
                <span className="brief-pick-col-discarded">{discarded}</span>
              </button>
            );
          })}
        </div>
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
                      <span className="ticker">{c.ticker}</span>
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
        {/* Hero */}
        <header className="brief-hero">
          <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 20 }}>
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
          </div>
          <h1 className="brief-headline t-display">
            <span className="brief-eyebrow">{brief?.entityName}</span>
            What's new on <em>{brief?.ticker}</em> this morning.
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

          {(brief.coverageStart || brief.windowStart) && (
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
      </main>

      {/* ── Right rail: meta ── */}
      <aside className="brief-rail brief-rail-right">
        <div className="rail-section">
          <div className="t-cap" style={{ marginBottom: 12 }}>About this brief</div>
          <div className="entity-card surface">
            <div className="entity-card-name t-h3">{brief.entityName}</div>
            <div className="entity-card-meta">
              <span className="entity-card-ticker">{brief.ticker}</span>
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

        {setView && setAuditEntityId && (
          <div className="rail-section">
            <button
              className="audit-link-btn"
              onClick={() => {
                setAuditEntityId(brief.entityId);
                if (setAuditDate) setAuditDate(selectedDate);
                setView("history-details");
              }}
            >
              View Audit →
            </button>
          </div>
        )}

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
          {bullet.citations.map((c, i) => (
            <span key={c.id} className="bullet-source-chip">
              <span className="t-mono cite-num">{i + 1}</span>
              <span className="cite-source">{c.source}</span>
            </span>
          ))}
          <button className="bullet-action" onClick={onActivate}>
            {active ? "− hide sources" : "+ all sources"}
          </button>
        </div>
        {active && (
          <div className="bullet-sources-expanded">
            {bullet.citations.map((c, i) => (
              <div key={c.id} className="source-block">
                <div className="source-block-head">
                  <span className="cite-num-big tnum">{i + 1}</span>
                  <div>
                    <div className="source-block-source">{c.source} · <span className="muted">{c.date}</span></div>
                    <div className="source-block-headline">{c.headline}</div>
                  </div>
                </div>
                <p className="source-block-excerpt">"{c.excerpt}"</p>
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
