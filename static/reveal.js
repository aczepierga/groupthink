// External file: the pages carry no inline scripts or handlers.
// Debate lines sit in a <template> as escaped text nodes. They are shown one per 900 ms by copying
// each part with textContent (never innerHTML), then the picks are unhidden. Without JS the picks
// are visible at once and a <noscript> copy of the lines is shown.
// Arcade layer: a scoreboard (#arena) shows "ROUND n / N" and one energy bar per advocate. A line that
// argues for something other than the referee's pick costs its speaker 30 energy when it is shown.
// Bar widths are CSS classes (hp-100 ... hp-10), so no inline style is ever written.
(function () {
  var root = document.documentElement;
  root.classList.add("js");

  var STEP_MS = 900;
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var step = reduced ? 0 : STEP_MS;

  var tpl = document.getElementById("debate-lines");
  var out = document.getElementById("debate");
  var results = document.getElementById("results");
  var skip = document.getElementById("skip-debate");
  var arena = document.getElementById("arena");
  var HIT = 30;
  var FLOOR = 10;
  var fighters = {};
  var winnerId = null;
  var totalRounds = 1;
  var roundLabel = null;

  function part(row, cls) {
    var span = document.createElement("span");
    span.className = cls;
    var src = row.querySelector("." + cls);
    span.textContent = src ? src.textContent : "";
    return span;
  }

  function setRound(n) {
    if (roundLabel) { roundLabel.textContent = "ROUND " + n + " / " + totalRounds; }
  }

  function setEnergy(name) {
    var f = fighters[name];
    if (!f) { return; }
    var pct = Math.max(FLOOR, f.energy);
    f.fill.className = "hp hp-" + pct;
    f.box.setAttribute("aria-label", name + " energy " + pct + " percent");
  }

  // One bar per advocate, built from the template rows, plus the round label. Hidden arena stays hidden
  // when anything here fails.
  function buildArena(rows) {
    if (!arena) { return; }
    var n = parseInt(arena.getAttribute("data-rounds"), 10);
    totalRounds = n > 0 ? n : 1;
    rows.forEach(function (row) {
      if (row.classList.contains("referee")) { winnerId = row.getAttribute("data-product") || winnerId; }
    });
    var head = document.createElement("div");
    head.className = "fighters";
    rows.forEach(function (row) {
      if (!row.classList.contains("advocate")) { return; }
      var who = row.querySelector(".who");
      var name = who ? who.textContent : "";
      if (!name || fighters[name]) { return; }
      var box = document.createElement("div");
      box.className = "fighter";
      var label = document.createElement("span");
      label.className = "fname";
      label.textContent = name;
      var track = document.createElement("div");
      track.className = "track";
      track.setAttribute("role", "img");
      var fill = document.createElement("div");
      fill.className = "hp hp-100";
      track.appendChild(fill);
      box.appendChild(label);
      box.appendChild(track);
      head.appendChild(box);
      fighters[name] = { energy: 100, fill: fill, box: track };
      setEnergy(name);
    });
    roundLabel = document.createElement("p");
    roundLabel.className = "round";
    arena.appendChild(roundLabel);
    arena.appendChild(head);
    setRound(1);
    arena.hidden = false;
  }

  function score(row) {
    var round = parseInt(row.getAttribute("data-round"), 10);
    if (round > 0) { setRound(round); }
    if (row.classList.contains("referee")) {
      if (arena) { arena.classList.add("ko-on"); }
      return;
    }
    if (!row.classList.contains("advocate") || !winnerId) { return; }
    var who = row.querySelector(".who");
    var name = who ? who.textContent : "";
    if (fighters[name] && row.getAttribute("data-product") !== winnerId) {
      fighters[name].energy -= HIT;
      setEnergy(name);
    }
  }

  function showRow(row) {
    var li = document.createElement("li");
    li.className = row.className;
    li.appendChild(part(row, "who"));
    li.appendChild(part(row, "say"));
    if (row.classList.contains("referee")) { li.appendChild(part(row, "ko")); }
    out.appendChild(li);
    try { score(row); } catch (e) { /* the scoreboard is decoration; the lines already showed */ }
  }

  // The picks are visible in the HTML. Only when the template API is there do we hide them, and any
  // failure below shows them again, so a script error can never leave the page without its results.
  function showResults() {
    if (results) { results.classList.remove("pending"); }
    if (skip) { skip.hidden = true; }
    markSeen();
  }

  // A debate is animated once per run per tab. Voting, picking and reloading come back with ?seen=1 or
  // find the sessionStorage key, and go straight to the results. Storage can be blocked: every access is
  // inside try/catch and a failure just means the reveal plays again.
  var seenKey = "gt-seen:" + location.pathname + ":" + (out ? out.getAttribute("data-run") : "");

  function markSeen() {
    try { window.sessionStorage.setItem(seenKey, "1"); } catch (e) { /* storage blocked: replay is harmless */ }
  }

  function alreadySeen() {
    if (/(^|[?&])seen=1(&|$)/.test(location.search)) { return true; }
    try { return window.sessionStorage.getItem(seenKey) !== null; } catch (e) { return false; }
  }

  // After a reveal on a #results URL the rows have grown the page, so bring the results back into view.
  function landOnResults() {
    try {
      if (location.hash === "#results" && results && results.scrollIntoView) { results.scrollIntoView(); }
    } catch (e) { /* scrolling is a nicety */ }
  }

  if (tpl && out && tpl.content && results) {
    try {
      var rows = Array.prototype.slice.call(tpl.content.querySelectorAll("li"));
      var shown = 0;
      var timer = null;

      var finish = function () {
        try {
          if (timer !== null) { window.clearTimeout(timer); timer = null; }
          while (shown < rows.length) { showRow(rows[shown]); shown += 1; }
        } finally {
          showResults();
        }
        landOnResults();
      };

      var next = function () {
        timer = null;
        try {
          if (shown < rows.length) {
            showRow(rows[shown]);
            shown += 1;
            timer = window.setTimeout(next, step);
          } else {
            finish();
          }
        } catch (e) {
          showResults();
        }
      };

      try { buildArena(rows); } catch (e) { if (arena) { arena.hidden = true; } }
      if (reduced || alreadySeen()) {
        finish();
      } else {
        results.classList.add("pending");
        if (skip) { skip.hidden = false; skip.addEventListener("click", finish); }
        next();
      }
    } catch (e) {
      showResults();
    }
  }

  var copyButtons = document.querySelectorAll("[data-copy]");
  Array.prototype.forEach.call(copyButtons, function (btn) {
    btn.addEventListener("click", function () {
      var input = document.getElementById(btn.getAttribute("data-copy"));
      if (!input) { return; }
      input.select();
      var done = function () { btn.textContent = "Copied"; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(input.value).then(done, function () {
          document.execCommand("copy");
          done();
        });
      } else {
        document.execCommand("copy");
        done();
      }
    });
  });
})();
