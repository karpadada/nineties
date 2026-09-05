(function () {
  "use strict";

  var state = document.getElementById("storage-state");
  if (!state) return;
  var wasAvailable = state.dataset.storageAvailable === "true";

  function schedule() {
    window.setTimeout(poll, 2000);
  }

  function poll() {
    fetch(state.dataset.storageUrl, {headers: {"Accept": "application/json"}})
      .then(function (response) {
        if (!response.ok) throw new Error("storage status request failed");
        return response.json();
      })
      .then(function (data) {
        if (Boolean(data.storage_available) !== wasAvailable) {
          if (state.dataset.storageReturnUrl) {
            window.location.assign(state.dataset.storageReturnUrl);
            return;
          }
          window.location.reload();
          return;
        }
        schedule();
      })
      .catch(schedule);
  }

  schedule();
}());
