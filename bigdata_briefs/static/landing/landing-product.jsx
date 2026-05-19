// ============================================================
//  Variation B — Product-forward landing page.
// ============================================================

// ─── Cost Explorer data ────────────────────────────────────────
const EXPLORER_REGIONS = [
  { id: "index_us",   label: "US",   max: 3000, maxLabel: "3k"   },
  { id: "index_eu",   label: "EU",   max: 1500, maxLabel: "1.5k" },
  { id: "index_asia", label: "Asia", max: 4000, maxLabel: "4k"   },
];
const EXPLORER_TOPN_BASE = [
  { n: 10,   label: "10"  },
  { n: 50,   label: "50"  },
  { n: 100,  label: "100" },
  { n: 250,  label: "250" },
  { n: 500,  label: "500" },
  { n: 1000, label: "1k"  },
  { n: 2000, label: "2k"  },
  { n: 3000, label: "3k"  },
];

// ─── Cumulative chart ──────────────────────────────────────────
// companies   = full universe sorted descending by cost
// targetN     = selected top-N (actual slice count, moves the marker)
// targetLabel = display string for the callout (e.g. "3k" or "100")
function CumulativeCostChart({ companies, targetN, targetLabel }) {
  if (!companies || companies.length === 0) return null;

  const W = 760, H = 260;
  const PAD = { t: 24, r: 72, b: 40, l: 64 };
  const iW = W - PAD.l - PAD.r;
  const iH = H - PAD.t - PAD.b;
  const n = companies.length;

  // Cumulative cost over the full universe
  const cum = [];
  let running = 0;
  for (const co of companies) { running += co.c; cum.push(running); }
  const total = cum[n - 1] || 1;

  // X axis scaled on actual rank values from the CSV
  const minRank = companies[0].r || 1;
  const maxRank = companies[n - 1].r || n;
  const sx = (rank) => PAD.l + ((rank - minRank) / Math.max(maxRank - minRank, 1)) * iW;
  const sy = (cost) => PAD.t + iH - (cost / total) * iH;

  // Full-curve SVG path — X position from actual rank
  const pts = cum.map((c, i) => `${sx(companies[i].r).toFixed(1)},${sy(c).toFixed(1)}`);
  const linePath = "M " + pts.join(" L ");
  const areaPath = `M ${PAD.l.toFixed(1)},${(PAD.t + iH).toFixed(1)} L ${pts.join(" L ")} L ${sx(maxRank).toFixed(1)},${(PAD.t + iH).toFixed(1)} Z`;

  // Y grid
  const yPcts = [0.25, 0.5, 0.75, 1.0];

  // X axis ticks (always show 1 and n, plus a few intermediate)
  const xTicks = (() => {
    if (n <= 30)  return [1, 10, 25, n];
    if (n <= 60)  return [1, 10, 25, 50, n];
    if (n <= 120) return [1, 25, 50, 100, n];
    if (n <= 260) return [1, 50, 100, 200, 250, n];
    return [1, 100, 200, 300, 400, 500];
  })().filter((v, i, a) => a.indexOf(v) === i && v <= n);

  // Target marker — targetN = number of companies selected, use their actual rank
  const tIdx  = Math.min(targetN, n) - 1;
  const tRank = companies[tIdx] ? companies[tIdx].r : maxRank;
  const tX    = sx(tRank);
  const tCost = cum[tIdx] || 0;
  const tY    = sy(tCost);
  const tLabel = tCost >= 10 ? `$${tCost.toFixed(1)}` : `$${tCost.toFixed(2)}`;
  // Place callout left or right depending on position
  const calloutRight = tIdx < n * 0.72;

  const gradId = "ccc-grad";

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}>
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.20" />
          <stop offset="100%" stopColor="var(--accent)" stopOpacity="0.02" />
        </linearGradient>
      </defs>

      {/* Y grid */}
      {yPcts.map(pct => {
        const y = sy(total * pct);
        const val = total * pct;
        const label = val >= 10 ? `$${val.toFixed(0)}` : `$${val.toFixed(1)}`;
        return (
          <g key={pct}>
            <line x1={PAD.l} y1={y} x2={PAD.l + iW} y2={y}
                  stroke="var(--rule)" strokeWidth="1" />
            <text x={PAD.l - 6} y={y + 4} textAnchor="end"
                  fontFamily="var(--mono)" fontSize="10" fill="var(--ink-mute)">
              {label}
            </text>
          </g>
        );
      })}

      {/* Area fill */}
      <path d={areaPath} fill={`url(#${gradId})`} />

      {/* Full curve */}
      <path d={linePath} fill="none" stroke="var(--accent)" strokeWidth="2"
            strokeLinejoin="round" strokeLinecap="round" />


      {/* ── Target marker (moves with topN) ── */}
      {/* Vertical line */}
      <line x1={tX} y1={PAD.t} x2={tX} y2={PAD.t + iH}
            stroke="var(--ink)" strokeWidth="1.5" strokeDasharray="5,3" />
      {/* Horizontal line from Y axis to the dot */}
      <line x1={PAD.l} y1={tY} x2={tX} y2={tY}
            stroke="var(--ink)" strokeWidth="1" strokeDasharray="3,3" opacity="0.5" />
      {/* Dot on the curve */}
      <circle cx={tX} cy={tY} r="5"
              fill="var(--paper)" stroke="var(--ink)" strokeWidth="2" />
      {/* Callout box */}
      <g transform={`translate(${calloutRight ? tX + 10 : tX - 10},${tY - 26})`}>
        <rect x={calloutRight ? 0 : -74} y="0" width="74" height="32"
              fill="var(--ink)" rx="2" />
        <text x={calloutRight ? 6 : -68} y="13"
              fontFamily="var(--mono)" fontSize="10" fontWeight="600" fill="var(--paper)">
          top {targetLabel || targetN}
        </text>
        <text x={calloutRight ? 6 : -68} y="26"
              fontFamily="var(--mono)" fontSize="11" fontWeight="700" fill="var(--paper)">
          {tLabel}/day
        </text>
      </g>

      {/* X axis line */}
      <line x1={PAD.l} y1={PAD.t + iH} x2={PAD.l + iW} y2={PAD.t + iH}
            stroke="var(--ink-mute)" strokeWidth="1" />

      {/* X axis: only the selected top-N rank */}
      <text x={tX} y={PAD.t + iH + 14} textAnchor="middle"
            fontFamily="var(--mono)" fontSize="10" fontWeight="700" fill="var(--ink)">
        {targetLabel || targetN}
      </text>

      {/* Y axis label */}
      <text x={PAD.l - 50} y={PAD.t + iH / 2} textAnchor="middle"
            fontFamily="var(--sans)" fontSize="9" fill="var(--ink-mute)"
            transform={`rotate(-90,${PAD.l - 50},${PAD.t + iH / 2})`}>
        cumulative USD/day
      </text>

    </svg>
  );
}

// ─── Cost Explorer section ──────────────────────────────────────
function CostExplorer({ universeData }) {
  const [regionId, setRegionId] = React.useState("index_us");
  const [topN, setTopN] = React.useState(100);

  const currentRegion = EXPLORER_REGIONS.find(r => r.id === regionId);
  // Full universe — already sorted descending by cost in the JSON
  const allSorted = (universeData && universeData[regionId]) || [];
  // Options: standard breakpoints below max, then the rounded max
  const options = React.useMemo(() => {
    const base = EXPLORER_TOPN_BASE.filter(o => o.n < currentRegion.max);
    return [...base, { n: currentRegion.max, label: currentRegion.maxLabel }];
  }, [regionId]);

  React.useEffect(() => { setTopN(100); }, [regionId]);

  // For the chart marker: clamp to actual data length
  const markerN = Math.min(topN, allSorted.length);
  // Display label for the current selection
  const topNLabel = (options.find(o => o.n === topN) || {}).label || String(topN);

  // Stats: slice to the actual clamped count
  const selected = allSorted.slice(0, markerN);
  const total = selected.reduce((s, c) => s + c.c, 0);
  const avg = selected.length > 0 ? total / selected.length : 0;
  const top1 = allSorted[0];

  const fmtD = (v) => v >= 10 ? `$${v.toFixed(1)}` : `$${v.toFixed(2)}`;

  return (
    <section className="pr-explorer-wrap">
      <div className="pr-explorer">
        <div className="pr-explorer-head">
          <div className="lbl" style={{ marginBottom: 8 }}>Cost at scale</div>
          <h2 className="pr-explorer-h">
            How costs scale <em>with coverage.</em>
          </h2>
          <p className="pr-explorer-deck">
            Cumulative daily pipeline cost across companies, ranked by market cap.
            The curve shows how concentrated cost is at the top of any universe.
          </p>
        </div>

        <div className="pr-explorer-controls">
          <div className="pr-explorer-regions">
            {EXPLORER_REGIONS.map(r => (
              <button key={r.id}
                      className={"pr-region-btn" + (regionId === r.id ? " active" : "")}
                      onClick={() => setRegionId(r.id)}>
                {r.label}
              </button>
            ))}
          </div>
          <div className="pr-seg">
            {options.map(({ n, label }) => (
              <button key={n}
                      className={topN === n ? "active" : ""}
                      onClick={() => setTopN(n)}>
                {label}
              </button>
            ))}
          </div>
        </div>

        <div className="pr-explorer-chart">
          {allSorted.length === 0
            ? <div style={{ height: 260, display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "var(--sans)", fontSize: 12, color: "var(--ink-mute)", letterSpacing: "0.06em" }}>Loading…</div>
            : <CumulativeCostChart companies={allSorted} targetN={markerN} targetLabel={topNLabel} />
          }
        </div>

        <div className="pr-explorer-stats">
          <div className="pr-explorer-stat">
            <div className="pr-explorer-stat-val">{fmtUSDsmart(total)}<span className="pr-explorer-stat-day">/day</span></div>
            <div className="pr-explorer-stat-lbl">Total · top {topNLabel}</div>
          </div>
          <div className="pr-explorer-stat">
            <div className="pr-explorer-stat-val">{fmtD(avg)}<span className="pr-explorer-stat-day">/day</span></div>
            <div className="pr-explorer-stat-lbl">Average per company</div>
          </div>
          <div className="pr-explorer-stat">
            <div className="pr-explorer-stat-val">{top1 ? fmtD(top1.c) : "—"}<span className="pr-explorer-stat-day">/day</span></div>
            <div className="pr-explorer-stat-lbl">
              Highest · {top1 ? top1.name : "—"}
            </div>
          </div>
        </div>

      </div>

      <div className="cost-disclaimer" role="note">
        <div className="cost-disclaimer-body">
          <strong>Note:</strong> The costs shown do not include the required data licence fees.
        </div>
      </div>
    </section>
  );
}

// ─── Main component ─────────────────────────────────────────────
function LandingProduct() {
  const universeData = useUniverseData();

  // Calculator: Top US 500
  const companies = (universeData && universeData["top_us_500"]) || [];
  const numCompanies = companies.length || 500;

  const manual   = manualCost(numCompanies);
  const pipeline = pipelineCost(companies);
  const savings  = manual.totals.cost - pipeline.cost;
  const ratio    = pipeline.cost > 0 ? manual.totals.cost / pipeline.cost : 0;

  return (
    <div className="landing-prod">
      {/* Nav */}
      <nav className="pr-nav">
        <div className="pr-nav-inner">
          <img className="pr-logo" src="/app/desk/bigdata-logo-black.png" alt="Bigdata.com by RavenPack" />
          <div className="pr-nav-spacer"></div>
          <a href="/app/desk" className="pr-btn pr-btn-primary" style={{ padding: "9px 16px" }}>Open the app →</a>
        </div>
      </nav>

      {/* Hero */}
      <section className="pr-hero">
        <h1 className="pr-h1">
          The morning brief.<br />
          <em>Automated.</em>
        </h1>
        <p className="pr-deck">
          We ran the numbers on what it costs a research desk to write morning briefs by hand.
          Then we built the alternative.
        </p>
        <div className="pr-cta-row">
          <a href="/app/desk" className="pr-btn pr-btn-primary">Open the app →</a>
        </div>
      </section>

      {/* Calculator — Top US 500 */}
      <section className="pr-calc-wrap" id="calc">
        <div className="pr-calc">
          <div className="pr-calc-head">
            <div className="pr-calc-h-left">
              <div className="lbl">The cost of the morning huddle</div>
              <h2>What does covering <em>Top US 500</em> cost you?</h2>
            </div>
          </div>

          <div className="pr-calc-grid">
            {/* Manual side */}
            <div className="pr-side pr-side-manual">
              <span className="pr-side-tag">Scenario A · Manual</span>
              <h3 className="pr-side-h">Analyst writes the briefs by hand.</h3>
              <p className="pr-side-sub">Scans, reads, drafts, validates. {MANUAL_DEFAULTS.analysts} analyst at ${MANUAL_DEFAULTS.hourlyRate}/hr.</p>

              <div className="pr-bignum">{fmtUSD(manual.totals.cost)}<span className="small">/day</span></div>
              <div className="pr-bignum-foot">total cost · {numCompanies} co.</div>

              <div className="pr-meta-row">
                <div className="pr-meta">
                  <div className="pr-meta-lbl">Per company</div>
                  <div className="pr-meta-val">{Math.round(manual.perCompany.totalMin)} min</div>
                </div>
                <div className="pr-meta">
                  <div className="pr-meta-lbl">Scanning (once)</div>
                  <div className="pr-meta-val">{Math.round(manual.scan.totalMin)} min</div>
                </div>
                <div className="pr-meta">
                  <div className="pr-meta-lbl">Total</div>
                  <div className="pr-meta-val">{fmtHours(manual.totals.minutes)}</div>
                </div>
              </div>
            </div>

            {/* Pipeline side */}
            <div className="pr-side pr-side-pipeline">
              <span className="pr-side-tag">Scenario B · Pipeline</span>
              <h3 className="pr-side-h">Pipeline runs the briefs automatically.</h3>
              <p className="pr-side-sub">Cost metered per chunk, per token.</p>

              <div className="pr-bignum">{fmtUSDsmart(pipeline.cost)}<span className="small">/day</span></div>
              <div className="pr-bignum-foot">total compute · {numCompanies} co.</div>

              <div className="pr-meta-row" style={{ marginTop: 24, borderTop: "1px solid var(--rule)", paddingTop: 18 }}>
                <div className="pr-meta">
                  <div className="pr-meta-lbl">Per company</div>
                  <div className="pr-meta-val"><em>${pipeline.perCompany.toFixed(2)}</em></div>
                </div>
                <div className="pr-meta">
                  <div className="pr-meta-lbl">Time per company</div>
                  <div className="pr-meta-val">&lt; 2 min</div>
                </div>
                <div className="pr-meta">
                  <div className="pr-meta-lbl">Full parallelism (optional)</div>
                  <div className="pr-meta-val">~ 2 min</div>
                </div>
              </div>
            </div>
          </div>

          {/* Result strip */}
          <div className="pr-result">
            <div className="pr-result-save-group">
              <div className="pr-result-save-big">
                You save {fmtUSD(savings)}<span className="pr-result-save-day">/day</span>
              </div>
              <p className="pr-result-save-sub">
                and roughly <em>{fmtHours(manual.totals.minutes)}</em> of analyst time, every time the desk runs the morning brief.
              </p>
            </div>
            <div style={{ textAlign: "right" }}>
              <div className="pr-result-num">{ratio >= 100 ? Math.round(ratio).toLocaleString() : ratio.toFixed(0)}×</div>
              <div className="pr-result-num-foot">cheaper · per run</div>
            </div>
          </div>
        </div>
      </section>

      {/* Narrative */}
      <section className="pr-narr">
        <div className="pr-narr-block manual">
          <div className="lbl">The manual workflow</div>
          <h3>Open every wire. Skim every headline. Read what survives.</h3>
          <p>
            Once a day, the analyst opens <span className="num">{MANUAL_DEFAULTS.numSources} sources</span> one
            by one, about <span className="num">{MANUAL_DEFAULTS.secondsScanPerSource}s per source</span>,
            scanning headlines across the full universe, which alone takes{" "}
            <span className="num">{fmtMinShort(manual.scan.totalMin)}</span>.
          </p>
          <p>
            Then, for each of the <span className="num">{numCompanies}</span> companies, the analyst
            reads up to <span className="num">{MANUAL_DEFAULTS.maxArticlesRead} articles</span>,
            roughly <span className="num">{MANUAL_DEFAULTS.avgWordsPerArticle} words</span> each.
            At <span className="num">{MANUAL_DEFAULTS.readingSpeed} wpm</span>, that is another{" "}
            <span className="num">{fmtMinShort(manual.perCompany.readMin)}</span> per company.
            Finally, briefs are drafted and validated for each name.
          </p>
          <p>
            All in: <span className="num">{fmtUSD(manual.totals.cost)}/day</span> and{" "}
            <span className="num">{fmtHours(manual.totals.minutes)}</span> of analyst time to cover{" "}
            <span className="num">{numCompanies} companies</span> once.
          </p>
        </div>

        <div className="pr-narr-block pipeline">
          <div className="lbl">The pipeline workflow</div>
          <h3>Search, draft, then <em>verify</em> what actually moved.</h3>
          <p>
            The pipeline searches for relevant news on each company. No source list
            to maintain, no wires to open. The briefs are drafted, then tested against
            the desk's prior output and confirmed against the historical news record.
            Only what has materially changed since the last run is published.
          </p>
          <p>
            Every claim is grounded back to its source. Compute is metered per token
            and per chunk, so the bill is honest down to the cent.
          </p>
          <p>
            All in: <span className="num">{fmtUSDsmart(pipeline.cost)}/day</span> for the same{" "}
            <span className="num">{numCompanies} companies</span>, and the analyst spends the morning
            thinking, not scrolling.
          </p>
        </div>
      </section>

      {/* Method */}
      <section className="pr-method" id="how">
        <h2 className="pr-method-h">Three steps. <em>One brief.</em></h2>
        <p className="pr-method-deck">
          Search, cross-reference, verify. Every step is metered and grounded.
        </p>
        <div className="pr-method-grid">
          <div className="pr-method-card">
            <div className="step">01 · Surface</div>
            <h4>Relevant news, automatically</h4>
            <p>The pipeline searches Bigdata's licensed news corpus and surfaces the content relevant to each issuer. No source list to maintain, no wires to open.</p>
          </div>
          <div className="pr-method-card">
            <div className="step">02 · First screen</div>
            <h4>Fast. Catches what was already covered.</h4>
            <p>Every item is quickly checked against the desk's prior briefs. Anything already covered is set aside. Fast and efficient: removes the obvious before the deeper check runs.</p>
          </div>
          <div className="pr-method-card">
            <div className="step">03 · Deep check</div>
            <h4>Precise. Confirms what actually moved.</h4>
            <p>A thorough search through the historical news record confirms whether each remaining item is genuinely new. Slower and more expensive than the first pass, but precise. What survives is grounded, attributed, and ready to read.</p>
          </div>
        </div>
      </section>

      {/* Cost Explorer */}
      <CostExplorer universeData={universeData} />

      {/* Footer CTA */}
      <section className="pr-foot">
        <h2 className="pr-foot-h">Run the calc on <em>your</em> universe.</h2>
        <p className="pr-foot-deck">
          Drop us your watchlist or pick a benchmark universe and we'll show you what it costs.
        </p>
        <div className="pr-cta-row">
          <a href="/app/desk" className="pr-btn pr-btn-primary">Open the app →</a>
        </div>
      </section>
    </div>
  );
}

window.LandingProduct = LandingProduct;
