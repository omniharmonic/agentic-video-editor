/* Helpers shared by the still-content pages in motion-kit/stills/ (posts, thumbnails, carousel slides, reel cards).
 * Loaded after timeline.js. Everything is deterministic: no timers, no transitions, layout is a pure function of the props.
 *
 * The renderer injects these props next to the template's slots (names start with an underscore):
 *   _style   colour roles resolved from tokens.json (bg, fg, fg_soft, accent, panel)
 *   _type    the still type scale from brand/styles.json, authored for a 1080 px wide post
 *   _safe    {x, top, bottom}: fractions of the canvas that stay free of text
 *   _format  "4x5" etc.
 * Elements marked data-measure are reported to the critic (rect of the text, font size, colour, overflow).
 */
(function () {
  "use strict";
  var A = window.AVE, P = A.props, C = A.canvas;
  var ST = P._style, TY = P._type, SAFE = P._safe;
  var s = C.w / 1080;                                   /* px scale: every size below is authored for a 1080 px wide canvas */

  function px(n) { return n * s; }

  function setup() {
    var r = document.documentElement.style;
    r.setProperty("--s", s + "px");
    Object.keys(ST).forEach(function (k) { r.setProperty("--st-" + k.replace(/_/g, "-"), ST[k]); });
    r.setProperty("--safe-l", (SAFE.x * C.w) + "px");
    r.setProperty("--safe-r", (SAFE.x * C.w) + "px");
    r.setProperty("--safe-t", (SAFE.top * C.h) + "px");
    r.setProperty("--safe-b", (SAFE.bottom * C.h) + "px");
    r.setProperty("--fs-label-s", px(TY.label) + "px");
    r.setProperty("--tr-label-s", P._tracking.label + "em");
    r.setProperty("--fs-name-s", px(TY.name) + "px");
    r.setProperty("--fs-body-s", px(TY.body) + "px");
  }

  /* Largest font size in [min, max] (step px) for which fits() is true; falls back to min and records the failure. */
  function fit(el, maxPx, minPx, fits) {
    var size = maxPx, step = Math.max(1, s), ok = false;
    while (size >= minPx) {
      el.style.fontSize = size + "px";
      if (fits()) { ok = true; break; }
      size -= step;
    }
    if (!ok) { size = minPx; el.style.fontSize = size + "px"; }
    window.__FIT__ = window.__FIT__ || {};
    window.__FIT__[el.id] = { size: size, ok: ok && fits() };
    return size;
  }

  function textRect(el) {                                /* tight bounds of the rendered text, not of the element box */
    var r = document.createRange();
    r.selectNodeContents(el);
    var b = r.getBoundingClientRect();
    return { x: b.left, y: b.top, w: b.width, h: b.height };
  }

  function measure() {
    var out = [];
    document.querySelectorAll("[data-measure]").forEach(function (el) {
      if (el.style.display === "none" || !el.textContent.trim()) return;
      var cs = getComputedStyle(el), r = textRect(el);
      out.push({ id: el.id, name: el.getAttribute("data-measure"), text: el.textContent.slice(0, 80), rect: r,
                 font_px: parseFloat(cs.fontSize), color: cs.color, hang: Math.max(0, -(parseFloat(cs.textIndent) || 0)),
                 overflow: el.scrollWidth > el.clientWidth + 1 && cs.overflow !== "visible" });
    });
    return out;
  }
  window.__measure = measure;

  /* Show every ellipsized/hidden helper text only when it has content (keeps empty slots from taking space). */
  function setText(id, value) { return A.text(id, value); }

  /* Typographic apostrophes between letters (couldn't -> couldn\u2019t). Display only: stored slot text is never changed. */
  function typo(t) { return String(t).replace(/(\w)'(\w)/g, "$1\u2019$2"); }

  /* Wait for images, then let timeline.js load fonts and expose __render/__ready. */
  function boot(render, images) {
    setup();
    var waits = (images || []).map(function (img) { return img.decode ? img.decode().catch(function () {}) : Promise.resolve(); });
    Promise.all(waits).then(function () { A.ready(render); });
  }

  window.STILL = { s: s, px: px, ST: ST, TY: TY, SAFE: SAFE, fit: fit, measure: measure, boot: boot, setText: setText, typo: typo,
                   counter: function (i, n) { return (i < 10 ? "0" : "") + i + " / " + (n < 10 ? "0" : "") + n; } };
})();
