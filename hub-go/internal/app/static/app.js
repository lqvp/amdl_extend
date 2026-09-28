/* amd-hub's client, and it is deliberately small.
 *
 * No HTMX. The server-rendered half is what is here -- every page arrives complete and
 * every action is a form that works without this file. What is missing is the HTMX
 * *runtime*: htmx's SSE extension is what the live queue would use, and a script fetched
 * from a CDN is a worse trade for a tool on a home LAN than what is below, which uses the
 * platform's own `EventSource`. So the interactive half is native, and the templates use
 * `data-action` attributes rather than `hx-*` ones -- an `hx-post` on a page with no htmx
 * silently does nothing, which is the worst of both.
 *
 * Three independent halves, and each one returns early if its page is not the one that
 * loaded: the queue (the stream and the bulk controls), the library (the filter), and the
 * copy buttons (either). Nothing is required except `EventSource`, which is every browser
 * this is meant to run in.
 *
 * The things this file must not get wrong:
 *
 *   - the snapshot. The stream sends a full queue first and then one change at a time, so a
 *     reconnect is a resync rather than a patch. `replaceRows` is therefore only ever called
 *     for a snapshot, and `upsertRow` otherwise; applying a snapshot as a merge would leave
 *     rows the user just cancelled on screen.
 *   - the rows. `textContent` everywhere, never `innerHTML`. `skip_reason` holds directory
 *     names read off the filesystem, and a user-curated library really does contain
 *     directories called `<img src=x onerror=...>`. The server escapes them too; this is the
 *     second of the two, not the only.
 *   - the row shape. `buildRow` rebuilds every row from JSON on the first frame, so a row
 *     the server rendered is replaced by one built here a moment later. The two are kept in
 *     step by hand, in the same field order, with the same classes.
 */
(function () {
  "use strict";

  // -- small helpers -------------------------------------------------------

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function post(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(payload || {}),
    }).then(function (response) {
      return response.json().then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
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

  function reload(result) {
    if (result && !result.ok) {
      log((result.data && result.data.detail) || "the request failed");
    }
    window.location.reload();
  }

  function log(line) {
    var pane = document.getElementById("log");
    if (!pane) return;
    pane.textContent += line + "\n";
    pane.scrollTop = pane.scrollHeight;
  }

  // -- the queue -----------------------------------------------------------

  function initQueue() {
    var body = document.getElementById("queue-body");
    if (!body) return;

    var state = document.getElementById("stream-state");
    var empty = document.getElementById("queue-empty");
    var jobs = new Map();

    /* The finished set, in one place. `job_row.html` encodes the same four statuses and the
     * two have to agree, because the browser rebuilds every row from JSON on the first
     * stream frame -- so a row the server rendered correctly is replaced by one this
     * function built. */
    var FINISHED_STATUSES = ["done", "failed", "skipped", "cancelled"];

    function setStreamState(mode, label) {
      if (!state) return;
      state.dataset.state = mode;
      state.textContent = label;
    }

    function progressCell(job) {
      var td = el("td", "progress");
      if (job.progress !== null && job.progress !== undefined) {
        var bar = el("progress");
        bar.max = 1;
        bar.value = job.progress;
        bar.setAttribute("aria-label", "download progress");
        td.appendChild(bar);
        td.appendChild(el("span", "percent", Math.round(job.progress * 100) + "%"));
      } else if (job.status === "running") {
        var unknown = el("progress");
        unknown.setAttribute("aria-label", "working");
        td.appendChild(unknown);
      } else {
        td.appendChild(el("span", "faint", "—"));
      }
      return td;
    }

    /* The matched paths, one list item each, each with a copy button.
     *
     * `skip_reason` is the literal `duplicate:` followed by one or more root-qualified
     * absolute paths -- each `roots[root_index] / relpath`, so it opens in a file browser --
     * joined with `|`. The paths are the only evidence a `loose` match can be adjudicated
     * against, so they are shown individually rather than as the raw string, and each is
     * text. The copy button carries the path in a `data-` attribute, which is an attribute
     * and not a script.
     */
    function detailCell(job) {
      var td = el("td", "detail");
      if (job.status === "skipped" && job.skip_reason) {
        td.appendChild(el("span", "muted small", "already on disk at"));
        var ul = el("ul", "paths");
        String(job.skip_reason).replace(/^duplicate:/, "").split("|").forEach(function (path) {
          if (!path) return;
          var li = el("li");
          li.appendChild(el("code", null, path));
          var copy = el("button", "copy", "copy");
          copy.type = "button";
          copy.dataset.copy = path;
          copy.setAttribute("aria-label", "Copy this path");
          li.appendChild(copy);
          ul.appendChild(li);
        });
        td.appendChild(ul);
      } else if (job.error) {
        td.appendChild(el("span", "error-text", job.error));
      } else if (job.parent_url) {
        var link = el("a", "source", "source");
        link.href = job.parent_url;
        link.rel = "noreferrer noopener";
        td.appendChild(link);
      }
      return td;
    }

    function actionsCell(job) {
      var td = el("td", "actions");
      var button;
      if (FINISHED_STATUSES.indexOf(job.status) !== -1) {
        button = el("button", "ghost small", "Retry");
        button.dataset.action = "job-retry";
      } else if (job.status === "queued") {
        button = el("button", "ghost small", "Cancel");
        button.dataset.action = "job-cancel";
      }
      if (button) {
        button.type = "button";
        button.dataset.jobId = job.id;
        td.appendChild(button);
      }
      return td;
    }

    /* The row, in the template's field order. `job_row.html` renders the same seven cells
     * with the same classes, because the first stream frame replaces everything the server
     * drew with what this builds. */
    function buildRow(job) {
      var finished = FINISHED_STATUSES.indexOf(job.status) !== -1;
      var tr = el("tr", "status-" + job.status + (job.status === "running" ? " is-running" : ""));
      tr.id = "job-" + job.id;
      tr.dataset.jobId = job.id;
      tr.dataset.finished = finished ? "1" : "0";

      var title = el("td", "title");
      if (job.title) title.appendChild(el("span", "name", job.title));
      else title.appendChild(el("span", "faint", "(no title)"));

      var status = el("td", "status-cell");
      status.appendChild(el("span", "badge badge-" + job.status, job.status));

      var codec = el("td", "col-codec");
      codec.appendChild(el("span", "chip", job.codec));

      tr.appendChild(title);
      tr.appendChild(status);
      tr.appendChild(codec);
      tr.appendChild(progressCell(job));
      tr.appendChild(detailCell(job));
      tr.appendChild(el("td", "num col-id id-cell", job.id));
      tr.appendChild(actionsCell(job));
      return tr;
    }

    /* The empty state is shown from the DOM rather than from a count, because the DOM is
     * what the user is looking at: hidden rows do not count, so a queue filtered down to
     * nothing does not claim to be empty -- the rows are still there behind the filter. */
    function updateEmptyState() {
      if (!empty) return;
      var visible = 0;
      Array.prototype.forEach.call(body.children, function (row) {
        if (!row.hidden) visible += 1;
      });
      empty.hidden = visible > 0;
    }

    function upsertRow(job) {
      jobs.set(job.id, job);
      var row = document.getElementById("job-" + job.id);
      if (row) body.replaceChild(buildRow(job), row);
      else body.appendChild(buildRow(job));
      // Rows arrive over SSE as well as in the snapshot, so the "hide finished" filter has
      // to be applied on every path that changes a row. A job that finishes while the filter
      // is on must disappear, not sit there looking unfinished.
      applyFinishedFilter();
    }

    /* A snapshot *replaces* the table. It is the whole queue as of one moment, so merging it
     * would leave rows for jobs that have since been deleted on screen for ever. */
    function replaceRows(list) {
      jobs.clear();
      body.textContent = "";
      list.forEach(upsertRow);
      applyFinishedFilter();
    }

    function handle(event) {
      var payload;
      try {
        payload = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      switch (payload.kind) {
        case "snapshot":
          replaceRows(payload.jobs || []);
          break;
        case "job":
          upsertRow(payload.job);
          break;
        case "batch":
          // A new batch's ids are in `created`; the rows themselves arrive as `job` events
          // as the scheduler runs them. Nothing is inserted here, because a row for a queued
          // job that has not been claimed yet would have no title and no progress.
          (payload.created || []).forEach(function (id) {
            if (!jobs.has(id)) log("queued job #" + id);
          });
          break;
        case "library":
          if (payload.detail) log(payload.detail);
          break;
        case "wrapper":
          log("the wrapper is not ready: " + payload.problem);
          break;
        case "log":
          log(payload.line);
          break;
        default:
          break;
      }
    }

    var source = new EventSource("/api/jobs/stream");
    source.addEventListener("open", function () {
      setStreamState("live", "live");
    });
    source.addEventListener("message", handle);
    source.addEventListener("error", function () {
      // EventSource reconnects on its own and the stream's first frame is a fresh snapshot,
      // so a drop is a resync rather than a gap. Saying so is better than a spinner that
      // lies.
      setStreamState("reconnecting", "reconnecting…");
    });

    // -- collapsing the finished rows ---------------------------------------
    //
    // A queue that only grows is a queue you scroll, and this one is a record of every track
    // the user has ever asked for. The rows stay in the DOM -- hiding them with
    // `display: none` rather than removing them keeps the SSE upsert working against the
    // same nodes, so a job that finishes while the filter is on does not reappear at the top
    // of an unfiltered-looking table.
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
      applyFinishedFilter();
    }

    function applyFinishedFilter() {
      var hide = !finishedVisible();
      body.querySelectorAll("tr[data-finished]").forEach(function (row) {
        row.hidden = hide && row.dataset.finished === "1";
      });
      updateEmptyState();
    }

    setFinishedVisible(false);

    // -- the bulk controls --------------------------------------------------
    document.addEventListener("click", function (event) {
      var target = event.target.closest("[data-action]");
      if (!target) return;
      var action = target.dataset.action;
      var jobId = target.dataset.jobId;
      if (action === "job-cancel" && jobId) {
        fetch("/api/jobs/" + jobId, {
          method: "DELETE",
          credentials: "same-origin",
        }).then(reload);
      } else if (action === "job-retry" && jobId) {
        post("/api/jobs/" + jobId + "/retry").then(reload);
      } else if (action === "queue-delete-finished") {
        // Irreversible, so it asks. The wording names what is *not* being deleted, because
        // "delete" next to a list of tracks is the sentence a user reads as "delete my music".
        if (!window.confirm("Remove every finished row from the queue?\n\nThe files stay on disk. Only the list is cleared.")) {
          return;
        }
        fetch("/api/jobs/finished", { method: "DELETE", credentials: "same-origin" }).then(reload);
      } else if (action === "queue-toggle-finished") {
        setFinishedVisible(!finishedVisible());
      }
    });

    var requeueForm = document.querySelector('[data-action="queue-requeue"]');
    if (requeueForm) {
      requeueForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var select = requeueForm.querySelector('[name="scope"]');
        post("/api/jobs/requeue", { scope: select ? select.value : "failed" }).then(function (result) {
          if (!result.ok) {
            log((result.data && result.data.detail) || "the re-queue failed");
            return;
          }
          // Both lists are reported, because a refused row is not an error: it is a track
          // another job already holds, and saying so is the difference between "the button is
          // broken" and "one of these was already on its way".
          var refused = result.data.refused || [];
          if (refused.length) {
            log(refused.length + " could not be re-queued: already running as job " + refused.join(", "));
          }
          reload(result);
        });
      });
    }

    var enqueue = document.getElementById("enqueue");
    if (enqueue) {
      enqueue.addEventListener("submit", function (event) {
        event.preventDefault();
        showError("#enqueue-error", "");
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
          if (!result.data) return;
          var problems = result.data.problems || [];
          if (problems.length) {
            // A partial batch is a 200, so the detail has to be surfaced here: the tracks that
            // were queued are queued, and the one that was refused is named.
            showError(
              "#enqueue-error",
              problems.map(function (p) { return p.url + ": " + p.detail; }).join("\n")
            );
          } else if (!result.ok) {
            showError("#enqueue-error", result.data.detail);
            return;
          }
          (result.data.rejected || []).forEach(function (name) {
            log("not queued: " + name);
          });
          window.location.reload();
        });
      });
    }
  }

  // -- the wrapper's own controls ------------------------------------------
  // These live outside the queue guard: the wrapper card is on the queue page, but the
  // buttons and the login form are addressed by attribute rather than by position, so a
  // page that grows one gets the behaviour without another branch.

  function initWrapper() {
    var loginCard = document.getElementById("login-card");
    var loginForm = document.getElementById("login-form");
    var twofaForm = document.getElementById("twofa-form");
    if (!loginCard && !loginForm && !twofaForm) return;

    document.addEventListener("click", function (event) {
      var target = event.target.closest("[data-action]");
      if (!target) return;
      var action = target.dataset.action;
      if (action === "wrapper-start") post("/api/wrapper/start").then(reload);
      else if (action === "wrapper-stop") post("/api/wrapper/stop").then(reload);
      else if (action === "wrapper-restart") post("/api/wrapper/restart").then(reload);
      else if (action === "wrapper-login" && loginCard) loginCard.hidden = !loginCard.hidden;
    });

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
            if (twofaForm) twofaForm.hidden = false;
            var deadline = document.getElementById("twofa-deadline");
            if (deadline) {
              deadline.textContent = "The wrapper is waiting for a code. It gives up in " +
                (result.data.expires_in || 60) + "s.";
            }
          } else {
            reload(result);
          }
        });
      });
    }

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
  }

  // -- copy buttons ---------------------------------------------------------
  //
  // Progressive: the path is already on screen as selectable text, and this only saves a
  // drag. `navigator.clipboard` needs a secure context, which a LAN hub served over plain
  // http is not -- so the fallback selects the text instead of pretending to have copied it.

  function copyText(text, button) {
    function done(label) {
      button.dataset.copied = "1";
      button.textContent = label;
      window.setTimeout(function () {
        button.dataset.copied = "";
        button.textContent = "copy";
      }, 1200);
    }

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () {
        done("copied");
      }, function () {
        done("select");
      });
      return;
    }
    var range = document.createRange();
    var node = button.parentNode.querySelector("code");
    if (node && window.getSelection) {
      window.getSelection().removeAllRanges();
      range.selectNodeContents(node);
      window.getSelection().addRange(range);
      done("selected");
    }
  }

  function initCopy() {
    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-copy]");
      if (!button) return;
      copyText(button.dataset.copy, button);
    });
  }

  // -- the library filter ---------------------------------------------------

  function initFilter() {
    var input = document.querySelector("[data-filter='albums']");
    var table = document.getElementById("albums");
    if (!input || !table) return;

    var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr"));
    var counter = document.getElementById("album-count");
    var empty = document.getElementById("album-filter-empty");

    function apply() {
      var needle = input.value.trim().toLowerCase();
      var shown = 0;
      rows.forEach(function (row) {
        // A row's text is what a person searching would type: the artist, the album and the
        // path, in the order they see them.
        var hit = !needle || row.textContent.toLowerCase().indexOf(needle) !== -1;
        row.hidden = !hit;
        if (hit) shown += 1;
      });
      if (counter) counter.textContent = shown + (shown === 1 ? " row" : " rows");
      if (empty) empty.dataset.shown = shown ? "0" : "1";
    }

    input.addEventListener("input", apply);
    // `/` focuses the filter, the way every list with a search box behaves -- except while
    // the user is already typing somewhere.
    document.addEventListener("keydown", function (event) {
      if (event.key !== "/" || event.metaKey || event.ctrlKey) return;
      var active = document.activeElement;
      if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
      event.preventDefault();
      input.focus();
    });
    apply();
  }

  initQueue();
  initWrapper();
  initFilter();
  initCopy();
})();
