/* SK-22: feed rendering — the task "working" card and the collapsible think block.
   The chat feed must not spam: steps live INSIDE one card per task (collapsed
   header "⚙ <active step>" + work indicator, click expands the step list), the
   think stream is one collapsed "Thinking…" block per task, and only the final
   chat answer is always expanded.

   DOM-agnostic by contract: every builder takes `doc` — the real document in the
   sidebar, a stub in the node render contracts (tests/test_feed_sk22.py). No
   global DOM access here, no inline styles (SK-13: classes only, theme tokens in
   sidebar.css/theme.css). Pure helpers (headerText/hasFailed/fmtTime/noteStep)
   are exported for direct node unit tests. */
(function (root) {
"use strict";

const STEP_ICON = { queued: "○", active: "▶", done: "✓", failed: "✗" };
// task-level header mark: spinner while running, text marks otherwise (spec SK-22)
const TASK_MARK = { running: "spin", paused: "⏸", done: "✓", failed: "✗" };
const THINK_TAIL = 20000; // memory cap: the think stream keeps only its tail
// i18n (SK-71): the sidebar page injects the active-language translator via
// Feed.setT(I18N.t, I18N.has). The node render contracts get the built-in EN
// default pack below, whose values mirror extension/locales/en.json feed.*
// byte-for-byte (parity is pinned by tests/test_i18n_sk71.py).
const EN_PACK = {
  "feed.sec": " sec", "feed.min": " min ", "feed.working": "working",
  "feed.thinking": "💭 Thinking…", "feed.thought": "💭 Thought ({{d}})",
  "feed.tab_closed": "🌐 tab closed", "feed.hide": "hide", "feed.more": "details",
  "feed.shot_alt": "what the agent saw at step {{n}}",
};
let T = null, HAS = null;
function setT(tfn, hasfn) { T = typeof tfn === "function" ? tfn : null; HAS = typeof hasfn === "function" ? hasfn : null; }
function fill(s, args) {
  if (!args) return s;
  return s.replace(/\{\{(\w+)\}\}/g, (_, k) => (Object.prototype.hasOwnProperty.call(args, k) ? String(args[k]) : ""));
}
function L(key, args) { return fill(T ? T(key, args) : (EN_PACK[key] !== undefined ? EN_PACK[key] : key), args); }
function Lhas(key) { return HAS ? HAS(key) : Object.prototype.hasOwnProperty.call(EN_PACK, key); }
// SK-30: status glyphs migrate to inline-SVG. In the live DOM (document exists) the label
// is replaced by the sidebar sprite icon; in the node contracts' DOM stub
// (makeEl without createElementNS) the text glyph stays — SK-22 contracts are green,
// live UI without emoji (task card/steps/think — spec p.2's "SVG candidates").
const SVG_NS = "http://www.w3.org/2000/svg";
const STEP_ICO = { queued: "ring", active: "play", done: "check", failed: "cross" };
const TASK_ICO = { paused: "pause", done: "check", failed: "cross" };
function svgIcon(doc, name) {
  if (!doc || typeof doc.createElementNS !== "function") return null;
  const s = doc.createElementNS(SVG_NS, "svg");
  s.setAttribute("class", "icn");
  s.setAttribute("aria-hidden", "true");
  const u = doc.createElementNS(SVG_NS, "use");
  u.setAttribute("href", "#ic-" + name);
  s.appendChild(u);
  return s;
}
// el ← <svg ic-name/> + text (strip — cut the leading glyph); when SVG is unavailable
// (stub) — text verbatim together with the glyph: the fallback is honest and tested.
function setIcoText(doc, el, name, text, strip) {
  const ic = name ? svgIcon(doc, name) : null;
  if (!ic) { el.textContent = text; return; }
  const t = strip ? String(text).replace(/^\S+\s*/, "") : text;
  el.replaceChildren(ic, doc.createTextNode(t));
}

function fmtTime(ts) { // local "HH:MM:SS"; "" for unknown
  if (!ts) return "";
  const d = new Date(ts);
  const p = (x) => String(x).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + L("feed.sec");
  return Math.floor(sec / 60) + L("feed.min") + (sec % 60) + L("feed.sec");
}
// Step clocks are sidebar-side: the bridge's step frames carry no ts (contract v1
// is frozen), so we record first-seen and last state-change per step. Rows show
// the last change; a replay after a sidebar reload stamps everything "now" —
// honest best effort, never a fabricated time.
function noteStep(times, key, state, now) {
  let t = times[key];
  const s = state || "queued";
  if (!t) t = times[key] = { first: now, last: now, state: s };
  else if (s !== t.state) { t.last = now; t.state = s; }
  return t;
}
// collapsed header: the ACTIVE step, else the last one by n, else the task title
// SK-36: the human-readable step text = the agent caption, else the dictionary
// caption template (capkey/capargs from agentd, SK-71), else model-refined text.
// st.model (agentd's small-model refinement, Russian-only) outranks the template
// so RU keeps the model phrase; EN users see the EN template of the same step.
function stepLabel(s) {
  if (!s) return "";
  if (s.caption) return s.caption;
  if (!s.model && s.capkey && Lhas(s.capkey)) return L(s.capkey, s.capargs);
  return s.text || "";
}
function headerText(tl) {
  const steps = (tl.steps || []).slice().sort((a, b) => a.n - b.n);
  const cur = steps.find((s) => s.state === "active") || steps[steps.length - 1];
  return "⚙ " + (stepLabel(cur) || tl.title || L("feed.working"));
}
function hasFailed(tl) {
  return tl.state === "failed" || (tl.steps || []).some((s) => s.state === "failed");
}
function thinkHead() { return L("feed.thinking"); }
function thinkDoneHead(sec) { return L("feed.thought", { d: fmtDur(sec) }); }

// ---- task card ------------------------------------------------------------------
// SK-25 p.4: buildCard(doc, task, onFocusTab) — the header gained a tab chip
// ("🌐 <title>", hidden while the task has no tab). Clicking the chip = tab focus
// the agent (a human gesture); stopPropagation: clicking the header stays a collapse
// steps (SK-22). Callers without onFocusTab get the card exactly as before.
function buildCard(doc, task, onFocusTab) {
  const card = doc.createElement("div"); card.className = "m task";
  const hd = doc.createElement("div"); hd.className = "tkhd";
  const ic = doc.createElement("span"); ic.className = "tkic";
  const tx = doc.createElement("span"); tx.className = "tktx";
  const tb = doc.createElement("span"); tb.className = "tktab"; tb.hidden = true;
  const cr = doc.createElement("span"); cr.className = "tkcaret"; setIcoText(doc, cr, "chevron", "▸");
  hd.append(ic, tx, tb, cr);
  const body = doc.createElement("div"); body.className = "tksteps";
  card.append(hd, body);
  const c = { card, hd, ic, tx, tb, cr, body, task: String(task), doc, open: false,
    failedShown: false, live: true, stale: false, shots: new Set(), raws: new Set(), paint: null,
    onFocusTab: typeof onFocusTab === "function" ? onFocusTab : null, gone: false };
  hd.onclick = () => { c.open = !c.open; if (c.paint) c.paint(); };
  tb.onclick = (e) => {
    if (e && typeof e.stopPropagation === "function") e.stopPropagation();
    if (c.onFocusTab && !c.gone) c.onFocusTab(c.task);
  };
  return c;
}
// paintCard(doc, c, tl, times, now) — full repaint of one card from the TL snapshot.
// Auto-expand on failed happens ONCE (the human may collapse it again afterwards).
function paintCard(doc, c, tl, times, now) {
  c.paint = () => paintCard(doc, c, c.tl || tl, times, Date.now());
  c.tl = tl;
  if (hasFailed(tl) && !c.failedShown) { c.open = true; c.failedShown = true; }
  c.card.className = "m task " + (tl.state || "running") + (c.open ? " open" : "") +
    (c.stale ? " stale" : "");
  // stale (the task vanished with no terminal frame) — an honest "○"; else the label
  // terminal states: running → spinner, done → ✓, failed → ✗
  const mark = c.stale ? "○" : (TASK_MARK[tl.state] || "spin");
  if (mark === "spin") { c.ic.className = "tkic spin"; c.ic.textContent = ""; }
  else {
    c.ic.className = "tkic";
    setIcoText(doc, c.ic, c.stale ? "ring" : TASK_ICO[tl.state], mark);
  }
  setIcoText(doc, c.tx, "settings", headerText(tl), true); // SK-30: the gear — SVG
  // SK-25 p.4: the tab chip — the task tab's title + click = focus (while there is no tab
  // or tl in the old format without tabTitle — the chip hides, the card behaves as in SK-22)
  c.gone = !!tl.tabGone;
  if (c.tb) {
    if (tl.tabTitle || tl.tabGone) {
      c.tb.hidden = false;
      setIcoText(doc, c.tb, "globe",
        tl.tabGone ? L("feed.tab_closed") : "🌐 " + String(tl.tabTitle).slice(0, 40), true);
      c.tb.title = String(tl.url || "");
      c.tb.className = "tktab" + (tl.tabGone ? " gone" : "");
    } else { c.tb.hidden = true; c.tb.textContent = ""; }
  }
  const rows = (tl.steps || []).slice().sort((a, b) => a.n - b.n).map((s) => {
    const t = noteStep(times, c.task + "|" + s.n, s.state, now);
    const row = doc.createElement("div");
    // SK-40: "open" (word wrap) is needed for the expanded raw too — a pre with
    // flex-basis:100% in a nowrap row squeezed the step caption into a vertical column.
    row.className = "srow " + (s.state || "queued") + (s.shot ? " hasshot" : "") +
      ((c.shots.has(s.n) || c.raws.has(s.n)) ? " open" : "");
    const ic = doc.createElement("span"); ic.className = "ic";
    setIcoText(doc, ic, STEP_ICO[s.state] || "ring", STEP_ICON[s.state] || "○");
    const tx = doc.createElement("span"); tx.className = "tx";
    tx.textContent = s.n + ". " + stepLabel(s); // SK-36: the agent's caption outranks the template
    const tm = doc.createElement("span"); tm.className = "stime";
    tm.textContent = fmtTime(t.last);
    row.append(ic, tx, tm);
    // SK-36: the raw command (method+params) is hidden by default — only on click
    // "details", monospace. Bare JSON/selectors never show in the feed.
    if (s.raw) {
      const more = doc.createElement("button");
      more.type = "button"; more.className = "smore";
      more.textContent = c.raws.has(s.n) ? L("feed.hide") : L("feed.more");
      more.onclick = (e) => {
        if (e && typeof e.stopPropagation === "function") e.stopPropagation();
        c.raws.has(s.n) ? c.raws.delete(s.n) : c.raws.add(s.n);
        if (c.paint) c.paint();
      };
      row.append(more);
      if (c.raws.has(s.n)) {
        const pre = doc.createElement("pre"); pre.className = "sraw";
        pre.textContent = String(s.raw);
        row.append(pre);
      }
    }
    if (s.shot) { // "what the agent saw" — the SK-06 shot stays inside the expanded card
      if (c.shots.has(s.n)) {
        const img = doc.createElement("img"); img.className = "shot";
        img.src = s.shot; img.alt = L("feed.shot_alt", { n: s.n });
        row.append(img);
      } else {
        const hint = doc.createElement("span"); hint.className = "hint";
        setIcoText(doc, hint, "camera", "📷");
        row.append(hint);
      }
      row.onclick = () => { c.shots.has(s.n) ? c.shots.delete(s.n) : c.shots.add(s.n); if (c.paint) c.paint(); };
    }
    return row;
  });
  c.body.replaceChildren(...rows);
  return c;
}
// A task that vanished without a terminal frame (WS died mid-task): freeze it so
// two spinners never run at once; "○" is honest — we do not know how it ended.
function markStale(c) {
  c.live = false;
  c.stale = true;
  c.ic.className = "tkic"; setIcoText(c.doc, c.ic, "ring", "○");
  c.card.className = "m task stale" + (c.open ? " open" : "");
}

// ---- think block ----------------------------------------------------------------
function buildThink(doc, task) {
  const box = doc.createElement("div"); box.className = "m think";
  const hd = doc.createElement("div"); hd.className = "thd"; setIcoText(doc, hd, "think", thinkHead(), true);
  const tx = doc.createElement("div"); tx.className = "tx2";
  box.append(hd, tx);
  const b = { box, hd, tx, task: String(task), doc, open: false, done: false,
    t0: 0, acc: "" };
  hd.onclick = () => {
    b.open = !b.open; paintThink(b);
    if (b.open) tx.scrollTop = tx.scrollHeight; // opening reveals the tail
  };
  return b;
}
function paintThink(b) {
  b.box.className = "m think" + (b.open ? " open" : "") + (b.done ? " done" : "");
}
function thinkDelta(b, text, now) {
  if (!text) return;
  if (b.done) { b.done = false; setIcoText(b.doc, b.hd, "think", thinkHead(), true); } // late delta: honest again
  if (!b.t0) b.t0 = now;
  b.acc = (b.acc + text).slice(-THINK_TAIL);
  b.tx.textContent = b.acc;
  paintThink(b);
  if (b.open) b.tx.scrollTop = b.tx.scrollHeight; // autoscroll to tail only while expanded
}
function thinkFinish(b, now) {
  if (b.done) return;
  b.done = true;
  setIcoText(b.doc, b.hd, "think", thinkDoneHead(b.t0 ? (now - b.t0) / 1000 : 0), true);
  paintThink(b);
}

const api = { STEP_ICON, TASK_MARK, THINK_TAIL, fmtTime, fmtDur, noteStep, headerText,
  hasFailed, thinkHead, thinkDoneHead, buildCard, paintCard, markStale,
  buildThink, paintThink, thinkDelta, thinkFinish, stepLabel,
  setT, L, Lhas };
if (typeof module !== "undefined" && module.exports) module.exports = api;
else root.Feed = api;

})(typeof window !== "undefined" ? window : globalThis);
