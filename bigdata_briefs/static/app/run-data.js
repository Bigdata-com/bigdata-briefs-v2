// Sync XHR loader for window.RUN_DATA — see data.js for rationale.
(function () {
  try {
    const xhr = new XMLHttpRequest();
    xhr.open("GET", "/api/frontend/run-data.json", false);
    xhr.send();
    if (xhr.status === 200) {
      window.RUN_DATA = JSON.parse(xhr.responseText);
    } else {
      console.error("[run-data.js] returned", xhr.status);
      window.RUN_DATA = {};
    }
  } catch (e) {
    console.error("[run-data.js] failed to load:", e);
    window.RUN_DATA = {};
  }
})();
