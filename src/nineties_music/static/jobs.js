(function () {
  "use strict";

  var container = document.getElementById("jobs");
  if (!container) return;

  function text(value) {
    return document.createTextNode(value == null ? "" : String(value));
  }

  function render(data) {
    container.replaceChildren();
    if (!data.jobs.length) {
      var empty = document.createElement("p");
      empty.appendChild(text("No downloads are running."));
      container.appendChild(empty);
      return;
    }
    var list = document.createElement("ul");
    data.jobs.forEach(function (job) {
      var item = document.createElement("li");
      var link = document.createElement("a");
      link.href = job.detail_url;
      link.appendChild(text(job.title));
      item.appendChild(link);
      var progress = job.progress || {};
      item.appendChild(text(
        ": " + job.status + " — " +
        (progress.track_index || "0") + " / " +
        (progress.track_total || "?") + " — " +
        (progress.current_title || "Waiting") + " " +
        (progress.percent || "")
      ));
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  function poll() {
    fetch(container.dataset.jobsUrl, {headers: {"Accept": "application/json"}})
      .then(function (response) {
        if (!response.ok) throw new Error("status request failed");
        return response.json();
      })
      .then(render)
      .catch(function () {})
      .finally(function () { window.setTimeout(poll, 2000); });
  }

  window.setTimeout(poll, 500);
}());

