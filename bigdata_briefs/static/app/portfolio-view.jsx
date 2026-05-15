// ── My Portfolio view (Change 3) ──────────────────────────────────────
// Left panel: portfolio composition (add/remove/search + dates + start).
// Right panel: empty by default; on "Start update" shows a support-contact box.

const { useState: useStateP, useEffect: useEffectP, useRef: useRefP } = React;

const PORTFOLIO_STORAGE_KEY = "bigdata.briefs.portfolio.v1";

function loadPortfolio() {
  try {
    const raw = localStorage.getItem(PORTFOLIO_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length > 0) return parsed;
    }
  } catch {}
  // Default seed: 5 names
  return ["AAPL01", "MSFT01", "NVDA01", "JPM001", "TSLA01"];
}

function PortfolioView({ tweaks }) {
  const ALL = window.DATA?.companies || [];
  const today = new Date().toISOString().slice(0, 10);
  const now = new Date();
  const hh = String(now.getHours()).padStart(2, "0");
  const mm = String(now.getMinutes()).padStart(2, "0");

  const [portfolio, setPortfolio] = useStateP(loadPortfolio);
  const [search, setSearch] = useStateP("");
  const [showResults, setShowResults] = useStateP(false);
  const [updateDate, setUpdateDate] = useStateP(today);
  const [updateTime, setUpdateTime] = useStateP(`${hh}:${mm}`);
  const [showSupport, setShowSupport] = useStateP(false);

  const searchRef = useRefP(null);

  useEffectP(() => {
    try { localStorage.setItem(PORTFOLIO_STORAGE_KEY, JSON.stringify(portfolio)); } catch {}
  }, [portfolio]);

  // Close search dropdown on outside click
  useEffectP(() => {
    function handler(e) {
      if (searchRef.current && !searchRef.current.contains(e.target)) setShowResults(false);
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const portfolioCompanies = portfolio
    .map(id => ALL.find(c => c.id === id))
    .filter(Boolean);

  const searchLower = search.trim().toLowerCase();
  const searchResults = searchLower
    ? ALL
        .filter(c =>
          !portfolio.includes(c.id) &&
          (c.name.toLowerCase().includes(searchLower) || (c.ticker || "").toLowerCase().includes(searchLower))
        )
        .slice(0, 8)
    : [];

  function addCompany(id) {
    if (portfolio.includes(id)) return;
    setPortfolio([...portfolio, id]);
    setSearch("");
    setShowResults(false);
  }
  function removeCompany(id) {
    setPortfolio(portfolio.filter(x => x !== id));
  }
  function handleStart() {
    setShowSupport(true);
  }

  return (
    <div className="portfolio-layout">
      {/* LEFT: portfolio composition + controls */}
      <aside className="portfolio-left">
        <div className="dateline">My Portfolio</div>
        <h1 className="scan-config-title">Build your <em>portfolio</em>.</h1>
        <p className="scan-config-lede">
          Track the companies that matter to you. Briefs are generated each morning for everything in this list.
        </p>

        {/* Add bar */}
        <div className="portfolio-add-row" ref={searchRef}>
          <input
            className="portfolio-add-input"
            type="text"
            placeholder="Search ticker or company name…"
            value={search}
            onChange={e => { setSearch(e.target.value); setShowResults(true); }}
            onFocus={() => setShowResults(true)}
            autoComplete="off"
          />
          <button
            className="portfolio-add-btn"
            disabled={searchResults.length === 0}
            onClick={() => searchResults[0] && addCompany(searchResults[0].id)}
          >
            + Add
          </button>
          {showResults && search && (
            <div className="portfolio-search-results">
              {searchResults.length === 0 ? (
                <div className="portfolio-search-empty">
                  {portfolio.some(id => {
                    const c = ALL.find(x => x.id === id);
                    return c && (c.name.toLowerCase().includes(searchLower) || c.ticker.toLowerCase().includes(searchLower));
                  })
                    ? "Already in your portfolio."
                    : "No matches in the coverage universe."}
                </div>
              ) : (
                searchResults.map(c => (
                  <button key={c.id} className="portfolio-search-result" onClick={() => addCompany(c.id)}>
                    <span className="portfolio-search-result-ticker">{c.ticker}</span>
                    <span className="portfolio-search-result-name">{c.name}</span>
                    <span className="portfolio-search-result-add">+ add</span>
                  </button>
                ))
              )}
            </div>
          )}
        </div>

        <div className="portfolio-list-count">
          <span>Holdings</span>
          <span>{portfolioCompanies.length} {portfolioCompanies.length === 1 ? "company" : "companies"}</span>
        </div>

        {/* Date + time */}
        <section className="portfolio-date-section">
          <div className="scan-step-num" style={{ marginBottom: 6 }}>02</div>
          <h2 className="scan-section-title">Update window</h2>
          <div className="portfolio-date-row">
            <div>
              <label>Date</label>
              <input type="date" value={updateDate} max={today} onChange={e => setUpdateDate(e.target.value)} />
            </div>
            <div>
              <label>Time</label>
              <input type="time" value={updateTime} onChange={e => setUpdateTime(e.target.value)} />
            </div>
          </div>
        </section>

        <button
          className="portfolio-start-btn"
          onClick={handleStart}
          disabled={portfolioCompanies.length === 0}
        >
          ▶&nbsp; Start update
        </button>
      </aside>

      {/* RIGHT: portfolio companies list → support box on Start */}
      <main className="portfolio-right">
        {!showSupport ? (
          <div className="portfolio-right-list-wrap">
            <header className="portfolio-right-header">
              <div className="dateline">Holdings</div>
              <h2 className="portfolio-right-title">
                {portfolioCompanies.length === 0
                  ? <>Your portfolio is <em>empty</em>.</>
                  : <><em>{portfolioCompanies.length}</em> {portfolioCompanies.length === 1 ? "company" : "companies"} tracked</>}
              </h2>
              <p className="portfolio-right-sub">
                {portfolioCompanies.length === 0
                  ? "Search on the left to add your first company."
                  : "Briefs are generated each morning for everything in this list. Remove any name to stop tracking."}
              </p>
            </header>

            {portfolioCompanies.length > 0 ? (
              <ul className="portfolio-list portfolio-list-right">
                {portfolioCompanies.map(c => (
                  <li key={c.id} className="portfolio-list-row">
                    <span className="portfolio-list-ticker">{c.ticker}</span>
                    <span className="portfolio-list-name">
                      {c.name}
                      <span className="meta">{c.sector?.split(" ")[0]} · {c.exchange}</span>
                    </span>
                    <button className="portfolio-list-remove" onClick={() => removeCompany(c.id)} aria-label={`Remove ${c.name}`}>
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="portfolio-list portfolio-list-right" style={{ background: "var(--paper)" }}>
                <div className="portfolio-list-empty">
                  Your portfolio is empty. Search on the left to add your first company.
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="portfolio-support-box">
            <div className="portfolio-support-box-eyebrow">Update unavailable</div>
            <h2 className="portfolio-support-box-title">Contact support to enable portfolio updates.</h2>
            <p className="portfolio-support-box-body">
              Portfolio update orchestration is not available in your current workspace.
              Reach out and the team will configure it for you.
            </p>
            <a className="portfolio-support-box-link" href="mailto:support@bigdata.com">
              support@bigdata.com
            </a>
            <div className="portfolio-support-box-meta">
              Window requested: {updateDate} · {updateTime}
            </div>
            <div style={{ marginTop: 18 }}>
              <button
                className="portfolio-list-remove"
                style={{ padding: "8px 14px", fontSize: 12 }}
                onClick={() => setShowSupport(false)}
              >
                Back to configuration
              </button>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

window.PortfolioView = PortfolioView;
