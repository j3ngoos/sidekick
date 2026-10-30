const log = document.getElementById("log"), inp = document.getElementById("in"), go = document.getElementById("go"), st = document.getElementById("st");
// SK-40: the feed's empty state. Synced by DOM (not by module flags —
// AUD/HIST init below): empty and no second view → show the glyph.
const EMPTY = document.getElementById("empty");
function syncEmpty() {
  if (!EMPTY) return;
  EMPTY.hidden = !!(log.hidden || log.children.length ||
    !document.getElementById("aud").hidden || !document.getElementById("hist").hidden);
}
const ttl = document.getElementById("ttl"); // SK-38: only the chat title is JS-driven in the header
let port;
// ---- SK-30: inline-SVG icons. Emoji are banned in chrome UI; status glyphs
// the text labels (resolved labels, handoff, think) stay the source and the honest
// fallback, but in the live DOM they are replaced by the SVG candidate (GLYPH_ICON).
const SVG_NS = "http://www.w3.org/2000/svg";
function svgIcon(name, cls) {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("class", "icn" + (cls ? " " + cls : ""));
  s.setAttribute("aria-hidden", "true");
  const u = document.createElementNS(SVG_NS, "use");
  u.setAttribute("href", "#ic-" + name);
  s.appendChild(u);
  return s;
}
const GLYPH_ICON = { "💭": "think", "🖐": "hand", "⏸": "pause", "▶": "play",
  "●": "dot", "✓": "check", "✗": "cross", "○": "ring", "⚠": "alert",
  "⏱": "clock", "↪": "replace", "🗑": "memory", "⚙": "settings", "🌐": "globe" };
// "💭 thinking" → <svg ic-think/> thinking; no matches — text as is.
function setGlyphText(el, str) {
  const m = /^(\S+)([\s\S]*)$/.exec(String(str == null ? "" : str));
  const name = m && GLYPH_ICON[m[1]];
  if (!name) { el.textContent = str; return; }
  el.replaceChildren(svgIcon(name), document.createTextNode((m[2] || "").replace(/^\s+/, "")));
}
// SK-71: UI strings come from the locale pack (locales/en.json|ru.json). L() is the
// one-line alias: greppable, and pages own the rendering; pure modules (feed, audit)
// receive the translator via their setT().
const L = (k, a) => I18N.t(k, a);
let TL = { task: "", title: "", state: "", steps: [], warn: "", handoff: null }; // SK-06 snapshot from background (single source of truth)
// SK-09: agent state — rendered verbatim from the bridge's {type:"state"} frames (via the
// background). "unknown" = no frame seen yet / the WS to the bridge is down.
// SK-38 p.1: status chips ("💭 thinking"/"● acting") were removed from the header — the feed
// the bottom (the task card + think block in the feed), and the toolbar badge carries the agent status
// (background badge()). AST stays: the only live consumers are the future bell
// (SK-37) and debugging; the header no longer paints status.
let AST = "unknown";
const PERM_BLOCKS = new Map(); // ask id -> element (inline, never a modal)
const HO_BLOCKS = new Map(); // SK-08 handoff id -> element (the "you're needed" banner)
// SK-71: structural key -> dictionary key (perm.*); renderers wrap lookups in L().
const ACTION_RU = { sensitive_input: "perm.sensitive_input", payment: "perm.payment",
  oauth: "perm.oauth", "2fa": "perm.2fa", click: "perm.click",
  input: "perm.input", submit: "perm.submit", navigate: "perm.navigate", action: "perm.action",
  close_tabs: "perm.close_tabs", // SK-32
  send_message: "perm.send_message", send_email: "perm.send_email" }; // SK-34
const REASON_RU = { level_ask: "perm.site_ask", no_grant: "perm.site_default",
  bank_domain: "perm.bank_domain", auth_or_payment_path: "perm.login_page",
  "action:sensitive_input": "perm.sensitive_field", "action:payment": "perm.payment",
  "action:oauth": "OAuth", "action:2fa": "2FA",
  "action:send_message": "perm.out_text", "action:send_email": "perm.out_mail", // SK-34
  close_tabs: "perm.mass_close" }; // SK-32
function add(cls, text, memory) {
  const d = document.createElement("div"); d.className = "m " + cls; d.textContent = text;
  // SK-40: bubble avatar glyph — a lightning bolt on a gradient (agent) / a person (you).
  // The absolute bubble corner: the feed's composition grid and node contracts stay untouched.
  if (cls === "me" || cls === "hm") {
    const av = document.createElement("span");
    av.className = "av " + (cls === "me" ? "hu" : "ag");
    av.appendChild(svgIcon(cls === "me" ? "user" : "bolt"));
    d.insertBefore(av, d.firstChild);
  }
  // SK-31: the answer was assembled from knet (not from this chat) — a small glyph on the message;
  // SK-30: instead of emoji — SVG candidate ic-memory (the "🗑" text stays in
  // comments and in step data "🗑 knet_search · …" — the source of truth is there).
  if (memory) {
    const g = document.createElement("span"); g.className = "mem";
    g.appendChild(svgIcon("memory"));
    g.title = L("sb.memory_chip");
    d.appendChild(g);
  }
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  syncEmpty();
}
// ---- SK-06: step timeline + inline Stop. The sidebar renders the background's
// snapshot; Stop click posts {type:"stop"} and the echo (paused) comes back as a
// fresh snapshot — one source of truth, so several sidebars never diverge.
// SK-38 p.1/4: the header no longer paints status (no chips); the only reaction to
// the snapshot — morphs the send button into stop (setGoRun) and enables/disables the menu item.
// ---- SK-06/SK-22: task state + step timeline. Since SK-22 the steps are NOT a
// permanent band above the chat: each task gets ONE card inside the chat feed —
// collapsed header «⚙ <active step>» + work indicator (spinner/✓/✗), click
// expands the step list (times recorded sidebar-side, shots SK-06 inside).
// Auto-expand on failed. SK-38: header chips and the ■ button are gone — the card
// in the feed is the status surface (card: "keep it at the bottom", t_7cf9bfad).
const TASK_CARDS = new Map(); // task -> card state (one card per task, never a duplicate)
const STEP_TIMES = {};        // "task|n" -> {first,last,state} — sidebar-side clock
// SK-22 p.5 (backward compat): a task without steps (old bridge) gets no card —
// the chat feed works exactly as before.
  // SK-23: the card belongs to the chat its task ran in (frames without chat =
  // "default"); while another chat is open the card itself is not this chat's
  // feed and stays out (SK-38: global status is no longer duplicated in the header).
  // SK-25 p.3: PARALLEL tasks — one card per task of its own chat (array
  // tasks from background; a single legacy-format snapshot — fallback. Feeds never mix:
  // the card key is the task id. The card's tab chip (p.4) focuses the tab on click
  // the human (port tab_focus → tabs.update active + focus the window).
  function focusTab(task) { try { port.postMessage({ type: "tab_focus", task }); } catch (e) { /* port between reconnects */ } }
  function renderCardFor(t) {
    let c = TASK_CARDS.get(t.task);
    if (!c) {
      c = Feed.buildCard(document, t.task, focusTab);
      TASK_CARDS.set(t.task, c);
      const th = THINK_BLOCKS.get(t.task); // the card comes before the task's think block
      if (th) log.insertBefore(c.card, th.box); else log.appendChild(c.card);
      log.scrollTop = log.scrollHeight;
    }
    c.live = t.state !== "done" && t.state !== "failed";
    Feed.paintCard(document, c, t, STEP_TIMES, Date.now());
  }
  // SK-38 p.4: the button morph. GO_RUN is the single source of truth about state; the class and
  // the a11y label change together (title/aria-label — visible to a screen reader too).
  let GO_RUN = false;
  function setGoRun(on) {
    if (GO_RUN === on) return;
    GO_RUN = on;
    go.classList.toggle("running", on);
    go.title = on ? L("sb.stop_esc") : L("sb.send");
    go.setAttribute("aria-label", on ? L("sbt.stop") : L("sbt.aria_send"));
  }
  function renderTL() {
    // SK-30 p.4 / SK-38 p.4: "Stop task" only lives during a running task
    // (parallel SK-25 — one is enough); the same anyRun feeds the button morph.
    const anyRun = (Array.isArray(TL.tasks) ? TL.tasks : [TL]).some((t) => t && t.state === "running");
    setGoRun(anyRun);
    stopTaskBtn.disabled = !anyRun;
    const list = Array.isArray(TL.tasks) ? TL.tasks
      : (TL.task ? [{ task: TL.task, title: TL.title, state: TL.state, chat: TL.chat, steps: TL.steps }] : []);
    const seen = new Set();
    for (const t of list) {
      if (!t || !t.task || !(t.steps && t.steps.length)) continue;
      if (((t.chat) || "default") !== activeChat) continue;
      seen.add(t.task);
      renderCardFor(t);
    }
    // a task that vanished without a terminal frame (WS died) must not keep spinning
    // next to the live ones — an honest "○": we do not know how it ended
    for (const [k, old] of TASK_CARDS) if (!seen.has(k) && old.live) Feed.markStale(old);
  }
// ---- SK-30 p.1 / SK-38 p.3: the "⋯" menu lives in the header (right edge). Items in
// the #menu portal (a direct child of body, ids unchanged — SK-23/10/11 contracts),
// opens downward from the header (CSS: top from --hd-h).
// Keyboard: Enter/Space on "⋯" (native button), ↑↓/Home/End between items,
// Tab — cycles the menu, Esc — closes with focus returned to "⋯". A click outside —
// closing. Position/animation — pure CSS (#menu), no inline styles.
const menuBtn = document.getElementById("menuBtn"), menu = document.getElementById("menu"),
      optBtn = document.getElementById("optBtn"), stopTaskBtn = document.getElementById("stopTask"),
      themeBtn = document.getElementById("themeBtn"), themeLbl = document.getElementById("themeLbl");
function menuItems() { return Array.from(menu.querySelectorAll(".mi")).filter((b) => !b.disabled); }
function menuFocus(el) { if (el) el.focus(); }
function menuOpen(on) {
  menu.hidden = !on;
  menuBtn.setAttribute("aria-expanded", on ? "true" : "false");
  if (on) menuFocus(menuItems()[0]);
}
menuBtn.onclick = () => menuOpen(menu.hidden);
document.addEventListener("mousedown", (e) => {
  if (!menu.hidden && !menu.contains(e.target) && !menuBtn.contains(e.target)) menuOpen(false);
});
menu.addEventListener("click", (e) => { if (e.target.closest(".mi")) menuOpen(false); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!menu.hidden) { e.preventDefault(); menuOpen(false); menuBtn.focus(); return; }
  // SK-38 p.4: Esc = the same stop as the morph button. Hierarchy: first the "⋯" menu (top),
  // then the skill menu and the name field (their handlers swallow the event via stopPropagation) —
  // Esc reaches here only when there is nothing to close and something to stop.
  if (GO_RUN) { e.preventDefault(); port.postMessage({ type: "stop", task: TL.task }); }
});
menu.addEventListener("keydown", (e) => {
  const items = menuItems();
  if (!items.length) return;
  const i = Math.max(0, items.indexOf(document.activeElement));
  if (e.key === "ArrowDown") { e.preventDefault(); menuFocus(items[(i + 1) % items.length]); }
  else if (e.key === "ArrowUp") { e.preventDefault(); menuFocus(items[(i - 1 + items.length) % items.length]); }
  else if (e.key === "Home") { e.preventDefault(); menuFocus(items[0]); }
  else if (e.key === "End") { e.preventDefault(); menuFocus(items[items.length - 1]); }
  else if (e.key === "Tab") { // focus never leaves the open menu
    e.preventDefault();
    menuFocus(items[(i + (e.shiftKey ? -1 : 1) + items.length) % items.length]);
  }
});
optBtn.onclick = () => browser.runtime.openOptionsPage();
stopTaskBtn.onclick = () => port.postMessage({ type: "stop", task: TL.task });

// ---- SK-35: "call the human" bell + manual panel collapse -------------
// The journal (task_done / task_failed / perm) is kept by background, a frame flies here:
// {type:"bell_log", events}. SK-50 (v0.7): OPENING the journal reads nothing —
// the human opened "look". Read-marking: a click on a specific event;
// "mark all read" — an explicit footer button. Task rings are cleared background-side
// background; "mark all read" does NOT clear perm rings (they live until the verdict).
// Manual collapse: window.close() from THIS document closes the panel without
// a gesture (firefox-158 probe); the schema rejects sidebarAction.close() without a gesture.
const bellBtn = document.getElementById("bellBtn"), belln = document.getElementById("belln"),
      bellpanel = document.getElementById("bellpanel"), bellrows = document.getElementById("bellrows"),
      bellClose = document.getElementById("bellClose"), bellReadAll = document.getElementById("bellReadAll"),
      collapseBtn = document.getElementById("collapseBtn");
let BELL_LOG = [];
const BELL_KIND_ICO = { task_done: ["#ic-hand", "k-done"], task_failed: ["#ic-close", "k-failed"], perm: ["#ic-bell", "k-perm"] };
function bellCount() { return BELL_LOG.filter((e) => e && !e.read).length; }
function bellRender() {
  const n = bellCount();
  belln.hidden = n === 0;
  belln.textContent = n > 9 ? "9+" : String(n);
  if (bellpanel.hidden) return; // rows are redrawn only while the portal is open
  if (bellReadAll) bellReadAll.disabled = n === 0; // nothing to read — the button is not needed
  if (!BELL_LOG.length) { bellrows.replaceChildren(Object.assign(document.createElement("div"), { className: "bempty", textContent: L("sb.empty_chats") })); return; }
  bellrows.replaceChildren(...BELL_LOG.map((e) => {
    const b = document.createElement("button");
    const [ico, kcls] = BELL_KIND_ICO[e.kind] || ["#ic-bell", ""];
    b.className = "brow " + kcls + (e.read ? "" : " unread");
    b.dataset.id = String(e.id || "");
    b.dataset.kind = String(e.kind || "");
    const ic = document.createElement("span"); ic.className = "bic";
    ic.innerHTML = '<svg class="icn" aria-hidden="true"><use href="' + ico + '"/></svg>';
    const tx = document.createElement("span"); tx.className = "btx";
    const t1 = document.createElement("span"); t1.textContent = e.key ? L(e.key) : String(e.text || ""); // SK-71: journal rows localize from the entry key
    tx.append(t1);
    if (e.sub) { const s = document.createElement("span"); s.className = "bsub"; s.textContent = String(e.sub); tx.append(s); }
    const tm = document.createElement("span"); tm.className = "btime";
    tm.textContent = e.ts ? new Date(e.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
    b.append(ic, tx, tm);
    b.onclick = () => bellEventClick(e);
    return b;
  }));
}
function bellPanelOpen(on) {
  bellpanel.hidden = !on;
  bellBtn.setAttribute("aria-expanded", on ? "true" : "false");
  if (on) {
    // SK-50: opening the journal does NOT mark everything read — "look" ≠
    // "dispose". Read-marking: a click on a specific event, or the explicit button
    // "Mark all read" in the footer. The journal has no auto-close timers and
    // must not exist (live check 25.10: "it closes after 10s" — that was
    // PANEL collapse by the LONG_MS heuristic; SK-51 removed it — the panel collapses on
    // only the agent's explicit long:true or the header button).
    bellRender();
    bellClose.focus();
  }
}
bellBtn.onclick = () => bellPanelOpen(bellpanel.hidden);
bellClose.onclick = () => { bellPanelOpen(false); bellBtn.focus(); };
if (bellReadAll) bellReadAll.onclick = () => {
  // Explicit "mark all read" (SK-50): counter to zero, TASK rings cleared,
  // perm rings live until the verdict — bellAckIds background-side touches
  // task_done/task_failed only.
  try { port.postMessage({ type: "bell_read" }); } catch (e) { /* the bg bell blinked — the counter survives to the next frame */ }
};
document.addEventListener("mousedown", (e) => {
  if (!bellpanel.hidden && !bellpanel.contains(e.target) && !bellBtn.contains(e.target)) bellPanelOpen(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !bellpanel.hidden) { e.preventDefault(); bellPanelOpen(false); bellBtn.focus(); }
});
// Clicking a journal event: clear read state and show the human the source.
function bellEventClick(ev) {
  if (!ev || !ev.id) return;
  try { port.postMessage({ type: "bell_read_id", id: String(ev.id) }); } catch (e) {}
  if (String(ev.kind || "").startsWith("task_")) {
    const chat = String(ev.chat || "");
    if (chat && chat !== activeChat) { switchChat(chat); setTimeout(() => flashTaskCard(ev.id), 600); return; }
    flashTaskCard(ev.id);
  } else bellFocus(String(ev.id), "perm");
  bellRender();
}
// Manual panel collapse (a human gesture — and window.close() works without a gesture too
// works, verified by a probe: the panel closes, the window lives).
collapseBtn.onclick = () => window.close();
function flashTaskCard(id) {
  const c = TASK_CARDS.get(String(id));
  const el = c && c.card;
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
}

// SK-30: theme — system → light → dark (html[data-theme] overrides
// prefers-color-scheme in theme.css; the choice lives in storage.local).
const THEME_KEY = "sk30Theme";
const THEME_MODES = ["system", "light", "dark"];
const THEME_LBL = { system: "sb.theme_sys", light: "sb.theme_light", dark: "sb.theme_dark" };
let THEME = "system";
function applyTheme(mode) {
  THEME = THEME_MODES.includes(mode) ? mode : "system";
  if (THEME === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = THEME;
  themeLbl.textContent = L(THEME_LBL[THEME]);
}
themeBtn.onclick = () => {
  applyTheme(THEME_MODES[(THEME_MODES.indexOf(THEME) + 1) % THEME_MODES.length]);
  browser.storage.local.set({ [THEME_KEY]: THEME });
};
function answer(id, choice) {
  port.postMessage({ type: "perm_answer", id, choice }); // background forwards + greys out
}
// SK-34 p.4: deny-reason chips in one tap (audit 21.10 p.8: 16 denies without a reason —
// future audits must understand what the human meant). The keys are stable —
// audit.jsonl writes them, the translation lives only in UI/audit.js.
const DENY_CHIPS = [["wrong_target", "sb.hotkey_wrong_btn"], ["wrong_field", "sb.hotkey_wrong_field"],
  ["stop_task", "sb.hotkey_stop"]]; // SK-71: label = dictionary key
function addPermAsk(m) {
  if (PERM_BLOCKS.has(m.id)) return; // reconnect re-push must not duplicate
  const d = permCard(m);
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  PERM_BLOCKS.set(m.id, d);
}
// SK-71: builder — the card is rebuilt from m on a language switch; the shot
// lives in m (a late perm_shot frame updates it via renderPermShot).
function permCard(m) {
  const d = document.createElement("div"); d.className = "m perm"; d.dataset.id = m.id; d._m = m;
  const why = String(m.reason || "").split("+").map((r) => L(REASON_RU[r] || r)).filter(Boolean).join(" · ");
  const hd = document.createElement("div"); hd.className = "hd";
  hd.textContent = L("sb.perm_asks", { what: L(ACTION_RU[m.action] || m.action) });
  const sub = document.createElement("div"); sub.className = "sub";
  // SK-37: the target name (m.name — where exactly the agent's hand looks) — the domain alone is not enough,
  // the human must see the SUBJECT, not just the site.
  sub.textContent = [(m.domain || L("sb.perm_site_unknown")), m.name || "", why, m.hint || "", m.url || ""].filter(Boolean).join(" — ");
  d.append(hd, sub);
  renderPermShot(d, m.shot); // SK-37: tab-frame thumbnail (arrives as a separate frame if not yet here)
  const btns = document.createElement("div"); btns.className = "btns";
  // SK-34 p.1: "Always allow for {domain}" — only when the bridge sent
  // show_always (the first "once" for this type already happened; on the sensitive path
  // the flag is never there — the bridge drops it and demotes "always" to once).
  const opts = [["once", L("sb.perm_allow_once"), "ok"]];
  if (m.show_always && m.domain)
    opts.push(["always", L("sb.perm_allow_always", { site: m.domain }), "alw"]);
  opts.push(["deny", L("sb.perm_deny"), "no"]);
  for (const [choice, label, cls] of opts) {
    const b = document.createElement("button"); b.className = cls; b.textContent = label;
    b.onclick = () => answer(m.id, choice);
    btns.appendChild(b);
  }
  d.append(btns);
  return d;
}
// SK-37: task-frame thumbnail inside the permission card. The bridge brings the frame as a separate
// a perm_shot frame (capture is async, the card does not wait) — or a replay of the parked
// the frame for a late sidebar. An empty shot/capture failure — a card with no image,
// the verdicts stay the same.
function renderPermShot(d, shot) {
  if (!shot || !String(shot).startsWith("data:")) return;
  let img = d.querySelector("img.pshot");
  if (!img) {
    img = document.createElement("img"); img.className = "pshot"; img.alt = L("sb.perm_shot");
    const sub = d.querySelector(".sub");
    sub ? sub.after(img) : d.prepend(img);
  }
  if (img.getAttribute("src") !== shot) img.src = shot;
}
// SK-37: clicking a notification — background already opened the sidebar; scroll
// to the card and highlight it (the flash is re-created by reflow: a second click triggers
// restart the animation).
// SK-35: a task ring lives in the task's chat — switch to it if needed and
// we highlight the card after its feed arrives (replay + chats_data — a round trip).
function bellFocus(id, kind, chat) {
  if (String(kind || "").startsWith("task_")) {
    if (chat && chat !== activeChat) { switchChat(chat); setTimeout(() => flashTaskCard(id), 600); return; }
    flashTaskCard(id);
    return;
  }
  const d = (kind === "handoff" ? HO_BLOCKS.get(id) : PERM_BLOCKS.get(id))
    || PERM_BLOCKS.get(id) || HO_BLOCKS.get(id);
  if (!d) return;
  d.scrollIntoView({ behavior: "smooth", block: "center" });
  d.classList.remove("flash"); void d.offsetWidth; d.classList.add("flash");
}
function markResolved(id, choice) {
  const d = PERM_BLOCKS.get(id); if (!d || d.classList.contains("done")) return;
  d.classList.add("done");
  const label = { once: L("sb.perm_res_once"), always: L("sb.perm_res_always"),
    deny: L("sb.perm_res_deny"), timeout: L("sb.perm_res_timeout") }[choice] || L("sb.perm_res_sent");
  const btns = d.querySelector(".btns");
  const res = Object.assign(document.createElement("span"), { className: "res" });
  setGlyphText(res, label); // SK-30: ✓/✗/⏱ → SVG candidates
  btns.replaceChildren(res);
  if (choice !== "deny") return;
  // SK-34 p.4: the verdict is already sent (the agent is unfrozen), the reason — on the next tap.
  // "stop task" — not only a reason: it is also ■ Stop (chip = deny + stop).
  for (const [reason, text] of DENY_CHIPS) {
    const c = document.createElement("button");
    c.className = "chip"; c.textContent = L(text); // SK-71: chip label = dictionary key
    c.onclick = () => {
      port.postMessage({ type: "perm_reason", id, reason });
      if (reason === "stop_task") port.postMessage({ type: "stop", task: TL.task });
      btns.replaceChildren(Object.assign(document.createElement("span"),
        { className: "res", textContent: label.replace(/^\S+\s*/, "") + L("sb.perm_reason", { why: L(text) }) }));
    };
    btns.appendChild(c);
  }
}
// ---- SK-08: hand-off "you're needed". The agent is blocked on the bridge (hub/paused —
// page.*/previews are refused there); the human acts in the browser, then presses the
// one green button. Never a modal; the block lives in the chat feed and survives a
// late sidebar open via the background's PENDING_HO replay.
function addHandoff(m) {
  // SK-48: a re-push of the same hand-off after a failed expect-verification — do NOT duplicate the block,
  // we update the existing one: note (what is wrong), frame (what was on screen), attempt,
  // live button. The first show — the normal path.
  const old = HO_BLOCKS.get(m.id);
  if (old) {
    // SK-48: a re-push of the same hand-off after a failed expect-verification — do NOT duplicate the block,
    // we update the existing one: note (what is wrong), frame (what was on screen), attempt,
    // live button. Background optimistically hides the banner on the "Done" click (as if the hand-off closed) —
    // a re-push with a note means "the bridge did not confirm the target", and the block revives.
    if (old.classList.contains("done") && !(m.attempt || m.note || m.shot)) return;
    old.classList.remove("done");
    renderHo(old, m);
    log.scrollTop = log.scrollHeight;
    return;
  }
  const d = document.createElement("div"); d.className = "m ho"; d.dataset.id = m.id;
  HO_BLOCKS.set(m.id, d);
  renderHo(d, m);
  log.appendChild(d); log.scrollTop = log.scrollHeight;
}
function renderHo(d, m) {
  d.replaceChildren();
  d._m = m; // SK-71: rebuild source for the language switch
  const hd = document.createElement("div"); hd.className = "hd";
  setGlyphText(hd, L("sb.ho_title") + (m.attempt ? L("sb.ho_attempt", { n: +m.attempt + 1 }) : ""));
  const tx = document.createElement("div"); tx.className = "tx2";
  tx.textContent = m.what || L("sb.ho_hint");
  const sub = document.createElement("div"); sub.className = "sub";
  sub.textContent = [m.url || "", m.task ? L("sb.ho_task", { task: m.task }) : ""].filter(Boolean).join(" — ");
  d.append(hd, tx);
  if (sub.textContent) d.append(sub);
  if (m.note) { // SK-48: the bridge checked the expected state — did not find it; the human sees why
    const nt = Object.assign(document.createElement("div"), { className: "note" });
    setGlyphText(nt, "⚠ " + m.note);
    d.append(nt);
  }
  if (typeof m.shot === "string" && m.shot.startsWith("data:image/")) {
    const im = Object.assign(document.createElement("img"), { className: "shot", alt: L("sb.ho_shot") });
    im.src = m.shot;
    d.append(im);
  }
  const b = document.createElement("button"); b.textContent = L("sb.ho_done");
  b.onclick = () => {
    // SK-48: until the bridge verdict (a cheap page read) the button sleeps: "Done" —
    // a verification request, not a switch; an offline click re-animates it (markHoOffline)
    b.disabled = true; b.textContent = L("sb.ho_checking");
    port.postMessage({ type: "resume", id: m.id, task: m.task || TL.task });
  };
  d.append(b);
}
function markHoResolved(id, choice) {
  const d = HO_BLOCKS.get(id); if (!d || d.classList.contains("done")) return;
  d.classList.add("done");
  const label = { resumed: L("sb.ho_res_done"),
    timeout: L("sb.ho_res_timeout"),
    superseded: L("sb.ho_res_replaced") }[choice] || L("sb.ho_res_closed");
  const b = d.querySelector("button");
  if (b) {
    const res = Object.assign(document.createElement("span"), { className: "res" });
    setGlyphText(res, label); // SK-30: ✓/⏱/↪ → SVG candidates
    b.replaceWith(res);
  }
  const w = d.querySelector(".warn"); if (w) w.remove();
}
function markHoOffline(id) { // the click never reached the bridge — keep the button live, say so
  const d = HO_BLOCKS.get(id); if (!d || d.classList.contains("done")) return;
  const b = d.querySelector("button"); // SK-48: "Checking…" with no bridge answer = it was never pressed
  if (b && b.disabled) { b.disabled = false; b.textContent = L("sb.ho_done"); }
  if (d.querySelector(".warn")) return;
  const w = Object.assign(document.createElement("div"), { className: "warn" });
  setGlyphText(w, L("sb.ho_offline"));
  d.appendChild(w);
}
// ---- SK-10: saved prompts («/»-menu). Storage is storage.local (sk10Skills) — this is
// NOT grants (SK-04: permission decisions live on the bridge); it is the human's own
// prompt library, so it lives entirely in the sidebar. Pure logic: skills.js.
const skMenu = document.getElementById("skmenu"), skList = document.getElementById("sklist"),
      skHint = document.getElementById("skhint"), skSaveBtn = document.getElementById("sksave"),
      skName = document.getElementById("skname"), skNameIn = document.getElementById("sknamein"),
      skNameOk = document.getElementById("sknameok"), skNameCancel = document.getElementById("sknamecancel"),
      skIoBtn = document.getElementById("skioBtn"), skIo = document.getElementById("skio"),
      skExp = document.getElementById("skexp"), skImpr = document.getElementById("skimpr"),
      skFile = document.getElementById("skfile"), skPaste = document.getElementById("skpaste"),
      skPasteOk = document.getElementById("skpasteok"), skIoMsg = document.getElementById("skioMsg");
let SKILLS = [], SKSHOWN = [], SKHL = 0;
browser.storage.local.get("sk10Skills").then((m) => {
  SKILLS = (Array.isArray(m.sk10Skills) ? m.sk10Skills : []).filter((s) => s && s.name && s.text);
  skRender(); // a «/» typed before storage answered refreshes too (skRender is a no-op while closed)
});
function skPersist() { browser.storage.local.set({ sk10Skills: SKILLS }); }
function skRender() {
  const q = Skills.slashQuery(inp.value);
  if (q === null) { skMenu.hidden = true; return; } // no leading «/» — nothing to show
  skMenu.hidden = false;
  SKSHOWN = Skills.filter(SKILLS, q);
  if (SKHL >= SKSHOWN.length) SKHL = 0;
  skList.replaceChildren(...SKSHOWN.map((s, i) => {
    const row = document.createElement("div");
    row.className = "sk" + (i === SKHL ? " hl" : ""); row.dataset.i = i;
    const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = s.name;
    const pv = document.createElement("span"); pv.className = "pv"; pv.textContent = s.text;
    row.append(nm, pv);
    // mousedown (not click): preventDefault keeps the caret in the composer
    row.addEventListener("mousedown", (e) => { e.preventDefault(); skPick(i); });
    return row;
  }));
  skHint.textContent = SKSHOWN.length
    ? SKILLS.length + L("sb.sk_saved_hint")
    : (SKILLS.length ? L("sb.sk_no_match") : L("sb.sk_empty"));
}
function skPick(i) {
  const s = SKSHOWN[i]; if (!s) return;
  inp.value = s.text; skRender(); inp.focus(); // skRender closes: the value no longer starts with «/»
  inp.setSelectionRange(inp.value.length, inp.value.length);
}
inp.addEventListener("input", skRender);
skSaveBtn.onclick = () => {
  if (!inp.value.trim()) { inp.focus(); return; }
  skName.hidden = false; skNameIn.value = ""; skNameIn.focus();
};
function skConfirm() {
  const r = Skills.upsert(SKILLS, skNameIn.value, inp.value, Date.now());
  if (!r.ok) { skNameIn.focus(); return; }
  SKILLS = r.list; skPersist(); skName.hidden = true;
  add("sys", (r.action === "updated" ? L("sb.sk_updated", { name: r.name }) : L("sb.sk_saved", { name: r.name })) + L("sb.sk_slash"));
}
skNameOk.onclick = skConfirm;
skNameCancel.onclick = () => { skName.hidden = true; inp.focus(); };
skNameIn.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); skConfirm(); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); skName.hidden = true; inp.focus(); } // SK-38: Esc in the skill-name field — cancel only, not stop
});
skIoBtn.onclick = () => { skIo.hidden = !skIo.hidden; skIoMsg.textContent = ""; };
skExp.onclick = () => { // blob download — the file is the export format (import accepts it back)
  const blob = new Blob([Skills.exportJson(SKILLS)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "sidekick-skills.json";
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  skIoMsg.textContent = L("sb.sk_unloaded", { n: SKILLS.length });
};
function skApplyImport(txt) {
  const r = Skills.importJson(txt, SKILLS);
  skIoMsg.textContent = r.ok
    ? L("sb.sk_imported", { added: r.added, updated: r.updated }) + (r.skipped ? L("sb.sk_skipped", { n: r.skipped }) : "")
    : L("sb.sk_import_fail", { why: r.err });
  if (r.ok) { SKILLS = r.list; skPersist(); }
}
skImpr.onclick = () => skFile.click();
skFile.onchange = () => {
  const f = skFile.files && skFile.files[0]; if (!f) return;
  const r = new FileReader();
  r.onload = () => skApplyImport(String(r.result));
  r.readAsText(f); skFile.value = "";
};
skPasteOk.onclick = () => { skApplyImport(skPaste.value); skPaste.value = ""; };
// ---- SK-11: audit feed (second view). The bridge owns audit.jsonl; the sidebar pulls
// it through the background (WS audit_get -> audit_data) while the panel is open: on
// open, on ⟳, on filter change (debounced) and every 2 s while visible. The footer
// shows «shown / total in file» so the human can cross-check the feed against the
// jsonl (card: "the feed is complete — compare with jsonl"). Lines are re-masked locally
// (Audit.mask) — defense in depth under the bridge's own read-time masking.
const audBtn = document.getElementById("audBtn"), aud = document.getElementById("aud"),
      audF = document.getElementById("audf"), audRef = document.getElementById("audref"),
      audRawBtn = document.getElementById("audrawBtn"), audClose = document.getElementById("audclose"),
      audList = document.getElementById("audlist"), audRaw = document.getElementById("audraw"),
      audN = document.getElementById("audn");
let AUD = { open: false, raw: false, lines: [], total: 0, truncated: false, err: "" };
let AUDREQ = "", AUDQ = "", AUDTIMER = null, AUDDEB = null;
function audFetch() {
  if (!AUD.open || !port) return;
  AUDREQ = "aud" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  try { port.postMessage({ type: "audit_get", id: AUDREQ, limit: 500, domain: AUDQ }); }
  catch (e) { AUD.err = L("sb.bridge_offline"); audRender(); } // port died between reconnects
}
function audRender() {
  const lines = (Array.isArray(AUD.lines) ? AUD.lines : []).map(Audit.mask);
  if (AUD.raw) { // "raw file": the same tail monospace, masked twice over
    audList.hidden = true; audRaw.hidden = false;
    audRaw.textContent = lines.length ? lines.join("\n") : L("sb.chat_empty_marker");
  } else {
    audRaw.hidden = true; audList.hidden = false;
    audList.replaceChildren(...lines.map((ln) => {
      const e = Audit.parseLine(ln);
      const d = document.createElement("div");
      d.className = "arow " + e.tone; d.title = e.sub || e.title;
      const t = document.createElement("span"); t.className = "at"; t.textContent = e.time;
      const c = document.createElement("span"); c.className = "aic";
      // SK-30: audit.js returns a stable icon key ("lock"/"eye"/…) — we draw
      // SVG from the sprite; an unknown key honestly stays text.
      if (e.icon && /^[a-z]+$/.test(e.icon)) c.appendChild(svgIcon(e.icon));
      else c.textContent = e.icon;
      const x = document.createElement("span"); x.className = "atx"; x.textContent = e.title;
      d.append(t, c, x);
      if (e.domain) { const m = document.createElement("span"); m.className = "adm"; m.textContent = e.domain; d.append(m); }
      if (e.sub) { const s = document.createElement("span"); s.className = "asub"; s.textContent = e.sub; d.append(s); }
      return d;
    }));
  }
  audN.textContent = (AUD.err ? AUD.err + " · " : "")
    + L("sb.aud_shown", { n: lines.length, total: AUD.total || 0 })
    + (AUD.truncated ? L("sb.aud_tail") : "")
    + (AUDQ ? L("sb.aud_filter", { q: AUDQ }) : "");
}
function audOpen(on) {
  if (on && HIST.open) histOpen(false); // SK-23: second views are exclusive — one at a time
  if (on) menuOpen(false); // SK-30: opening the second view closes the "⋯" menu
  AUD.open = on;
  aud.hidden = !on;
  audBtn.classList.toggle("on", on);
  log.hidden = on || HIST.open; // the second view: the chat log steps aside, the trail takes its place
  if (on) { audFetch(); AUDTIMER = setInterval(() => { if (!document.hidden) audFetch(); }, 2000); }
  else if (AUDTIMER) { clearInterval(AUDTIMER); AUDTIMER = null; }
  syncEmpty(); // SK-40: the second view hides the empty state; closing brings it back
}
audBtn.onclick = () => audOpen(!AUD.open);
audClose.onclick = () => audOpen(false);
audRef.onclick = audFetch;
audRawBtn.onclick = () => { AUD.raw = !AUD.raw; audRawBtn.classList.toggle("on", AUD.raw); audRender(); };
audF.addEventListener("input", () => { // the filter runs on the BRIDGE over the whole
  clearTimeout(AUDDEB);                // file (a rare domain survives a busy tail window)
  AUDDEB = setTimeout(() => { AUDQ = audF.value.trim(); audFetch(); }, 300);
});

// ---- SK-23: chat history. The active chat_id lives in storage.local (survives an
// add-on reload); the conversation itself lives on the BRIDGE (chats.jsonl) and is
// pulled over WS chats_get — so "history and the active chat are in place" after a reload.
// «＋» = new chat (clean feed; agentd has no session for it → fresh hermes session);
// «🕘» = the history list (title, date, last utterance; the current chat is marked).
// Frames without a chat field belong to "default" (p.4 compatibility).
const newBtn = document.getElementById("newBtn"), histBtn = document.getElementById("histBtn"),
      hist = document.getElementById("hist"), histList = document.getElementById("histlist"),
      histRef = document.getElementById("histref"), histClose = document.getElementById("histclose"),
      histN = document.getElementById("histn");
const CHAT_KEY = "sk23Chat";
let activeChat = "default";
let HIST = { open: false, chats: [], err: "", req: "" };
let CHATREQ = { id: "", chat: "" }; // pull in flight: {id, chat} — a stale answer must not paint
const inChat = (m) => ((m && m.chat) || "default") === activeChat;
function fmtDate(ts) { // "DD.MM HH:MM" for history rows; "" for unknown
  if (!ts) return "";
  const d = new Date(ts * 1000), p = (x) => String(x).padStart(2, "0");
  return p(d.getDate()) + "." + p(d.getMonth() + 1) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}
function clearFeed() { // chat switch: the feed is per-chat; global blocks come back via replay
  log.replaceChildren();
  TASK_CARDS.clear(); THINK_BLOCKS.clear(); PERM_BLOCKS.clear(); HO_BLOCKS.clear();
  for (const k of Object.keys(STEP_TIMES)) delete STEP_TIMES[k];
  syncEmpty(); // SK-40: a clean feed honestly shows the empty state
}
function loadChat(cid) { // pull this chat's feed from the bridge (chats_get with chat_id)
  CHATREQ = { id: "cht" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), chat: cid };
  try { port.postMessage({ type: "chats_get", id: CHATREQ.id, chat_id: cid }); }
  catch (e) { CHATREQ = { id: "", chat: "" }; } // bridge offline — an empty feed is honest
}
function switchChat(cid) {
  activeChat = cid;
  browser.storage.local.set({ [CHAT_KEY]: cid });
  clearFeed();
  setChatTitle(""); // SK-38: before the chat feed arrives the header honestly says "New chat"
  loadChat(cid);
  port.postMessage({ type: "replay" }); // pending perm/handoff/tl re-render into the fresh feed
  if (HIST.open) histRender();
}
newBtn.onclick = () => { // a clean feed + a new Hermes session (no entry in sessions.json — no resume)
  switchChat("c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6));
  inp.focus();
};
function histRender() {
  histList.replaceChildren(...HIST.chats.map((c) => {
    const row = document.createElement("div");
    row.className = "hrow" + (c.id === activeChat ? " cur" : "");
    const tx = document.createElement("div"); tx.className = "htx";
    const t = document.createElement("span"); t.className = "ht";
    t.textContent = c.title || L("sb.chat_untitled"); t.title = c.title || c.id;
    const dt = document.createElement("span"); dt.className = "hdate"; dt.textContent = fmtDate(c.ts);
    tx.append(t, dt);
    if (c.id === activeChat) {
      const mk = document.createElement("span"); mk.className = "hcur"; setGlyphText(mk, L("sb.chat_current"));
      tx.append(mk);
    }
    const last = document.createElement("div"); last.className = "hlast";
    last.textContent = c.last ? (c.last.role === "agent" ? "Hermes: " : L("sb.chat_you")) + c.last.text : "";
    row.append(tx, last);
    row.onclick = () => { switchChat(c.id); histOpen(false); };
    return row;
  }));
  histN.textContent = (HIST.err ? HIST.err + " · " : "")
    + L("sb.chat_count", { n: HIST.chats.length });
}
function histFetch() {
  if (!HIST.open || !port) return;
  HIST.req = "hst" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  try { port.postMessage({ type: "chats_get", id: HIST.req }); }
  catch (e) { HIST.err = L("sb.bridge_offline"); histRender(); } // port died between reconnects
}
function histOpen(on) {
  if (on && AUD.open) audOpen(false); // second views are exclusive — one at a time
  if (on) menuOpen(false); // SK-30
  HIST.open = on;
  hist.hidden = !on;
  histBtn.classList.toggle("on", on);
  log.hidden = on || AUD.open;
  if (on) histFetch();
  syncEmpty(); // SK-40
}
histBtn.onclick = () => histOpen(!HIST.open);
histClose.onclick = () => histOpen(false);
histRef.onclick = histFetch;

// ---- SK-38 p.1: chat title in the header — first request, truncated with "…", empty
// chat — "New chat". Hard-cut to 60 chars (otherwise a giant first request
// would move into both the DOM and the title tooltip), CSS ellipsis — the narrow-panel fallback.
let chatTitle = "";
function setChatTitle(t) {
  chatTitle = String(t == null ? "" : t).replace(/\s+/g, " ").trim();
  const cut = chatTitle.length > 60 ? chatTitle.slice(0, 60).trimEnd() + "…" : chatTitle;
  ttl.textContent = cut || L("sb.chat_new");
  ttl.title = chatTitle;
}
function submit() {
  const t = inp.value.trim(); if (!t) return;
  if (!chatTitle) setChatTitle(t); // SK-38: header = the chat's first request
  port.postMessage({ type: "chat", text: t, chat: activeChat }); inp.value = "";
}
// SK-38 p.4: one button, two states (morph via CSS, class .running).
// Running — the click is ■ stop (same stop frame as the menu item and Esc). Enter in
// the field stays "send" even during a task (parallel tasks SK-25 are alive).
go.onclick = () => {
  if (GO_RUN) { port.postMessage({ type: "stop", task: TL.task }); return; }
  submit();
};
// Enter is the SK-10 fork: while the «/»-menu is open it belongs to the menu
// (insert the highlighted prompt), only a closed menu submits.
inp.addEventListener("keydown", (e) => {
  if (!skMenu.hidden) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (SKSHOWN.length) { SKHL = (SKHL + (e.key === "ArrowDown" ? 1 : -1) + SKSHOWN.length) % SKSHOWN.length; skRender(); }
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); skMenu.hidden = true; return; } // SK-38: Esc in the skill menu — close only, not stop
    if (e.key === "Enter") { e.preventDefault(); if (SKSHOWN.length) skPick(SKHL); return; }
  }
  if (e.key === "Enter") submit();
});
// ---- SK-38 p.5: live composer placeholder — phrase rotation with fade (150ms CSS,
// the #phin overlay above the empty field). Paused on focus (the ticking stopper is untouched
// visible phrase) and while typing (the overlay is hidden while text is present). prefers-reduced-motion
// → no rotation, no overlay: the static first phrase in the native placeholder.
const phin = document.getElementById("phin");
const PH_KEYS = ["sb.composer_ph", "sb.chip_tabs", "sb.chip_trash",
  "sb.chip_youtube", "sb.chip_group", "sb.chip_explain"]; // SK-71: dictionary keys
const PH_MS = 3200, PH_FADE = 170; // fade 150ms (CSS) + slack for the text swap
const RMOTION = matchMedia("(prefers-reduced-motion: reduce)");
let PHI = 0, PHT = null;
function phTick() {
  if (document.activeElement === inp) return; // paused on focus — the phrase stands still
  phin.classList.add("off");
  setTimeout(() => {
    if (document.activeElement !== inp) { PHI = (PHI + 1) % PH_KEYS.length; phin.textContent = L(PH_KEYS[PHI]); }
    phin.classList.remove("off");
  }, PH_FADE);
}
function phSync() { phin.hidden = !!inp.value || RMOTION.matches; }
function phStart() {
  if (RMOTION.matches) { // static mode: the overlay is off, the native placeholder speaks
    if (PHT) { clearInterval(PHT); PHT = null; }
    inp.placeholder = L(PH_KEYS[0]); phin.hidden = true;
    return;
  }
  inp.placeholder = ""; // native one is silent: the overlay paints the live phrase
  phin.textContent = L(PH_KEYS[PHI]);
  phSync();
  if (!PHT) PHT = setInterval(phTick, PH_MS);
}
inp.addEventListener("input", phSync); // paused/hidden while typing
if (RMOTION.addEventListener) RMOTION.addEventListener("change", phStart);
// SK-71: старт ротации — после загрузки словаря, иначе первый paint
// рисует сырые ключи ("sb.composer_ph") до первой загрузки L()
I18N.load().then(phStart);
// SK-13: the «bridge: …» footer became a status DOT in the header — green = online,
// amber = the human must fix something (token/protocol), red = offline/reconnecting,
// gray = nothing seen yet. The full text lives in the dot's tooltip (title).
// SK-28: retry (◌ address cycling) — amber; offline (✗ no address
// answers) — red with an instruction; the address is mixed into the verdict so the human
// sees WHERE exactly it fails (the "Server" field in settings).
const BRIDGE_DOT = { online: "online", retry: "warn", offline: "err", no_token: "warn",
  token_rejected: "warn", proto_mismatch: "warn" };
const BRIDGE_HINT = { online: "sb.bridge_online", retry: "sb.bridge_retry",
  offline: "sb.bridge_offline_hint", no_token: "sb.bridge_no_token",
  token_rejected: "sb.bridge_token_rejected", proto_mismatch: "sb.bridge_proto" };
let LAST_BRIDGE = null; // SK-71: repaint the dot tooltip on a language switch
function setBridge(state, url) {
  LAST_BRIDGE = { state, url };
  st.dataset.s = BRIDGE_DOT[state] || "idle";
  st.title = (BRIDGE_HINT[state] ? L(BRIDGE_HINT[state]) : ("bridge: " + state))
    + (url && state !== "online" ? " · " + url : "");
}
// ---- SK-21/SK-22: think stream from sidekick-agentd. Since SK-22 a full block:
// one per task, collapsed by default ("Thinking…"), a click expands it
// the scroll container; after the final — "Thought (N sec)". Auto-scroll to the tail
// only when expanded — the chat feed is not jerked around by reasoning. Logic in feed.js.
const THINK_BLOCKS = new Map(); // task -> think block state (Feed)
function addThink(m) {
  if (!m.text) return;
  let b = THINK_BLOCKS.get(m.task);
  if (!b) {
    b = Feed.buildThink(document, m.task);
    THINK_BLOCKS.set(m.task, b);
    log.appendChild(b.box);
  }
  Feed.thinkDelta(b, m.text, Date.now());
}
function thinkDone(task) { // the final answer/terminal frame — the block is no longer "thinking"
  const b = THINK_BLOCKS.get(task);
  if (b) Feed.thinkFinish(b, Date.now());
}
function connect() {
  port = browser.runtime.connect({ name: "sidebar" });
  port.onMessage.addListener((m) => {
    // SK-23: chat/think frames belong to one chat — while another chat is open they
    // must not paint over its feed (frames without chat = "default", p.4 compat).
    if (m.type === "chat") { if (!inChat(m)) return; add(m.role === "me" ? "me" : "hm", m.text, m.memory === true); if (m.task) thinkDone(m.task); } // SK-31: memory flag — the "🗑 from memory" glyph
    else if (m.type === "think") { if (inChat(m)) addThink(m); } // SK-21 stub
    else if (m.type === "tl") { TL = m; renderTL();
      // SK-21: a terminal task frame also closes "thinking…" — if the chat frame
      // got lost (a WS blink of the extension), the title never sticks forever
      if (m.state === "done" || m.state === "failed") thinkDone(m.task);
    } // SK-06 snapshot (task state + steps)
    else if (m.type === "agent_state") { AST = m.state || "unknown"; } // SK-38: the header paints no status (the badge is background-side, bell — SK-37)
    else if (m.type === "perm_ask") addPermAsk(m);
    else if (m.type === "perm_resolved") markResolved(m.id, m.choice);
    else if (m.type === "perm_shot") { // SK-37: the thumbnail caught up with the card
      const d = PERM_BLOCKS.get(m.id);
      if (d && !d.classList.contains("done")) renderPermShot(d, m.shot);
    }
    else if (m.type === "bell_focus") bellFocus(m.id, m.kind, m.chat); // SK-37: clicking a notification — show the card; SK-35: chat — the task's chat
    else if (m.type === "bell_log") { BELL_LOG = Array.isArray(m.events) ? m.events : []; bellRender(); } // SK-35: the header journal
    else if (m.type === "sidebar_collapse") { window.close(); } // SK-35: a long task started working — the panel collapses (window.close() from this document works without a gesture, firefox-158 probe)
    else if (m.type === "handoff") addHandoff(m); // SK-08
    else if (m.type === "handoff_resolved") markHoResolved(m.id, m.choice);
    else if (m.type === "handoff_offline") markHoOffline(m.id);
    else if (m.type === "audit_data") { // SK-11: our pull's answer (other sidebars' ids pass by)
      if (m.id !== AUDREQ || !AUD.open) return;
      AUD.lines = Array.isArray(m.lines) ? m.lines : [];
      AUD.total = Number(m.total) || 0; AUD.truncated = !!m.truncated; AUD.err = "";
      audRender();
    }
    else if (m.type === "audit_err") { // background could not reach the bridge
      if (m.id !== AUDREQ || !AUD.open) return;
      AUD.err = L("sb.bridge_offline"); audRender();
    }
    else if (m.type === "chats_data") { // SK-23: answers to OUR pulls (history list /
      // chat feed). A chat-feed answer that raced a chat switch is dropped — the new
      // chat's own pull is already in flight. The feed renders, then replays the
      // global blocks (task card, pending asks) BELOW the loaded history.
      if (m.id === HIST.req && HIST.open) {
        HIST.chats = Array.isArray(m.chats) ? m.chats : []; HIST.err = "";
        histRender();
      } else if (m.id === CHATREQ.id && m.chat) {
        const stale = CHATREQ.chat !== activeChat;
        CHATREQ = { id: "", chat: "" };
        if (!stale) {
          clearFeed();
          const msgs = Array.isArray(m.chat.msgs) ? m.chat.msgs : [];
          for (const msg of msgs)
            add(msg.role === "user" ? "me" : "hm", msg.text);
          // SK-38 p.1: chat title = the first user request (the same
          // source, like the title in chats_records on the bridge)
          const firstUser = msgs.find((x) => x.role === "user" && String(x.text || "").trim());
          setChatTitle(firstUser ? firstUser.text : "");
          port.postMessage({ type: "replay" });
        }
      }
    }
    else if (m.type === "chats_err") { // background could not reach the bridge
      if (m.id === HIST.req && HIST.open) { HIST.err = L("sb.warn_offline"); histRender(); }
      else if (m.id === CHATREQ.id) CHATREQ = { id: "", chat: "" }; // startup load — empty feed is honest
    }
    else if (m.type === "bridge") {
      setBridge(m.state, m.url); // SK-28: url — where exactly it fails (tooltip on retry/offline)
    }
  });
  port.onDisconnect.addListener(() => { setBridge("offline"); setTimeout(connect, 1200); });
}
// (SK-10) submit()/go.onclick and the Enter fork moved up to the composer section —
// the old unconditional Enter-submits listener is gone: with the «/»-menu open, Enter inserts.
// SK-38: the header starts honestly ("New chat" in the markup, no chips) — the status
// the header render is removed, only the task-snapshot pass remains (button morph, cards).
// ---- SK-71: i18n boot. Load the pack (default en; ru from storage), apply it to
// the static markup, hand the translator to the pure modules, then render. On a
// language switch everything repaints in place from saved state — no page reload.
await I18N.load();
Feed.setT((k, a) => L(k, a), (k) => I18N.has(k));
Audit.setT((k, a) => L(k, a));
I18N.apply(document);
I18N.watchStorage(relangAll);
function relangAll() {
  Feed.setT((k, a) => L(k, a), (k) => I18N.has(k));
  Audit.setT((k, a) => L(k, a));
  I18N.apply(document);
  go.title = GO_RUN ? L("sb.stop_esc") : L("sb.send");
  go.setAttribute("aria-label", GO_RUN ? L("sbt.stop") : L("sbt.aria_send"));
  themeLbl.textContent = L(THEME_LBL[THEME]);
  inp.placeholder = RMOTION.matches ? L(PH_KEYS[0]) : "";
  phin.textContent = L(PH_KEYS[PHI]);
  if (LAST_BRIDGE) setBridge(LAST_BRIDGE.state, LAST_BRIDGE.url);
  setChatTitle(chatTitle);
  renderTL();
  bellRender();
  audRender();
  histRender();
  if (!skMenu.hidden) skRender();
  PERM_BLOCKS.forEach((d, id) => { // open asks repaint from the saved m
    if (d.classList.contains("done") || !d._m) return;
    const nd = permCard(d._m);
    d.replaceWith(nd);
    PERM_BLOCKS.set(id, nd);
  });
  HO_BLOCKS.forEach((d) => { if (d._m) renderHo(d, d._m); });
}
renderTL();
connect();
syncEmpty(); // SK-40: startup — an empty feed, the empty state shows at once
// SK-23: the active chat_id survives an add-on reload (storage.local); the feed
// itself is re-pulled from the bridge (chats.jsonl is the source of truth). The
// port exists already — connect() above ran synchronously, this is a microtask.
browser.storage.local.get(CHAT_KEY).then((m) => {
  if (typeof m[CHAT_KEY] === "string" && m[CHAT_KEY]) activeChat = m[CHAT_KEY];
  loadChat(activeChat);
});
// SK-30: the chosen theme mode survives an add-on reload (storage.local).
browser.storage.local.get(THEME_KEY).then((m) => applyTheme(m[THEME_KEY]));
