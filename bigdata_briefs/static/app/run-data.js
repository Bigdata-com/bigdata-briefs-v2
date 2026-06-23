// Async loader for window.RUN_DATA — populated in background, does not block rendering.
window.RUN_DATA = {};
fetch("/api/frontend/run-data.json")
  .then(function(r) { return r.json(); })
  .then(function(data) { window.RUN_DATA = data; })
  .catch(function() {});
