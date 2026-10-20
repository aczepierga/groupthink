// Reveals elements marked data-reveal one after another. External file: the pages carry no inline scripts or handlers.
(function () {
  document.documentElement.classList.add("js");
  var items =document.querySelectorAll("[data-reveal]");
  var delay = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 250;
  Array.prototype.forEach.call(items, function (el, i) {
    window.setTimeout(function () { el.classList.add("revealed"); }, i * delay);
  });
})();
