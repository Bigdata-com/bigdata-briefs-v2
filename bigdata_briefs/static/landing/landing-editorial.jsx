// ============================================================
//  Variation A, Editorial-heavy landing page.
//  Reads like the front page of a morning paper: serif display,
//  long deck, prose narrative, hairline rules, sober numbers.
// ============================================================

function LandingEditorial() {
  const universeData = useUniverseData();
  const [universeId, setUniverseId] = React.useState("top_us_100");
  const [topN, setTopN] = React.useState(null); // null = full universe

  const today = React.useMemo(() => {
    const d = new Date();
    return d.toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  }, []);

  // Top-N options scale with universe size
  const fullList = (universeData && universeData[universeId]) || [];
  const topNOptions = React.useMemo(() => {
    const n = fullList.length;
    if (n <= 30) return [10, 25];
    if (n <= 100) return [10, 25, 50];
    if (n <= 200) return [25, 50, 100];
    return [50, 100, 250];
  }, [fullList.length]);

  // reset topN when universe changes
  React.useEffect(() => { setTopN(null); }, [universeId]);

  const companies = selectCompanies(universeData || {}, universeId, topN || undefined);
  const numCompanies = companies.length;

  const universeMeta = UNIVERSE_OPTIONS.find(u => u.id === universeId);
  const manual   = manualCost(numCompanies);
  const pipeline = pipelineCost(companies);

  const savings = manual.totals.cost - pipeline.cost;
  const ratio   = pipeline.cost > 0 ? manual.totals.cost / pipeline.cost : 0;

  const universeShown = topN ? `Top ${topN} of ${universeMeta?.label || ""}` : (universeMeta?.label || "");

  return (
    <div className="landing-edit">
      {/* Masthead */}
      <header className="masthead-wrap">
        <div className="masthead-row">
          <div className="ed-edition">
            <div className="lbl">Vol. I · No. 001</div>
            <div className="dt">{today}</div>
          </div>
          <img className="ed-logo" src="assets/bigdata-logo.svg" alt="bigdata by RavenPack" />
          <div className="ed-actions">
            <span>Product</span>
            <span>Coverage</span>
            <span>Pricing</span>
            <span style={{ color: "var(--accent)" }}>Sign in →</span>
          </div>
        </div>
        <div className="ed-strap">
          <div className="ed-strap-inner">
            <span>The case against the morning huddle</span>
            <span>Issued for buy-side research desks · 2026 edition</span>
          </div>
        </div>
      </header>

      <main className="ed-page">
        {/* Hero */}
        <section className="ed-hero">
          <div className="ed-kicker">A morning-note manifesto</div>
          <h1 className="ed-headline">
            One analyst.<br />
            <em>{numCompanies.toLocaleString()} companies.</em><br />
            One coffee.
          </h1>
          <p className="ed-deck">
            The way every research desk has been writing morning briefs hasn't changed in
            thirty years: open the wires, scan the headlines, read the long ones, write
            it up. We ran the numbers. Then we built the alternative.
          </p>
        </section>

        {/* Universe picker */}
        <section className="ed-picker">
          <span className="ed-picker-label">Choose your coverage universe ▸</span>
          <div className="ed-picker-univs">
            {UNIVERSE_OPTIONS.map(u => (
              <button
                key={u.id}
                className={"univ-pill" + (universeId === u.id ? " active" : "")}
                onClick={() => setUniverseId(u.id)}
              >
                {u.label}
              </button>
            ))}
          </div>
          <div className="ed-topn">
            <span className="ed-topn-label">or top-N</span>
            <div className="ed-topn-buttons">
              <button
                className={"topn-btn" + (topN === null ? " active" : "")}
                onClick={() => setTopN(null)}
              >ALL</button>
              {topNOptions.map(n => (
                <button
                  key={n}
                  className={"topn-btn" + (topN === n ? " active" : "")}
                  onClick={() => setTopN(n)}
                >TOP {n}</button>
              ))}
            </div>
          </div>
        </section>

        {/* The two-column comparison */}
        <section className="ed-compare">
          {/* MANUAL */}
          <div className="ed-col ed-col-manual">
            <span className="ed-col-tag">Scenario A · The desk</span>
            <h2 className="ed-col-headline">The way it gets done today.</h2>
            <p className="ed-col-sub">Wires, scrolls, skims, reads, writes, double-checks.</p>

            <div className="ed-bignum">{fmtUSD(manual.totals.cost)}</div>
            <div className="ed-bignum-foot">
              total · {MANUAL_DEFAULTS.analysts} analyst · {numCompanies.toLocaleString()} co.
            </div>

            <div className="ed-prose">
              <p className="dropcap-init">
                Once a day, the analyst opens{" "}
                <span className="num">{MANUAL_DEFAULTS.numSources} news sources</span> one by one,
                spending about <span className="num">{MANUAL_DEFAULTS.secondsScanPerSource} seconds</span> per source
                scanning headlines across the entire coverage universe. That is{" "}
                <span className="num">{fmtMinShort(manual.scan.totalMin)}</span> before a single
                article is read for any company.
              </p>
              <p>
                Then, for each of the <span className="num">{numCompanies}</span> companies, the analyst
                reads up to <span className="num">{MANUAL_DEFAULTS.maxArticlesRead} articles</span>,
                each averaging <span className="num">{MANUAL_DEFAULTS.avgWordsPerArticle} words</span>.
                At <span className="num">{MANUAL_DEFAULTS.readingSpeed} wpm</span>, that is{" "}
                <span className="num">{fmtMinShort(manual.perCompany.readMin)}</span> per company.
              </p>
              <p>
                Finally, briefs are generated and validated for each company. In total, this process
                costs <span className="num">{fmtUSD(manual.totals.cost)}</span>, with the analyst
                spending <span className="num">{fmtHours(manual.totals.minutes)}</span> on the work.
              </p>
            </div>

            <div className="ed-breakdown">
              <div className="ed-breakdown-row">
                <span className="ed-breakdown-label">Headline scanning (once, all co.)</span>
                <span className="ed-breakdown-val">{Math.round(manual.scan.totalMin)} min</span>
              </div>
              <div className="ed-breakdown-row">
                <span className="ed-breakdown-label">Article reading</span>
                <span className="ed-breakdown-val">{Math.round(manual.perCompany.readMin)} min / co.</span>
              </div>
              <div className="ed-breakdown-row">
                <span className="ed-breakdown-label">Brief validation</span>
                <span className="ed-breakdown-val">{Math.round(manual.perCompany.validateMin)} min / co.</span>
              </div>
              <div className="ed-breakdown-row total">
                <span className="ed-breakdown-label">Total time per analyst</span>
                <span className="ed-breakdown-val">{fmtHours(manual.totals.minutes)}</span>
              </div>
            </div>
          </div>

          {/* PIPELINE */}
          <div className="ed-col ed-col-pipeline">
            <span className="ed-col-tag">Scenario B · The pipeline</span>
            <h2 className="ed-col-headline">Same brief. <em>A fraction of the cost.</em></h2>
            <p className="ed-col-sub">Fired off before the analyst's first inbox-zero of the day.</p>

            <div className="ed-bignum">{fmtUSDsmart(pipeline.cost)}</div>
            <div className="ed-bignum-foot">
              total compute · {numCompanies.toLocaleString()} co. · ~ <span style={{ color: "var(--accent)" }}>${(pipeline.perCompany).toFixed(2)}</span> per company
            </div>

            <div className="ed-prose">
              <p className="dropcap-init">
                Instead of an analyst's morning, the pipeline searches for relevant news on all{" "}
                <span className="num">{numCompanies}</span> companies. An AI drafts the briefs,
                which are then tested against the desk's prior output and verified against the
                historical news record. Only what has genuinely moved is published.
              </p>
              <p>
                It costs <span className="num">{fmtUSDsmart(pipeline.cost)}</span> in compute.
                Every chunk, every model call is billed and accounted for. Every line in every
                brief is grounded back to the source it came from.
              </p>
              <p>
                <span className="lit">In other words:</span> the analyst gets to do the part of
                the job that nobody can automate: picking what matters, asking the better question,
                calling the portfolio manager. Not scrolling Reuters at 7:14 a.m.
              </p>
            </div>

            <div className="ed-breakdown">
              <div className="ed-breakdown-row">
                <span className="ed-breakdown-label">Per company</span>
                <span className="ed-breakdown-val">~ ${pipeline.perCompany.toFixed(2)}</span>
              </div>
              <div className="ed-breakdown-row total">
                <span className="ed-breakdown-label">Total compute</span>
                <span className="ed-breakdown-val">{fmtUSDsmart(pipeline.cost)}</span>
              </div>
              <div className="ed-breakdown-row">
                <span className="ed-breakdown-label">Time per company</span>
                <span className="ed-breakdown-val">&lt; 2 min</span>
              </div>
              <div className="ed-breakdown-row">
                <span className="ed-breakdown-label">Full parallelism (optional)</span>
                <span className="ed-breakdown-val">~ 2 min total</span>
              </div>
            </div>
          </div>
        </section>

        {/* The savings kicker */}
        <section className="ed-savings">
          <div>
            <div className="ed-savings-label">Savings · {universeShown}</div>
            <div className="ed-savings-val">{fmtUSD(savings)}</div>
          </div>
          <div className="ed-savings-mid">
            That's <strong>{ratio >= 100 ? Math.round(ratio).toLocaleString() : ratio.toFixed(0)}×</strong> cheaper
            than what a single analyst would spend to cover the same universe manually, and roughly{" "}
            <strong>{fmtHours(manual.totals.minutes)}</strong> of time redirected from scrolling to thinking.
          </div>
          <div className="ed-savings-meta">
            Per run · per universe<br />Refresh daily, on-demand
          </div>
        </section>

        {/* Method explainer */}
        <h2 className="ed-method-h">How the <em>pipeline</em> reads a thousand wires before breakfast.</h2>
        <div className="ed-method-grid">
          <div className="ed-method-step">
            <div className="ed-method-num">01 · Surface</div>
            <div className="ed-method-title">Relevant news, automatically</div>
            <p className="ed-method-body">
              The pipeline searches across Bigdata's licensed news corpus and surfaces
              the content relevant to each issuer. No source list to maintain,
              no wires to open.
            </p>
          </div>
          <div className="ed-method-step">
            <div className="ed-method-num">02 · First screen</div>
            <div className="ed-method-title">Fast. Catches what was already covered.</div>
            <p className="ed-method-body">
              Every item is quickly checked against the desk's prior briefs. Anything
              already covered is set aside. This pass is fast and efficient: it removes
              the obvious before the deeper check runs.
            </p>
          </div>
          <div className="ed-method-step">
            <div className="ed-method-num">03 · Deep check</div>
            <div className="ed-method-title">Precise. Confirms what actually moved.</div>
            <p className="ed-method-body">
              A thorough search through the historical news record confirms whether
              each remaining item is genuinely new. Slower and more expensive than
              the first pass, but precise. What survives is grounded, attributed,
              and ready to read.
            </p>
          </div>
        </div>

        {/* Footer */}
        <footer className="ed-footer">
          <div>
            <h3 className="ed-foot-cta">Filed before the open. <em>Costed to the cent.</em></h3>
            <p className="ed-foot-sub">
              See a live brief or drop in your own universe.
            </p>
          </div>
          <div className="ed-foot-actions">
            <a href="/app" className="ed-btn ed-btn-primary">Open the app →</a>
          </div>
        </footer>
      </main>
    </div>
  );
}

window.LandingEditorial = LandingEditorial;
