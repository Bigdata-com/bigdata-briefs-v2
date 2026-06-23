// Sync XHR to /api/frontend/data.json — runs before Babel/JSX so window.DATA
// is ready when components mount. The endpoint returns real DB data with the
// theme palette and sensible defaults for fields without backend data yet.
(function () {
  try {
    const xhr = new XMLHttpRequest();
    xhr.open("GET", "/api/frontend/data.json", false);
    xhr.send();
    if (xhr.status === 200) {
      window.DATA = JSON.parse(xhr.responseText);
    } else {
      console.error("[data.js] /api/frontend/data.json returned", xhr.status);
      window.DATA = {};
    }
  } catch (e) {
    console.error("[data.js] failed to load:", e);
    window.DATA = {};
  }
})();
