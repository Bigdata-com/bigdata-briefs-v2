// Shared components: masthead, navigation, sparkline, theme-dot, citation popover
const { useState, useEffect, useRef, useMemo } = React;

// ── Global display timezone ───────────────────────────────────────────
// Change DISPLAY_TZ to adjust all time displays across the app.
// Supported values: "UTC", "New York", "CET"
const DISPLAY_TZ = "ET";
const _TZ_MAP  = { "UTC": "UTC", "ET": "America/New_York", "CET": "Europe/Paris" };
const _TZ_LONG = { "UTC": "UTC", "ET": "ET / New York Time", "CET": "CET / Central European Time" };
function _tzIana(tz) { return _TZ_MAP[tz != null ? tz : DISPLAY_TZ] || "UTC"; }
function _tzLong(tz) { return _TZ_LONG[tz != null ? tz : DISPLAY_TZ] || tz || DISPLAY_TZ; }

function _tk(ticker) { return ticker || "PRIVATE"; }

// Group citations by source name, then by headline within each source.
// Returns [{source, date, headlineGroups: [{headline, excerpts: [str]}]}]
function _groupCitations(citations) {
  const bySource = new Map();
  for (const c of citations) {
    const src = c.source || "";
    if (!bySource.has(src)) bySource.set(src, { source: src, date: c.date || "", headlines: new Map() });
    const sg = bySource.get(src);
    const hl = c.headline || "";
    if (!sg.headlines.has(hl)) sg.headlines.set(hl, []);
    const ex = String(c.excerpt != null ? c.excerpt : (c.text != null ? c.text : "")).trim();
    if (ex) sg.headlines.get(hl).push(ex);
  }
  return Array.from(bySource.values()).map(sg => ({
    source: sg.source,
    date: sg.date,
    headlineGroups: Array.from(sg.headlines.entries()).map(([hl, excerpts]) => ({ headline: hl, excerpts })),
  }));
}

function _fmtRunDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const parts = new Intl.DateTimeFormat("en-US", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "numeric", minute: "2-digit", hour12: true, timeZone: _tzIana(),
  }).formatToParts(d);
  const g = t => parts.find(p => p.type === t)?.value || "";
  return `${g("year")}-${g("month")}-${g("day")} ${g("hour")}:${g("minute")} ${g("dayPeriod")} ${DISPLAY_TZ}`;
}

function _fmtWindow(start, end) {
  if (!start) return "—";
  const zone = _tzIana();
  const fmtDate = iso => new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: zone });
  const fmtTime = iso => new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: zone, hour12: true });
  const fmt    = iso => `${fmtDate(iso)} ${fmtTime(iso)}`;
  if (!end) return `${fmt(start)} ${DISPLAY_TZ}`;
  const sDate = fmtDate(start), eDate = fmtDate(end);
  return sDate === eDate
    ? `${fmt(start)} → ${fmtTime(end)} ${DISPLAY_TZ}`
    : `${fmt(start)} → ${fmt(end)} ${DISPLAY_TZ}`;
}

function formatUtcClockHm() {
  return new Date().toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  });
}

// ── Masthead ───────────────────────────────────────────────────────
function Masthead({ view, setView, theme, setTheme, headerStyle, setHeaderStyle }) {
  const [utcClock, setUtcClock] = useState(formatUtcClockHm);
  useEffect(() => {
    const tick = () => setUtcClock(formatUtcClockHm());
    tick();
    const id = setInterval(tick, 15_000);
    return () => clearInterval(id);
  }, []);

  const today = new Date();
  const fmt = today.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "UTC" });

  return (
    <React.Fragment>
      <div className="masthead" data-style={headerStyle || "paren-lockup"}>
        <div className="masthead-inner">
          <div className="masthead-edition">
            <span className="edition-label">Vol. II · No. 0427</span>
            <span className="edition-date">{fmt}</span>
          </div>
          <MastheadLockup style={headerStyle || "paren-lockup"} theme={theme} />
          <div className="masthead-actions" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            {setHeaderStyle && (
              <div style={{ display: "flex", gap: 4 }}>
                {[["stacked","Stack"],["inline","Inline"],["paren-lockup","Paren"]].map(([val, label]) => (
                  <button key={val}
                    className={"theme-chip" + ((headerStyle || "paren-lockup") === val ? " active" : "")}
                    onClick={() => setHeaderStyle(val)}
                    style={{ fontSize: 11 }}>
                    {label}
                  </button>
                ))}
              </div>
            )}
            <button className="btn-ghost btn-sm" style={{ display: "inline-flex", alignItems: "center", gap: 6, fontFamily: "var(--sans)", fontSize: 12, fontWeight: 600, color: "var(--ink-soft)" }}
            onClick={() => setTheme(theme === "light" ? "dark" : "light")}>
              {theme === "light" ? "◐ Dark" : "◑ Light"}
            </button>
          </div>
        </div>
      </div>
      <div className="section-nav">
        <div className="section-nav-inner">
          <a href="#" className={view === "brief" ? "active" : ""} onClick={(e) => {e.preventDefault();setView("brief");}}>The Brief</a>
          <a href="#" className={view === "portfolio" ? "active" : ""} onClick={(e) => {e.preventDefault();setView("portfolio");}}>My Portfolio</a>
          <a href="#" className={view === "cost" ? "active" : ""} onClick={(e) => {e.preventDefault();setView("cost");}}>Costs</a>
          <span className="nav-spacer"></span>
          <span className="live-status">
            <span className="live-dot"></span>
            <span>Live · {utcClock} UTC</span>
          </span>
        </div>
      </div>

    </React.Fragment>);

}

// ── Masthead title lockup — three variants ──────────────────────────
// "paren-lockup" (default) — "The Brief" big LEFT + "(powered by / [logo])" RIGHT in parens
// "inline"                 — single line: "The Brief (powered by [logo])"
// "stacked"                — title big, "powered by" small below, "[logo]" below that, all centered
function MastheadLockup({ style, theme }) {
  const LOGO_SRC = theme === "dark" ? "bigdata-logo-white.png" : "bigdata-logo-black.png";

  if (style === "inline") {
    return (
      <div className="masthead-title-lockup is-inline" aria-label="The Brief, powered by Bigdata.com">
        <h1 className="masthead-title-big">The Brief</h1>
        <span className="ml-paren-group">
          <span className="ml-paren">(</span>
          <span className="ml-powered-text">powered by</span>
          <img className="ml-logo" src={LOGO_SRC} alt="Bigdata.com by RavenPack" />
          <span className="ml-paren">)</span>
        </span>
      </div>
    );
  }

  if (style === "stacked") {
    return (
      <div className="masthead-title-lockup is-stacked" aria-label="The Brief, powered by Bigdata.com">
        <h1 className="masthead-title-big">The Brief&nbsp;&nbsp;&nbsp;</h1>
        <div className="ml-stacked-sub">
          <span className="ml-powered-text">powered by</span>
          <img className="ml-logo" src={LOGO_SRC} alt="Bigdata.com by RavenPack" />
        </div>
      </div>
    );
  }

  // Default: "paren-lockup"
  return (
    <div className="masthead-title-lockup is-paren" aria-label="The Brief, powered by Bigdata.com">
      <h1 className="masthead-title-big">The Brief</h1>
      <div className="ml-paren-block" aria-hidden="false">
        <div className="ml-paren-inner">
          <span className="ml-powered-text">powered by</span>
          <img className="ml-logo" src={LOGO_SRC} alt="Bigdata.com by RavenPack" />
        </div>
      </div>
    </div>
  );
}

// ── Theme dot ───────────────────────────────────────────────────────
// Derives a vivid, stable colour from any label string via a simple hash.
function hashThemeColor(label) {
  if (!label) return "var(--ink-mute)";
  let h = 0;
  for (let i = 0; i < label.length; i++) {
    h = Math.imul(31, h) + label.charCodeAt(i) | 0;
  }
  const hue = ((h % 360) + 360) % 360;
  const dark = document.documentElement.dataset.theme === "dark";
  return `hsl(${hue}, 70%, ${dark ? 62 : 38}%)`;
}

function ThemeDot({ theme, color: colorProp }) {
  const color = colorProp || hashThemeColor(theme);
  return <span className="theme-dot" style={{ background: color }}></span>;
}

// ── Sparkline (saved bullets per day) ───────────────────────────────
function Sparkline({
  data,
  height = 22,
  width = 80,
  color = "var(--ink-soft)",
  fillColor = null,
  showLast = false,
  fluid = false,
}) {
  if (!data || data.length === 0) return null;
  const max = Math.max(...data);
  const min = Math.min(...data);
  const range = Math.max(max - min, 1);
  const span = Math.max(data.length - 1, 1);
  const step = width / span;
  const points = data.map((v, i) => {
    const x = i * step;
    const y = height - 2 - (v - min) / range * (height - 4);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const last = data[data.length - 1];
  const lastY = height - 2 - (last - min) / range * (height - 4);
  const lastX = (data.length - 1) * step;

  const svgClass = "spark" + (fluid ? " spark--fluid" : "");
  const common = {
    className: svgClass,
    viewBox: `0 0 ${width} ${height}`,
  };
  const svgProps = fluid
    ? {
        ...common,
        preserveAspectRatio: "none",
        style: { width: "100%", height, display: "block" },
      }
    : { ...common, width, height };

  return (
    <svg {...svgProps}>
      {fillColor && (
        <polyline points={`0,${height} ${points} ${width},${height}`} fill={fillColor} stroke="none" />
      )}
      <polyline points={points} fill="none" stroke={color} strokeWidth="1.25" strokeLinejoin="round" strokeLinecap="round" />
      {showLast && <circle cx={lastX} cy={lastY} r="2" fill={color} />}
    </svg>
  );
}

// ── Bar mini-chart ──────────────────────────────────────────────────
function MiniBars({ data, height = 28, barWidth = 6, gap = 2, color = "var(--ink)", mutedColor = "var(--rule)" }) {
  if (!data || data.length === 0) return null;
  const max = Math.max(...data.map((d) => typeof d === "number" ? d : d.value), 1);
  return (
    <svg height={height} width={data.length * (barWidth + gap)} style={{ display: "block" }}>
      {data.map((d, i) => {
        const value = typeof d === "number" ? d : d.value;
        const muted = typeof d === "object" && d.muted;
        const h = value > 0 ? Math.max(value / max * (height - 2), 2) : 1;
        return <rect key={i} x={i * (barWidth + gap)} y={height - h} width={barWidth} height={h} fill={muted ? mutedColor : color} />;
      })}
    </svg>);

}

// ── Citation popover ────────────────────────────────────────────────
function CitationRef({ citation, idx }) {
  return (
    <span className="cite-ref" tabIndex="0">
      <sup className="cite-marker">{idx + 1}</sup>
      <span className="cite-pop surface">
        <span className="cite-pop-source">{citation.source} · <span className="muted">{citation.date}</span></span>
        <span className="cite-pop-headline">{citation.headline}</span>
        <span className="cite-pop-excerpt">{citation.excerpt}</span>
      </span>
    </span>);

}

// ── Status badges ──────────────────────────────────────────────────
function StatusBadge({ status }) {
  const map = {
    succeeded: { cls: "tag-novel", label: "Succeeded" },
    running: { cls: "tag-running", label: "Running" },
    failed: { cls: "tag-discard", label: "Failed" }
  };
  const m = map[status] || { cls: "", label: status };
  return <span className={`tag ${m.cls}`}>{m.label}</span>;
}

window.Masthead = Masthead;
window.MastheadLockup = MastheadLockup;
window.ThemeDot = ThemeDot;
window.Sparkline = Sparkline;
window.MiniBars = MiniBars;
window.CitationRef = CitationRef;
window.StatusBadge = StatusBadge;