// Shared logic and components for both landing page variations.
// All cost-math lives here so the two designs share an identical engine.

const UNIVERSE_OPTIONS = [
  { id: "top_us_100",  label: "Top US 100",   short: "US 100",  region: "US" },
  { id: "top_us_500",  label: "Top US 500",   short: "US 500",  region: "US" },
  { id: "top_us_10",   label: "Top US 10",    short: "US 10",   region: "US" },
  { id: "dow_30",      label: "Dow 30",       short: "DOW 30",  region: "US" },
  { id: "top_eu_100",  label: "Top EU 100",   short: "EU 100",  region: "EU" },
  { id: "top_eu_500",  label: "Top EU 500",   short: "EU 500",  region: "EU" },
  { id: "eurostoxx_50",label: "EURO STOXX 50",short: "STOXX 50",region: "EU" },
];

// Defaults for the manual scenario (locked from the user's brief)
const MANUAL_DEFAULTS = {
  hourlyRate: 48,           // $/hour
  readingSpeed: 350,        // wpm
  briefsPerCompany: 10,
  minutesPerBrief: 1,       // generation + validation
  numSources: 50,
  avgWordsPerArticle: 600,
  secondsScanPerSource: 60,
  maxArticlesRead: 20,
  analysts: 1,
};

function selectCompanies(universeData, universeId, topN) {
  const list = (universeData && universeData[universeId]) || [];
  if (topN && topN < list.length) return list.slice(0, topN);
  return list;
}

function manualCost(numCompanies, p = MANUAL_DEFAULTS) {
  // Scanning all sources is done ONCE across the full universe, not per company.
  const scanMin     = (p.numSources * p.secondsScanPerSource) / 60;
  // Reading and validation are per company.
  const readMin     = (p.maxArticlesRead * p.avgWordsPerArticle) / p.readingSpeed;
  const validateMin = p.briefsPerCompany * p.minutesPerBrief;
  const perCompanyMin = readMin + validateMin;
  const totalMin    = scanMin + perCompanyMin * numCompanies;
  const totalHours  = totalMin / 60;
  const totalCost   = totalHours * p.hourlyRate;
  return {
    scan: { totalMin: scanMin },          // fixed cost, done once
    perCompany: {
      readMin,
      validateMin,
      totalMin: perCompanyMin,
    },
    totals: {
      minutes: totalMin,
      hours: totalHours,
      cost: totalCost,
    },
  };
}

function pipelineCost(companies) {
  // Each entity has a mean DAILY cost in `c`. The pipeline produces one
  // refresh per company per run, so summing once is the per-run cost.
  const total = companies.reduce((s, x) => s + x.c, 0);
  return {
    cost: total,
    perCompany: companies.length ? total / companies.length : 0,
    minPerCompany: 1,                 // pipeline runs in ~minutes, parallelisable
    seconds: companies.length * 60,   // cosmetic estimate
  };
}

function fmtUSD(n, digits = 0) {
  return "$" + n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtUSDsmart(n) {
  if (n >= 100) return fmtUSD(n, 0);
  if (n >= 10)  return fmtUSD(n, 2);
  return fmtUSD(n, 2);
}
function fmtHours(totalMinutes) {
  const h = Math.floor(totalMinutes / 60);
  const m = Math.round(totalMinutes - h * 60);
  if (h <= 0) return `${m}m`;
  return `${h}h ${m}m`;
}
function fmtMinShort(min) {
  if (min < 1) return `${Math.round(min * 60)}s`;
  return `${Math.round(min)} min`;
}

// Useful: load CSV-derived costs JSON
function useUniverseData() {
  const [data, setData] = React.useState(null);
  React.useEffect(() => {
    fetch("assets/universe_costs.json?v=3")
      .then(r => r.json())
      .then(setData)
      .catch(() => setData({}));
  }, []);
  return data;
}

Object.assign(window, {
  UNIVERSE_OPTIONS,
  MANUAL_DEFAULTS,
  selectCompanies,
  manualCost,
  pipelineCost,
  fmtUSD,
  fmtUSDsmart,
  fmtHours,
  fmtMinShort,
  useUniverseData,
});
