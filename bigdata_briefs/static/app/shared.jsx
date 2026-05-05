// Shared components: masthead, navigation, sparkline, theme-dot, citation popover
const { useState, useEffect, useRef, useMemo } = React;

function formatUtcClockHm() {
  return new Date().toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  });
}

// ── Masthead ───────────────────────────────────────────────────────
function Masthead({ view, setView, theme, setTheme }) {
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
      <div className="masthead">
        <div className="masthead-inner">
          <div className="masthead-edition">
            <span className="edition-label">Vol. II · No. 0427</span>
            <span className="edition-date">{fmt}</span>
          </div>
          <div className="masthead-title masthead-title-with-logo">
            <img
              className="masthead-logo"
              src="/app/desk/bigdata-by-ravenpack-logo-light.svg"
              alt="Bigdata by RavenPack"
            />
            <span className="masthead-title-text">Briefs</span>
          </div>
          <div className="masthead-actions">
            <button className="btn-ghost btn-sm" style={{ display: "inline-flex", alignItems: "center", gap: 6, fontFamily: "var(--sans)", fontSize: 12, fontWeight: 600, color: "var(--ink-soft)" }}
            onClick={() => setTheme(theme === "light" ? "dark" : "light")}>
              {theme === "light" ? "◐ Dark" : "◑ Light"}
            </button>
            <span style={{ fontFamily: "var(--sans)", fontSize: 12, fontWeight: 600, color: "var(--ink-mute)" }}>analyst@desk</span>
          </div>
        </div>
      </div>
      <div className="section-nav">
        <div className="section-nav-inner">
          <a href="#" className={view === "home" ? "active" : ""} onClick={(e) => {e.preventDefault();setView("home");}}>Home</a>
          <a href="#" className={view === "brief" ? "active" : ""} onClick={(e) => {e.preventDefault();setView("brief");}}>The Brief</a>
          <a href="#" className={view === "scan" ? "active" : ""} onClick={(e) => {e.preventDefault();setView("scan");}}>News Scan</a>
          <a href="#" className={["history","history-details","cost"].includes(view) ? "active" : ""} onClick={(e) => {e.preventDefault();setView("history");}}>Reports</a>
          <span className="nav-spacer"></span>
          <span className="live-status">
            <span className="live-dot"></span>
            <span>Live · {utcClock} UTC</span>
          </span>
        </div>
      </div>
      {["history","history-details","cost"].includes(view) && (
        <div className="section-subnav">
          <div className="section-subnav-inner">
            <a href="#" className={view === "history" ? "active" : ""} onClick={(e) => {e.preventDefault();setView("history");}}>Archive</a>
            <a href="#" className={view === "history-details" ? "active" : ""} onClick={(e) => {e.preventDefault();setView("history-details");}}>Audit</a>
            <a href="#" className={view === "cost" ? "active" : ""} onClick={(e) => {e.preventDefault();setView("cost");}}>Cost</a>
          </div>
        </div>
      )}
    </React.Fragment>);

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
window.ThemeDot = ThemeDot;
window.Sparkline = Sparkline;
window.MiniBars = MiniBars;
window.CitationRef = CitationRef;
window.StatusBadge = StatusBadge;