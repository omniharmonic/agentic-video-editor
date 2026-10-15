/* Tiny in-house timeline helper (no third-party animation library, no dependencies).
 *
 * Why this exists: frames must be a pure function of time. Templates never run CSS transitions or timers; they implement
 * window.__render(t) and the renderer calls it once per frame with t = frame / fps. Everything below is plain math.
 *
 * Globals injected by the renderer before the page loads: __TOKENS__, __CANVAS__, __PROPS__, __DURATION__, __OUTRO__.
 */
(function () {
  "use strict";
  var T = window.__TOKENS__, C = window.__CANVAS__;

  function clamp(x, a, b) { return Math.min(b, Math.max(a, x)); }
  function lerp(a, b, k) { return a + (b - a) * k; }

  /* CSS cubic-bezier(x1,y1,x2,y2) solved by bisection, so it is exact and identical on every run. */
  function bezier(x1, y1, x2, y2) {
    function pt(t, a, b) { var s = 1 - t; return 3 * s * s * t * a + 3 * s * t * t * b + t * t * t; }
    return function (x) {
      if (x <= 0) return 0;
      if (x >= 1) return 1;
      var lo = 0, hi = 1, t = x;
      for (var i = 0; i < 48; i++) { t = (lo + hi) / 2; if (pt(t, x1, x2) < x) lo = t; else hi = t; }
      return pt(t, y1, y2);
    };
  }

  var eases = {};
  Object.keys(T.easing).forEach(function (k) { var c = T.easing[k]; eases[k] = bezier(c[0], c[1], c[2], c[3]); });
  function ease(name, x) { if (!eases[name]) throw new Error("unknown easing: " + name); return eases[name](x); }

  /* Progress 0..1 of an eased segment that starts at `start` seconds and lasts `dur` seconds. */
  function phase(t, start, dur, easing) { return ease(easing || "out", clamp((t - start) / dur, 0, 1)); }

  /* Whole-template visibility: 1 between the intro and the outro, 0 after the outro. With outro disabled it stays 1. */
  function hold(t) {
    if (!window.__OUTRO__) return 1;
    var o = T.motion.out_s, end = window.__DURATION__ - (window.__FRAME__ || 0);   /* fully gone on the last frame */
    return 1 - ease("in", clamp((t - (end - o)) / o, 0, 1));
  }

  /* Brand fonts that exist on disk (the renderer lists them); everything else falls back to the token stacks. */
  function loadFonts() {
    var faces = window.__FONTFACES__ || [];
    if (!faces.length) return Promise.resolve();
    var css = faces.map(function (f) {
      return '@font-face{font-family:"' + f.family + '";src:url("' + f.url + '");font-weight:' + f.weight + ';}';
    }).join("\n");
    var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
    /* weight may be a range ("200 800") for a variable font: load at its first value */
    return Promise.all(faces.map(function (f) { return document.fonts.load(String(f.weight).split(" ")[0] + ' 1em "' + f.family + '"'); }));
  }

  function setup() {
    var r = document.documentElement.style, u = Math.min(C.w, C.h) / T.layout.unit_height;
    r.setProperty("--u", u + "px");
    Object.keys(T.colors).forEach(function (k) { r.setProperty("--c-" + k.replace(/_/g, "-"), T.colors[k]); });
    Object.keys(T.alpha).forEach(function (k) { r.setProperty("--a-" + k.replace(/_/g, "-"), T.alpha[k]); });
    Object.keys(T.fonts).forEach(function (k) {
      var f = T.fonts[k], names = [f.family].concat(f.fallback || []);
      r.setProperty("--f-" + k, names.map(function (n) { return /^[a-z-]+$/.test(n) ? n : '"' + n + '"'; }).join(", "));
    });
    Object.keys(T.type).forEach(function (k) { r.setProperty("--fs-" + k, (T.type[k] * u) + "px"); });
    Object.keys(T.tracking).forEach(function (k) { r.setProperty("--tr-" + k, T.tracking[k] + "em"); });
    r.setProperty("--mx", (T.layout.margin_x * C.w) + "px");
    r.setProperty("--my", (T.layout.margin_y * C.h) + "px");
    r.setProperty("--lt-bottom", (T.layout.lower_third_bottom * C.h) + "px");
    r.setProperty("--rise", (T.motion.rise * u) + "px");
    return u;
  }

  /* Fade up from `rise` px below: k is a 0..1 progress. Pure style writes, no transitions. */
  function rise(el, k) { el.style.opacity = k; el.style.transform = "translateY(" + ((1 - k) * T.motion.rise * (Math.min(C.w, C.h) / T.layout.unit_height)) + "px)"; }
  function backdropAlpha(name) { return { solid: T.alpha.backdrop_solid, scrim: T.alpha.scrim, none: 0 }[name]; }
  function el(id) { return document.getElementById(id); }

  function text(id, value) { var e = document.getElementById(id); if (!value) { e.style.display = "none"; } else { e.textContent = value; } return e; }

  /* Templates call ready(fn): wait for fonts, set up variables, then expose __render and the __ready flag. */
  function ready(render) {
    setup();
    window.__render = render;
    var go = function () { render(0); window.__ready = true; };
    loadFonts().then(function () { return document.fonts.ready; }).then(go, go);
  }

  window.AVE = {
    tokens: T, canvas: C, props: window.__PROPS__, duration: window.__DURATION__,
    clamp: clamp, rise: rise, backdropAlpha: backdropAlpha, el: el, lerp: lerp, bezier: bezier, ease: ease, phase: phase, hold: hold, text: text, ready: ready,
    u: function () { return Math.min(C.w, C.h) / T.layout.unit_height; }
  };
})();
