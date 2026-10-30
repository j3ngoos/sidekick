/* SK-22: feed rendering — the task «работа» card and the collapsible think block.
   The chat feed must not spam: steps live INSIDE one card per task (collapsed
   header «⚙ <active step>» + work indicator, click expands the step list), the
   think stream is one collapsed «💭 Думает…» block per task, and only the final
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
// SK-30: статус-глифы мигрируют в inline-SVG. В живом DOM (document есть) метка
// подменяется иконкой из спрайта сайдбара; в DOM-заглушке node-контрактов
// (makeEl без createElementNS) остаётся текстовый глиф — контракты SK-22 зелёные,
// живой UI без эмодзи (карточка задачи/шаги/think — «SVG-кандидаты» спеки п.2).
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
// el ← <svg ic-name/> + текст (strip — срезать ведущий глиф); SVG недоступен
// (stub) — текст verbatim вместе с глифом: fallback честен и тестируем.
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
  if (sec < 60) return sec + " сек";
  return Math.floor(sec / 60) + " мин " + (sec % 60) + " сек";
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
// SK-36: человекочитаемый текст шага = подпись агента, иначе шаблон/модель (text).
function stepLabel(s) { return (s && (s.caption || s.text)) || ""; }
function headerText(tl) {
  const steps = (tl.steps || []).slice().sort((a, b) => a.n - b.n);
  const cur = steps.find((s) => s.state === "active") || steps[steps.length - 1];
  return "⚙ " + (stepLabel(cur) || tl.title || "работа");
}
function hasFailed(tl) {
  return tl.state === "failed" || (tl.steps || []).some((s) => s.state === "failed");
}
function thinkHead() { return "💭 Думает…"; }
function thinkDoneHead(sec) { return "💭 Подумал (" + fmtDur(sec) + ")"; }

// ---- task card ------------------------------------------------------------------
// SK-25 п.4: buildCard(doc, task, onFocusTab) — в заголовке появился чип вкладки
// («🌐 <title>», hidden пока у задачи нет вкладки). Клик по чипу = фокус вкладки
// агента (жест человека), stopPropagation: клик по заголовку остаётся сворачиванием
// шагов (SK-22). Вызывающий без onFocusTab получает карточку ровно как раньше.
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
  // stale (задача пропала без терминального кадра) — честное «○»; иначе метка
  // состояния из терминала: running → спиннер, done → ✓, failed → ✗
  const mark = c.stale ? "○" : (TASK_MARK[tl.state] || "spin");
  if (mark === "spin") { c.ic.className = "tkic spin"; c.ic.textContent = ""; }
  else {
    c.ic.className = "tkic";
    setIcoText(doc, c.ic, c.stale ? "ring" : TASK_ICO[tl.state], mark);
  }
  setIcoText(doc, c.tx, "settings", headerText(tl), true); // SK-30: шестерня — SVG
  // SK-25 п.4: чип вкладки — заголовок вкладки задачи + клик = фокус (пока вкладки нет
  // или tl старый формату без tabTitle — чип скрыт, карточка ведёт себя как в SK-22)
  c.gone = !!tl.tabGone;
  if (c.tb) {
    if (tl.tabTitle || tl.tabGone) {
      c.tb.hidden = false;
      setIcoText(doc, c.tb, "globe",
        tl.tabGone ? "🌐 вкладка закрыта" : "🌐 " + String(tl.tabTitle).slice(0, 40), true);
      c.tb.title = String(tl.url || "");
      c.tb.className = "tktab" + (tl.tabGone ? " gone" : "");
    } else { c.tb.hidden = true; c.tb.textContent = ""; }
  }
  const rows = (tl.steps || []).slice().sort((a, b) => a.n - b.n).map((s) => {
    const t = noteStep(times, c.task + "|" + s.n, s.state, now);
    const row = doc.createElement("div");
    // SK-40: «open» (перенос строк) нужен и для раскрытого raw — pre с
    // flex-basis:100% в nowrap-ряду сжимал подпись шага в вертикальный столбик.
    row.className = "srow " + (s.state || "queued") + (s.shot ? " hasshot" : "") +
      ((c.shots.has(s.n) || c.raws.has(s.n)) ? " open" : "");
    const ic = doc.createElement("span"); ic.className = "ic";
    setIcoText(doc, ic, STEP_ICO[s.state] || "ring", STEP_ICON[s.state] || "○");
    const tx = doc.createElement("span"); tx.className = "tx";
    tx.textContent = s.n + ". " + stepLabel(s); // SK-36: подпись агента важнее шаблона
    const tm = doc.createElement("span"); tm.className = "stime";
    tm.textContent = fmtTime(t.last);
    row.append(ic, tx, tm);
    // SK-36: сырая команда (method+params) спрятана по умолчанию — только по клику
    // «подробнее», моноширинно. Голый JSON/селекторы в ленте не светятся.
    if (s.raw) {
      const more = doc.createElement("button");
      more.type = "button"; more.className = "smore";
      more.textContent = c.raws.has(s.n) ? "скрыть" : "подробнее";
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
    if (s.shot) { // «что агент видел» — the SK-06 shot stays, inside the expanded card
      if (c.shots.has(s.n)) {
        const img = doc.createElement("img"); img.className = "shot";
        img.src = s.shot; img.alt = "что агент видел на шаге " + s.n;
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
  buildThink, paintThink, thinkDelta, thinkFinish, stepLabel };
if (typeof module !== "undefined" && module.exports) module.exports = api;
else root.Feed = api;

})(typeof window !== "undefined" ? window : globalThis);
