// Async loader for window.EXTRAS — populated in background, does not block rendering.
window.EXTRAS = {};
fetch("/api/frontend/extras.json")
  .then(function(r) { return r.json(); })
  .then(function(data) { window.EXTRAS = data; })
  .catch(function() {});
