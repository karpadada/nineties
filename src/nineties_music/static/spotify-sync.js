(function () {
  "use strict";

  var container = document.getElementById("spotify-sync-progress");
  if (!container) return;

  var status = document.getElementById("spotify-sync-status");
  var counts = document.getElementById("spotify-sync-counts");
  var bar = document.getElementById("spotify-sync-bar");
  var phaseLabels = {
    queued: "Preparing the Spotify playlist",
    starting: "Reading the Spotify playlist",
    matching: "Searching YouTube Music for",
    reusing: "Reusing the existing file for",
    downloading: "Downloading",
    processed: "Processed",
    finalizing: "Finalizing the music directory"
  };

  function number(value) {
    var parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
  }

  function render(job) {
    var progress = job.progress || {};
    var completed = number(progress.completed_total);
    var total = number(progress.track_total);
    var available = number(progress.available_total);
    var missing = number(progress.missing_total);
    var currentPosition = number(progress.current_position);
    var currentTitle = progress.current_title || "";
    var label = phaseLabels[progress.phase] || "Syncing";

    if (total > 0) {
      bar.max = total;
      bar.value = Math.min(completed, total);
    } else {
      bar.max = 1;
      bar.removeAttribute("value");
    }

    if (currentTitle && currentPosition && total) {
      status.textContent = label + " " + currentPosition + "/" + total +
        " — " + currentTitle;
    } else {
      status.textContent = label + (total ? " — 0/" + total : "…");
    }
    counts.textContent = "Processed " + completed + " of " +
      (total || "?") + " tracks. Available: " + available +
      ". Missing: " + missing + ".";

    if (job.terminal) {
      status.textContent = job.status === "failed"
        ? (job.error || "The Spotify playlist sync failed.")
        : "Sync finished. Loading the final report…";
      window.setTimeout(function () { window.location.reload(); }, 250);
      return false;
    }
    return true;
  }

  function poll() {
    fetch(container.dataset.statusUrl, {
      cache: "no-store",
      headers: {"Accept": "application/json"}
    })
      .then(function (response) {
        if (!response.ok) throw new Error("status request failed");
        return response.json();
      })
      .then(function (job) {
        if (render(job)) window.setTimeout(poll, 750);
      })
      .catch(function () {
        status.textContent = "Progress is temporarily unavailable; retrying…";
        window.setTimeout(poll, 2000);
      });
  }

  window.setTimeout(poll, 250);
}());
