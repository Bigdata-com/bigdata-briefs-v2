// Sync XHR loader for window.EXTRAS — see data.js for rationale.
(function () {
  try {
    const xhr = new XMLHttpRequest();
    xhr.open("GET", "/api/frontend/extras.json", false);
    xhr.send();
    if (xhr.status === 200) {
      window.EXTRAS = JSON.parse(xhr.responseText);
    } else {
      console.error("[extras-data.js] returned", xhr.status);
      window.EXTRAS = {};
    }
  } catch (e) {
    console.error("[extras-data.js] failed to load:", e);
    window.EXTRAS = {};
  }
})();
