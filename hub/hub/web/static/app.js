/* amd-hub's client, in one file.
 *
 * No HTMX. The brief named server-rendered HTMX templates, and the server-rendered half is
 * what is here -- every page arrives complete from Jinja2 and every action is a form that
 * works without this file. What is missing is the HTMX *runtime*: htmx's live-update
 * extensions would otherwise provide the queue connection, and a ~3 KB script fetched from
 * a CDN at page load is a worse trade for a tool on a home LAN than native WebSocket support.
 * The queue socket
 * reconnects with backoff and receives a fresh snapshot after every reconnect. The templates
 * use `data-action` attributes rather than `hx-*` ones -- a `hx-post` on a page with no htmx
 * silently does nothing, which is the worst of both.
 *
 * (This line used to say "about a hundred lines", and said so for long enough that it had
 * become the file's least accurate statement. A header that understates the file is not
 * harmless: it is the number someone quotes when deciding whether to read it.)
 *
 * The two things this file must not get wrong:
 *
 *   - the snapshot. The stream sends every active job and a bounded recent terminal window,
 *     then one change at a time. A reconnect replaces that window and rehydrates history pages
 *     the user already opened; applying a snapshot as a merge would leave deleted rows visible.
 *   - the rows. `textContent` everywhere, never `innerHTML`. `skip_reason` holds directory
 *     names read off the filesystem, and a user-curated library really does contain
 *     directories called `<img src=x onerror=...>`. The server escapes them too; this is the
 *     second of the two, not the only.
 */
(function () {
  "use strict";

  /* The copy buttons live wherever a path is shown -- a skipped row's matched paths, a
   * library listing's album directories -- so this half runs before the queue guard below,
   * which returns early on pages that have no queue. Clipboard honesty: `navigator.clipboard`
   * only exists in a secure context, and the hub is served over plain HTTP on a LAN, so the
   * legacy path is not a fallback, it is the normal route. Both end in the same feedback:
   * the button says what happened, for a moment, and the text is never trusted into
   * `innerHTML` anywhere.
   */
  function copyFeedback(button, ok) {
    button.textContent = ok ? "copied" : "failed";
    window.setTimeout(function () {
      button.textContent = "copy";
    }, 1200);
  }

  function legacyCopy(text, done) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }
    document.body.removeChild(area);
    done(ok);
  }

  function copyText(button) {
    var text = button.dataset.text || "";
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { copyFeedback(button, true); },
        function () { legacyCopy(text, function (ok) { copyFeedback(button, ok); }); }
      );
    } else {
      legacyCopy(text, function (ok) { copyFeedback(button, ok); });
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest('[data-action="copy-text"]');
    if (target) copyText(target);
  });

  initLibraryPage();

  function initLibraryPage() {
    var search = document.getElementById("library-search");
    var rows = document.getElementById("library-albums");
    if (!search || !rows) return;
    var count = document.getElementById("library-result-count");
    var timer = null;
    var request = 0;

    function text(tag, value, className) {
      var node = document.createElement(tag);
      if (className) node.className = className;
      node.textContent = value == null ? "" : String(value);
      return node;
    }

    function drawAlbums(albums, total) {
      rows.replaceChildren();
      albums.forEach(function (album) {
        var tr = document.createElement("tr");
        tr.appendChild(text("td", album.artist || "—"));
        tr.appendChild(text("td", album.name || "—"));
        tr.appendChild(text("td", album.tracks, "num"));
        var pathCell = text("td", "", "small");
        pathCell.appendChild(text("code", album.path));
        var copy = text("button", "copy");
        copy.type = "button";
        copy.dataset.action = "copy-text";
        copy.dataset.text = album.path;
        copy.title = "copy this path";
        pathCell.appendChild(copy);
        tr.appendChild(pathCell);
        rows.appendChild(tr);
      });
      if (!albums.length) {
        var empty = document.createElement("tr");
        empty.appendChild(text("td", "No matching album directories.", "muted"));
        empty.firstChild.colSpan = 4;
        rows.appendChild(empty);
      }
      if (count) count.textContent = albums.length + " of " + total + " album directories";
    }

    function runSearch() {
      var current = ++request;
      var url = "/api/library/albums?q=" + encodeURIComponent(search.value);
      fetch(url, { credentials: "same-origin" }).then(function (response) {
        if (!response.ok) throw new Error("Library search failed (" + response.status + ")");
        return response.json();
      }).then(function (data) {
        if (current === request) drawAlbums(data.albums, data.total_albums);
      }).catch(function (error) {
        if (current === request && count) count.textContent = error.message;
      });
    }

    search.addEventListener("input", function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(runSearch, 160);
    });

    var duplicateButton = document.getElementById("library-duplicates-toggle");
    var report = document.getElementById("duplicate-report");
    if (duplicateButton && report) {
      duplicateButton.addEventListener("click", function () {
        report.hidden = false;
        duplicateButton.disabled = true;
        report.replaceChildren(text("p", "Scanning for possible name matches…", "muted"));
        fetch("/api/library/duplicates", { credentials: "same-origin" }).then(function (response) {
          if (!response.ok) throw new Error("Duplicate report failed (" + response.status + ")");
          return response.json();
        }).then(function (data) {
          report.replaceChildren();
          report.appendChild(text("p", data.warning, "warning"));
          report.appendChild(text("p", data.candidate_count + " possible match groups"));
          var list = document.createElement("ul");
          list.className = "duplicate-list";
          data.candidates.forEach(function (candidate) {
            var item = document.createElement("li");
            item.appendChild(text("strong", candidate.album_name + " — " + candidate.track_name));
            var paths = document.createElement("ul");
            candidate.directories.forEach(function (directory) {
              var path = document.createElement("li");
              path.appendChild(text("code", directory.path));
              var copy = text("button", "copy", "copy");
              copy.type = "button";
              copy.dataset.action = "copy-text";
              copy.dataset.text = directory.path;
              path.appendChild(copy);
              paths.appendChild(path);
            });
            item.appendChild(paths);
            list.appendChild(item);
          });
          if (!data.candidates.length) report.appendChild(text("p", "No candidate groups found.", "muted"));
          else report.appendChild(list);
        }).catch(function (error) {
          report.replaceChildren(text("p", error.message, "error"));
        }).finally(function () { duplicateButton.disabled = false; });
      });
    }
  }

  function healthBanner(body) {
    /* The three problems the page can see and the user cannot: the wrapper cannot
       serve downloads, a library root cannot be read, and a root that is mounted
       and empty (Docker autocreated the directory over a drive that is not there).
       Healthy input returns null -- the banner is never *present* when it is not
       needed, rather than present and empty. */
    var nodes = [];
    if (body && body.wrapper && body.wrapper.problem) {
      var p = el("p", "error");
      p.setAttribute("role", "alert");
      if (body.wrapper.problem === "no-account") {
        p.textContent = "Apple にログインしていません。";
      } else {
        p.textContent = body.wrapper.detail || "wrapper が応答しません";
      }
      nodes.push(p);
    }
    if (body && body.library) {
      var roots = (body.library.degraded_roots || []).concat(
        (body.library.per_root || [])
          .map(function (count, index) {
            return count === 0 ? (body.library.roots || [])[index] : null;
          })
          .filter(function (r) { return r; })
      );
      Array.prototype.forEach.call(roots, function (root) {
        var p = el("p", "error");
        p.setAttribute("role", "status");
        p.textContent = "ライブラリルートに問題があります: " + root;
        nodes.push(p);
      });
    }
    if (!nodes.length) return null;
    var box = el("div", "health-banner-messages");
    nodes.forEach(function (n) { box.appendChild(n); });
    return box;
  }

  function updateHealthBanner(body) {
    var mount = document.getElementById("health-banner");
    if (!mount) return;
    mount.textContent = "";
    var banner = healthBanner(body);
    if (banner) mount.appendChild(banner);
  }

  fetch("/api/status", { credentials: "same-origin" })
    .then(function (response) {
      return response.ok ? response.json() : null;
    })
    .then(function (body) {
      if (body && body.pool) {
        poolText = body.pool.ripping + "/" + body.pool.limit + " ripping";
        renderStreamLabel();
      }
      if (body) updateHealthBanner(body);
    })
    .catch(function () {
      /* No pool line rather than a broken page; socket messages carry it when available. */
    });

  var body = document.getElementById("queue-body");
  if (!body) return;

  var logPane = document.getElementById("log");
  var state = document.getElementById("stream-state");
  var queueTools = document.getElementById("queue-tools");
  var queueSearch = document.getElementById("queue-search");
  var queueStatus = document.getElementById("queue-status-filter");
  var queueVisibleCount = document.getElementById("queue-visible-count");
  var queueTableWrap = document.getElementById("queue-table-wrap");
  var queueEmpty = document.getElementById("queue-empty");
  var queueNoResults = document.getElementById("queue-no-results");
  var queueSummary = document.getElementById("queue-summary");
  var jobs = new Map();
  var jobRevisions = new Map();
  var queueCounts = null;
  var historyLoadedIds = new Set();
  var historyBeforeId = null;
  var historyHasMore = false;
  var historyLoading = false;
  var historyTools = document.getElementById("queue-history-tools");
  var historyButton = document.getElementById("queue-load-history");
  var historyState = document.getElementById("queue-history-state");
  var pauseButton = document.getElementById("queue-pause");
  var resumeButton = document.getElementById("queue-resume");
  var pauseState = document.getElementById("queue-pause-state");
  function setQueuePaused(paused) {
    if (pauseButton) pauseButton.hidden = Boolean(paused);
    if (resumeButton) resumeButton.hidden = !paused;
    if (pauseState) pauseState.textContent = paused ? "Paused — running downloads will finish." : "";
  }
  if (historyTools) {
    historyBeforeId = Number(historyTools.dataset.beforeId) || null;
    historyHasMore = historyTools.dataset.hasMore === "true";
  }

  // -- rows ---------------------------------------------------------------

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  /* The progress cell, as `job_row.html` renders it: a percentage first and the bar
   * second. The number is text and the bar is a position, and the two together are what a
   * reader needs -- a bar alone cannot distinguish 62% from 63%, and `<progress>` with no
   * `value` is an indeterminate bar that claims progress it does not have.
   *
   * `Math.round` is round-half-up, which is what the template's `100 * progress + 0.5` then
   * truncate gives too. The two have to agree because `upsertRow` rebuilds this cell from
   * JSON on every stream frame: Jinja's own `round(0)` is round-half-to-even, so 0.625
   * would have rendered 62 on first paint and 63 one frame later, a percentage that moves
   * with no progress behind it. */
  function progressCell(job) {
    var td = el("td", "progress");
    if (job.progress !== null && job.progress !== undefined) {
      td.appendChild(el("span", "pct", Math.round(100 * job.progress) + "%"));
      var done = el("progress");
      done.max = 1;
      done.value = job.progress;
      td.appendChild(done);
    } else if (job.status === "running") {
      td.appendChild(el("progress"));
    } else {
      td.appendChild(el("span", "muted", "—"));
    }
    return td;
  }

  /* The matched paths, one list item each.
   *
   * `skip_reason` is the literal `duplicate:` followed by one or more root-qualified absolute
   * paths -- each `roots[root_index] / relpath`, so it opens in a file browser -- joined with
   * `|`. It used to carry a bare relpath instead, which does not resolve once more than one
   * library root is configured; see `DuplicateHit.resolved`. The paths are the only evidence a
   * `loose` match can be adjudicated against -- so they are shown individually rather than
   * as the raw string, and each is text.
   */
  function detailCell(job) {
    var td = el("td", "detail");
    if (job.status === "skipped" && job.skip_reason) {
      td.appendChild(el("span", "muted", "already on disk at"));
      var ul = el("ul", "paths");
      String(job.skip_reason).replace(/^duplicate:/, "").split("|").forEach(function (path) {
        if (!path) return;
        var li = el("li");
        li.appendChild(el("code", null, path));
        var copy = el("button", "copy", "copy");
        copy.type = "button";
        copy.dataset.action = "copy-text";
        copy.dataset.text = path;
        copy.title = "copy this path";
        li.appendChild(copy);
        ul.appendChild(li);
      });
      td.appendChild(ul);
    } else if (job.error) {
      td.appendChild(el("span", "error-text", job.error));
    } else if (job.parent_url) {
      // The template renders `<a class="muted small" ...>source</a>` here; the script used to
      // render a `<span>` holding the raw URL, so every row's source link silently became a
      // long unprominent URL the moment the first stream frame arrived -- and only then,
      // which is the whole reason `test_the_scripts_row_builder_agrees_with_the_template`
      // exists. `href` is set as a property, never concatenated into markup: the URL came
      // from the server, and a string built here is a string nobody has escaped.
      var link = el("a", "muted small", "source");
      link.href = job.parent_url;
      link.rel = "noreferrer noopener";
      td.appendChild(link);
    }
    return td;
  }

  function actionsCell(job) {
    var td = el("td", "actions");
    var button;
    if (["done", "failed", "skipped", "cancelled"].indexOf(job.status) !== -1) {
      button = el("button", null, "Retry");
      button.type = "button";
      button.dataset.action = "job-retry";
    } else if (job.status === "queued") {
      button = el("button", null, "Cancel");
      button.type = "button";
      button.dataset.action = "job-cancel";
    }
    if (button) {
      button.dataset.jobId = job.id;
      td.appendChild(button);
    }
    return td;
  }

  /* The finished set, in one place. `job_row.html` encodes the same four statuses and the
   * two have to agree, because the browser rebuilds every row from JSON on the first stream
   * frame -- so a row the server rendered correctly is replaced by one this function built.
   * `test_the_scripts_row_builder_agrees_with_the_template` is what keeps them in step. */
  var FINISHED_STATUSES = ["done", "failed", "skipped", "cancelled"];
  var QUEUE_STATUS_ORDER = ["queued", "waiting", "running", "done", "failed", "skipped", "cancelled"];

  function updateQueueSummary() {
    if (!queueSummary) return;
    var counts = Object.create(null);
    if (queueCounts) {
      Object.keys(queueCounts).forEach(function (status) {
        if (status !== "total") counts[status] = queueCounts[status];
      });
    } else {
      body.querySelectorAll("tr[data-job-id]").forEach(function (row) {
        var status = row.querySelector(".status-cell .status");
        if (!status) return;
        var name = status.textContent.trim();
        counts[name] = (counts[name] || 0) + 1;
      });
    }
    var statuses = QUEUE_STATUS_ORDER.concat(Object.keys(counts).filter(function (status) {
      return QUEUE_STATUS_ORDER.indexOf(status) === -1;
    }));
    queueSummary.textContent = "";
    statuses.forEach(function (status) {
      if (!counts[status]) return;
      var button = el("button", "count");
      button.type = "button";
      button.dataset.count = status;
      button.dataset.action = "filter-status";
      button.dataset.status = status;
      button.setAttribute("aria-pressed", queueStatus && queueStatus.value === status ? "true" : "false");
      button.title = "Filter the queue to " + status + " jobs";
      button.appendChild(el("span", "count-n", counts[status]));
      button.appendChild(document.createTextNode(" " + status));
      queueSummary.appendChild(button);
    });
  }

  /* The age label. `created_at` is already in the job JSON -- `asdict(Job)`, and the column
   * was made browser-parseable for exactly this -- and the label ticks every five seconds
   * below so "waiting 40s" does not lie about a job that has waited two minutes. It hangs
   * off the title cell rather than joining the column contract: the td list
   * `test_the_scripts_row_builder_agrees_with_the_template` compares is the row's, and the
   * server-rendered rows lose the span on the first snapshot like everything else.
   */
  function ago(iso) {
    var then = new Date(iso).getTime();
    if (isNaN(then)) return "now";
    var secs = Math.floor((Date.now() - then) / 1000);
    if (secs < 0) secs = 0;
    if (secs < 60) return secs + "s";
    var mins = Math.floor(secs / 60);
    if (mins < 60) return mins + "m";
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + "h " + (mins % 60) + "m";
    return Math.floor(hours / 24) + "d " + (hours % 24) + "h";
  }

  function ageSpan(job) {
    var span = el("span", "ago", " " + ago(job.created_at));
    span.title = "enqueued " + job.created_at;
    span.dataset.at = job.created_at;
    return span;
  }

  function buildRow(job) {
    var tr = el("tr", "status-" + job.status);
    tr.id = "job-" + job.id;
    tr.dataset.jobId = job.id;
    if (job.parent_url) {
      tr.dataset.parentUrl = job.parent_url;
      tr.dataset.parentType = job.parent_type || "group";
    }
    tr.dataset.finished = FINISHED_STATUSES.indexOf(job.status) === -1 ? "0" : "1";

    var status = el("td", "status-cell");
    status.appendChild(el("span", "status status-" + job.status, job.status));

    // The order is the template's: the title leads and the database rowid is last, because
    // the id is what the buttons address and not what a reader is looking for.
    tr.appendChild(el("td", "title", job.title || "(no title)"));
    tr.appendChild(status);
    tr.appendChild(el("td", "num", job.codec));
    tr.appendChild(progressCell(job));
    tr.appendChild(detailCell(job));
    tr.appendChild(el("td", "num muted", job.id));
    tr.appendChild(actionsCell(job));
    if (job.created_at) tr.children[0].appendChild(ageSpan(job));
    return tr;
  }

  function bumpJobRevision(id) {
    jobRevisions.set(id, (jobRevisions.get(id) || 0) + 1);
  }

  function upsertIfUnchanged(job, revision) {
    if (job && (jobRevisions.get(job.id) || 0) === revision) upsertRow(job);
  }

  function upsertRow(job) {
    bumpJobRevision(job.id);
    jobs.set(job.id, job);
    var row = document.getElementById("job-" + job.id);
    if (row) body.replaceChild(buildRow(job), row);
    else body.appendChild(buildRow(job));
    updateQueueSummary();
    // A new id can open a new group, so the headers are re-derived before the filters.
    syncGroupHeaders();
    // Re-apply search, status and finished filters whenever a live row changes.
    applyQueueFilters();
  }

  function removeJobs(ids) {
    (ids || []).forEach(function (id) {
      bumpJobRevision(id);
      jobs.delete(id);
      var row = document.getElementById("job-" + id);
      if (row) row.remove();
    });
    updateQueueSummary();
    // A row set can empty a group; its header goes with it.
    syncGroupHeaders();
    applyQueueFilters();
  }

  var hydratingJobs = new Map();

  function hydrateJobs(ids) {
    var wanted = [];
    var waiting = [];
    var seen = new Set();
    (ids || []).forEach(function (id) {
      if (seen.has(id) || jobs.has(id)) return;
      seen.add(id);
      var pending = hydratingJobs.get(id);
      if (pending) waiting.push(pending);
      else wanted.push(id);
    });
    if (!wanted.length) return Promise.all(waiting);
    var pendingRequest;
    var chunks = [];
    for (var start = 0; start < wanted.length; start += 500) {
      chunks.push(wanted.slice(start, start + 500));
    }
    pendingRequest = Promise.all(chunks.map(function (chunk) {
      var params = chunk.map(function (id) { return "ids=" + encodeURIComponent(id); }).join("&");
      return fetch("/api/jobs/lookup?" + params, { credentials: "same-origin" }).then(function (response) {
        if (!response.ok) throw new Error("queue refresh failed with " + response.status);
        return response.json();
      }).then(function (data) {
        (data.jobs || []).forEach(function (job) {
          // A WebSocket update may have overtaken this request. Never replace a newer live
          // row with the older copy from an ID lookup.
          if (!jobs.has(job.id)) upsertRow(job);
        });
      });
    }))
      .catch(function () {
        log("Could not refresh the requested rows; reconnecting will reload the queue.");
      })
      .finally(function () {
        wanted.forEach(function (id) {
          if (hydratingJobs.get(id) === pendingRequest) hydratingJobs.delete(id);
        });
      });
    wanted.forEach(function (id) { hydratingJobs.set(id, pendingRequest); });
    return Promise.all(waiting.concat([pendingRequest]));
  }

  /* A snapshot replaces the active/recent window. Older terminal pages are rehydrated by ID
   * after reconnect so rows deleted while offline do not survive as stale client-side cache. */
  function updateHistoryWindow(data, reconnect) {
    var previousCursor = historyBeforeId;
    var previousHasMore = historyHasMore;
    var hadLoadedHistory = historyLoadedIds.size > 0;
    if (!hadLoadedHistory || !reconnect) {
      historyBeforeId = data.history_before_id || null;
      historyHasMore = Boolean(data.history_has_more);
    } else {
      historyBeforeId = previousCursor;
      historyHasMore = previousHasMore;
    }
    if (historyTools) {
      historyTools.hidden = !historyHasMore;
      historyTools.dataset.beforeId = historyBeforeId || "";
      historyTools.dataset.hasMore = historyHasMore ? "true" : "false";
    }
  }

  function replaceRows(list) {
    jobs.forEach(function (_job, id) { bumpJobRevision(id); });
    jobs.clear();
    body.textContent = "";
    list.forEach(function (job) {
      bumpJobRevision(job.id);
      jobs.set(job.id, job);
      body.appendChild(buildRow(job));
    });
    updateQueueSummary();
    syncGroupHeaders();
    applyQueueFilters();
  }

  function sortQueueRows() {
    Array.prototype.slice.call(body.querySelectorAll("tr[data-job-id]"))
      .sort(function (a, b) { return Number(a.dataset.jobId) - Number(b.dataset.jobId); })
      .forEach(function (row) { body.appendChild(row); });
    syncGroupHeaders();
  }

  /* Group headers are derived from the rows, and the rows are the truth: the same
     reconcile after every snapshot, upsert-sort, or history load. A header is
     removed and re-added so a batch that changes parent -- or a row set that leaves
     the header orphaned -- cannot strand a stale control on the page. */
  function groupHeaderRow(parentUrl, parentType) {
    var tr = el("tr", "group-header");
    var td = el("td", null);
    td.colSpan = 7;
    var link = el("a", "group-url muted small", parentType || "group");
    link.href = parentUrl;
    link.rel = "noreferrer noopener";
    td.appendChild(link);
    var actions = el("span", "group-actions");
    var cancel = el("button", null, "Cancel queued");
    cancel.type = "button";
    cancel.dataset.action = "cancel-group";
    cancel.dataset.parentUrl = parentUrl;
    actions.appendChild(cancel);
    var requeue = el("button", null, "Re-queue failed");
    requeue.type = "button";
    requeue.dataset.action = "requeue-failed-group";
    requeue.dataset.parentUrl = parentUrl;
    actions.appendChild(requeue);
    td.appendChild(actions);
    tr.appendChild(td);
    return tr;
  }

  function syncGroupHeaders() {
    Array.prototype.slice.call(body.querySelectorAll(":scope > tr.group-header"))
      .forEach(function (row) { body.removeChild(row); });
    var rows = Array.prototype.slice.call(body.querySelectorAll("tr[data-job-id]"));
    var previousParent = null;
    rows.forEach(function (row) {
      var parent = row.dataset.parentUrl;
      if (parent && parent !== previousParent) {
        body.insertBefore(groupHeaderRow(parent, row.dataset.parentType), row);
      }
      previousParent = parent;
    });
  }

  function loadHistory() {
    if (historyLoading || !historyHasMore || !historyBeforeId) return;
    historyLoading = true;
    if (historyButton) historyButton.disabled = true;
    if (historyState) historyState.textContent = "Loading…";
    fetch("/api/jobs/history?before_id=" + encodeURIComponent(historyBeforeId), {
      credentials: "same-origin",
    }).then(function (response) {
      if (!response.ok) throw new Error("History request failed (" + response.status + ")");
      return response.json();
    }).then(function (data) {
      (data.jobs || []).forEach(function (job) {
        historyLoadedIds.add(job.id);
        upsertRow(job);
      });
      sortQueueRows();
      historyBeforeId = data.before_id || historyBeforeId;
      historyHasMore = Boolean(data.has_more);
      if (historyTools) {
        historyTools.hidden = !historyHasMore;
        historyTools.dataset.beforeId = historyBeforeId || "";
        historyTools.dataset.hasMore = historyHasMore ? "true" : "false";
      }
      if (historyState) historyState.textContent = data.jobs.length + " older job(s) loaded";
    }).catch(function (error) {
      if (historyState) historyState.textContent = error.message;
    }).finally(function () {
      historyLoading = false;
      if (historyButton) historyButton.disabled = false;
    });
  }

  var countRefreshTimer = null;
  function refreshQueueCounts() {
    window.clearTimeout(countRefreshTimer);
    countRefreshTimer = window.setTimeout(function () {
      fetch("/api/jobs/counts", { credentials: "same-origin" }).then(function (response) {
        return response.ok ? response.json() : null;
      }).then(function (data) {
        if (data && data.counts) {
          queueCounts = data.counts;
          updateQueueSummary();
        }
      }).catch(function () { /* The live row still renders if a count refresh is offline. */ });
    }, 250);
  }

  // -- the stream ---------------------------------------------------------

  /* The log pane is `aria-live`, and it is also the only thing on the page that grows
   * without bound. A hub left open across a week of ripping appends a line per job, per
   * wrapper hiccup and per re-queue; `textContent +=` reallocations the whole string every
   * time, so without a ceiling the pane gets slower to append to and slower to repaint, on
   * a node a screen reader re-reads on every change. 2,000 lines is the last 2,000, kept
   * as whole lines -- trimming by length would leave a half-line at the top forever. */
  var LOG_LIMIT = 2000;
  var logLines = 0;

  function log(line) {
    if (!logPane) return;
    logPane.textContent += line + "\n";
    logLines += 1;
    if (logLines > LOG_LIMIT) {
      var text = logPane.textContent;
      var drop = logLines - LOG_LIMIT;
      for (var i = 0; i < drop; i++) {
        var newline = text.indexOf("\n");
        if (newline === -1) break;
        text = text.slice(newline + 1);
      }
      logPane.textContent = text;
      logLines = LOG_LIMIT;
    }
    logPane.scrollTop = logPane.scrollHeight;
  }

  /* `data-state` is what `#stream-state`'s dot colours itself from; the label is what a
   * reader gets. Setting the attribute rather than adding a `<span>` matters because the
   * label is written with `textContent`, which would delete a child node on the first
   * frame -- and the dot is then the one thing that survives a reconnect. */
  /* The stream's own line is where live state is allowed to speak, and the pool count
   * rides it: "live · 2/4 ripping" is one honest sentence, rebuilt rather than patched,
   * because `textContent` is what keeps filesystem-derived strings out of the markup.
   * The initial count comes from `/api/status` and does not wait for the stream -- the
   * pool is the scheduler's truth with or without a subscriber -- and a `pool` frame on
   * the jobs channel overtakes it the moment the table changes.
   */
  var streamBase = "connecting…";
  var poolText = "";

  function renderStreamLabel() {
    if (!state) return;
    state.textContent = poolText ? streamBase + " · " + poolText : streamBase;
  }

  function setStreamState(name, label) {
    if (!state) return;
    state.dataset.state = name;
    streamBase = label;
    renderStreamLabel();
  }

  function handle(event) {
    var payload;
    try {
      payload = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    switch (payload.kind) {
      case "snapshot": {
        var reconnectingWithHistory = historyLoadedIds.size > 0;
        updateHistoryWindow(payload, reconnectingWithHistory);
        queueCounts = payload.counts || queueCounts;
        if (payload.queue_paused !== undefined) setQueuePaused(payload.queue_paused);
        replaceRows(payload.jobs || []);
        if (queueCounts) updateQueueSummary();
        if (reconnectingWithHistory) hydrateJobs(Array.from(historyLoadedIds));
        // The snapshot arrives on connect/reconnect: refresh the banner from the live
        // server so a wrapper or library that changed while the socket was down is
        // reflected. The wrapper frame carries live transitions; the snapshot carries
        // everything the socket missed.
        fetch("/api/status", { credentials: "same-origin" })
          .then(function (response) { return response.ok ? response.json() : null; })
          .then(function (body) { if (body) updateHealthBanner(body); })
          .catch(function () { /* a later snapshot or wrapper frame carries it */ });
        break;
      }
      case "job": {
        var previousJob = jobs.get(payload.job.id);
        var statusChanged = !previousJob || previousJob.status !== payload.job.status;
        upsertRow(payload.job);
        if (statusChanged) refreshQueueCounts();
        break;
      }
      case "batch":
        // The batch carries ids, not row details. Hydrate just those rows over the REST API;
        // a later `job` message wins if the scheduler has already changed their state.
        (payload.created || []).forEach(function (id) {
          if (!jobs.has(id)) log("queued job #" + id);
        });
        hydrateJobs(payload.created || []);
        refreshQueueCounts();
        break;
      case "deleted":
        (payload.ids || []).forEach(function (id) { historyLoadedIds.delete(id); });
        removeJobs(payload.ids || []);
        refreshQueueCounts();
        break;
      case "library":
        if (payload.detail) log(payload.detail);
        break;
      case "pool":
        poolText = payload.ripping + "/" + payload.limit + " ripping";
        renderStreamLabel();
        break;
      case "queue-control":
        setQueuePaused(payload.paused);
        break;
      case "wrapper":
        log(
          payload.problem === null || payload.problem === undefined
            ? "the wrapper recovered"
            : "the wrapper is not ready: " + payload.problem
        );
        // The wrapper's problem is exactly the banner's subject; refresh the real
        // status rather than trusting the frame's summary (no cache, one probe).
        fetch("/api/status", { credentials: "same-origin" })
          .then(function (response) { return response.ok ? response.json() : null; })
          .then(function (body) { if (body) updateHealthBanner(body); })          .catch(function () { /* the next frame or snapshot will carry it */ });
        break;
      case "log":
        log(payload.line);
        break;
      default:
        break;
    }
  }

  var socket = null;
  var reconnectAttempt = 0;
  var openedAt = 0;

  function connectStream() {
    setStreamState(
      reconnectAttempt ? "reconnecting" : "connecting",
      reconnectAttempt ? "reconnecting…" : "connecting…"
    );
    openedAt = 0;
    var protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
    socket = new WebSocket(protocol + window.location.host + "/api/jobs/ws");
    socket.addEventListener("open", function () {
      openedAt = Date.now();
      setStreamState("live", "live");
    });
    socket.addEventListener("message", handle);
    socket.addEventListener("close", function (event) {
      // Authentication and origin failures cannot recover without changing the page/session.
      if (event.code === 4401 || event.code === 4403) {
        setStreamState("disconnected", "session expired — reload the page");
        return;
      }
      // Reset the exponential backoff only after a stable connection. Immediate failures
      // therefore cannot turn into a tight reconnect loop.
      if (openedAt && Date.now() - openedAt >= 30000) reconnectAttempt = 0;
      var delay = Math.min(1000 * Math.pow(2, reconnectAttempt), 30000);
      reconnectAttempt = Math.min(reconnectAttempt + 1, 5);
      setStreamState("reconnecting", "reconnecting…");
      window.setTimeout(connectStream, delay + Math.random() * 500);
    });
    socket.addEventListener("error", function () {
      // Browsers follow an error with close; closing here makes that transition explicit.
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close();
    });
  }

  connectStream();

  /* The pool count before any message: the scheduler's table as the server last saw it.
   * A failure here is silent on purpose -- the stream is the loud channel, and a page
   * that cannot reach `/api/status` once will hear about it there instead. */

  /* The age labels tick on a five-second beat, and only while the tab is visible -- a
   * backgrounded queue does not need a timer rewriting text nobody is reading. A snapshot
   * or an upsert rebuilds each row's span from `created_at` anyway, so the beat only ever
   * ages rows that are genuinely sitting in the table, and a row that changes state gets a
   * fresh label with the rest of its markup.
   */
  window.setInterval(function () {
    if (document.hidden) return;
    document.querySelectorAll("#queue-body .ago").forEach(function (span) {
      span.textContent = " " + ago(span.dataset.at);
    });
  }, 5000);

  // -- actions ------------------------------------------------------------

  function requestJson(url, options) {
    return fetch(url, Object.assign({ credentials: "same-origin" }, options || {})).then(
      function (response) {
        return response.json().then(function (data) {
          return { ok: response.ok, status: response.status, data: data };
        });
      }
    );
  }

  function post(url, payload) {
    return requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
  }

  function showError(selector, text) {
    var node = document.querySelector(selector);
    if (!node) return;
    node.textContent = text || "";
    node.hidden = !text;
  }

  function readForm(form) {
    var out = {};
    new FormData(form).forEach(function (value, key) {
      out[key] = value;
    });
    return out;
  }

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-action]");
    if (!target) return;
    var action = target.dataset.action;
    var jobId = target.dataset.jobId;

    if (action === "queue-load-history") {
      loadHistory();
    } else if (action === "filter-status") {
      var selectedStatus = target.dataset.status;
      if (queueStatus && selectedStatus) {
        queueStatus.value = queueStatus.value === selectedStatus ? "all" : selectedStatus;
        if (FINISHED_STATUSES.indexOf(queueStatus.value) !== -1) setFinishedVisible(true);
        else applyQueueFilters();
      }
    } else if (action === "queue-pause" || action === "queue-resume") {
      var paused = action === "queue-pause";
      post("/api/jobs/" + (paused ? "pause" : "resume")).then(function (result) {
        if (result.ok) setQueuePaused(result.data.paused);
        else log((result.data && result.data.detail) || "Queue control failed.");
      }).catch(function () { log("Could not reach the hub to change queue state."); });
    } else if (action === "wrapper-start") post("/api/wrapper/start").then(reload);
    else if (action === "wrapper-stop") post("/api/wrapper/stop").then(reload);
    else if (action === "wrapper-restart") post("/api/wrapper/restart").then(reload);
    else if (action === "wrapper-login") {
      var card = document.getElementById("login-card");
      if (card) card.hidden = !card.hidden;
    } else if (action === "job-cancel" && jobId) {
      var cancelRevision = jobRevisions.get(Number(jobId)) || 0;
      requestJson("/api/jobs/" + jobId, { method: "DELETE" }).then(function (result) {
        if (!result.ok) log((result.data && result.data.detail) || "the job could not be cancelled");
        else upsertIfUnchanged(result.data, cancelRevision);
      }).catch(function () { log("Could not reach the hub to cancel this job."); });
    } else if (action === "job-retry" && jobId) {
      var retryRevision = jobRevisions.get(Number(jobId)) || 0;
      post("/api/jobs/" + jobId + "/retry").then(function (result) {
        if (!result.ok) log((result.data && result.data.detail) || "the job could not be retried");
        else upsertIfUnchanged(result.data, retryRevision);
      }).catch(function () { log("Could not reach the hub to retry this job."); });
    } else if (action === "queue-delete-finished") {
      // Irreversible, so it asks. The wording names what is *not* being deleted, because
      // "delete" next to a list of tracks is the sentence a user reads as "delete my music".
      if (!window.confirm("Remove every finished row from the queue?\n\nThe files stay on disk. Only the list is cleared.")) {
        return;
      }
      target.disabled = true;
      requestJson("/api/jobs/finished", { method: "DELETE" }).then(function (result) {
        if (!result.ok) {
          log((result.data && result.data.detail) || "finished rows could not be deleted");
          return;
        }
        var ids = (result.data && result.data.ids) || [];
        removeJobs(ids);
        log(
          ids.length
            ? "Removed " + ids.length + " finished row(s). The files stay on disk."
            : "There are no finished rows to remove."
        );
      }).catch(function () {
        log("Could not reach the hub to remove finished rows.");
      }).finally(function () {
        target.disabled = false;
      });
    } else if (action === "cancel-group" && target.dataset.parentUrl) {
      post("/api/jobs/cancel", {parent_url: target.dataset.parentUrl}).then(function (result) {
        if (!result.ok) log((result.data && result.data.detail) || "the group could not be cancelled");
        // The cancelled rows come back as `job` frames; no manual DOM bookkeeping.
      }).catch(function () { log("Could not reach the hub to cancel the group."); });
    } else if (action === "requeue-failed-group" && target.dataset.parentUrl) {
      post("/api/jobs/requeue", {scope: "failed", parent_url: target.dataset.parentUrl}).then(function (result) {
        if (!result.ok) log((result.data && result.data.detail) || "the group could not be re-queued");
        // The requeued rows come back as `job` frames; no manual DOM bookkeeping.
      }).catch(function () { log("Could not reach the hub to re-queue the group."); });
    } else if (action === "queue-toggle-finished") {
      setFinishedVisible(!finishedVisible());
    } else if (action === "queue-clear-filters") {
      clearQueueFilters();
    }
  });

  // -- collapsing the finished rows -----------------------------------------
  //
  // A queue that only grows is a queue you scroll, and this one is a record of every
  // track the user has ever asked for. The rows stay in the DOM -- hiding them with
  // `display: none` rather than removing them keeps WebSocket upserts working against the
  // same nodes, so a job that finishes while the filter is on does not reappear at the
  // top of an unfiltered-looking table.
  function finishedVisible() {
    return document.body.dataset.finished === "1";
  }

  function setFinishedVisible(visible) {
    document.body.dataset.finished = visible ? "1" : "0";
    var button = document.querySelector('[data-action="queue-toggle-finished"]');
    if (button) {
      button.setAttribute("aria-pressed", visible ? "true" : "false");
      button.textContent = visible ? "Hide finished" : "Show finished";
    }
    applyQueueFilters();
  }

  function applyQueueFilters() {
    var rows = Array.prototype.slice.call(body.querySelectorAll("tr[data-job-id]"));
    var query = queueSearch ? queueSearch.value.trim().toLowerCase() : "";
    var status = queueStatus ? queueStatus.value : "all";
    var visible = 0;

    rows.forEach(function (row) {
      var matchesFinished = finishedVisible() || row.dataset.finished !== "1";
      var matchesStatus = status === "all" || row.classList.contains("status-" + status);
      var matchesSearch = !query || row.textContent.toLowerCase().indexOf(query) !== -1;
      row.hidden = !(matchesFinished && matchesStatus && matchesSearch);
      if (!row.hidden) visible += 1;
    });

    // Group headers follow their run: a header stays only while one of the rows after
    // it (up to the next header) is visible.
    var header = null;
    Array.prototype.slice.call(body.children).forEach(function (child) {
      if (child.classList && child.classList.contains("group-header")) {
        header = child;
        header.hidden = true;
      } else if (header && !child.hidden) {
        header.hidden = false;
      }
    });

    if (queueVisibleCount) {
      queueVisibleCount.textContent = "Showing " + visible + " of " + rows.length + " jobs";
    }
    if (queueEmpty) queueEmpty.hidden = rows.length !== 0;
    if (queueTableWrap) queueTableWrap.hidden = rows.length === 0;
    if (queueNoResults) queueNoResults.hidden = rows.length === 0 || visible !== 0;
    document.querySelectorAll("#queue-summary button[data-status]").forEach(function (button) {
      button.setAttribute("aria-pressed", button.dataset.status === status ? "true" : "false");
    });
  }

  function clearQueueFilters() {
    if (queueSearch) queueSearch.value = "";
    if (queueStatus) queueStatus.value = "all";
    setFinishedVisible(true);
    if (queueSearch) queueSearch.focus();
  }

  if (queueSearch) queueSearch.addEventListener("input", applyQueueFilters);
  if (queueStatus) {
    queueStatus.addEventListener("change", function () {
      if (FINISHED_STATUSES.indexOf(queueStatus.value) !== -1) setFinishedVisible(true);
      else applyQueueFilters();
    });
  }
  if (queueTools) queueTools.hidden = false;
  setFinishedVisible(false);

  // -- re-queueing and clearing ---------------------------------------------
  var requeueForm = document.querySelector('[data-action="queue-requeue"]');
  if (requeueForm) {
    requeueForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var select = requeueForm.querySelector('[name="scope"]');
      var button = requeueForm.querySelector('button[type="submit"]');
      if (button) button.disabled = true;
      post("/api/jobs/requeue", { scope: select ? select.value : "failed" }).then(function (result) {
        if (!result.ok) {
          log((result.data && result.data.detail) || "the re-queue failed");
          return;
        }
        // Both lists are reported, because a refused row is not an error: it is a track
        // another job already holds, and saying so is the difference between "the button
        // is broken" and "one of these was already on its way".
        var requeued = result.data.requeued || [];
        var refused = result.data.refused || [];
        log(requeued.length + " job(s) re-queued.");
        if (refused.length) {
          log(refused.length + " could not be re-queued: already running as job " + refused.join(", "));
        }
        // Each re-queued row is also published on the socket; those events update the table
        // in place, and a reconnect snapshot is the recovery path if this tab is offline.
      }).catch(function () {
        log("Could not reach the hub to re-queue jobs.");
      }).finally(function () {
        if (button) button.disabled = false;
      });
    });
  }

  function reload(result) {
    if (result && !result.ok) {
      log((result.data && result.data.detail) || "the request failed");
    }
    window.location.reload();
  }

  var loginForm = document.getElementById("login-form");
  if (loginForm) {
    loginForm.addEventListener("submit", function (event) {
      event.preventDefault();
      showError("#login-error", "");
      post("/api/wrapper/login", readForm(loginForm)).then(function (result) {
        if (!result.ok) {
          showError("#login-error", result.data.detail);
          return;
        }
        if (result.data.challenge_id) {
          // The launcher polls for 60 s and then gives up, so the page says so and starts
          // counting rather than leaving the user to guess whether it is still worth typing.
          document.getElementById("twofa-form").hidden = false;
          var left = result.data.expires_in || 60;
          document.getElementById("twofa-deadline").textContent =
            "The wrapper is waiting for a code. It gives up in " + left + "s.";
        } else {
          reload(result);
        }
      });
    });
  }

  var twofaForm = document.getElementById("twofa-form");
  if (twofaForm) {
    twofaForm.addEventListener("submit", function (event) {
      event.preventDefault();
      showError("#login-error", "");
      post("/api/wrapper/login/2fa", readForm(twofaForm)).then(function (result) {
        if (!result.ok) {
          showError("#login-error", result.data.detail);
          return;
        }
        reload(result);
      });
    });
  }

  var enqueue = document.getElementById("enqueue");
  if (enqueue) {
    /* The form remembers itself. One operator typing the same codec every session should
     * not re-pick it, and `localStorage` is the only storage that survives a reload of a
     * page this hub does not own the server state of. Both directions are wrapped: a
     * private-mode or full store means the form starts empty, which is what it did before.
     */
    var ENQUEUE_MEMORY = "amd-hub.enqueue";
    var remembered = (function () {
      try {
        return JSON.parse(localStorage.getItem(ENQUEUE_MEMORY)) || {};
      } catch (err) {
        return {};
      }
    })();
    var codecSelect = enqueue.querySelector('[name="codec"]');
    var languageBox = enqueue.querySelector('[name="language"]');
    var forceBox = enqueue.querySelector('[name="force"]');
    var enqueueButton = enqueue.querySelector('button[type="submit"]');
    var enqueueFeedback = document.getElementById("enqueue-feedback");

    function setEnqueueFeedback(text) {
      if (!enqueueFeedback) return;
      enqueueFeedback.textContent = text || "";
      enqueueFeedback.hidden = !text;
    }
    if (
      remembered.codec &&
      codecSelect &&
      codecSelect.querySelector('option[value="' + remembered.codec + '"]')
    ) {
      codecSelect.value = remembered.codec;
    }
    if (typeof remembered.language === "string" && languageBox) languageBox.value = remembered.language;
    if (remembered.force && forceBox) forceBox.checked = true;

    enqueue.addEventListener("submit", function (event) {
      event.preventDefault();
      showError("#enqueue-error", "");
      setEnqueueFeedback("Adding your links…");
      if (enqueueButton) {
        enqueueButton.disabled = true;
        enqueueButton.textContent = "Adding…";
      }
      var fields = readForm(enqueue);
      var urls = String(fields.urls || "").split("\n").map(function (line) {
        return line.trim();
      }).filter(Boolean);
      post("/api/jobs", {
        urls: urls,
        codec: fields.codec,
        language: fields.language || "",
        force: fields.force === "1",
      }).then(function (result) {
        if (!result.data) {
          showError("#enqueue-error", "The hub returned an empty response.");
          setEnqueueFeedback("");
          return;
        }
        var problems = result.data.problems || [];
        if (!result.ok && !problems.length) {
          showError("#enqueue-error", result.data.detail || "The links could not be added.");
          setEnqueueFeedback("");
          return;
        }
        if (problems.length) {
          // A partial batch is a 200, so the detail has to be surfaced here: the tracks that
          // were queued are queued, and the one that was refused is named.
          showError(
            "#enqueue-error",
            problems.map(function (problem) { return problem.url + ": " + problem.detail; }).join("\n")
          );
        }
        try {
          localStorage.setItem(
            ENQUEUE_MEMORY,
            JSON.stringify({
              codec: fields.codec,
              language: fields.language || "",
              force: fields.force === "1",
            })
          );
        } catch (err) {
          /* The submit still worked. A refused store costs the next visit one click. */
        }
        (result.data.rejected || []).forEach(function (name) {
          log("not queued: " + name);
        });
        var created = result.data.created || [];
        var summary = [];
        if (created.length) summary.push("Added " + created.length + " job(s)");
        if ((result.data.deduplicated || []).length) {
          summary.push((result.data.deduplicated || []).length + " already queued");
        }
        if ((result.data.skipped || []).length) {
          summary.push((result.data.skipped || []).length + " skipped");
        }
        if (problems.length) summary.push(problems.length + " link(s) need attention above");
        if (!summary.length) summary.push("No new jobs were added");
        if (created.length && !problems.length) enqueue.querySelector('[name="urls"]').value = "";
        return hydrateJobs(created).then(function () {
          setEnqueueFeedback(summary.join(" · ") + ". The queue below is up to date.");
          if (queueEmpty && jobs.size === 0) applyQueueFilters();
        });
      }).catch(function () {
        showError("#enqueue-error", "Could not reach the hub. Your links are still in the form.");
        setEnqueueFeedback("");
      }).finally(function () {
        if (enqueueButton) {
          enqueueButton.disabled = false;
          enqueueButton.textContent = "Add to queue";
        }
      });
    });
  }
})();
