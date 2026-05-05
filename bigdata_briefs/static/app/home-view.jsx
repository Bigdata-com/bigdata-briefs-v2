// ── Home view — explains the app workflow ──────────────────

function HomeView({ setView }) {
  return (
    <div className="home-view">
      <div className="home-intro">
        <div className="dateline">Bigdata Briefs</div>
        <h1 className="t-display" style={{ marginTop: 12, marginBottom: 8, letterSpacing: "-0.025em" }}>
          Get Started.
        </h1>
        <p style={{ fontFamily: "var(--serif)", fontStyle: "italic", fontSize: 19, color: "var(--ink-soft)", maxWidth: 560, lineHeight: 1.55, marginTop: 0 }}>
          Scan the news. Read the brief. Review the record.
        </p>
      </div>

      {/* Primary workflow — 3 steps */}
      <div className="home-workflow">
        <div className="home-wf-step">
          <div className="home-wf-num">01</div>
          <div className="home-wf-label">News Scan</div>
          <p className="home-wf-desc">
            Select a universe, a custom portfolio, or individual companies. The pipeline scans
            the news for each company, drafts a brief, and runs novelty checks. Resume from
            the last run or specify a custom date range. One brief per entity per day.
          </p>
          <button className="home-wf-btn" onClick={() => setView("scan")}>Go to News Scan →</button>
        </div>

        <div className="home-wf-divider">
          <div className="home-wf-num" style={{ visibility: "hidden" }}>00</div>
          <div className="home-wf-label" style={{ color: "var(--ink-mute)", textTransform: "none", letterSpacing: 0, fontWeight: 400, marginBottom: 0 }}>→</div>
        </div>

        <div className="home-wf-step">
          <div className="home-wf-num">02</div>
          <div className="home-wf-label">The Brief</div>
          <p className="home-wf-desc">
            Open the morning brief for any company in the universe. The latest output is always
            one click away — novelty-filtered, grounded to source, formatted like a morning note.
            Pick a company, pick a date, read.
          </p>
          <button className="home-wf-btn" onClick={() => setView("brief")}>Go to The Brief →</button>
        </div>

        <div className="home-wf-divider">
          <div className="home-wf-num" style={{ visibility: "hidden" }}>00</div>
          <div className="home-wf-label" style={{ color: "var(--ink-mute)", textTransform: "none", letterSpacing: 0, fontWeight: 400, marginBottom: 0 }}>→</div>
        </div>

        <div className="home-wf-step">
          <div className="home-wf-num">03</div>
          <div className="home-wf-label">Reports</div>
          <p className="home-wf-desc">
            The full record of every run. Browse the archive by company and date, audit each
            pipeline decision bullet by bullet, and drill into compute costs per run.
          </p>
          <button className="home-wf-btn" onClick={() => setView("history")}>Go to Reports →</button>
        </div>
      </div>
    </div>
  );
}
