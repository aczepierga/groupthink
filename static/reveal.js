// External file: the pages carry no inline scripts or handlers.
// Debate lines sit in a <template> as escaped text nodes. They are shown one per 900 ms by copying
// each part with textContent (never innerHTML), then the picks are unhidden. Without JS the picks
// are visible at once and a <noscript> copy of the lines is shown.
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

  function part(row, cls) {
    var span = document.createElement("span");
    span.className = cls;
    var src = row.querySelector("." + cls);
    span.textContent = src ? src.textContent : "";
    return span;
  }

  function showRow(row) {
    var li = document.createElement("li");
    li.className = row.className;
    li.appendChild(part(row, "who"));
    li.appendChild(part(row, "say"));
    out.appendChild(li);
  }

  // The picks are visible in the HTML. Only when the template API is there do we hide them, and any
  // failure below shows them again, so a script error can never leave the page without its results.
  function showResults() {
    if (results) { results.classList.remove("pending"); }
    if (skip) { skip.hidden = true; }
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

      results.classList.add("pending");
      if (skip) { skip.hidden = false; skip.addEventListener("click", finish); }
      next();
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
