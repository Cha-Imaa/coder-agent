/* The talk's behaviour: screen navigation, the two loop diagrams, and the charts.
 *
 * Plain JavaScript, no libraries. The charts are SVG drawn from `data.json`, which
 * `evals/talk_data.py` writes from the committed eval results, so every bar on these screens
 * traces back to a results file the same way the PNGs on the results page do.
 *
 * Sections:
 *   1. helpers          small SVG and number utilities
 *   2. navigation       dot rail, stage line, counter, progress bar, keyboard (F for full
 *                       screen), "is-in" reveal
 *   3. loop diagrams    the outer and inner loop, drawn around a ring, walked one stage per key
 *   4. charts           columns, horizontal stacked bars, and the tooltip
 *   5. data             fetch data.json, fill the quoted numbers, draw every chart
 */
(function () {
  "use strict";

  // Set first, before anything paints: the stylesheet only hides content for its entrance
  // animation when the script that will reveal it is actually running.
  document.documentElement.classList.add("js");

  /* ---------------------------------------------------------------------------------------- */
  /* 1. Helpers                                                                               */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * The value of a colour token from the stylesheet, such as `token("navy")` -> "#1e4a8a".
   * Read at draw time, so the SVG always matches the stylesheet's palette.
   */
  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue("--" + name).trim();
  }

  // The palette is read from the stylesheet, not written here, so a colour is set in one place.
  // SERIES is the figures' fixed series order (src/coder_agent/evals/figures.py).
  var SERIES = ["navy", "dusty", "sage", "lavender"].map(token);
  var COLOR = {
    navy: token("navy"), blue: token("blue"), title: token("title"),
    card: token("card"), box: token("box"), chip: token("chip"),
    track: token("loop-track"), line: token("loop-line"), label: token("loop-label"), edge: token("loop-edge"),
    pass: token("loop-pass"), passTint: token("loop-pass-tint"),
    fail: token("loop-fail"), failTint: token("loop-fail-tint"),
    hold: token("loop-hold"), holdTint: token("loop-hold-tint"),
  };
  var SVG_NS = "http://www.w3.org/2000/svg";
  var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /** Create an SVG element with attributes and append it to `parent` (if given). */
  function el(tag, attrs, parent) {
    var node = document.createElementNS(SVG_NS, tag);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) node.setAttribute(key, attrs[key]);
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  /** An SVG text node: `el` plus its content. */
  function text(parent, x, y, content, attrs) {
    var node = el("text", Object.assign({ x: x, y: y }, attrs || {}), parent);
    node.textContent = content;
    return node;
  }

  /** 29662 -> "30k", 9493 -> "9.5k": the figures' own short form (`_kilo` in figures.py). */
  function kilo(value) {
    if (value === 0) return "0";
    return value < 10000 ? (value / 1000).toFixed(1) + "k" : Math.round(value / 1000) + "k";
  }

  /** A whole-number percent: 0.9167 -> "92%". */
  function percent(value) {
    return Math.round(value * 100) + "%";
  }

  /** 68869 -> "68,869". */
  function grouped(value) {
    return Math.round(value).toLocaleString("en-US");
  }

  /**
   * Round axis ticks from zero to just above `max`: a step of 1, 2 or 5 times a power of ten,
   * about `count` of them. Returns the tick values, the last of which is the axis maximum.
   */
  function niceTicks(max, count) {
    var raw = max / count;
    var power = Math.pow(10, Math.floor(Math.log10(raw)));
    var step = [1, 2, 5, 10].map(function (m) { return m * power; })
      .find(function (s) { return s >= raw; });
    var ticks = [];
    for (var v = 0; v <= max + step * 0.999; v += step) ticks.push(v);
    return ticks;
  }

  /** A column with a rounded top and a square foot on the baseline. */
  function columnPath(x, y, width, height, radius) {
    var r = Math.min(radius, width / 2, height);
    return "M" + x + "," + (y + height) +
      "V" + (y + r) + "Q" + x + "," + y + " " + (x + r) + "," + y +
      "H" + (x + width - r) + "Q" + (x + width) + "," + y + " " + (x + width) + "," + (y + r) +
      "V" + (y + height) + "Z";
  }

  /** A horizontal bar with a rounded right end (or square, for a segment with another after it). */
  function hbarPath(x, y, width, height, radius) {
    var r = Math.min(radius, height / 2, width);
    return "M" + x + "," + y + "H" + (x + width - r) +
      "Q" + (x + width) + "," + y + " " + (x + width) + "," + (y + r) +
      "V" + (y + height - r) + "Q" + (x + width) + "," + (y + height) + " " + (x + width - r) + "," + (y + height) +
      "H" + x + "Z";
  }

  /* ---------------------------------------------------------------------------------------- */
  /* 2. Navigation                                                                            */
  /* ---------------------------------------------------------------------------------------- */

  var screens = Array.prototype.slice.call(document.querySelectorAll(".screen"));
  var rail = document.querySelector(".rail");
  var counterNow = document.querySelector(".counter-now");
  var kicker = document.querySelector(".kicker");
  var stageLinks = Array.prototype.slice.call(document.querySelectorAll(".stages a"));
  var progressFill = document.querySelector(".progress span");
  var current = 0;

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  /** One dot per screen, each a link to it, with the screen's title as its label. */
  function buildRail() {
    document.querySelector(".counter-all").textContent = pad(screens.length);
    // The closing screen's own header shows the same count, so it stays right if screens change.
    var endCount = document.querySelector(".end-count");
    if (endCount) endCount.textContent = screens.length + " / " + screens.length;
    screens.forEach(function (screen) {
      var link = document.createElement("a");
      link.href = "#" + screen.id;
      var label = document.createElement("span");
      label.textContent = screen.getAttribute("data-title");
      link.appendChild(label);
      link.setAttribute("aria-label", screen.getAttribute("data-title"));
      rail.appendChild(link);
    });
  }

  /**
   * Mark screen `index` as the current one in the rail, the top-left label, the counter, the
   * progress bar and the address bar.
   */
  function setCurrent(index) {
    // A loop left half-walked would look broken when the reader comes back to it, so the screen
    // being left shows its whole diagram again.
    if (index !== current) resetLoop(screens[current]);
    current = index;
    counterNow.textContent = pad(index + 1);
    // The top-left label shows this slide's eyebrow; a slide without one (the closing screen)
    // leaves the corner empty.
    var eyebrow = screens[index].querySelector(".eyebrow");
    kicker.textContent = eyebrow ? eyebrow.textContent : "";
    // The closing screen carries its own header, so the corner chrome steps aside there.
    document.documentElement.classList.toggle("on-end", screens[index].id === "end");
    progressFill.style.transform = "scaleX(" + (index + 1) / screens.length + ")";
    var links = rail.querySelectorAll("a");
    for (var i = 0; i < links.length; i++) links[i].setAttribute("aria-current", i === index ? "true" : "false");
    markStage(screens[index].getAttribute("data-stage"));
    // replaceState, not location.hash: a shareable link to this screen without a history entry
    // per scroll, and without the jump that assigning the hash would cause.
    if (window.history && history.replaceState) history.replaceState(null, "", "#" + screens[index].id);
  }

  /**
   * Light up the chapter `stage` in the stage line, the way the CLI moves from one stage to the
   * next: chapters already passed are marked done, the current one is highlighted, and the ones
   * still ahead stay faint.
   */
  function markStage(stage) {
    var now = stageLinks.map(function (link) { return link.getAttribute("data-stage"); }).indexOf(stage);
    stageLinks.forEach(function (link, i) {
      link.setAttribute("aria-current", i === now ? "step" : "false");
      link.classList.toggle("is-done", i < now);
    });
  }

  function goTo(index) {
    var target = Math.max(0, Math.min(screens.length - 1, index));
    // Updated now rather than when the scroll settles, so pressing a key three times quickly
    // moves three screens instead of aiming at the same next one three times.
    setCurrent(target);
    screens[target].scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth" });
  }

  /**
   * One step forward (1) or back (-1). On a loop screen the step lights the loop's next stage
   * first, and only turns the page once the walk is over, so a presenter tells the loop one
   * stage per key press.
   */
  function step(direction) {
    if (!walkLoop(direction)) goTo(current + direction);
  }

  /** Enter or leave full screen, the presenter's F key. Esc also leaves, as in any browser. */
  function toggleFullScreen() {
    if (!document.fullscreenEnabled) return;
    // Both return promises that reject when the browser says no (for example when the key press
    // did not come from a person); there is nothing to do then, so the refusal is swallowed
    // rather than logged as an uncaught error.
    var request = document.fullscreenElement
      ? document.exitFullscreen()
      : document.documentElement.requestFullscreen();
    if (request && request.catch) request.catch(function () {});
  }

  /**
   * Arrow keys, Page Up/Down, Space and Home/End move one whole screen (or one loop stage).
   * Native arrow scrolling moves forty pixels, which mandatory snapping would just pull back, so
   * these are taken over. Page Up/Down matter most: they are what a presentation clicker sends.
   * Space on a control (the player's pause button) is left to the control.
   */
  function onKey(event) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    var inControl = event.target.closest && event.target.closest("button, a, input, textarea, select");
    var next = { ArrowDown: 1, ArrowRight: 1, PageDown: 1, ArrowUp: -1, ArrowLeft: -1, PageUp: -1 };
    if (event.key in next) {
      step(next[event.key]);
    } else if (event.key === " " && !inControl) {
      step(event.shiftKey ? -1 : 1);
    } else if (event.key === "Home") {
      goTo(0);
    } else if (event.key === "End") {
      goTo(screens.length - 1);
    } else if (event.key === "f" || event.key === "F") {
      toggleFullScreen();
    } else {
      return;
    }
    event.preventDefault();
  }

  /**
   * Two observers: one decides which screen is current (the one filling most of the viewport),
   * the other adds `is-in` the first time a screen is well into view, which starts its entrance
   * animation. Kept separate because "current" must flip back and forth, while an animation
   * that replays on every pass gets tiresome.
   */
  function watchScreens() {
    if (!("IntersectionObserver" in window)) {
      screens.forEach(function (s) { s.classList.add("is-in"); });
      return;
    }
    var currentWatch = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) setCurrent(screens.indexOf(entry.target));
      });
    }, { threshold: 0.55 });
    var revealWatch = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in");
          revealWatch.unobserve(entry.target);
        }
      });
    }, { threshold: 0.3 });
    screens.forEach(function (s) {
      currentWatch.observe(s);
      revealWatch.observe(s);
    });
  }

  /* ---------------------------------------------------------------------------------------- */
  /* 3. Loop diagrams                                                                         */
  /* ---------------------------------------------------------------------------------------- */

  // The two loops of the ivory loop figures. Nodes sit evenly around a ring, clockwise from
  // the top; edge i runs from node i to node i+1 and wraps round to the first.
  //
  // Colour is kept for meaning only. A plain node is a white box with a hairline border and a
  // navy badge (no `accent`); a node that stands for an outcome takes that outcome's colour:
  // rose for learning from a failure, lavender for the pause to ask you. Arrows are slate, except
  // the one a failure takes (`tone: "fail"`), and the way out is green.
  var LOOPS = {
    outer: {
      center: ["4 iterations", "max_iterations"],
      nodes: [
        { title: "Plan", sub: "one call, numbered steps", code: "plan" },
        { title: "Execute", sub: "the inner tool loop", code: "act" },
        { title: "Run the tests", sub: "the repo's test command", code: "run_tests" },
        { title: "Reflect", sub: "failure output fed back", code: "reflect", accent: COLOR.fail, fill: COLOR.failTint },
      ],
      edges: [
        { label: "plan ready" },
        { label: "no more tool calls" },
        { label: "tests fail", tone: "fail" },
        { label: "try again" },
      ],
      exit: { from: 2, label: "tests pass", text: "Done" },
    },
    inner: {
      center: ["repeat", "until no tool call"],
      nodes: [
        { title: "The model decides", sub: "emits tool calls", code: "act" },
        { title: "Approval", sub: "for edits and commands", code: "approve", accent: COLOR.hold, fill: COLOR.holdTint },
        { title: "The tool runs", sub: "over MCP, in the sandbox", code: "tools" },
      ],
      edges: [
        { label: "tool call" },
        { label: "approved" },
        { label: "tool result" },
      ],
    },
  };

  var RING = 190;                      // ring radius
  var BOX = { w: 184, h: 96 };         // node box size
  var DEG = Math.PI / 180;

  /** The point on the ring at `angle` degrees (0 = right, 90 = down, as on screen). */
  function onRing(angle) {
    return { x: RING * Math.cos(angle * DEG), y: RING * Math.sin(angle * DEG) };
  }

  /** Whether a point lies inside a node's box grown by `margin`. */
  function insideBox(point, center, margin) {
    return Math.abs(point.x - center.x) < BOX.w / 2 + margin &&
      Math.abs(point.y - center.y) < BOX.h / 2 + margin;
  }

  /** Walk along the ring from `angle` in `direction` until leaving the node's box. */
  function leaveBox(angle, center, direction, margin) {
    var a = angle;
    while (insideBox(onRing(a), center, margin)) a += direction;
    return a;
  }

  /** A filled triangle pointing along the clockwise tangent of the ring at `angle`. */
  function arrowHead(parent, angle, color, cls) {
    var tip = onRing(angle);
    var dir = { x: -Math.sin(angle * DEG), y: Math.cos(angle * DEG) };    // clockwise tangent
    var side = { x: -dir.y, y: dir.x };
    var back = { x: tip.x - dir.x * 13, y: tip.y - dir.y * 13 };
    var points = [
      [tip.x, tip.y],
      [back.x + side.x * 7, back.y + side.y * 7],
      [back.x - side.x * 7, back.y - side.y * 7],
    ].map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ");
    el("polygon", { points: points, fill: color, class: cls }, parent);
  }

  /**
   * An arrow's label centred on (x, y). It sits on a patch of the card's own colour with no
   * outline, so it just interrupts the ring instead of adding one more shape to the drawing.
   */
  function pill(parent, x, y, label, color) {
    var width = label.length * 7 + 18;
    el("rect", {
      x: x - width / 2, y: y - 13, width: width, height: 26, rx: 13, fill: COLOR.card,
    }, parent);
    text(parent, x, y + 4.5, label, { class: "loop-edge", fill: color, "text-anchor": "middle" });
  }

  /**
   * A group that lights up during the step-by-step walk. `steps` lists the walk steps it belongs
   * to: a node is lit at its own step, and the exit's source node again at the exit step.
   */
  function stepGroup(parent, steps) {
    return el("g", { class: "loop-step", "data-steps": steps.join(" ") }, parent);
  }

  /**
   * Draw one loop into `host`: ring, arcs with arrows and labels, nodes, centre text, exit.
   * The walk has one step per node (the node and the arrow leaving it), plus one for the exit
   * if the loop has one; the step count is kept on the host for `walkLoop`.
   */
  function drawLoop(host, spec) {
    var svg = el("svg", { "aria-hidden": "true" });
    var count = spec.nodes.length;
    var angles = spec.nodes.map(function (_, i) { return -90 + (360 * i) / count; });
    var centers = angles.map(onRing);
    var exitStep = spec.exit ? count : -1;
    host.loopSteps = count + (spec.exit ? 1 : 0);
    host.walkStep = -1;   // -1: no walk, the whole diagram shows

    // The pale track the arrows run along.
    el("circle", { cx: 0, cy: 0, r: RING, fill: "none", stroke: COLOR.track, "stroke-width": 30 }, svg);

    // Arcs between consecutive nodes, each starting and ending just clear of the boxes. An arc
    // lights with the node it leaves, so each step says "this happens, then it goes there".
    spec.edges.forEach(function (edge, i) {
      var group = stepGroup(svg, [i]);
      var lineColor = edge.tone === "fail" ? COLOR.fail : COLOR.line;
      var labelColor = edge.tone === "fail" ? COLOR.fail : COLOR.label;
      var next = (i + 1) % count;
      var endAngle = angles[next] + (next === 0 ? 360 : 0);
      var start = leaveBox(angles[i], centers[i], 1, 10);
      var end = leaveBox(endAngle, centers[next], -1, 14);
      var a = onRing(start);
      var b = onRing(end - 4);   // the line stops short of the tip, so the head stays sharp
      el("path", {
        d: "M" + a.x.toFixed(1) + "," + a.y.toFixed(1) +
          "A" + RING + "," + RING + " 0 0 1 " + b.x.toFixed(1) + "," + b.y.toFixed(1),
        fill: "none", stroke: lineColor, "stroke-width": 2, "stroke-linecap": "round",
        pathLength: 1, class: "loop-arc",
      }, group);
      arrowHead(group, end, lineColor, "loop-head");
      var mid = onRing((start + end) / 2);
      pill(group, mid.x, mid.y, edge.label, labelColor);
    });

    // Nodes: a box, a numbered badge on its corner, a title, a line of description, the node's
    // name in the code.
    spec.nodes.forEach(function (node, i) {
      var group = stepGroup(svg, spec.exit && spec.exit.from === i ? [i, exitStep] : [i]);
      var c = centers[i];
      var x = c.x - BOX.w / 2;
      var y = c.y - BOX.h / 2;
      // A plain node: white box, hairline border, navy badge. An outcome node: its colour.
      var border = node.accent || COLOR.edge;
      var badge = node.accent || COLOR.title;
      el("rect", { x: x, y: y, width: BOX.w, height: BOX.h, rx: 14, fill: node.fill || COLOR.box, stroke: border, "stroke-width": 1.5 }, group);
      el("circle", { cx: x + 8, cy: y + 4, r: 14, fill: badge, stroke: COLOR.card, "stroke-width": 3 }, group);
      text(group, x + 8, y + 9, String(i + 1), { class: "loop-badge", "text-anchor": "middle" });
      text(group, c.x, c.y - 12, node.title, { class: "loop-title", "text-anchor": "middle" });
      text(group, c.x, c.y + 10, node.sub, { class: "loop-sub", "text-anchor": "middle" });
      text(group, c.x, c.y + 32, node.code, { class: "loop-code", "text-anchor": "middle" });
    });

    text(svg, 0, 4, spec.center[0], { class: "loop-center", "text-anchor": "middle" });
    text(svg, 0, 32, spec.center[1], { class: "loop-center-sub", "text-anchor": "middle" });

    // The way out of the loop: an arrow from the right side of one node to a "Done" pill. It is
    // the walk's last step, lit together with the node it leaves from.
    var right = RING + BOX.w / 2;
    if (spec.exit) {
      var exit = stepGroup(svg, [exitStep]);
      var from = centers[spec.exit.from];
      var x0 = from.x + BOX.w / 2 + 8;
      var x1 = x0 + 74;
      // The way out is the run's "passed", so it is drawn in the passed green.
      el("path", { d: "M" + x0 + "," + from.y + "H" + (x1 - 12), stroke: COLOR.pass, "stroke-width": 2, fill: "none" }, exit);
      el("polygon", { points: x1 + "," + from.y + " " + (x1 - 13) + "," + (from.y - 7) + " " + (x1 - 13) + "," + (from.y + 7), fill: COLOR.pass }, exit);
      text(exit, (x0 + x1) / 2, from.y + 26, spec.exit.label, { class: "loop-edge", fill: COLOR.pass, "text-anchor": "middle" });
      el("rect", { x: x1 + 6, y: from.y - 22, width: 86, height: 44, rx: 22, fill: COLOR.passTint, stroke: COLOR.pass, "stroke-width": 1.5 }, exit);
      text(exit, x1 + 49, from.y + 6, spec.exit.text, { class: "loop-title", style: "fill: " + COLOR.pass, "text-anchor": "middle" });
      right = Math.max(right, x1 + 96);
    }

    // The view box is the drawing's own extent plus a margin, so nothing is clipped and the
    // diagram is centred whatever the number of nodes.
    // The ring itself counts too: with three nodes, the bottom of the ring and its edge label
    // reach lower than any box.
    var ys = centers.map(function (c) { return c.y; });
    var top = Math.min(Math.min.apply(null, ys) - BOX.h / 2 - 24, -RING - 24);
    var bottom = Math.max(Math.max.apply(null, ys) + BOX.h / 2 + 12, RING + 24);
    var left = -RING - BOX.w / 2 - 12;
    var width = Math.max(right + 12, -left) - left;
    svg.setAttribute("viewBox", [left, top, width, bottom - top].join(" "));
    host.appendChild(svg);
  }

  /**
   * Light walk step `stepIndex` of the loop in `host`: its groups at full strength, the rest
   * dimmed. -1 ends the walk and shows the whole diagram again.
   */
  function lightStep(host, stepIndex) {
    host.walkStep = stepIndex;
    host.classList.toggle("is-walking", stepIndex >= 0);
    Array.prototype.forEach.call(host.querySelectorAll(".loop-step"), function (group) {
      var steps = group.getAttribute("data-steps").split(" ");
      group.classList.toggle("is-lit", steps.indexOf(String(stepIndex)) >= 0);
    });
  }

  /**
   * Move the current screen's loop walk one step in `direction`. Returns false when there is no
   * loop on the screen or no step left that way, which tells the caller to turn the page.
   */
  function walkLoop(direction) {
    var host = screens[current].querySelector("[data-loop]");
    if (!host || host.loopSteps === undefined) return false;
    var target = host.walkStep + direction;
    if (target < -1 || target >= host.loopSteps) return false;
    lightStep(host, target);
    return true;
  }

  /** Show the whole diagram again on `screen`, if it has a loop that is mid-walk. */
  function resetLoop(screen) {
    var host = screen && screen.querySelector("[data-loop]");
    if (host && host.walkStep >= 0) lightStep(host, -1);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* 4. Charts                                                                                */
  /* ---------------------------------------------------------------------------------------- */

  var tip = document.querySelector(".tip");

  /** Show the tooltip for a mark: its first line bold, the second plain. */
  function showTip(event, lines) {
    tip.innerHTML = "";
    var head = document.createElement("b");
    head.textContent = lines[0];
    tip.appendChild(head);
    if (lines[1]) tip.appendChild(document.createTextNode(lines[1]));
    tip.hidden = false;
    // Above and to the right of the pointer, flipped left near the window's right edge.
    var x = event.clientX + 14;
    var y = event.clientY - tip.offsetHeight - 12;
    if (x + tip.offsetWidth > window.innerWidth - 8) x = event.clientX - tip.offsetWidth - 14;
    tip.style.left = x + "px";
    tip.style.top = Math.max(8, y) + "px";
  }

  function hideTip() {
    tip.hidden = true;
  }

  /** An invisible hover target over a mark's whole band, wired to the tooltip. */
  function hitArea(parent, x, y, width, height, lines) {
    var hit = el("rect", { x: x, y: y, width: width, height: height, class: "hit" }, parent);
    hit.addEventListener("pointermove", function (e) { showTip(e, lines); });
    hit.addEventListener("pointerleave", hideTip);
    return hit;
  }

  /** An `<svg>` sized to its host box in pixels; charts are drawn in real pixels, not scaled. */
  function canvas(host) {
    host.innerHTML = "";
    var width = host.clientWidth;
    var height = host.clientHeight;
    var svg = el("svg", { width: width, height: height, viewBox: "0 0 " + width + " " + height }, host);
    return { svg: svg, width: width, height: height };
  }

  /** Horizontal guides with their tick labels, and the baseline, for a vertical value axis. */
  function valueAxis(svg, ticks, scaleY, left, right, format) {
    ticks.forEach(function (t) {
      var y = scaleY(t);
      if (t > 0) el("line", { x1: left, x2: right, y1: y, y2: y, class: "grid-line" }, svg);
      text(svg, left - 10, y + 4, format(t), { class: "tick", "text-anchor": "end" });
    });
    var base = scaleY(0);
    el("line", { x1: left, x2: right, y1: base, y2: base, class: "baseline" }, svg);
  }

  /**
   * Grouped columns. `groups` is a list of {label, bars: [{value, color, text, tip}]}; each
   * group gets an equal band, its bars centred in it side by side with a small gap. `ticks` is
   * the value axis (its last entry is the maximum), `format` writes a tick.
   */
  function drawColumns(host, groups, ticks, format) {
    var c = canvas(host);
    var m = { top: 28, right: 8, bottom: 34, left: 52 };
    var plotW = c.width - m.left - m.right;
    var plotH = c.height - m.top - m.bottom;
    var max = ticks[ticks.length - 1];
    function scaleY(v) { return m.top + plotH - (v / max) * plotH; }
    valueAxis(c.svg, ticks, scaleY, m.left, c.width - m.right, format);

    var band = plotW / groups.length;
    var gap = 6;
    var most = Math.max.apply(null, groups.map(function (g) { return g.bars.length; }));
    // Bars are as wide as the band allows, up to 64px: wide enough to carry their label, never
    // so wide the band loses its air.
    var barW = Math.min(64, (band * 0.72 - gap * (most - 1)) / most);

    groups.forEach(function (group, gi) {
      var center = m.left + band * gi + band / 2;
      var total = group.bars.length * barW + (group.bars.length - 1) * gap;
      group.bars.forEach(function (bar, bi) {
        var x = center - total / 2 + bi * (barW + gap);
        var y = scaleY(bar.value);
        var h = scaleY(0) - y;
        var mark = el("path", { d: columnPath(x, y, barW, h, 5), fill: bar.color, class: "bar" }, c.svg);
        mark.style.transitionDelay = (0.1 + 0.07 * (gi * most + bi)) + "s";
        text(c.svg, x + barW / 2, y - 8, bar.text, { class: "value", "text-anchor": "middle" });
        // Added last, so the transparent target sits above the bar and its label.
        hitArea(c.svg, x - gap / 2, m.top - 20, barW + gap, plotH + 20, bar.tip);
      });
      text(c.svg, center, c.height - 10, group.label, { class: "axis-label", "text-anchor": "middle" });
    });
  }

  /**
   * Horizontal stacked bars, one row per item: [{label, segments: [{value, color, name}], text}].
   * A 2px gap in the card colour separates the segments of a row.
   */
  function drawStacked(host, rows) {
    var c = canvas(host);
    var m = { top: 8, right: 170, bottom: 30, left: 56 };
    var plotW = c.width - m.left - m.right;
    var plotH = c.height - m.top - m.bottom;
    var totals = rows.map(function (r) { return r.segments.reduce(function (s, g) { return s + g.value; }, 0); });
    var ticks = niceTicks(Math.max.apply(null, totals), 4);
    var max = ticks[ticks.length - 1];
    function scaleX(v) { return m.left + (v / max) * plotW; }

    ticks.forEach(function (t) {
      var x = scaleX(t);
      if (t > 0) el("line", { x1: x, x2: x, y1: m.top, y2: m.top + plotH, class: "grid-line" }, c.svg);
      text(c.svg, x, c.height - 8, kilo(t), { class: "tick", "text-anchor": "middle" });
    });
    el("line", { x1: m.left, x2: m.left + plotW, y1: m.top + plotH, y2: m.top + plotH, class: "baseline" }, c.svg);

    var band = plotH / rows.length;
    var thickness = Math.min(44, band * 0.55);
    rows.forEach(function (row, ri) {
      var y = m.top + band * ri + (band - thickness) / 2;
      var x = m.left;
      row.segments.forEach(function (seg, si) {
        var w = Math.max(0, scaleX(seg.value) - m.left - (si < row.segments.length - 1 ? 2 : 0));
        var isLast = si === row.segments.length - 1;
        var mark = el("path", {
          d: isLast ? hbarPath(x, y, w, thickness, 5) : "M" + x + "," + y + "h" + w + "v" + thickness + "h" + -w + "Z",
          fill: seg.color, class: "bar horizontal",
        }, c.svg);
        mark.style.transitionDelay = (0.1 + 0.15 * ri) + "s";
        x += w + 2;
      });
      hitArea(c.svg, m.left, m.top + band * ri, plotW, band, row.tip);
      // The row that holds most of the total gets a bolder label, so the eye lands on it first.
      var isKey = totals[ri] === Math.max.apply(null, totals);
      text(c.svg, m.left - 12, y + thickness / 2 + 5, row.label, { class: isKey ? "axis-label is-key" : "axis-label", "text-anchor": "end" });
      text(c.svg, scaleX(totals[ri]) + 12, y + thickness / 2 + 5, row.text, { class: "value" });
    });
  }

  /** A legend of colour swatches and names, in HTML under its chart. */
  function legend(host, entries) {
    host.innerHTML = "";
    entries.forEach(function (entry) {
      var item = document.createElement("span");
      var swatch = document.createElement("i");
      swatch.style.background = entry.color;
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(entry.name));
      host.appendChild(item);
    });
  }

  /* ---------------------------------------------------------------------------------------- */
  /* 5. Data                                                                                  */
  /* ---------------------------------------------------------------------------------------- */

  /** Each chart, keyed by the `data-chart` name its box carries in the page. */
  var CHARTS = {
    "model-pass": function (host, data) {
      drawColumns(host, data.models.map(function (m, i) {
        return { label: m.name, bars: [{
          value: m.passed / m.tasks, color: SERIES[i], text: m.passed + "/" + m.tasks,
          tip: [m.name, m.passed + " of " + m.tasks + " tasks passed (" + percent(m.passed / m.tasks) + ")"],
        }] };
      }), [0, 0.2, 0.4, 0.6, 0.8, 1], percent);
    },
    "model-tokens": function (host, data) {
      var ticks = niceTicks(Math.max.apply(null, data.models.map(function (m) { return m.tokens; })) * 1.1, 4);
      drawColumns(host, data.models.map(function (m, i) {
        return { label: m.name, bars: [{
          value: m.tokens, color: SERIES[i], text: kilo(m.tokens),
          tip: [m.name, grouped(m.tokens) + " tokens per task, " + m.steps + " steps"],
        }] };
      }), ticks, kilo);
    },
    "model-seconds": function (host, data) {
      var ticks = niceTicks(Math.max.apply(null, data.models.map(function (m) { return m.seconds; })) * 1.1, 4);
      drawColumns(host, data.models.map(function (m, i) {
        return { label: m.name, bars: [{
          value: m.seconds, color: SERIES[i], text: Math.round(m.seconds) + "s",
          tip: [m.name, Math.round(m.seconds) + " seconds per task, on average"],
        }] };
      }), ticks, function (t) { return t + "s"; });
    },
    categories: function (host, data) {
      var series = data.categories.series;
      drawColumns(host, data.categories.categories.map(function (cat) {
        var bars = [];
        series.forEach(function (s, si) {
          var v = s.values[cat];
          if (!v) return;
          bars.push({
            value: v[0] / v[1], color: SERIES[si], text: v[0] + "/" + v[1],
            tip: [s.name + " · " + cat, v[0] + " of " + v[1] + " passed"],
          });
        });
        return { label: cat, bars: bars };
      }), [0, 0.2, 0.4, 0.6, 0.8, 1], percent);
    },
    "ablation-pass": function (host, data) {
      drawColumns(host, data.ablation.arms.map(function (a, i) {
        return { label: a.mode, bars: [{
          value: a.passed / a.tasks, color: SERIES[i], text: a.passed + "/" + a.tasks,
          tip: ["retrieval " + a.mode, a.passed + " of " + a.tasks + " passed"],
        }] };
      }), [0, 0.2, 0.4, 0.6, 0.8, 1], percent);
    },
    "ablation-tokens": function (host, data) {
      var ticks = niceTicks(Math.max.apply(null, data.ablation.arms.map(function (a) { return a.tokens; })) * 1.1, 4);
      drawColumns(host, data.ablation.arms.map(function (a, i) {
        return { label: a.mode, bars: [{
          value: a.tokens, color: SERIES[i], text: kilo(a.tokens),
          tip: ["retrieval " + a.mode, grouped(a.tokens) + " tokens per task"],
        }] };
      }), ticks, kilo);
    },
    nodes: function (host, data) {
      drawStacked(host, data.nodes.nodes.map(function (n) {
        return {
          label: n.node,
          segments: [{ value: n.input, color: SERIES[0] }, { value: n.output, color: SERIES[2] }],
          text: grouped(n.total) + "  (" + n.calls.toFixed(1) + " calls)",
          tip: [n.node, grouped(n.input) + " in · " + grouped(n.output) + " out · " + n.calls.toFixed(1) + " calls"],
        };
      }));
    },
  };

  /** Legends for the charts with more than one series. */
  function drawLegends(data) {
    var legends = {
      categories: data.categories.series.map(function (s, i) { return { name: s.name, color: SERIES[i] }; }),
      nodes: [{ name: "input tokens", color: SERIES[0] }, { name: "output tokens", color: SERIES[2] }],
    };
    Object.keys(legends).forEach(function (key) {
      var host = document.querySelector('[data-legend="' + key + '"]');
      if (host) legend(host, legends[key]);
    });
  }

  /** Swap each quoted number for the value computed from the results. */
  function fillStats(data) {
    var nodes = document.querySelectorAll("[data-stat]");
    for (var i = 0; i < nodes.length; i++) {
      var value = data.stats[nodes[i].getAttribute("data-stat")];
      if (value !== undefined) nodes[i].textContent = value;
    }
    var caption = document.querySelector('[data-caption="nodes"]');
    if (caption) {
      caption.textContent = "Tokens per run by graph node, averaged over " + data.nodes.runs +
        " runs of " + data.nodes.model + ".";
    }
  }

  /**
   * Fill each score card's meter and percentage from its score text ("11 / 12"), so the bar
   * and the number can never disagree. Run once at start (with the numbers in the page) and
   * again after the results load.
   */
  function fillMeters() {
    Array.prototype.forEach.call(document.querySelectorAll(".result"), function (card) {
      var parts = card.querySelector(".result-value").textContent.split("/");
      var share = Number(parts[0]) / Number(parts[1]);
      if (!isFinite(share)) return;
      card.querySelector(".meter span").style.width = (share * 100).toFixed(1) + "%";
      card.querySelector(".result-pct").textContent = percent(share);
      card.classList.toggle("is-full", share === 1);
    });
  }

  /** Draw every chart, then redraw one whenever its box changes size. */
  function drawCharts(data) {
    var hosts = document.querySelectorAll("[data-chart]");
    Array.prototype.forEach.call(hosts, function (host) {
      var draw = CHARTS[host.getAttribute("data-chart")];
      if (!draw) return;
      draw(host, data);
      host.setAttribute("role", "img");
      host.setAttribute("aria-label", host.closest(".screen").getAttribute("data-title") + ". Hover a bar for its numbers.");
      if ("ResizeObserver" in window) {
        var lastWidth = host.clientWidth;
        var lastHeight = host.clientHeight;
        new ResizeObserver(function () {
          if (host.clientWidth === lastWidth && host.clientHeight === lastHeight) return;
          lastWidth = host.clientWidth;
          lastHeight = host.clientHeight;
          draw(host, data);
        }).observe(host);
      }
    });
  }

  function loadData() {
    fetch("data.json")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        fillStats(data);
        fillMeters();
        drawCharts(data);
        drawLegends(data);
      })
      .catch(function () {
        // Opened straight from disk, the browser refuses the fetch: say how to serve it.
        Array.prototype.forEach.call(document.querySelectorAll("[data-chart]"), function (host) {
          host.innerHTML = '<p class="source">The chart data could not be loaded. Serve the folder, ' +
            "for example with <code>python -m http.server</code>, rather than opening the file.</p>";
        });
      });
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Start                                                                                    */
  /* ---------------------------------------------------------------------------------------- */

  // A reload would otherwise restore the old pixel offset, which can land between two screens;
  // instead, open on the screen the address names, or the first one. The jump is "instant":
  // left to the stylesheet's smooth scrolling, it would glide past the first screens, which
  // would briefly count as current, so an early key press would start from screen 1.
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
  var start = document.getElementById(location.hash.slice(1));
  if (start && start.classList.contains("screen")) {
    current = screens.indexOf(start);
    start.scrollIntoView({ behavior: "instant" });
  }

  buildRail();
  fillMeters();
  watchScreens();
  document.addEventListener("keydown", onKey);
  Array.prototype.forEach.call(document.querySelectorAll("[data-loop]"), function (host) {
    drawLoop(host, LOOPS[host.getAttribute("data-loop")]);
  });
  loadData();
})();
