/* The web player for a recorded `coder` run.
 *
 * `scripts/demo.py render CAST OUT.json --frames` writes the screen model - per frame, how long
 * it stays and only the rows that changed - and this file paints those rows into a <pre> inside
 * the same window chrome the GIF is drawn in. There is no terminal emulator here: the emulator
 * is the Python one that already draws the GIF and the SVG, so the three renderings of a cast
 * cannot drift apart.
 *
 * Markup it looks for: <div class="cast-player" data-src="figures/demo.frames.json"></div>
 */
(function (root) {
  "use strict";

  var FONT = '"DejaVu Sans Mono","SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace';
  var ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;" };

  function esc(text) {
    return text.replace(/[&<>]/g, function (c) { return ESCAPES[c]; });
  }

  // A run is positional with its trailing defaults dropped: [text], [text, fg], [text, fg, bg],
  // [text, fg, bg, 1]. A missing colour means "whatever the <pre> inherits".
  function runHtml(run) {
    var style = "";
    if (run[1]) style += "color:" + run[1] + ";";
    if (run[2]) style += "background:" + run[2] + ";";
    if (run[3]) style += "font-weight:700;";
    return style ? '<span style="' + style + '">' + esc(run[0]) + "</span>" : esc(run[0]);
  }

  function rowHtml(runs) {
    var html = "";
    for (var i = 0; i < runs.length; i++) html += runHtml(runs[i]);
    return html;
  }

  // Frames carry only what changed, so a still - the finished run, which is what a reader who
  // asked for reduced motion gets - is the frames up to that point folded together.
  function compose(data, upto) {
    var rows = [];
    for (var r = 0; r < data.rows; r++) rows.push([]);
    var last = upto === undefined ? data.frames.length - 1 : upto;
    for (var i = 0; i <= last; i++) {
      var changed = data.frames[i].rows;
      for (var key in changed) if (changed.hasOwnProperty(key)) rows[key] = changed[key];
    }
    return rows;
  }

  function element(tag, className, parent) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (parent) parent.appendChild(node);
    return node;
  }

  function build(host, data) {
    var theme = data.theme || {};
    var win = element("div", "cast-window", host);
    for (var key in theme) {
      if (theme.hasOwnProperty(key) && typeof theme[key] === "string") {
        win.style.setProperty("--cast-" + key.toLowerCase(), theme[key]);
      }
    }
    var bar = element("div", "cast-bar", win);
    var dots = element("span", "cast-dots", bar);
    for (var d = 0; d < 3; d++) element("i", null, dots);
    element("span", "cast-title", bar).textContent = data.title || "";
    var toggle = element("button", "cast-toggle", bar);
    toggle.type = "button";
    element("span", "cast-shell", bar).textContent = data.shell || "";

    var screen = element("pre", "cast-screen", win);
    screen.setAttribute("aria-label", "Recorded terminal session: " + (data.title || ""));
    var lines = [];
    for (var r = 0; r < data.rows; r++) {
      if (r) screen.appendChild(document.createTextNode("\n"));
      lines.push(element("span", null, screen));
    }
    return { win: win, screen: screen, lines: lines, toggle: toggle };
  }

  // The reader's monospace font is not ours, so the columns are fitted rather than assumed: one
  // measurement of the stack at a known size gives the advance width per pixel of font size.
  function unit() {
    if (!unit.value) {
      var ctx = document.createElement("canvas").getContext("2d");
      ctx.font = "100px " + FONT;
      unit.value = ctx.measureText("MMMMMMMMMM").width / 1000;
    }
    return unit.value;
  }

  // The padding is read back rather than assumed: a narrow screen gets a smaller one from the
  // stylesheet, and the type is only shrunk to the point where it is still worth reading - below
  // that the screen scrolls sideways instead, which beats eighty-eight columns of six-pixel text.
  function fit(view, data) {
    var style = root.getComputedStyle(view.screen);
    var room = view.screen.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    if (room <= 0) return;
    var size = Math.min(data.fontSize || 15, Math.max(9, room / data.cols / unit()));
    view.win.style.setProperty("--cast-font", size.toFixed(2) + "px");
  }

  function play(view, data) {
    var index = 0;
    var timer = null;
    // A reader who asked for reduced motion gets the finished run rather than an empty terminal,
    // and the button offers it to them anyway.
    var wanted = !(root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches);

    function paint(rows) {
      for (var key in rows) {
        if (rows.hasOwnProperty(key)) view.lines[key].innerHTML = rowHtml(rows[key]);
      }
    }

    function rewind() {  // frames are diffs, so starting over means starting from a blank screen
      index = 0;
      for (var r = 0; r < view.lines.length; r++) view.lines[r].innerHTML = "";
    }

    function step() {
      if (index >= data.frames.length) rewind();
      var frame = data.frames[index++];
      paint(frame.rows);
      timer = root.setTimeout(step, Math.max(20, frame.d * 1000));
    }

    function start() {
      if (timer === null) step();
    }

    function stop() {
      root.clearTimeout(timer);
      timer = null;
    }

    function running(on) {
      // Escaped, not typed: a stylesheet or a script is served without a charset often enough that
      // a literal glyph here comes back as mojibake on somebody's host.
      view.toggle.textContent = on ? "\u275A\u275A" : "\u25B6";
      view.toggle.setAttribute("aria-label", on ? "Pause the recording" : "Play the recording");
      view.win.classList.toggle("is-paused", !on);
    }

    view.toggle.addEventListener("click", function () {
      wanted = timer === null;
      running(wanted);
      if (wanted) start(); else stop();
    });

    running(wanted);
    if (wanted) start(); else paint(compose(data));
    // Scrolled out of sight, the recording is stopped and rewound rather than left to run: a
    // browser throttles timers in a tab nobody is looking at, and a reader who scrolls down to a
    // player that has been running without them arrives in the middle of a run. This is an
    // improvement, not the mechanism - the callback only fires while the page is being rendered,
    // which is why playback starts above rather than waiting for the first one.
    if (root.IntersectionObserver) {
      new root.IntersectionObserver(function (entries) {
        var visible = entries[entries.length - 1].isIntersecting;
        if (visible && wanted) start();
        if (!visible && wanted) {
          stop();
          rewind();
        }
      }).observe(view.win);
    }
  }

  function mount(host) {
    root.fetch(host.getAttribute("data-src")).then(function (response) {
      return response.json();
    }).then(function (data) {
      var view = build(host, data);
      fit(view, data);
      root.addEventListener("resize", function () { fit(view, data); });
      play(view, data);
    }).catch(function () {
      // A player that cannot load its frames says so rather than leaving a hole in the page.
      host.innerHTML = '<p class="cast-failed">The recording could not be loaded.</p>';
    });
  }

  var api = { esc: esc, runHtml: runHtml, rowHtml: rowHtml, compose: compose };
  if (typeof module === "object" && module.exports) {
    module.exports = api;  // so the tests can check the player against the renderer
  } else {
    root.castPlayer = api;
    document.addEventListener("DOMContentLoaded", function () {
      var hosts = document.querySelectorAll(".cast-player[data-src]");
      for (var i = 0; i < hosts.length; i++) mount(hosts[i]);
    });
  }
})(typeof window === "undefined" ? this : window);
