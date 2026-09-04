/* Deckster — control surface client.
 * Vanilla ES5-ish JS for old Android WebView. Wires the design UI to the agent's
 * WebSocket protocol (hello/pair/subscribe -> snapshot; set_volume/set_mute/
 * set_default_output|input/macro; state broadcasts). The SVG jog dial is ported
 * from the design's VolumeDial (its variable-rate math is load-bearing). */
(function () {
  "use strict";

  // ---------------------------------------------------------------- helpers
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls) { var e = document.createElement(tag); if (cls) e.className = cls; return e; }
  function hexA(h, a) { h = h.replace("#", ""); return "rgba(" + parseInt(h.substr(0, 2), 16) + "," + parseInt(h.substr(2, 2), 16) + "," + parseInt(h.substr(4, 2), 16) + "," + a + ")"; }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function buzz(p) { if (navigator.vibrate) { try { navigator.vibrate(p); } catch (e) {} } }
  // A tight haptic vocabulary — short, distinct cues rather than long buzzes, so
  // each gesture feels precise. Values are ms (patterns are [gap,on,...]).
  var HAPTIC = {
    tick: 7,          // crossing a 10% detent on the dial
    edge: [0, 20],    // hitting 0% or 100% (a firm wall)
    step: 5,          // a single +/- nudge
    mute: [0, 16],    // mute / unmute toggle
    tap: 9,           // selecting a tile, device, or mode
    hold: [0, 28],    // a long-press was recognised
  };

  // Known app accents/badges from the design; unknown apps get a palette color.
  var KNOWN = {
    discord: { a: "#7c8cff", b: "D" }, chrome: { a: "#ffb84d", b: "C" },
    spotify: { a: "#4ddb7f", b: "S" }, firefox: { a: "#ff9640", b: "F" },
    game: { a: "#ff6b5e", b: "G" }, system: { a: "#56c2ff", b: "⚙" },
    "system sounds": { a: "#56c2ff", b: "⚙" }, msedge: { a: "#56c2ff", b: "E" },
    vlc: { a: "#ff8c3b", b: "V" }, obs: { a: "#9aa0ff", b: "O" },
  };
  var PALETTE = ["#7c8cff", "#ffb84d", "#4ddb7f", "#ff6b5e", "#56c2ff", "#c58cff", "#ff8fb0", "#5ad1c8"];
  function accentFor(id, name) {
    var key = (name || id || "").toLowerCase().replace(/\.exe$/, "");
    if (KNOWN[key]) return KNOWN[key];
    var short = key.indexOf("game") >= 0 ? KNOWN.game : null;
    if (short) return short;
    var h = 0; for (var i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) & 0xffff;
    return { a: PALETTE[h % PALETTE.length], b: (name || id || "?").charAt(0).toUpperCase() };
  }
  function prettyName(name) {
    var n = (name || "").replace(/\.exe$/i, "");
    return n.charAt(0).toUpperCase() + n.slice(1);
  }
  // Short, fixed-length label for the compact name under the dial's app icon.
  function shortName(s) { s = s || ""; return s.length > 9 ? s.slice(0, 9) + "…" : s; }
  // Paint a badge node with the app's real exe icon when we have one, else the
  // colored letter fallback. `item` carries {iconKey, accent, badge}.
  function paintBadge(node, item) {
    if (item.iconKey) {
      node.textContent = "";
      node.classList.add("has-icon");
      // Set size/position/repeat EXPLICITLY, not just via the .has-icon CSS: for a
      // reused badge (the top bar) the previous letter state set the `background`
      // shorthand, which had reset background-size to auto and position to 0 0 as
      // inline styles — those would override the class and render the 128px icon at
      // natural size in a corner. Transparent bg so transparent icon corners don't
      // show a grey backdrop.
      node.style.background = "transparent";
      node.style.backgroundImage = 'url("/icon/' + item.iconKey + '")';
      node.style.backgroundSize = "contain";
      node.style.backgroundPosition = "center";
      node.style.backgroundRepeat = "no-repeat";
    } else {
      node.classList.remove("has-icon");
      node.style.backgroundImage = "";
      node.textContent = item.badge;
      node.style.background = item.accent;
    }
  }

  // ---------------------------------------------------------------- icons
  function micSVG(color, muted, size) {
    var s = size || 28;
    var slash = muted ? '<line x1="4" y1="3.5" x2="20" y2="20.5" stroke="' + color + '" stroke-width="2.4" stroke-linecap="round"></line>' : "";
    return '<svg viewBox="0 0 24 24" width="' + s + '" height="' + s + '"><rect x="9" y="2.5" width="6" height="11" rx="3" fill="' + color + '"></rect><path d="M5.5 11 a6.5 6.5 0 0 0 13 0" fill="none" stroke="' + color + '" stroke-width="2"></path><line x1="12" y1="17.5" x2="12" y2="21" stroke="' + color + '" stroke-width="2"></line><line x1="8" y1="21" x2="16" y2="21" stroke="' + color + '" stroke-width="2"></line>' + slash + "</svg>";
  }
  function spkSVG(color, muted, size) {
    var s = size || 28;
    var extra = muted
      ? '<line x1="15" y1="8" x2="21" y2="16" stroke="' + color + '" stroke-width="2"></line><line x1="21" y1="8" x2="15" y2="16" stroke="' + color + '" stroke-width="2"></line>'
      : '<path d="M15 8.5 Q17.5 12 15 15.5" fill="none" stroke="' + color + '" stroke-width="2"></path><path d="M17.5 6.5 Q21.5 12 17.5 17.5" fill="none" stroke="' + color + '" stroke-width="2"></path>';
    return '<svg viewBox="0 0 24 24" width="' + s + '" height="' + s + '"><polygon points="3,9 7,9 12,4.5 12,19.5 7,15 3,15" fill="' + color + '"></polygon>' + extra + "</svg>";
  }
  function checkSVG() { return '<svg viewBox="0 0 24 24" width="12" height="12"><path d="M5 13 l4 4 l10 -11" fill="none" stroke="#0c0d10" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"></path></svg>'; }
  function errSVG() { return '<svg viewBox="0 0 24 24" width="20" height="20"><circle cx="12" cy="12" r="9" fill="none" stroke="#e05a5a" stroke-width="2"></circle><line x1="12" y1="7" x2="12" y2="13" stroke="#e05a5a" stroke-width="2" stroke-linecap="round"></line><circle cx="12" cy="16.5" r="1.2" fill="#e05a5a"></circle></svg>'; }
  // ---- media transport icons ----
  function playSVG(c, s) { s = s || 26; return '<svg viewBox="0 0 24 24" width="' + s + '" height="' + s + '"><polygon points="7,4 20,12 7,20" fill="' + c + '"></polygon></svg>'; }
  function pauseSVG(c, s) { s = s || 26; return '<svg viewBox="0 0 24 24" width="' + s + '" height="' + s + '"><rect x="6" y="4.5" width="4.2" height="15" rx="1.3" fill="' + c + '"></rect><rect x="13.8" y="4.5" width="4.2" height="15" rx="1.3" fill="' + c + '"></rect></svg>'; }
  function prevSVG(c, s) { s = s || 22; return '<svg viewBox="0 0 24 24" width="' + s + '" height="' + s + '"><rect x="4" y="5" width="2.6" height="14" rx="1.1" fill="' + c + '"></rect><polygon points="20,5 20,19 9,12" fill="' + c + '"></polygon></svg>'; }
  function nextSVG(c, s) { s = s || 22; return '<svg viewBox="0 0 24 24" width="' + s + '" height="' + s + '"><polygon points="4,5 4,19 15,12" fill="' + c + '"></polygon><rect x="17.4" y="5" width="2.6" height="14" rx="1.1" fill="' + c + '"></rect></svg>'; }
  function noteSVG(c, s) { s = s || 26; return '<svg viewBox="0 0 24 24" width="' + s + '" height="' + s + '"><path d="M9 17.5 V6 l10 -2 v10.5" fill="none" stroke="' + c + '" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path><ellipse cx="6.5" cy="17.5" rx="2.6" ry="2.2" fill="' + c + '"></ellipse><ellipse cx="16.5" cy="15.5" rx="2.6" ry="2.2" fill="' + c + '"></ellipse></svg>'; }

  // ================================================================ DIAL
  // Ported from the design's VolumeDial. Renders an <svg>; drag math uses screen
  // coordinates around the pivot. orient 'left' (landscape) or 'up' (portrait).
  function Dial(host, cb) {
    this.host = host; this.cb = cb; // cb: {onChange, onToggleMute}
    this.orientation = "left";
    this.target = { value: 0, muted: false, accent: "#4ddb7f", source: "", mode: "jog", interactive: true };
    this.val = 0; this.muted = false; this.dragging = false; this.lastAngle = 0;
    this._lastTouch = 0;   // suppresses the synthesized mousedown that follows a touch
    this.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.host.appendChild(this.svg);
    this._bind();
  }
  Dial.prototype.D2R = Math.PI / 180; Dial.prototype.R2D = 180 / Math.PI;
  Dial.prototype.R_FILL = 188; Dial.prototype.HALF = 58;
  Dial.prototype.cfg = function () {
    if (this.orientation === "left") return { vbw: 300, vbh: 370, px: 264, py: 185, vsign: -1,
      numX: 140, numY: 169, numSize: 46, srcX: 140, srcY: 196, tagY: 210, muteX: 232, muteY: 185, muteR: 34,
      nudges: [{ tx: 232, ty: 96, plus: true }, { tx: 232, ty: 274, plus: false }], linX: 78, linTop: 54, linBot: 320 };
    return { vbw: 340, vbh: 300, px: 170, py: 286, vsign: 1,
      numX: 170, numY: 150, numSize: 58, srcX: 170, srcY: 177, tagY: 185, muteX: 170, muteY: 244, muteR: 37,
      nudges: [{ tx: 42, ty: 270, plus: false }, { tx: 298, ty: 270, plus: true }], linX: 96, linTop: 44, linBot: 250 };
  };
  Dial.prototype.linear = function () { return this.target.mode === "linear"; };
  Dial.prototype.readOnly = function () { return this.target.interactive === false; };
  Dial.prototype.setOrient = function (o) { if (o !== this.orientation) { this.orientation = o; this.render(); } };
  Dial.prototype.setTarget = function (t) {
    // Don't let a background reconnect/pong (setConnection -> setTarget) reset the
    // value out from under an in-progress drag; syncValue already guards this path.
    this.target = t;
    if (!this.dragging) { this.val = clamp(+t.value || 0, 0, 100); this.muted = !!t.muted; }
    this.render();
  };
  Dial.prototype.syncValue = function (value, muted) {
    if (this.dragging) return;            // don't fight the user's drag
    this.val = clamp(+value || 0, 0, 100); this.muted = !!muted; this.render();
  };
  // geometry
  Dial.prototype.aRad = function (v) { var deg = this.orientation === "left" ? (this.HALF - v / 100 * 2 * this.HALF) : (-this.HALF + v / 100 * 2 * this.HALF); return deg * this.D2R; };
  Dial.prototype.dirVec = function (v) { var a = this.aRad(v); return this.orientation === "left" ? [-Math.cos(a), -Math.sin(a)] : [Math.sin(a), -Math.cos(a)]; };
  Dial.prototype.pos = function (v, r) { var c = this.cfg(), d = this.dirVec(v); return { x: c.px + r * d[0], y: c.py + r * d[1] }; };
  Dial.prototype.linPos = function (v) { var c = this.cfg(); return { x: c.linX, y: c.linBot + (v / 100) * (c.linTop - c.linBot) }; };
  Dial.prototype.geom = function () { var c = this.cfg(), r = this.svg.getBoundingClientRect(), sx = r.width / c.vbw, sy = r.height / c.vbh;
    return { rect: r, sx: sx, sy: sy, px: r.left + c.px * sx, py: r.top + c.py * sy, rr: this.R_FILL * (sx + sy) / 2 }; };
  Dial.prototype.angleAt = function (x, y) { var g = this.geom(), fx = x - g.px, fy = y - g.py; return this.orientation === "left" ? Math.atan2(-fy, -fx) : Math.atan2(fx, -fy); };
  Dial.prototype.fracAt = function (x, y) { var g = this.geom(); return Math.hypot(x - g.px, y - g.py) / g.rr; };
  Dial.prototype.valFromLinear = function (y) { var c = this.cfg(), g = this.geom(), yb = g.rect.top + c.linBot * g.sy, yt = g.rect.top + c.linTop * g.sy; return (yb - y) / (yb - yt) * 100; };
  Dial.prototype.commit = function (nv) {
    var prev = this.val; nv = clamp(nv, 0, 100);
    var pb = Math.round(prev / 10), nb = Math.round(nv / 10);
    if (pb !== nb) buzz(nb === 0 || nb === 10 ? HAPTIC.edge : HAPTIC.tick);  // nb is tenths: 0=0%,10=100%
    this.val = nv; this.render();
    if (this.cb.onChange) this.cb.onChange(nv);
  };
  Dial.prototype.nudge = function (d) {
    if (this.readOnly()) return; var prev = this.val, v = clamp(prev + d, 0, 100);
    if (v !== prev) buzz(v === 0 || v === 100 ? HAPTIC.edge : HAPTIC.step);
    this.val = v; this.render(); if (this.cb.onChange) this.cb.onChange(v);
  };
  Dial.prototype._bind = function () {
    var self = this;
    var down = function (e) {
      // A tap fires touchstart AND a synthesized mousedown; ignore the latter so
      // the mute button (and +/- nudges) toggle exactly once, not twice.
      if (e.type === "mousedown") { if (Date.now() - self._lastTouch < 700) return; }
      else self._lastTouch = Date.now();
      var act = e.target.closest ? e.target.closest("[data-act]") : null;
      if (act) {
        var a = act.getAttribute("data-act");
        if (a === "mute") { if (!self.readOnly() && self.cb.onToggleMute) self.cb.onToggleMute(); }
        else if (a === "inc") self.nudge(1);
        else if (a === "dec") self.nudge(-1);
        e.preventDefault(); e.stopPropagation(); return;
      }
      if (self.readOnly()) return;
      if (e.cancelable) e.preventDefault();   // claim the gesture so touchmove always fires
      var t = (e.touches && e.touches[0]) || e;
      self.dragging = true;
      if (self.linear()) self.commit(self.valFromLinear(t.clientY));
      else { self._px = t.clientX; self._py = t.clientY; }   // finger baseline for tangential drag
      self.render();
      var move = function (ev) { if (ev.cancelable) ev.preventDefault(); var p = (ev.touches && ev.touches[0]) || ev; self._move(p.clientX, p.clientY); };
      var up = function () {
        document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up);
        document.removeEventListener("touchmove", move); document.removeEventListener("touchend", up);
        self.dragging = false; self.render(); if (self.cb.onCommit) self.cb.onCommit(self.val);
      };
      document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
      document.addEventListener("touchmove", move, { passive: false }); document.addEventListener("touchend", up);
    };
    this.svg.addEventListener("mousedown", down);
    this.svg.addEventListener("touchstart", down, { passive: false });
  };
  Dial.prototype._move = function (x, y) {
    if (this.linear()) { this.commit(this.valFromLinear(y)); return; }
    var g = this.geom();
    if (this._px == null) { this._px = x; this._py = y; return; }
    var mx = x - this._px, my = y - this._py; this._px = x; this._py = y;
    // Track the finger along the dial's straight 0%->100% CHORD: vertical in
    // landscape ('left', 0 at top / 100 at bottom), horizontal in portrait ('up').
    // The previous model projected onto the arc *tangent*, which cancelled to zero
    // for a cross-axis swipe near 50% (dial showed "GAIN" but nothing moved) and
    // lost ~half its sensitivity at the extremes. A chord drag has constant
    // sensitivity, no dead zone, and no sign flip. Following the curve still works
    // because the along-axis component of that motion still drives it.
    var chordPx = 2 * this.R_FILL * Math.sin(this.HALF * this.D2R) *
                  (this.orientation === "left" ? g.sy : g.sx);   // 0->100 span, screen px
    var travel = (this.orientation === "left") ? my : mx;         // down / right = louder
    this.commit(this.val + travel / (chordPx || 1) * 100 * 1.1);  // 1.1 = slight feel gain
  };
  Dial.prototype.render = function () {
    var c = this.cfg(), lin = this.linear(), muted = this.muted, dragging = this.dragging;
    var disc = this.target.state === "disconnected";
    var v = clamp(this.val, 0, 100), accent = this.target.accent || "#4ddb7f";
    var nearest = Math.round(v / 10) * 10, ticks = "", knob = "";
    var trackPath = "", fillPath = "", knobX = -99, knobY = -99, knobR = 0;
    if (lin) {
      var A = this.linPos(0), B = this.linPos(100), sv = this.linPos(v);
      trackPath = "M " + A.x + " " + A.y + " L " + B.x + " " + B.y;
      fillPath = (disc || muted || v <= 0) ? "" : "M " + A.x + " " + A.y + " L " + sv.x + " " + sv.y;
      for (var i = 0; i <= 10; i++) { var tv = i * 10, pp = this.linPos(tv), lit = (v >= tv) && !disc && !muted;
        ticks += '<circle cx="' + (pp.x + 22) + '" cy="' + pp.y + '" r="3" fill="' + (lit ? accent : "#3a3d45") + '"></circle>'; }
      if (!disc && !muted) { knobX = sv.x; knobY = sv.y; knobR = 17; }
    } else {
      var s0 = this.pos(0, this.R_FILL), s100 = this.pos(100, this.R_FILL), sv2 = this.pos(v, this.R_FILL);
      var sweep = this.orientation === "left" ? 0 : 1;
      trackPath = "M " + s0.x + " " + s0.y + " A " + this.R_FILL + " " + this.R_FILL + " 0 0 " + sweep + " " + s100.x + " " + s100.y;
      fillPath = (disc || muted || v <= 0) ? "" : "M " + s0.x + " " + s0.y + " A " + this.R_FILL + " " + this.R_FILL + " 0 0 " + sweep + " " + sv2.x + " " + sv2.y;
      for (var j = 0; j <= 10; j++) { var tv2 = j * 10, dp = this.pos(tv2, this.R_FILL + 20);
        var isCur = dragging && tv2 === nearest, lit2 = (v >= tv2) && !disc && !muted;
        ticks += '<circle cx="' + dp.x + '" cy="' + dp.y + '" r="' + (isCur ? 4 : 3) + '" fill="' + (isCur ? "#ffffff" : (lit2 ? accent : "#3a3d45")) + '"></circle>'; }
      if (!disc && !muted) { knobX = sv2.x; knobY = sv2.y; knobR = 17; }
    }
    var knobStroke = dragging ? "#ffffff" : (disc || muted ? "#33363e" : accent);
    if (knobR > 0) knob = '<circle cx="' + knobX + '" cy="' + knobY + '" r="' + knobR + '" fill="#0e0f13" stroke="' + knobStroke + '" stroke-width="4"></circle><circle cx="' + knobX + '" cy="' + knobY + '" r="' + Math.max(0, knobR - 8) + '" fill="' + accent + '"></circle>';

    var tag = disc ? "OFFLINE" : muted ? "MUTED" : dragging ? (lin ? "SET" : "GAIN") : "";
    var tagW = tag.length * 8 + 20;
    var tagBg = dragging && !muted && !disc ? "rgba(255,255,255,0.14)" : (disc || muted ? "#2a1416" : "rgba(255,255,255,0.06)");
    var tagFg = dragging && !muted && !disc ? "#ffffff" : (disc || muted ? "#e07a7a" : "#c9cdd6");
    var tagSvg = tag ? '<rect x="' + (c.srcX - tagW / 2) + '" y="' + c.tagY + '" width="' + tagW + '" height="20" rx="10" fill="' + tagBg + '"></rect><text x="' + c.srcX + '" y="' + (c.tagY + 14) + '" text-anchor="middle" font-size="10" font-weight="800" fill="' + tagFg + '" letter-spacing="1">' + tag + "</text>" : "";

    var muteBg = muted ? "#3a1d20" : "#101116", muteBorder = muted ? "#5a2b2f" : "#2c2f37";
    var muteIcon = disc ? "#4a4d55" : (muted ? "#f2b5b5" : accent);
    var iconInner = "";
    if (!lin) {
      iconInner += '<polygon points="-14,-6 -6,-6 2,-13 2,13 -6,6 -14,6" fill="' + muteIcon + '"></polygon>';
      if (!muted && !disc) iconInner += '<path d="M7 -9 Q13 0 7 9" fill="none" stroke="' + muteIcon + '" stroke-width="2.6" stroke-linecap="round"></path><path d="M12 -14 Q21 0 12 14" fill="none" stroke="' + muteIcon + '" stroke-width="2.6" stroke-linecap="round"></path>';
      if (muted) iconInner += '<line x1="-20" y1="-14" x2="22" y2="14" stroke="' + muteIcon + '" stroke-width="3.4" stroke-linecap="round"></line>';
    } else {
      iconInner += '<rect x="-5" y="-14" width="10" height="17" rx="5" fill="' + muteIcon + '"></rect><path d="M-9 -1 a9 9 0 0 0 18 0" fill="none" stroke="' + muteIcon + '" stroke-width="2.6" stroke-linecap="round"></path><line x1="0" y1="8" x2="0" y2="15" stroke="' + muteIcon + '" stroke-width="2.6" stroke-linecap="round"></line><line x1="-7" y1="15" x2="7" y2="15" stroke="' + muteIcon + '" stroke-width="2.6" stroke-linecap="round"></line>';
      if (muted) iconInner += '<line x1="-19" y1="-15" x2="19" y2="15" stroke="' + muteIcon + '" stroke-width="3.2" stroke-linecap="round"></line>';
    }
    var muteBtn = '<g data-act="mute" style="cursor:pointer;"><circle cx="' + c.muteX + '" cy="' + c.muteY + '" r="' + c.muteR + '" fill="' + muteBg + '" stroke="' + muteBorder + '" stroke-width="1.5"></circle><g transform="translate(' + c.muteX + ' ' + c.muteY + ')">' + iconInner + "</g></g>";

    var nudge = "";
    if (!this.readOnly() && !disc) {
      for (var k = 0; k < c.nudges.length; k++) { var n = c.nudges[k];
        nudge += '<g data-act="' + (n.plus ? "inc" : "dec") + '" transform="translate(' + n.tx + " " + n.ty + ')" style="cursor:pointer;"><circle cx="0" cy="0" r="22" fill="#15171d" stroke="#2c2f37" stroke-width="1.5"></circle><line x1="-9" y1="0" x2="9" y2="0" stroke="#e8eaef" stroke-width="3.4" stroke-linecap="round"></line>' + (n.plus ? '<line x1="0" y1="-9" x2="0" y2="9" stroke="#e8eaef" stroke-width="3.4" stroke-linecap="round"></line>' : "") + "</g>"; }
    }

    var numColor = (disc || muted) ? "#71757e" : "#f4f5f7";
    var display = disc ? "—" : (Math.round(v) + "%");
    var fam = "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif";
    this.svg.setAttribute("viewBox", "0 0 " + c.vbw + " " + c.vbh);
    this.svg.setAttribute("width", "100%"); this.svg.setAttribute("height", "100%");
    this.svg.style.cursor = this.readOnly() ? "default" : "grab"; this.svg.style.touchAction = "none";
    this.svg.innerHTML =
      '<path d="' + trackPath + '" fill="none" stroke="#191b21" stroke-width="9" stroke-linecap="round"></path>' +
      '<path d="' + fillPath + '" fill="none" stroke="' + ((disc || muted) ? "#33363e" : accent) + '" stroke-width="9" stroke-linecap="round"></path>' +
      ticks + knob +
      '<text x="' + c.numX + '" y="' + c.numY + '" text-anchor="middle" font-family="' + fam + '" font-size="' + c.numSize + '" font-weight="800" fill="' + numColor + '" letter-spacing="-1">' + display + "</text>" +
      tagSvg + muteBtn + nudge;
  };

  // ================================================================ APP
  var model = {
    connection: "connecting", latency: null, retry: 0,
    apps: [], system: { spkLevel: 0.7, spkMuted: false, micLevel: 0.65, micMuted: false },
    outputs: [], inputs: [], macros: [],
    meters: { output: 0, input: 0 },       // live signal peaks (0..1) for default devices
    appInputBindings: {},                    // appId -> {keys, label}
    inputMuted: {},                          // optimistic per-app mic-mute toggle (OS can't read it)
    media: [],                               // now-playing SMTC sessions
    mediaOpen: false,                        // media sheet visible
    paired: false,                           // false until the phone is paired (gates media)
    selectedId: null, dialMode: "app", page: 0,
  };
  var ws = null, reconnectDelay = 500, pingTimer = null, pingSentAt = 0;
  // QR pairing: the scanned URL carries ?pair=CODE, so we auto-pair on connect
  // instead of making the user type it. Falls back to the keypad if it fails.
  var urlPairCode = (function () { var m = location.search.match(/[?&]pair=([^&]+)/); return m ? decodeURIComponent(m[1]) : null; })();
  var urlPairTried = false;
  function stripPairParam() { if (window.history && history.replaceState) { try { history.replaceState(null, "", location.pathname); } catch (e) {} } }

  // ---- WebSocket lifecycle (token pairing + reconnect) ----
  function deviceId() { var id = localStorage.getItem("sc_device_id"); if (!id) { id = "dev-" + Math.random().toString(36).slice(2) + Date.now().toString(36); localStorage.setItem("sc_device_id", id); } return id; }
  function getToken() { return localStorage.getItem("sc_token") || ""; }
  function setToken(t) { localStorage.setItem("sc_token", t); }
  function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
  function wsUrl() { return (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host + "/ws"; }

  function connect() {
    setConnection("connecting");
    ws = new WebSocket(wsUrl());
    ws.onopen = function () {
      reconnectDelay = 500;
      send({ t: "hello", token: getToken() });
      pingTimer = setInterval(function () { pingSentAt = Date.now(); send({ t: "ping" }); }, 5000);
    };
    ws.onmessage = function (ev) { var m; try { m = JSON.parse(ev.data); } catch (e) { return; } handle(m); };
    ws.onclose = function () {
      if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
      model.retry++; setConnection(model.retry > 3 ? "disconnected" : "reconnecting");
      setTimeout(connect, reconnectDelay); reconnectDelay = Math.min(reconnectDelay * 2, 8000);
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }
  function ensureConnected() { if (ws && ws.readyState === 1) send({ t: "subscribe" }); else if (!ws || ws.readyState === 3) { reconnectDelay = 500; connect(); } }

  function handle(m) {
    switch (m.t) {
      case "pong": if (pingSentAt) model.latency = Date.now() - pingSentAt; setConnection("connected"); return;
      case "need_pair":
        model.paired = false; updateChrome();
        // Auto-pair from the scanned QR code; otherwise show the keypad.
        if (urlPairCode && !urlPairTried) { urlPairTried = true; send({ t: "pair", code: urlPairCode, device: { id: deviceId(), name: "Phone" } }); }
        else showPair(true);
        return;
      case "pair_ok": setToken(m.token); stripPairParam(); stopScan(); showPair(false); send({ t: "subscribe" }); return;
      case "pair_fail": urlPairCode = null; stripPairParam(); pairError(m.reason); return;
      case "snapshot": model.paired = true; updateChrome(); showPair(false); model.retry = 0; setConnection("connected"); ingestSnapshot(m); renderAll(); return;
      case "state": applyState(m); return;
      case "media": model.media = m.media || []; renderMedia(); return;
      case "app_input_ok": return;   // keystroke sent; optimistic toggle already applied
      case "macro_ok": return;
      case "error": showToast("Command failed", m.msg || m.code); return;
    }
  }

  // ---- snapshot -> model ----
  function ingestSnapshot(m) {
    var sessions = m.sessions || [];
    model.apps = sessions.map(function (s) {
      var ac = accentFor(s.id, s.appLabel || s.label || s.id);
      return { id: s.id, name: prettyName(s.appLabel || s.label || s.id), badge: ac.b, accent: ac.a,
        iconKey: s.iconKey || null,
        level: Math.round((s.level != null ? s.level : 0) * 100), muted: !!s.muted, active: s.active !== false };
    });
    applySavedOrder();   // honour the user's per-device tile arrangement
    var d = m.devices || {};
    model.system.spkLevel = (d.speakerMaster || {}).level != null ? d.speakerMaster.level : model.system.spkLevel;
    model.system.spkMuted = !!(d.speakerMaster || {}).muted;
    model.system.micLevel = (d.micMaster || {}).level != null ? d.micMaster.level : model.system.micLevel;
    model.system.micMuted = !!(d.micMaster || {}).muted;
    model.outputs = (d.outputs || []).map(devMap);
    model.inputs = (d.inputs || []).map(devMap);
    if (d.meters) { model.meters.output = +d.meters.output || 0; model.meters.input = +d.meters.input || 0; }
    model.macros = m.macros || [];
    model.appInputBindings = m.appInputBindings || {};
    model.media = m.media || model.media;
    if (!model.selectedId && model.apps.length) model.selectedId = model.apps[0].id;
    if (model.selectedId && !model.apps.some(function (a) { return a.id === model.selectedId; }))
      model.selectedId = model.apps.length ? model.apps[0].id : null;
  }
  function devMap(x) { var name = x.name || ""; var paren = name.match(/^(.*?)\s*\((.*)\)\s*$/);
    return { id: x.id, name: paren ? paren[1] : name, sub: paren ? paren[2] : "", isDefault: !!x.isDefault }; }

  function applyState(m) {
    var tg = m.target || {};
    if (tg.kind === "session") { var a = findApp(tg.id); if (a) { if (m.level != null) a.level = Math.round(m.level * 100); if (m.muted != null) a.muted = m.muted; } }
    else if (tg.kind === "speaker") { if (m.level != null) model.system.spkLevel = m.level; if (m.muted != null) model.system.spkMuted = m.muted; }
    else if (tg.kind === "mic") { if (m.level != null) model.system.micLevel = m.level; if (m.muted != null) model.system.micMuted = m.muted; }
    renderAll(); syncDialFromModel();
  }
  function findApp(id) { for (var i = 0; i < model.apps.length; i++) if (model.apps[i].id === id) return model.apps[i]; return null; }

  // ---- dial target derivation ----
  function dialTarget() {
    if (model.dialMode === "system") return { source: "Speakers", accent: "#4ddb7f", value: Math.round(model.system.spkLevel * 100), muted: model.system.spkMuted, mode: "jog", id: "sys", badge: "⚙" };
    if (model.dialMode === "mic") return { source: "Mic Sens", accent: "#ff7ab8", value: Math.round(model.system.micLevel * 100), muted: model.system.micMuted, mode: "linear", id: "mic", badge: "M" };
    var a = findApp(model.selectedId) || model.apps[0];
    if (!a) return { source: "—", accent: "#4ddb7f", value: 0, muted: false, mode: "jog", id: "none", badge: "?" };
    return { source: a.name, accent: a.accent, value: a.level, muted: a.muted, mode: "jog", id: "app-" + a.id, badge: a.badge, iconKey: a.iconKey };
  }
  var _lastTargetId = null;
  function syncDialFromModel() {
    var t = dialTarget();
    if (t.id !== _lastTargetId) { _lastTargetId = t.id; dial.setTarget({ value: t.value, muted: t.muted, accent: t.accent, source: t.source, mode: t.mode, interactive: model.connection !== "disconnected" }); }
    else dial.syncValue(t.value, t.muted);
  }

  // ---- sending from the dial ----
  var sendThrottle = 0;
  function dialOnChange(v) {
    var t = dialTarget(); // optimistic local update
    if (model.dialMode === "system") model.system.spkLevel = v / 100;
    else if (model.dialMode === "mic") model.system.micLevel = v / 100;
    else { var a = findApp(model.selectedId); if (a) a.level = Math.round(v); }
    renderTopbar(); renderSystem(); renderApps();
    var now = Date.now(); if (now - sendThrottle < 55) return; sendThrottle = now;
    sendVolume(t, v);
  }
  function dialOnCommit(v) { sendVolume(dialTarget(), v); }        // ensure final value lands
  function sendVolume(t, v) {
    var lvl = v / 100;
    if (t.id === "sys") send({ t: "set_volume", target: { kind: "speaker" }, level: lvl });
    else if (t.id === "mic") send({ t: "set_volume", target: { kind: "mic" }, level: lvl });
    else if (model.selectedId) send({ t: "set_volume", target: { kind: "session", id: model.selectedId }, level: lvl });
  }
  function dialToggleMute() {
    var t = dialTarget();
    if (t.id === "sys") { model.system.spkMuted = !model.system.spkMuted; send({ t: "set_mute", target: { kind: "speaker" }, muted: model.system.spkMuted }); }
    else if (t.id === "mic") { model.system.micMuted = !model.system.micMuted; send({ t: "set_mute", target: { kind: "mic" }, muted: model.system.micMuted }); }
    else { var a = findApp(model.selectedId); if (a) { a.muted = !a.muted; send({ t: "set_mute", target: { kind: "session", id: a.id }, muted: a.muted }); } }
    buzz(HAPTIC.mute); renderAll(); dial.setTarget(targetForDial());
  }
  function targetForDial() { var t = dialTarget(); return { value: t.value, muted: t.muted, accent: t.accent, source: t.source, mode: t.mode, interactive: model.connection !== "disconnected" }; }

  // ================================================================ RENDER
  var dial;
  function renderAll() { renderTopbar(); renderSystem(); renderApps(); renderDevices(); renderMedia(); syncDialFromModel(); }

  function renderTopbar() {
    // The current mix target's identity now lives above the dial. The icon is a
    // real <img> (not a CSS background) so it can't inherit the background-shorthand
    // reset that made the old reused badge render a clipped corner. A letter span
    // is the fallback for sources without an exe icon (system sounds, speaker, mic).
    var t = dialTarget();
    var img = $("dial-app-icon"), badge = $("dial-app-badge"), name = $("dial-source");
    if (img && badge) {   // guard: never let a partial/stale DOM throw and break renderAll
      if (t.iconKey) {
        var url = "/icon/" + t.iconKey;
        if (img.getAttribute("src") !== url) img.setAttribute("src", url);
        img.hidden = false; badge.hidden = true;
      } else {
        img.hidden = true; img.removeAttribute("src");
        badge.hidden = false; badge.textContent = t.badge; badge.style.background = t.accent;
      }
    }
    if (name) name.textContent = shortName(t.source);
  }

  function setConnection(state) {
    model.connection = state;
    var dot = $("conn-dot"), text = $("conn-text"), banner = $("banner");
    dot.className = "conn-dot" + (state === "reconnecting" ? " warn" : state === "disconnected" ? " err" : "");
    if (state === "connected") { text.textContent = model.latency != null ? "Connected · " + model.latency + "ms" : "Connected"; text.style.color = "var(--green-txt)"; banner.className = "banner hidden"; }
    else if (state === "connecting") { text.textContent = "connecting…"; text.style.color = "var(--sub)"; banner.className = "banner hidden"; }
    else if (state === "reconnecting") { text.textContent = "Reconnecting…"; text.style.color = "#ffcf88"; banner.className = "banner reconnecting"; banner.innerHTML = '<span class="b-dot"></span><div><div class="b-title">Reconnecting…</div><div class="b-sub">Lost the PC · controls paused</div></div><span class="b-right">' + model.retry + " / ∞</span>"; }
    else { text.textContent = "Disconnected"; text.style.color = "var(--red-tint)"; banner.className = "banner disconnected"; banner.innerHTML = '<span class="b-dot"></span><div><div class="b-title">Disconnected</div><div class="b-sub">Can’t reach the PC · is the desktop app running?</div></div>'; }
    if (dial) dial.setTarget(targetForDial());
    updateMediaConn();
  }

  function renderSystem() {
    var s = model.system;
    var micBtn = $("btn-mic"), spkBtn = $("btn-spk");
    micBtn.className = "sys-btn " + (s.micMuted ? "muted" : "on-mic");
    $("mic-ico").innerHTML = micSVG(s.micMuted ? "#f4bcbc" : "#ff7ab8", s.micMuted, 28);
    $("mic-cap").textContent = s.micMuted ? "MIC MUTED" : "MUTE MIC";
    $("mic-cap").style.color = s.micMuted ? "#f4bcbc" : "var(--ink)";
    spkBtn.className = "sys-btn " + (s.spkMuted ? "muted" : "");
    $("spk-ico").innerHTML = spkSVG(s.spkMuted ? "#f4bcbc" : "#cfd3dc", s.spkMuted, 28);
    $("spk-cap").textContent = s.spkMuted ? "MUTED" : Math.round(s.spkLevel * 100) + "%";
    $("spk-cap").style.color = s.spkMuted ? "#e07a7a" : "var(--ink)";
  }

  function renderApps() {
    // A rebuild during a press (pending long-press OR active drag) would detach the
    // very tile under the finger — a background audio poll doing so is what made the
    // reorder silently fail. Freeze rebuilds until the gesture resolves.
    if (RO.active || RO.pending) return;
    var grid = $("apps-grid"); grid.innerHTML = "";
    model.apps.forEach(function (a) {
      var on = a.id === model.selectedId && model.dialMode === "app";
      var tile = el("button", "tile" + (on ? " on" : "") + (a.active ? "" : " silent"));
      if (on) { tile.style.background = hexA(a.accent, 0.13); tile.style.borderColor = hexA(a.accent, 0.55); }
      tile.onclick = function () {
        if (Date.now() - reorderJustEnded < 400) return;   // this "click" is the tail of a drop
        model.selectedId = a.id; model.dialMode = "app"; updateModeButtons(); renderAll(); dial.setTarget(targetForDial()); nudgeActivity();
      };
      var top = el("div", "tile-top");
      var badge = el("span", "tile-badge"); paintBadge(badge, a);
      var name = el("span", "tile-name"); name.textContent = a.name; name.style.color = on ? "var(--ink)" : "var(--ink2)";
      top.appendChild(badge); top.appendChild(name);
      var bot = el("div", "tile-bot");
      var pct = el("span", "tile-pct");
      if (a.muted) { pct.className = "tile-pct muted"; pct.textContent = "—"; }
      else if (!a.active) { pct.className = "tile-pct idle"; pct.textContent = "IDLE · NO AUDIO"; }
      else pct.textContent = a.level + "%";
      var chips = el("span", "tile-chips");
      // Output (speaker) mute chip — direct OS control of the app's playback stream.
      var chip = el("span", "chip" + (a.muted ? " muted" : ""));
      chip.title = "Mute app audio";
      chip.innerHTML = spkSVG(a.muted ? "#f2b5b5" : "#c9cdd6", a.muted, 20);
      chip.onclick = function (e) { e.stopPropagation(); a.muted = !a.muted; send({ t: "set_mute", target: { kind: "session", id: a.id }, muted: a.muted }); buzz(HAPTIC.mute); renderApps(); if (model.selectedId === a.id) dial.setTarget(targetForDial()); nudgeActivity(); };
      // Input (mic) chip — the OS can't mute one app's mic, so this fires the app's
      // OWN mute/PTT hotkey (a user-bound combo). Toggle state is optimistic.
      var bound = model.appInputBindings[a.id];
      var micMuted = !!model.inputMuted[a.id];
      var micChip = el("span", "chip mic" + (bound ? "" : " ghost") + (micMuted ? " muted" : ""));
      micChip.title = bound ? ("Mic hotkey: " + bound.keys + " (long-press to edit)") : "Bind a mic-mute hotkey";
      micChip.innerHTML = micSVG(bound ? (micMuted ? "#f2b5b5" : "#ff9ec9") : "#5a5e68", micMuted, 20);
      bindLongPress(micChip, a, bound);
      chips.appendChild(chip); chips.appendChild(micChip);
      bot.appendChild(pct); bot.appendChild(chips);
      tile.setAttribute("data-app-id", a.id);
      tile.appendChild(top); tile.appendChild(bot); grid.appendChild(tile);
      enableReorder(tile, a);
    });
  }

  // ---- app-tile reordering (Android home-screen style: long-press to lift) ----
  // Order is per-device (localStorage): the PC only knows which sessions exist, not
  // the user's preferred layout. Long-press lifts a tile; dragging reorders live
  // (siblings reflow around a dashed slot); drop saves the order.
  var RO = { active: false, pending: null, app: null, tile: null, clone: null,
             ox: 0, oy: 0, lpTimer: null };
  var reorderJustEnded = 0;
  function roPt(e) { var t = (e.touches && e.touches[0]) || (e.changedTouches && e.changedTouches[0]) || e; return { x: t.clientX, y: t.clientY }; }

  function loadOrder() { try { return JSON.parse(localStorage.getItem("sc_app_order") || "[]") || []; } catch (e) { return []; } }
  function persistOrder() { try { localStorage.setItem("sc_app_order", JSON.stringify(model.apps.map(function (a) { return a.id; }))); } catch (e) {} }
  // Reorder the model to the saved layout; apps not in the saved list (newly opened)
  // keep their natural order at the end.
  function applySavedOrder() {
    var order = loadOrder(); if (!order.length) return;
    var pos = {}; for (var i = 0; i < order.length; i++) pos[order[i]] = i;
    var N = order.length;
    model.apps.forEach(function (a, idx) { a._ord = (pos[a.id] != null ? pos[a.id] : N + idx); });
    model.apps.sort(function (a, b) { return a._ord - b._ord; });
  }

  function enableReorder(tile, app) {
    tile.addEventListener("touchstart", function (e) { roPressStart(app, tile, e); }, { passive: true });
    tile.addEventListener("mousedown", function (e) { roPressStart(app, tile, e); });
  }
  function roPressStart(app, tile, e) {
    if (RO.active) return;
    if (e.target && e.target.closest && e.target.closest(".tile-chips")) return; // chips own their gestures
    var p = roPt(e);
    RO.pending = { app: app, tile: tile, x0: p.x, y0: p.y };
    if (RO.lpTimer) clearTimeout(RO.lpTimer);
    RO.lpTimer = setTimeout(function () { roBegin(p.x, p.y); }, 380);
    document.addEventListener("touchmove", roDocMove, { passive: false });
    document.addEventListener("touchend", roDocUp);
    document.addEventListener("touchcancel", roDocUp);
    document.addEventListener("mousemove", roDocMove);
    document.addEventListener("mouseup", roDocUp);
  }
  function roCleanup() {
    document.removeEventListener("touchmove", roDocMove);
    document.removeEventListener("touchend", roDocUp);
    document.removeEventListener("touchcancel", roDocUp);
    document.removeEventListener("mousemove", roDocMove);
    document.removeEventListener("mouseup", roDocUp);
    if (RO.lpTimer) { clearTimeout(RO.lpTimer); RO.lpTimer = null; }
    RO.pending = null;
  }
  function roDocMove(e) {
    var p = roPt(e);
    if (!RO.active) {
      // Movement before the hold fires means a scroll/tap, not a lift — bow out so
      // the grid scrolls (or the tap selects) natively. A finger jitters, so allow a
      // little slop before deciding it's a scroll.
      if (RO.pending && (Math.abs(p.x - RO.pending.x0) > 12 || Math.abs(p.y - RO.pending.y0) > 12)) roCleanup();
      return;
    }
    if (e.cancelable) e.preventDefault();   // we own the gesture now (no page scroll)
    roMove(p.x, p.y);
  }
  function roDocUp(e) {
    var wasActive = RO.active;
    if (wasActive && e && e.cancelable) e.preventDefault();
    roCleanup();          // clear pending BEFORE roEnd so its renderApps isn't frozen out
    if (wasActive) roEnd();
  }
  function roBegin(x, y) {
    if (!RO.pending) return;
    var app = RO.pending.app, tile = RO.pending.tile;
    // Defensive: if a rebuild slipped through and detached the tile, re-find it by id.
    if (!document.body.contains(tile)) {
      var kids = $("apps-grid").children;
      for (var i = 0; i < kids.length; i++) if (kids[i].getAttribute("data-app-id") === app.id) { tile = kids[i]; break; }
    }
    var r = tile.getBoundingClientRect();
    RO.active = true; RO.app = app; RO.tile = tile;
    RO.ox = x - r.left; RO.oy = y - r.top;
    var clone = tile.cloneNode(true);
    clone.className = "tile tile-drag-clone";
    clone.style.width = r.width + "px"; clone.style.height = r.height + "px";
    clone.style.left = r.left + "px"; clone.style.top = r.top + "px";
    document.body.appendChild(clone);   // body has no transform, so fixed coords are viewport-true
    RO.clone = clone;
    tile.classList.add("tile-drag-src");
    buzz(HAPTIC.hold); nudgeActivity();
  }
  function roMove(x, y) {
    if (!RO.clone) return;
    RO.clone.style.left = (x - RO.ox) + "px";
    RO.clone.style.top = (y - RO.oy) + "px";
    roAutoScroll(y);
    var over = document.elementFromPoint(x, y);   // clone is pointer-events:none, so this sees the tile beneath
    var overTile = over && over.closest ? over.closest(".tile") : null;
    var grid = $("apps-grid");
    if (overTile && overTile !== RO.tile && overTile.parentNode === grid) {
      var kids = Array.prototype.slice.call(grid.children);
      var from = kids.indexOf(RO.tile), to = kids.indexOf(overTile);
      if (from >= 0 && to >= 0) {
        grid.insertBefore(RO.tile, from < to ? overTile.nextSibling : overTile);
        roSyncModel(); buzz(HAPTIC.tick);
      }
    }
  }
  function roAutoScroll(y) {
    var grid = $("apps-grid"), r = grid.getBoundingClientRect(), edge = 46;
    if (y < r.top + edge) grid.scrollTop -= 12;
    else if (y > r.bottom - edge) grid.scrollTop += 12;
  }
  // DOM order is authoritative during a drag; mirror it back into model.apps.
  function roSyncModel() {
    var grid = $("apps-grid"), byId = {};
    model.apps.forEach(function (a) { byId[a.id] = a; });
    var next = [];
    Array.prototype.forEach.call(grid.children, function (node) {
      var id = node.getAttribute("data-app-id"); if (byId[id]) next.push(byId[id]);
    });
    if (next.length === model.apps.length) model.apps = next;
  }
  function roEnd() {
    RO.active = false;
    if (RO.clone && RO.clone.parentNode) RO.clone.parentNode.removeChild(RO.clone);
    RO.clone = null;
    if (RO.tile) RO.tile.classList.remove("tile-drag-src");
    RO.tile = null; RO.app = null;
    persistOrder();
    reorderJustEnded = Date.now();   // suppress the select-click that trails the drop
    buzz(HAPTIC.tap);
    renderApps();                    // clean rebuild in the new order
    nudgeActivity();
  }

  // Mic chip: tap = toggle (fire bound hotkey) or open editor if unbound;
  // long-press = open the editor to change/clear the binding.
  function bindLongPress(node, app, bound) {
    var timer = null, longFired = false;
    var start = function (e) {
      longFired = false;
      timer = setTimeout(function () { longFired = true; buzz(HAPTIC.hold); openBindingEditor(app); }, 500);
    };
    var end = function (e) {
      if (timer) { clearTimeout(timer); timer = null; }
      if (longFired) { e.preventDefault(); e.stopPropagation(); return; }
    };
    var cancel = function () { if (timer) { clearTimeout(timer); timer = null; } };
    node.addEventListener("touchstart", start, { passive: true });
    node.addEventListener("touchend", end);
    node.addEventListener("touchmove", cancel, { passive: true });
    node.addEventListener("mousedown", start);
    node.addEventListener("mouseup", end);
    node.onclick = function (e) {
      e.stopPropagation();
      if (longFired) { longFired = false; return; }   // the long-press already acted
      if (!bound) { openBindingEditor(app); return; }
      model.inputMuted[app.id] = !model.inputMuted[app.id];
      send({ t: "app_input_mute", appId: app.id });
      buzz(HAPTIC.mute); renderApps(); nudgeActivity();
    };
  }

  // ---- per-app mic-hotkey binding editor ----
  var bindingApp = null;
  function openBindingEditor(app) {
    bindingApp = app;
    var b = model.appInputBindings[app.id] || {};
    $("bind-title").textContent = "Mic hotkey · " + app.name;
    var input = $("bind-input");
    input.value = b.keys || "";
    $("bind-err").textContent = "";
    $("bind-clear").style.display = model.appInputBindings[app.id] ? "" : "none";
    $("binding").className = "pair";       // reuse the pairing overlay styling
    setTimeout(function () { try { input.focus(); } catch (e) {} }, 50);
  }
  function closeBindingEditor() { $("binding").className = "pair hidden"; bindingApp = null; }
  function saveBinding() {
    if (!bindingApp) return;
    var keys = ($("bind-input").value || "").trim();
    if (!keys) { $("bind-err").textContent = "Enter a hotkey, e.g. ctrl+shift+m"; return; }
    send({ t: "set_app_input_binding", appId: bindingApp.id, keys: keys, label: bindingApp.name });
    buzz(HAPTIC.tap); closeBindingEditor();
  }
  function clearBinding() {
    if (!bindingApp) return;
    send({ t: "clear_app_input_binding", appId: bindingApp.id });
    delete model.inputMuted[bindingApp.id];
    buzz(HAPTIC.tap); closeBindingEditor();
  }

  function renderDevices() {
    fillDevList($("outputs"), model.outputs, "#4ddb7f", "output");
    fillDevList($("inputs"), model.inputs, "#ff7ab8", "input");
  }
  function fillDevList(host, list, accent, flow) {
    host.innerHTML = "";
    list.forEach(function (d) {
      var row = el("button", "dev-row");
      if (d.isDefault) { row.style.background = hexA(accent, 0.09); row.style.borderColor = hexA(accent, 0.4); }
      row.onclick = function () { send({ t: flow === "input" ? "set_default_input" : "set_default_output", deviceId: d.id }); buzz(HAPTIC.tap); nudgeActivity(); };
      var check = el("span", "dev-check");
      if (d.isDefault) { check.style.background = accent; check.style.borderColor = accent; check.innerHTML = checkSVG(); }
      var info = el("span", "dev-info");
      var nm = el("span", "dev-name"); nm.textContent = d.name; nm.style.color = d.isDefault ? "var(--ink)" : "var(--ink2)";
      var sub = el("span", "dev-sub"); sub.textContent = d.sub || "";
      info.appendChild(nm); if (d.sub) info.appendChild(sub);
      row.appendChild(check); row.appendChild(info);
      if (d.isDefault) { // live signal meter for the active device (driven by meterFrame)
        var meter = el("span", "dev-meter");
        meter.setAttribute("data-flow", flow);
        var pattern = METER_PATTERN[flow];
        for (var bi = 0; bi < pattern.length; bi++) { var b = el("span"); b.style.height = "3px"; b.style.background = accent; meter.appendChild(b); }
        row.appendChild(meter);
      }
      host.appendChild(row);
    });
  }

  // ================================================================ MEDIA
  // Now-playing transport for whatever SMTC sees — Spotify, the Music app, and the
  // browser tab that's playing (or played last). Reached by an edge swipe: left in
  // portrait, down in landscape (or the edge handle / a tap).
  function renderMedia() {
    updateMediaConn();
    var host = $("media-list"); if (!host) return;
    host.innerHTML = "";
    var dots = $("media-dots"); if (dots) dots.innerHTML = "";
    if (!model.media.length) {
      host.classList.add("empty");
      var empty = el("div", "media-empty");
      empty.innerHTML = noteSVG("#4a4d55", 40) +
        '<div class="media-empty-t">Nothing playing</div>' +
        '<div class="media-empty-s">Start Spotify, YouTube, or any media and it appears here.</div>';
      host.appendChild(empty); return;
    }
    host.classList.remove("empty");
    // One portrait card per source, laid out in a horizontal scroll-snap row.
    model.media.forEach(function (s) {
      var playing = s.status === "playing";
      var card = el("div", "mcard" + (playing ? " playing" : ""));
      var app = el("div", "mcard-app"); app.textContent = s.app || "Media";
      var art = el("div", "mcard-art");
      if (s.thumbKey) art.style.backgroundImage = 'url("/media_thumb/' + s.thumbKey + '")';
      else art.innerHTML = noteSVG("#6b6f78", 46);
      var meta = el("div", "mcard-meta");
      var title = el("div", "mcard-title"); title.textContent = s.title || s.app || "Unknown";
      var sub = el("div", "mcard-sub"); sub.textContent = s.artist || "";
      meta.appendChild(title); if (s.artist) meta.appendChild(sub);
      var tr = el("div", "mcard-transport");
      // prev/next are always tappable (Spotify & co. skip tracks); the dim "hint"
      // just flags when the source itself reports no next/previous.
      var prev = el("button", "mbtn" + (s.canPrev ? "" : " hint")); prev.innerHTML = prevSVG("#e8eaef", 26);
      prev.onclick = function () { mediaControl("previous", s.id); };
      var pp = el("button", "mbtn play"); pp.innerHTML = playing ? pauseSVG("#0c0d10", 30) : playSVG("#0c0d10", 30);
      pp.onclick = function () { mediaControl("play_pause", s.id); };
      var next = el("button", "mbtn" + (s.canNext ? "" : " hint")); next.innerHTML = nextSVG("#e8eaef", 26);
      next.onclick = function () { mediaControl("next", s.id); };
      tr.appendChild(prev); tr.appendChild(pp); tr.appendChild(next);
      card.appendChild(app); card.appendChild(art); card.appendChild(meta); card.appendChild(tr);
      host.appendChild(card);
    });
    // Scroll-status dots — one per source (only shown when there's more than one).
    if (dots && model.media.length > 1) {
      for (var i = 0; i < model.media.length; i++) dots.appendChild(el("span", "media-dot"));
    }
    updateMediaDots();
  }
  function updateMediaConn() {
    var dot = $("media-conn-dot"), text = $("media-conn-text");
    if (!dot || !text) return;
    var st = model.connection;
    dot.className = "conn-dot" + (st === "reconnecting" ? " warn" : st === "disconnected" ? " err" : "");
    if (st === "connected") { text.textContent = model.latency != null ? "Connected · " + model.latency + "ms" : "Connected"; text.style.color = "var(--green-txt)"; }
    else if (st === "connecting") { text.textContent = "connecting…"; text.style.color = "var(--sub)"; }
    else if (st === "reconnecting") { text.textContent = "Reconnecting…"; text.style.color = "#ffcf88"; }
    else { text.textContent = "Disconnected"; text.style.color = "var(--red-tint)"; }
  }
  function updateMediaDots() {
    var host = $("media-list"), dots = $("media-dots");
    if (!host || !dots || !dots.childNodes.length) return;
    var first = host.children[0]; if (!first) return;
    var per = first.offsetWidth + 14 || host.clientWidth;   // card width + gap
    var idx = clamp(Math.round(host.scrollLeft / per), 0, dots.childNodes.length - 1);
    for (var i = 0; i < dots.childNodes.length; i++)
      dots.childNodes[i].className = "media-dot" + (i === idx ? " on" : "");
  }
  function mediaControl(action, id) {
    send({ t: "media_control", action: action, id: id }); buzz(HAPTIC.tap); nudgeActivity();
    if (action === "play_pause") {   // optimistic flip for a snappy feel; the poll corrects it
      for (var i = 0; i < model.media.length; i++)
        if (model.media[i].id === id) model.media[i].status = model.media[i].status === "playing" ? "paused" : "playing";
      renderMedia();
    }
  }
  function openMedia() { if (model.mediaOpen || !model.paired) return; model.mediaOpen = true; $("media").className = "media open"; renderMedia(); buzz(HAPTIC.tap); nudgeActivity(); }
  // Chrome (media handle etc.) is only available once the phone is paired.
  function updateChrome() { var h = $("media-handle"); if (h) h.style.display = model.paired ? "" : "none"; if (!model.paired && model.mediaOpen) closeMedia(); }
  function closeMedia() { if (!model.mediaOpen) return; model.mediaOpen = false; $("media").className = "media"; nudgeActivity(); }
  function toggleMedia() { if (model.mediaOpen) closeMedia(); else openMedia(); }

  // Media opens with a swipe UP from the bottom edge in BOTH orientations, and
  // closes with a swipe down. Bottom-edge + vertical keeps it clear of the
  // Mixer/Devices pager (horizontal) — no gesture conflict.
  var tsX = 0, tsY = 0, tsT = 0;
  function onTouchStart(e) { var t = e.touches && e.touches[0]; if (!t) return; tsX = t.clientX; tsY = t.clientY; tsT = Date.now(); }
  function onTouchEnd(e) {
    var t = e.changedTouches && e.changedTouches[0]; if (!t) return;
    var dx = t.clientX - tsX, dy = t.clientY - tsY; if (Date.now() - tsT > 800) return;
    var H = window.innerHeight, EDGE = 90, TH = 55;
    if (!model.mediaOpen) {
      if (tsY >= H - EDGE && dy < -TH && Math.abs(dy) > Math.abs(dx)) openMedia();
    } else if (dy > TH && Math.abs(dy) > Math.abs(dx)) {
      closeMedia();
    }
  }

  // ---- live device meters ----
  // Per-bar MAX heights; the displayed value scales the whole cluster by the eased
  // signal peak, so the bars "breathe" with real audio (0 when silent/muted).
  var METER_PATTERN = { output: [11, 19, 10, 21, 13, 16], input: [9, 15, 8, 17, 11, 14] };
  var meterDisp = { output: 0, input: 0 };
  function meterFrame() {
    ["output", "input"].forEach(function (flow) {
      // Perceptual gain: raw peaks are small; sqrt lifts quiet signal into view.
      var connected = model.connection === "connected";
      var target = connected ? clamp(Math.sqrt(model.meters[flow] || 0) * 1.35, 0, 1) : 0;
      // Fast attack, slow release — reads like a VU meter rather than strobing.
      var d = meterDisp[flow];
      meterDisp[flow] = d + (target - d) * (target > d ? 0.55 : 0.12);
      var host = flow === "output" ? $("outputs") : $("inputs");
      var meter = host ? host.querySelector('.dev-meter[data-flow="' + flow + '"]') : null;
      if (meter) {
        var pat = METER_PATTERN[flow], bars = meter.children, lvl = meterDisp[flow];
        for (var i = 0; i < bars.length && i < pat.length; i++)
          bars[i].style.height = (3 + pat[i] * lvl).toFixed(1) + "px";
      }
    });
    requestAnimationFrame(meterFrame);
  }

  // ---- mode switch, tabs, pager ----
  function updateModeButtons() {
    var btns = document.querySelectorAll("#mode-switch .mode-btn");
    for (var i = 0; i < btns.length; i++) btns[i].className = "mode-btn" + (btns[i].getAttribute("data-mode") === model.dialMode ? " mode-btn--on" : "");
  }
  function goPage(i) { var el = $("pager"); el.scrollTo({ left: i * el.clientWidth, behavior: "smooth" }); setPage(i); }
  function setPage(i) { model.page = i; $("tab-mixer").className = "tab" + (i === 0 ? " tab--on" : ""); $("tab-devices").className = "tab" + (i === 1 ? " tab--on" : ""); }

  // ---- toast ----
  var toastTimer = null;
  function showToast(title, sub) {
    var t = $("toast"); t.className = "toast";
    t.innerHTML = '<span class="t-ico">' + errSVG() + '</span><div class="spacer" style="flex:1"><div class="t-title">' + title + '</div><div class="t-sub">' + (sub || "") + '</div></div><span class="t-x">×</span>';
    t.querySelector(".t-x").onclick = function () { t.className = "toast hidden"; };
    if (toastTimer) clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.className = "toast hidden"; }, 5000);
  }

  // ---- pairing ----
  var pairCode = "";
  function showPair(show) { $("pair").className = "pair" + (show ? "" : " hidden"); if (show) { pairCode = ""; renderPair(); } }
  function pairError(reason) { $("pair").className = "pair error"; $("pair-err").textContent = reason || "That code expired. A fresh one is on your PC."; pairCode = ""; renderPair(); }
  function renderPair() {
    var cells = $("pair-cells"); cells.innerHTML = "";
    for (var i = 0; i < 6; i++) { var c = el("div", "pair-cell" + (i === pairCode.length ? " active" : pairCode[i] ? " filled" : "")); c.textContent = pairCode[i] || ""; cells.appendChild(c); }
    $("pair-go").disabled = pairCode.length !== 6;
    var keys = $("pair-keys");
    if (!keys.childNodes.length) {
      ["1","2","3","4","5","6","7","8","9","blank","0","del"].forEach(function (k) {
        var b = el("button", "pair-key" + (k === "blank" ? " blank" : ""));
        if (k === "blank") { b.disabled = true; }
        else if (k === "del") { b.textContent = "⌫"; b.onclick = function () { pairCode = pairCode.slice(0, -1); $("pair").className = "pair"; renderPair(); }; }
        else { b.textContent = k; b.onclick = function () { if (pairCode.length < 6) { pairCode += k; $("pair").className = "pair"; renderPair(); } }; }
        keys.appendChild(b);
      });
    }
  }

  // ---- in-app QR scanner (uses the phone camera to read the PC's QR) ----
  var scanStream = null, scanning = false, scanDetector = null;
  function scanSupported() {
    return ("BarcodeDetector" in window) && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
  }
  function extractPairCode(raw) {
    if (!raw) return null;
    var m = String(raw).match(/[?&]pair=([^&\s]+)/);
    if (m) return decodeURIComponent(m[1]);
    var d = String(raw).match(/\b(\d{6})\b/);   // QR that's just the 6-digit code
    return d ? d[1] : null;
  }
  function startScan() {
    if (!scanSupported()) return;
    $("scanner").className = "scanner"; $("scan-err").textContent = "";
    var video = $("scan-video");
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } }).then(function (stream) {
      scanStream = stream; video.srcObject = stream; scanning = true;
      try { scanDetector = scanDetector || new window.BarcodeDetector({ formats: ["qr_code"] }); }
      catch (e) { $("scan-err").textContent = "QR scanning isn't available on this browser."; return; }
      requestAnimationFrame(scanTick);
    }).catch(function () {
      $("scan-err").textContent = "Couldn't open the camera. Allow camera access, or type the code.";
    });
  }
  function scanTick() {
    if (!scanning || !scanDetector) return;
    scanDetector.detect($("scan-video")).then(function (codes) {
      if (codes && codes.length) {
        var code = extractPairCode(codes[0].rawValue);
        if (code) {
          buzz(HAPTIC.tap); scanning = false;   // pause so we don't re-submit every frame
          send({ t: "pair", code: code, device: { id: deviceId(), name: "Phone" } });
          setTimeout(function () {               // pair_ok closes the scanner; if it's still open, it failed
            var sc = $("scanner");
            if (sc && sc.className.indexOf("hidden") < 0 && scanStream) {
              $("scan-err").textContent = "That code didn't work — hold steady and try again.";
              scanning = true; requestAnimationFrame(scanTick);
            }
          }, 1800);
          return;
        }
      }
      if (scanning) requestAnimationFrame(scanTick);
    }).catch(function () { if (scanning) requestAnimationFrame(scanTick); });
  }
  function stopScan() {
    scanning = false;
    if (scanStream) { try { scanStream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {} scanStream = null; }
    var sc = $("scanner"); if (sc) sc.className = "scanner hidden";
  }

  // ---- resting dim + OLED burn-in protection ----
  // The wake lock keeps the panel on indefinitely (wall/desk mount), so a static
  // bright UI risks burn-in on OLED phones. Defences, in stages of inactivity:
  //   1) DIM at 20s — drop brightness and start a slow whole-UI pixel drift so no
  //      pixel holds the same bright value for long.
  //   2) SAVER at 2min — near-black overlay with a single dim clock that wanders
  //      the screen; tap anywhere to return. Black = OLED pixels off.
  // The base theme is already true black, which is the biggest win on OLED.
  var restTimer = null, saverTimer = null, clockTimer = null;
  var IDLE_DIM_MS = 20000, IDLE_SAVER_MS = 120000;
  function updateClock() {
    var elc = $("saver-clock"); if (!elc) return;
    var d = new Date(), h = d.getHours(), m = d.getMinutes();
    elc.textContent = (h < 10 ? "0" : "") + h + ":" + (m < 10 ? "0" : "") + m;
  }
  function enterSaver() {
    var s = $("saver"); if (!s) return;
    updateClock(); s.className = "saver";
    if (clockTimer) clearInterval(clockTimer); clockTimer = setInterval(updateClock, 20000);
  }
  function exitSaver() {
    var s = $("saver"); if (s) s.className = "saver hidden";
    if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
  }
  function nudgeActivity() {
    $("app").classList.remove("resting"); exitSaver();
    if (restTimer) clearTimeout(restTimer);
    if (saverTimer) clearTimeout(saverTimer);
    restTimer = setTimeout(function () { $("app").classList.add("resting"); }, IDLE_DIM_MS);
    saverTimer = setTimeout(enterSaver, IDLE_SAVER_MS);
  }

  // ---- fullscreen (reclaim the browser chrome; makes controls bigger) ----
  // The Fullscreen API needs a user gesture and a reload drops it, so we re-arm
  // on every gesture: if we're not fullscreen, ask again. (A PWA installed from
  // the manifest launches fullscreen for real and persists — the durable path.)
  function isFullscreen() { return !!(document.fullscreenElement || document.webkitFullscreenElement); }
  // Prefer landscape: the control surface is designed landscape-first (wall/desk mount).
  // The Screen Orientation lock only works once we're fullscreen (Android requirement),
  // so we attempt it right after entering fullscreen; the manifest also declares
  // orientation:landscape so an installed PWA launches locked. Both are best-effort.
  function lockLandscape() {
    try {
      if (screen.orientation && screen.orientation.lock) {
        var p = screen.orientation.lock("landscape");
        if (p && p.catch) p.catch(function () {});   // desktop / unsupported -> ignore
      }
    } catch (e) {}
  }
  function goFullscreen() {
    if (isFullscreen()) { lockLandscape(); return; }
    var el = document.documentElement;
    var fn = el.requestFullscreen || el.webkitRequestFullscreen || el.mozRequestFullScreen || el.msRequestFullscreen;
    if (fn) {
      try { var r = fn.call(el); if (r && r.then) r.then(lockLandscape, function () {}); else lockLandscape(); }
      catch (e) { lockLandscape(); }
    } else { lockLandscape(); }
  }

  // ---- wake lock + service worker ----
  function requestWakeLock() { if (!("wakeLock" in navigator)) return; try { navigator.wakeLock.request("screen").catch(function () {}); } catch (e) {} }
  function chooseOrient() { return (window.innerWidth >= window.innerHeight) ? "left" : "up"; }

  // ================================================================ init
  function init() {
    dial = new Dial($("dial-host"), { onChange: dialOnChange, onCommit: dialOnCommit, onToggleMute: dialToggleMute });
    dial.setOrient(chooseOrient());
    dial.setTarget(targetForDial());

    // mode switch
    var mbtns = document.querySelectorAll("#mode-switch .mode-btn");
    for (var i = 0; i < mbtns.length; i++) mbtns[i].onclick = (function (btn) { return function () { model.dialMode = btn.getAttribute("data-mode"); updateModeButtons(); renderAll(); dial.setTarget(targetForDial()); nudgeActivity(); }; })(mbtns[i]);
    // system hero buttons (mic mute is priority: always one tap away)
    $("btn-mic").onclick = function () {
      model.system.micMuted = !model.system.micMuted;
      send({ t: "set_mute", target: { kind: "mic" }, muted: model.system.micMuted });
      buzz(HAPTIC.mute); renderSystem(); if (model.dialMode === "mic") dial.setTarget(targetForDial()); nudgeActivity();
    };
    $("btn-spk").onclick = function () {
      model.system.spkMuted = !model.system.spkMuted;
      send({ t: "set_mute", target: { kind: "speaker" }, muted: model.system.spkMuted });
      buzz(HAPTIC.mute); renderSystem(); if (model.dialMode === "system") dial.setTarget(targetForDial()); nudgeActivity();
    };
    // tabs + pager
    $("tab-mixer").onclick = function () { goPage(0); }; $("tab-devices").onclick = function () { goPage(1); }; $("dev-back").onclick = function () { goPage(0); };
    $("pager").addEventListener("scroll", function (e) { var el = e.currentTarget, idx = Math.round(el.scrollLeft / el.clientWidth); if (idx !== model.page) setPage(idx); });
    // pairing
    $("pair-go").onclick = function () { if (pairCode.length === 6) send({ t: "pair", code: pairCode, device: { id: deviceId(), name: "Phone" } }); };
    // in-app QR scanner (only offered when the browser can actually scan)
    if ($("pair-scan")) {
      if (scanSupported()) $("pair-scan").onclick = startScan;
      else $("pair-scan").style.display = "none";
    }
    if ($("scan-cancel")) $("scan-cancel").onclick = stopScan;
    updateChrome();   // hide the media handle until paired
    // per-app mic-hotkey binding editor
    $("bind-save").onclick = saveBinding;
    $("bind-clear").onclick = clearBinding;
    $("bind-cancel").onclick = closeBindingEditor;
    $("bind-input").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); saveBinding(); } });

    // resting + activity
    ["touchstart", "mousedown", "keydown"].forEach(function (ev) { document.addEventListener(ev, nudgeActivity, { passive: true }); });
    // Re-arm fullscreen on every gesture so it persists (restores after a reload
    // or an accidental exit). Guarded to a no-op when already fullscreen.
    document.addEventListener("touchend", goFullscreen, { passive: true });
    document.addEventListener("click", goFullscreen);
    // tapping the screensaver returns to the controls
    if ($("saver")) $("saver").addEventListener("click", nudgeActivity);
    // media sheet: edge handle, close button, backdrop, and edge-swipe gesture
    if ($("media-handle")) $("media-handle").onclick = toggleMedia;
    if ($("media-up")) $("media-up").onclick = closeMedia;   // chevron: back up to mixer
    if ($("media-list")) $("media-list").addEventListener("scroll", updateMediaDots, { passive: true });
    document.addEventListener("touchstart", onTouchStart, { passive: true });
    document.addEventListener("touchend", onTouchEnd, { passive: true });
    // resume-resync
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") { ensureConnected(); requestWakeLock(); } });
    window.addEventListener("pageshow", ensureConnected); window.addEventListener("online", ensureConnected); window.addEventListener("focus", ensureConnected);
    window.addEventListener("resize", function () { dial.setOrient(chooseOrient()); });
    if ("serviceWorker" in navigator) window.addEventListener("load", function () { navigator.serviceWorker.register("/sw.js").catch(function () {}); });

    window.SC = { send: send, model: model, dial: dial };
    connect(); requestWakeLock(); nudgeActivity();
    if (window.requestAnimationFrame) requestAnimationFrame(meterFrame);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
