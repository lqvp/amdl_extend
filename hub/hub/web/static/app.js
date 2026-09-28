/* amd-hub's client, in one file and about four hundred and fifty lines.
 *
 * No HTMX. The brief named server-rendered HTMX templates, and the server-rendered half is
 * what is here -- every page arrives complete from Jinja2 and every action is a form that
 * works without this file. What is missing is the HTMX *runtime*: htmx's SSE extension is
 * what the live queue would use, and a ~3 KB script fetched from a CDN at page load is a
 * worse trade for a tool on a home LAN than a script this size that uses the platform's own
 * `EventSource`. So the interactive half is native, and the templates use `data-action`
 * attributes rather than `hx-*` ones -- a `hx-post` on a page with no htmx silently does
 * nothing, which is the worst of both.
 *
 * (This line used to say "about a hundred lines", and said so for long enough that it had
 * become the file's least accurate statement. A header that understates the file is not
 * harmless: it is the number someone quotes when deciding whether to read it.)
 *
 * The two things this file must not get wrong:
 *
 *   - the snapshot. The stream sends a full queue first and then one change at a time, so a
 *     reconnect is a resync rather than a patch. `replaceRows` is therefore only ever called
 *     for a snapshot, and `upsertRow` otherwise; applying a snapshot as a merge would leave
 *     rows the user just cancelled on screen.
 *   - the rows. `textContent` everywhere, never `innerHTML`. `skip_reason` holds directory
 *     names read off the filesystem, and a user-curated library really does contain
 *     directories called `<img src=x onerror=...>`. The server escapes them too; this is the
 *     second of the two, not the only.
 */
(function () {
  "use strict";

  var body = document.getElementById("queue-body");
  if (!body) return;

  var logPane = document.getElementById("log");
  var state = document.getElementById("stream-state");
  var jobs = new Map();

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

  function buildRow(job) {
    var tr = el("tr", "status-" + job.status);
    tr.id = "job-" + job.id;
    tr.dataset.jobId = job.id;
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
    return tr;
  }

  function upsertRow(job) {
    jobs.set(job.id, job);
    var row = document.getElementById("job-" + job.id);
    if (row) body.replaceChild(buildRow(job), row);
    else body.appendChild(buildRow(job));
    // Rows arrive over SSE as well as in the snapshot, so the "hide finished" filter has to
    // be applied on every path that changes a row. A job that finishes while the filter is
    // on must disappear, not sit there looking unfinished.
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
  function setStreamState(name, label) {
    if (!state) return;
    state.dataset.state = name;
    state.textContent = label;
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
        // A new batch's ids are in `created`; the rows themselves arrive as `job` events as
        // the scheduler runs them. Nothing is inserted here, because a row for a queued job
        // that has not been claimed yet would have no title and no progress.
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
    // EventSource reconnects on its own and the stream's first frame is a fresh snapshot, so
    // a drop is a resync rather than a gap. Saying so is better than a spinner that lies.
    setStreamState("reconnecting", "reconnecting…");
  });

  // -- actions ------------------------------------------------------------

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

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-action]");
    if (!target) return;
    var action = target.dataset.action;
    var jobId = target.dataset.jobId;

    if (action === "wrapper-start") post("/api/wrapper/start").then(reload);
    else if (action === "wrapper-stop") post("/api/wrapper/stop").then(reload);
    else if (action === "wrapper-restart") post("/api/wrapper/restart").then(reload);
    else if (action === "wrapper-login") {
      var card = document.getElementById("login-card");
      if (card) card.hidden = !card.hidden;
    } else if (action === "job-cancel" && jobId) {
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

  // -- collapsing the finished rows -----------------------------------------
  //
  // A queue that only grows is a queue you scroll, and this one is a record of every
  // track the user has ever asked for. The rows stay in the DOM -- hiding them with
  // `display: none` rather than removing them keeps the SSE upsert working against the
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
    applyFinishedFilter();
  }

  function applyFinishedFilter() {
    var hide = !finishedVisible();
    document.querySelectorAll("#queue-body tr[data-finished]").forEach(function (row) {
      row.hidden = hide && row.dataset.finished === "1";
    });
  }

  setFinishedVisible(false);

  // -- re-queueing and clearing ---------------------------------------------
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
        // another job already holds, and saying so is the difference between "the button
        // is broken" and "one of these was already on its way".
        var refused = result.data.refused || [];
        if (refused.length) {
          log(refused.length + " could not be re-queued: already running as job " + refused.join(", "));
        }
        reload(result);
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
})();
