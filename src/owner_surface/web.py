"""The mobile page for the owner control surface.

GATE-D1-10 A9: usable from a mobile viewport (390px) over the private network.

Self-contained on purpose — no CDN, no framework, no external font. The surface
must work on a phone on a tailnet with no public internet path, and every
external dependency is one more thing that can be unavailable at the moment the
owner actually needs to steer the build.
"""

from __future__ import annotations

MOBILE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>EFAH control</title>
<style>
  :root {
    --bg: #ffffff; --fg: #16181d; --muted: #5b6472; --line: #e3e6ea;
    --card: #f7f8fa; --accent: #1f6feb; --ok: #1a7f37; --warn: #9a6700; --bad: #cf222e;
    --radius: 12px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --line: #262c36;
      --card: #161b22; --accent: #4493f8; --ok: #3fb950; --warn: #d29922; --bad: #f85149;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
    -webkit-text-size-adjust: 100%;
  }
  .wrap { max-width: 640px; margin: 0 auto; padding: 16px 14px 96px; }
  header { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
  h1 { font-size: 19px; margin: 0; letter-spacing: -0.01em; }
  .ver { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 16px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius);
          padding: 14px; margin-bottom: 12px; }
  .card h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
             color: var(--muted); margin: 0 0 10px; font-weight: 600; }
  .stat { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0;
          border-bottom: 1px solid var(--line); font-size: 14px; }
  .stat:last-child { border-bottom: 0; }
  .stat dt { color: var(--muted); }
  .stat dd { margin: 0; font-variant-numeric: tabular-nums; text-align: right;
             overflow-wrap: anywhere; }
  dl { margin: 0; }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px;
          font-weight: 600; border: 1px solid transparent; }
  .pill.ok   { color: var(--ok);   border-color: var(--ok); }
  .pill.warn { color: var(--warn); border-color: var(--warn); }
  .pill.bad  { color: var(--bad);  border-color: var(--bad); }
  .verbs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
  button {
    font: inherit; font-size: 14px; padding: 11px 14px; min-height: 44px;
    border-radius: 10px; border: 1px solid var(--line); background: var(--bg);
    color: var(--fg); cursor: pointer; touch-action: manipulation;
  }
  button[aria-pressed="true"] { border-color: var(--accent); color: var(--accent); font-weight: 600; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff;
                   font-weight: 600; width: 100%; }
  button:active { opacity: .8; }
  textarea, input {
    width: 100%; font: inherit; font-size: 16px; padding: 11px 12px; margin-bottom: 8px;
    border: 1px solid var(--line); border-radius: 10px; background: var(--bg);
    color: var(--fg); resize: vertical;
  }
  textarea:focus, input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  .out { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; margin-top: 10px; }
  .out.ok  { border-left: 3px solid var(--ok);  padding-left: 10px; }
  .out.bad { border-left: 3px solid var(--bad); padding-left: 10px; }
  .meta { color: var(--muted); font-size: 12px; margin-top: 6px;
          font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  .blocker { border-left: 3px solid var(--warn); padding-left: 10px; margin-bottom: 12px; }
  .blocker .q { font-size: 14px; }
  .unit { display: flex; justify-content: space-between; gap: 10px; padding: 7px 0;
          border-bottom: 1px solid var(--line); font-size: 13px; }
  .unit:last-child { border-bottom: 0; }
  .unit .id { font-variant-numeric: tabular-nums; }
  footer { color: var(--muted); font-size: 11px; text-align: center; margin-top: 18px; line-height: 1.6; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>EFAH control</h1>
    <span class="ver">EFAH-CONTRACT-001 v1.1 &middot; &sect;11.7</span>
  </header>
  <p class="sub">Vendor-neutral owner surface. Every command is a request that
  enters the normal gate path.</p>

  <section class="card" aria-live="polite">
    <h2>Project</h2>
    <dl id="state"><div class="stat"><dt>loading</dt><dd>&hellip;</dd></div></dl>
  </section>

  <section class="card" id="blockers-card" hidden>
    <h2>Open owner blockers</h2>
    <div id="blockers"></div>
  </section>

  <section class="card" id="units-card" hidden>
    <h2>Work units</h2>
    <div id="units"></div>
  </section>

  <section class="card">
    <h2>Command</h2>
    <div class="verbs" role="group" aria-label="Verb">
      <button type="button" data-verb="OBSERVE" aria-pressed="true">Observe</button>
      <button type="button" data-verb="ANSWER_BLOCKER" aria-pressed="false">Answer</button>
      <button type="button" data-verb="RESUME" aria-pressed="false">Resume</button>
      <button type="button" data-verb="RETRY" aria-pressed="false">Retry</button>
      <button type="button" data-verb="CANCEL" aria-pressed="false">Cancel</button>
      <button type="button" data-verb="INSTRUCT" aria-pressed="false">Instruct</button>
    </div>
    <input id="target" placeholder="Target id (optional) e.g. WU-0042" autocomplete="off"
           autocapitalize="characters" spellcheck="false">
    <textarea id="text" rows="3" placeholder="Say what you want."></textarea>
    <button class="primary" id="send" type="button">Send</button>
    <div class="out" id="out" hidden></div>
    <div class="meta" id="meta" hidden></div>
  </section>

  <footer>
    No Anthropic credential is required to run this surface.<br>
    It cannot change scope, bypass a gate, self-approve, or reach protected assets.
  </footer>
</div>
<script>
(function () {
  "use strict";
  var verb = "OBSERVE";
  var $ = function (id) { return document.getElementById(id); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pill(state) {
    var cls = "warn";
    if (state === "VERIFIED_COMPLETE" || state === "RUNNING" || state === "PASSED") cls = "ok";
    if (String(state).indexOf("FAILED") === 0) cls = "bad";
    return '<span class="pill ' + cls + '">' + esc(state) + "</span>";
  }
  function row(k, v) {
    return "<div class='stat'><dt>" + esc(k) + "</dt><dd>" + v + "</dd></div>";
  }

  document.querySelectorAll("[data-verb]").forEach(function (b) {
    b.addEventListener("click", function () {
      verb = b.dataset.verb;
      document.querySelectorAll("[data-verb]").forEach(function (o) {
        o.setAttribute("aria-pressed", String(o === b));
      });
    });
  });

  function render(v) {
    $("state").innerHTML =
      row("Status", pill(v.project_state)) +
      row("Project", esc(v.project_id)) +
      row("Contract", esc(v.contract_id) + " v" + esc(v.contract_version)) +
      row("Work units", esc(v.tasks_passed) + " / " + esc(v.tasks_total) + " passed") +
      row("Blocked", esc(v.tasks_blocked)) +
      row("Graph", v.terminus_database
            ? esc(v.terminus_database) + "@" + esc(v.terminus_branch || "main")
            : "<span class='pill warn'>not initialised</span>");

    var bl = v.open_blockers || [];
    $("blockers-card").hidden = bl.length === 0;
    $("blockers").innerHTML = bl.map(function (b) {
      return "<div class='blocker'><div class='q'><strong>" + esc(b.blocker_id) + "</strong> &middot; " +
        esc(b.interrupt_type) + "<br>" + esc(b.question) + "</div>" +
        (b.options && b.options.length
          ? "<div class='meta'>" + b.options.map(esc).join(" &middot; ") + "</div>" : "") +
        "</div>";
    }).join("");

    var us = v.work_units || [];
    $("units-card").hidden = us.length === 0;
    $("units").innerHTML = us.map(function (u) {
      return "<div class='unit'><span class='id'>" + esc(u.work_unit_id) + "</span>" +
             pill(u.state) + "</div>";
    }).join("");
  }

  function refresh() {
    fetch("state").then(function (r) { return r.json(); }).then(render).catch(function (e) {
      $("state").innerHTML = row("Unreachable", "<span class='pill bad'>" + esc(e.message) + "</span>");
    });
  }

  $("send").addEventListener("click", function () {
    var btn = $("send");
    btn.disabled = true;
    btn.textContent = "Sending\\u2026";
    fetch("command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: $("text").value,
        verb: verb,
        target_id: $("target").value || null,
        contract_version: "1.1"
      })
    }).then(function (r) { return r.json(); }).then(function (d) {
      var out = $("out");
      out.hidden = false;
      out.className = "out " + (d.accepted ? "ok" : "bad");
      out.textContent = (d.accepted ? "\\u2713 " : "\\u2717 ") + d.message;
      var bits = [];
      if (d.rejection_reason) bits.push(d.rejection_reason);
      if (d.drift_finding) bits.push(d.drift_finding);
      if (d.record_id) bits.push("record " + d.record_id);
      if (d.terminus_commit) bits.push("commit " + String(d.terminus_commit).slice(0, 12));
      bits.push(d.entered_gate_path ? "entered gate path" : "read only");
      $("meta").hidden = false;
      $("meta").textContent = bits.join(" \\u00b7 ");
      if (d.view) render(d.view); else refresh();
    }).catch(function (e) {
      var out = $("out");
      out.hidden = false;
      out.className = "out bad";
      out.textContent = "\\u2717 " + e.message;
    }).finally(function () {
      btn.disabled = false;
      btn.textContent = "Send";
    });
  });

  refresh();
  setInterval(refresh, 15000);
})();
</script>
</body>
</html>
"""
