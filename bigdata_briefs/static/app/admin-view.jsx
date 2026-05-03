// Admin — destructive actions with explicit confirmations.
const { useState: useStateA, useEffect: useEffectA } = React;

function AdminView({ tweaks }) {
  const companies = window.DATA.companies || [];
  const [resetConfirm, setResetConfirm] = useStateA(0);
  const [resetText, setResetText] = useStateA("");
  const [resetBusy, setResetBusy] = useStateA(false);
  const [resetError, setResetError] = useStateA(null);
  const [deleteEntity, setDeleteEntity] = useStateA(companies[0]?.id || "");
  const [deleteIdManual, setDeleteIdManual] = useStateA("");
  const [deleteConfirm, setDeleteConfirm] = useStateA(0);
  const [deleteBusy, setDeleteBusy] = useStateA(false);
  const [deleteError, setDeleteError] = useStateA(null);
  const [stats, setStats] = useStateA(null);

  const armed = resetText.trim().toUpperCase() === "RESET DATABASE";
  const targetEntity = companies.find(c => c.id === deleteEntity) || companies[0];
  const deleteTargetId = (deleteIdManual || targetEntity?.id || "").trim();

  useEffectA(() => {
    fetch("/api/frontend/admin/stats")
      .then(r => r.json())
      .then(setStats)
      .catch(console.error);
  }, []);

  function refreshStats() {
    fetch("/api/frontend/admin/stats")
      .then(r => r.json())
      .then(setStats)
      .catch(console.error);
  }

  function doReset() {
    setResetError(null);
    setResetBusy(true);
    fetch("/api/frontend/admin/reset", { method: "POST" })
      .then(r => {
        if (!r.ok) return r.text().then(t => { throw new Error(t || r.statusText); });
        return r.json();
      })
      .then(() => {
        setResetConfirm(2);
        refreshStats();
      })
      .catch(e => setResetError(String(e.message || e)))
      .finally(() => setResetBusy(false));
  }

  function doDelete() {
    const id = deleteTargetId;
    if (!id) return;
    setDeleteError(null);
    setDeleteBusy(true);
    fetch(`/api/frontend/admin/entity/${encodeURIComponent(id)}`, { method: "DELETE" })
      .then(r => {
        if (!r.ok) return r.text().then(t => { throw new Error(t || r.statusText); });
        return r.json();
      })
      .then(() => {
        setDeleteConfirm(2);
        refreshStats();
      })
      .catch(e => setDeleteError(String(e.message || e)))
      .finally(() => setDeleteBusy(false));
  }

  const s = stats;

  if (companies.length === 0) {
    return (
      <div className="admin-layout" style={{ padding: 32, fontFamily: "var(--sans)" }}>
        <p style={{ color: "var(--ink-mute)" }}>No companies loaded. If you just reset the database, reload the app after re-seeding entities.</p>
      </div>
    );
  }

  return (
    <div className="admin-layout">
      <header className="admin-header">
        <div className="dateline">Admin · destructive operations</div>
        <h1 className="display admin-title">Danger zone.</h1>
        <p className="admin-lede">
          These actions cannot be undone. Each requires explicit confirmation. The pipeline
          will keep running on whatever data remains after the operation completes.
        </p>
      </header>

      <div className="admin-grid">
        <section className="admin-card admin-card-danger">
          <div className="admin-card-head">
            <span className="admin-tag">Catastrophic</span>
            <h2 className="admin-card-title">Reset entire database</h2>
            <p className="admin-card-desc">
              Wipes <strong>everything</strong>: all runs, all bullets, all embeddings, all novelty
              history, all source-deduplication hashes, all cost metrics. Tables are dropped and
              recreated empty. Use only when starting completely from scratch.
            </p>
          </div>

          <ul className="admin-impact">
            <li>
              <strong className="tnum">{s ? s.totalRuns : "—"}</strong> entity runs ·{" "}
              <strong className="tnum">{s ? s.failedRuns : "—"}</strong> failed
            </li>
            <li>
              <strong className="tnum">{s ? s.activeBullets.toLocaleString() : "—"}</strong> active bullets ·{" "}
              <strong className="tnum">{s ? s.totalBullets.toLocaleString() : "—"}</strong> total rows
            </li>
            <li>
              <strong className="tnum">{s ? s.embeddings.toLocaleString() : "—"}</strong> embeddings ·{" "}
              <strong className="tnum">{s ? s.chunkHashes.toLocaleString() : "—"}</strong> chunk hash rows
            </li>
            <li>Counts refresh from the database on load and after each operation.</li>
          </ul>

          {resetConfirm === 0 && (
            <button className="admin-btn admin-btn-danger"
                    onClick={() => setResetConfirm(1)}>
              I understand · proceed
            </button>
          )}
          {resetConfirm === 1 && (
            <div className="admin-confirm">
              <p className="admin-confirm-prompt">
                Type <code>RESET DATABASE</code> to confirm:
              </p>
              <input className="admin-confirm-input"
                     type="text"
                     value={resetText}
                     onChange={(e) => setResetText(e.target.value)}
                     placeholder="RESET DATABASE" />
              {resetError && (
                <p style={{ color: "var(--discard)", fontFamily: "var(--sans)", fontSize: 12, marginTop: 8 }}>{resetError}</p>
              )}
              <div className="admin-confirm-row">
                <button className="btn" onClick={() => { setResetConfirm(0); setResetText(""); setResetError(null); }}>
                  Cancel
                </button>
                <button className="admin-btn admin-btn-danger"
                        disabled={!armed || resetBusy}
                        onClick={doReset}>
                  {resetBusy ? "Resetting…" : "Reset database now"}
                </button>
              </div>
            </div>
          )}
          {resetConfirm === 2 && (
            <div className="admin-result admin-result-ok">
              <div className="admin-result-head">✓ Database reset</div>
              <p>All tables dropped and recreated. <span className="t-mono">brief_pipeline_state</span> is empty.</p>
              <button className="btn" onClick={() => { setResetConfirm(0); setResetText(""); setResetError(null); }}>Done</button>
            </div>
          )}
        </section>

        <section className="admin-card">
          <div className="admin-card-head">
            <span className="admin-tag admin-tag-warn">Targeted</span>
            <h2 className="admin-card-title">Delete entity data</h2>
            <p className="admin-card-desc">
              Removes <strong>all</strong> data for a single company — runs, bullets, embeddings,
              checkpoints, source hashes — leaving every other entity untouched. Use when re-running
              an entity from scratch is needed without losing other coverage.
            </p>
          </div>

          <div className="admin-form">
            <label className="admin-field">
              <span className="t-cap">Pick from known entities</span>
              <select value={deleteEntity}
                      onChange={(e) => setDeleteEntity(e.target.value)}>
                {companies.map(c => (
                  <option key={c.id} value={c.id}>{c.name} · {c.ticker} · {c.id}</option>
                ))}
              </select>
            </label>
            <div className="admin-or">or type entity ID manually</div>
            <label className="admin-field">
              <span className="t-cap">Entity ID</span>
              <input className="t-mono"
                     type="text"
                     value={deleteIdManual}
                     onChange={(e) => setDeleteIdManual(e.target.value.toUpperCase())}
                     placeholder="e.g. D8442A" />
            </label>
          </div>

          {targetEntity && deleteConfirm < 2 && (
            <div className="admin-impact-card">
              <div className="t-cap">Will remove for <strong className="t-mono">{deleteTargetId}</strong></div>
              <ul>
                <li>All pipeline runs and bullet logs for this entity ID.</li>
                <li>Embeddings, checkpoints, and orchestration rows tied to the entity.</li>
                <li>Other entities in the database are not modified.</li>
              </ul>
            </div>
          )}

          {deleteConfirm === 0 && (
            <button className="admin-btn admin-btn-warn"
                    onClick={() => setDeleteConfirm(1)}
                    disabled={!deleteTargetId}>
              Delete entity data
            </button>
          )}
          {deleteConfirm === 1 && (
            <div className="admin-confirm">
              <p className="admin-confirm-prompt">
                Confirm: delete all data for{" "}
                <strong>{targetEntity?.name || deleteTargetId}</strong> (<code>{deleteTargetId}</code>)?
              </p>
              {deleteError && (
                <p style={{ color: "var(--discard)", fontFamily: "var(--sans)", fontSize: 12, marginTop: 8 }}>{deleteError}</p>
              )}
              <div className="admin-confirm-row">
                <button className="btn" onClick={() => { setDeleteConfirm(0); setDeleteError(null); }}>Cancel</button>
                <button className="admin-btn admin-btn-warn"
                        disabled={deleteBusy}
                        onClick={doDelete}>
                  {deleteBusy ? "Deleting…" : "Yes, delete"}
                </button>
              </div>
            </div>
          )}
          {deleteConfirm === 2 && (
            <div className="admin-result admin-result-ok">
              <div className="admin-result-head">✓ Entity removed</div>
              <p>All data for <strong>{targetEntity?.name || deleteTargetId}</strong> deleted. Other entities unaffected.</p>
              <button className="btn" onClick={() => { setDeleteConfirm(0); setDeleteError(null); }}>Done</button>
            </div>
          )}
        </section>
      </div>

      <footer className="admin-footer">
        <div className="t-cap">Database snapshot</div>
        <p className="muted" style={{ fontFamily: "var(--sans)", fontSize: 12, marginTop: 4 }}>
          Destructive operations are executed on the server immediately after confirmation. There is no client-side audit trail;
          check application logs for a record of admin calls.
        </p>
      </footer>
    </div>
  );
}

window.AdminView = AdminView;
