// ── My Portfolio view ──────────────────────────────────────────────────
// Left panel: portfolio composition (add/remove/search).
// Right panel: holdings list; in PUBLIC_MODE, add/remove shows a support-contact box.

const { useState: useStateP, useEffect: useEffectP, useRef: useRefP } = React;

function PortfolioView({ tweaks, appPortfolio, setView }) {
  // portfolio: array of {entity_id, entity_name, kg_ticker} objects (loaded from API)
  const [portfolio, setPortfolio] = useStateP(appPortfolio || []);
  const [portfolioLoaded, setPortfolioLoaded] = useStateP(appPortfolio !== null);
  const [allCandidates, setAllCandidates] = useStateP([]);
  const [search, setSearch] = useStateP("");
  const [showResults, setShowResults] = useStateP(false);
  const [showSupport, setShowSupport] = useStateP(false);
  const publicMode = window.DATA?.publicMode === true;

  const searchRef = useRefP(null);

  // Load portfolio and universe candidates on mount
  useEffectP(() => {
    if (!appPortfolio) {
      fetch("/api/frontend/portfolio")
        .then(r => r.json())
        .then(data => {
          setPortfolio(data.portfolio || []);
          setPortfolioLoaded(true);
        })
        .catch(() => setPortfolioLoaded(true));
    }

    fetch("/api/frontend/portfolio/candidates")
      .then(r => r.json())
      .then(data => setAllCandidates(data.candidates || []))
      .catch(() => {});
  }, []);

  // Close search dropdown on outside click
  useEffectP(() => {
    function handler(e) {
      if (searchRef.current && !searchRef.current.contains(e.target)) setShowResults(false);
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const portfolioIds = new Set(portfolio.map(p => p.entity_id));
  const portfolioCompanies = portfolio; // full objects from API

  const searchLower = search.trim().toLowerCase();
  const searchResults = searchLower
    ? allCandidates
        .filter(c =>
          !portfolioIds.has(c.id) &&
          (c.name.toLowerCase().includes(searchLower) || (c.ticker || "").toLowerCase().includes(searchLower))
        )
        .slice(0, 8)
    : [];

  function addCompany(id) {
    setSearch("");
    setShowResults(false);
    if (publicMode) { setShowSupport(true); return; }
    const candidate = allCandidates.find(c => c.id === id);
    fetch("/api/frontend/portfolio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entity_id: id, entity_name: candidate?.name, kg_ticker: candidate?.ticker }),
    })
      .then(r => r.json())
      .then(() => fetch("/api/frontend/portfolio").then(r => r.json()).then(d => setPortfolio(d.portfolio || [])))
      .catch(() => {});
  }
  function removeCompany(id) {
    if (publicMode) { setShowSupport(true); return; }
    fetch(`/api/frontend/portfolio/${encodeURIComponent(id)}`, { method: "DELETE" })
      .then(() => setPortfolio(prev => prev.filter(p => p.entity_id !== id)))
      .catch(() => {});
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
                  {portfolio.some(p =>
                    p.entity_name.toLowerCase().includes(searchLower) ||
                    (p.kg_ticker || "").toLowerCase().includes(searchLower)
                  )
                    ? "Already in your portfolio."
                    : "No matches in the coverage universe."}
                </div>
              ) : (
                searchResults.map(c => (
                  <button key={c.id} className="portfolio-search-result" onClick={() => addCompany(c.id)}>
                    <span className="portfolio-search-result-ticker">{_tk(c.ticker)}</span>
                    <span className="portfolio-search-result-name">{c.name}</span>
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
                {portfolioCompanies.map(p => (
                  <li key={p.entity_id} className="portfolio-list-row">
                    <span className="portfolio-list-ticker">{p.kg_ticker || "PRIVATE"}</span>
                    <span className="portfolio-list-name">{p.entity_name}</span>
                    <button className="portfolio-list-remove" onClick={() => removeCompany(p.entity_id)} aria-label={`Remove ${p.entity_name}`}>
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
            <p className="portfolio-support-box-title">Contact support to enable portfolio updates.</p>
            <a className="portfolio-support-box-link" href="mailto:support@bigdata.com">
              support@bigdata.com
            </a>
          </div>
        )}
      </main>
    </div>
  );
}

window.PortfolioView = PortfolioView;
