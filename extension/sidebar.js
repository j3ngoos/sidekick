const log = document.getElementById("log"), inp = document.getElementById("in"), go = document.getElementById("go"), st = document.getElementById("st");
// SK-40: пустое состояние ленты. Синхронизация по DOM (не по модульным флагам —
// AUD/HIST инициализируются ниже): пусто и нет второго вида → показываем глиф.
const EMPTY = document.getElementById("empty");
function syncEmpty() {
  if (!EMPTY) return;
  EMPTY.hidden = !!(log.hidden || log.children.length ||
    !document.getElementById("aud").hidden || !document.getElementById("hist").hidden);
}
const ttl = document.getElementById("ttl"); // SK-38: из шапки в JS живёт только заголовок чата
let port;
// ---- SK-30: inline-SVG иконки. Эмодзи в chrome UI запрещены; статус-глифы
// текстовых меток (резолв-лейблы, handoff, think) остаются источником и честным
// fallback-ом, но в живом DOM подменяются SVG-кандидатом (GLYPH_ICON).
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
// «💭 думает» → <svg ic-think/> думает; совпадений нет — текст как есть.
function setGlyphText(el, str) {
  const m = /^(\S+)([\s\S]*)$/.exec(String(str == null ? "" : str));
  const name = m && GLYPH_ICON[m[1]];
  if (!name) { el.textContent = str; return; }
  el.replaceChildren(svgIcon(name), document.createTextNode((m[2] || "").replace(/^\s+/, "")));
}
let TL = { task: "", title: "", state: "", steps: [], warn: "", handoff: null }; // SK-06 snapshot from background (single source of truth)
// SK-09: agent state — rendered verbatim from the bridge's {type:"state"} frames (via the
// background). "unknown" = no frame seen yet / the WS to the bridge is down.
// SK-38 п.1: статусные чипы («💭 думает»/«● выполняет») из шапки убраны — их дублирует
// низ (карточка задачи + think-блок в ленте), а агентский статус несёт бейдж тулбара
// (background badge()). AST остаётся: единственные живые потребители — будущий bell
// (SK-37) и отладка; шапка статуса больше не рисует.
let AST = "unknown";
const PERM_BLOCKS = new Map(); // ask id -> element (inline, never a modal)
const HO_BLOCKS = new Map(); // SK-08 handoff id -> element («нужен ты» баннер)
const ACTION_RU = { sensitive_input: "действие с полем пароля/карты", payment: "платёж",
  oauth: "OAuth-вход / выдача доступа", "2fa": "код 2FA", click: "клик",
  input: "ввод текста", submit: "отправка формы", navigate: "переход", action: "действие",
  close_tabs: "закрыть вкладки", // SK-32
  send_message: "отправить сообщение наружу", send_email: "отправить письмо наружу" }; // SK-34
const REASON_RU = { level_ask: "уровень сайта: Ask", no_grant: "сайт без разрешения (умолчание Ask)",
  bank_domain: "банковский/платёжный домен", auth_or_payment_path: "страница входа/оплаты",
  "action:sensitive_input": "чувствительное поле", "action:payment": "платёж",
  "action:oauth": "OAuth", "action:2fa": "2FA",
  "action:send_message": "текст наружу", "action:send_email": "письмо наружу", // SK-34
  close_tabs: "массовое закрытие — подтверждение по умолчанию" }; // SK-32
function add(cls, text, memory) {
  const d = document.createElement("div"); d.className = "m " + cls; d.textContent = text;
  // SK-40: аватар-глиф пузыря — молния на градиенте (агент) / человек (ты).
  // Абсолютный угол пузыря: композиционная сетка ленты и node-контракты не затронуты.
  if (cls === "me" || cls === "hm") {
    const av = document.createElement("span");
    av.className = "av " + (cls === "me" ? "hu" : "ag");
    av.appendChild(svgIcon(cls === "me" ? "user" : "bolt"));
    d.insertBefore(av, d.firstChild);
  }
  // SK-31: ответ собран из knet (не из этого чата) — маленький глиф у сообщения;
  // SK-30: вместо эмодзи — SVG-кандидат ic-memory (текст «🗑» остаётся в
  // комментарии и в данных шага «🗑 knet_search · …» — источник истины там).
  if (memory) {
    const g = document.createElement("span"); g.className = "mem";
    g.appendChild(svgIcon("memory"));
    g.title = "из памяти: собрано через knet, не из этого чата";
    d.appendChild(g);
  }
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  syncEmpty();
}
// ---- SK-06: step timeline + inline Stop. The sidebar renders the background's
// snapshot; Stop click posts {type:"stop"} and the echo (paused) comes back as a
// fresh snapshot — one source of truth, so several sidebars never diverge.
// SK-38 п.1/4: шапка больше не рисует статус (чипов нет); единственная реакция на
// снапшот — морф кнопки отправки в стоп (setGoRun) и enabled/disabled пункта меню.
// ---- SK-06/SK-22: task state + step timeline. Since SK-22 the steps are NOT a
// permanent band above the chat: each task gets ONE card inside the chat feed —
// collapsed header «⚙ <active step>» + work indicator (spinner/✓/✗), click
// expands the step list (times recorded sidebar-side, shots SK-06 inside).
// Auto-expand on failed. SK-38: header chips and the ■ button are gone — the card
// in the feed is the status surface ("оставить только внизу" по карточке t_7cf9bfad).
const TASK_CARDS = new Map(); // task -> card state (one card per task, never a duplicate)
const STEP_TIMES = {};        // "task|n" -> {first,last,state} — sidebar-side clock
// SK-22 п.5 (backward compat): a task without steps (old bridge) gets no card —
// the chat feed works exactly as before.
  // SK-23: the card belongs to the chat its task ran in (frames without chat =
  // "default"); while another chat is open the card itself is not this chat's
  // feed and stays out (SK-38: глобальный статус в шапке больше не дублируется).
  // SK-25 п.3: ПАРАЛЛЕЛЬНЫЕ задачи — карточка на каждую задачу своего чата (массив
  // tasks от фона; одиночный снапшот старого формата — фолбэк). Ленты не путаются:
  // ключ карточки — task id. Чип вкладки карточки (п.4) — фокус вкладки по клику
  // человека (port tab_focus → tabs.update active + окно в фокус).
  function focusTab(task) { try { port.postMessage({ type: "tab_focus", task }); } catch (e) { /* порт между переподключениями */ } }
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
  // SK-38 п.4: морф кнопки. GO_RUN — единственная правда о состоянии; класс и
  // a11y-подпись меняются вместе (title/aria-label — слепому скрину тоже видно).
  let GO_RUN = false;
  function setGoRun(on) {
    if (GO_RUN === on) return;
    GO_RUN = on;
    go.classList.toggle("running", on);
    go.title = on ? "Стоп задача (Esc)" : "Отправить";
    go.setAttribute("aria-label", on ? "Стоп задача" : "Отправить");
  }
  function renderTL() {
    // SK-30 п.4 / SK-38 п.4: «Стоп задача» живёт только при running-задаче
    // (параллельные SK-25 — достаточно одной); тот же anyRun кормит морф кнопки.
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
    // next to the live ones — «○» честное: чем кончилось, мы не знаем
    for (const [k, old] of TASK_CARDS) if (!seen.has(k) && old.live) Feed.markStale(old);
  }
// ---- SK-30 п.1 / SK-38 п.3: «⋯» живёт в шапке (правый край). Пункты — в
// меню-портале #menu (прямой ребёнок body, id прежние — контракты SK-23/10/11),
// раскрывается вниз от шапки (CSS: top от --hd-h).
// Клавиатура: Enter/Space на «⋯» (нативный button), ↑↓/Home/End между пунктами,
// Tab — цикл по меню, Esc — закрытие с возвратом фокуса на «⋯». Клик вне —
// закрытие. Позиция/анимация — чистый CSS (#menu), инлайн-стилей нет.
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
  // SK-38 п.4: Esc = тот же стоп, что и морф-кнопка. Иерархия: сначала «⋯» (выше),
  // затем скилл-меню и поле имени (их обработчики гасят событие stopPropagation-ом) —
  // сюда Esc доходит только когда закрывать нечего и есть что останавливать.
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
  else if (e.key === "Tab") { // фокус не уходит из открытого меню
    e.preventDefault();
    menuFocus(items[(i + (e.shiftKey ? -1 : 1) + items.length) % items.length]);
  }
});
optBtn.onclick = () => browser.runtime.openOptionsPage();
stopTaskBtn.onclick = () => port.postMessage({ type: "stop", task: TL.task });

// ---- SK-35: колокольчик «позови человека» + ручное сворачивание панели -------------
// Журнал (task_done / task_failed / perm) ведёт background, сюда летит кадр
// {type:"bell_log", events}. SK-50 (v0.7): ОТКРЫТИЕ журнала ничего не читает —
// человек открыл «посмотреть». Прочитывается клик по конкретному событию;
// «прочитать всё» — явная кнопка в подвале. Звонок задач снимается на стороне
// фона, пермис-звонки «прочитать всё» НЕ снимает (живут до вердикта).
// Ручное сворачивание: window.close() из ЭТОГО документа закрывает панель без
// жеста (пробда firefox-158); sidebarAction.close() без жеста схема отклоняет.
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
  if (bellpanel.hidden) return; // ряды перерисовываем только при открытом портале
  if (bellReadAll) bellReadAll.disabled = n === 0; // читать нечего — кнопка не нужна
  if (!BELL_LOG.length) { bellrows.replaceChildren(Object.assign(document.createElement("div"), { className: "bempty", textContent: "Пока пусто" })); return; }
  bellrows.replaceChildren(...BELL_LOG.map((e) => {
    const b = document.createElement("button");
    const [ico, kcls] = BELL_KIND_ICO[e.kind] || ["#ic-bell", ""];
    b.className = "brow " + kcls + (e.read ? "" : " unread");
    b.dataset.id = String(e.id || "");
    b.dataset.kind = String(e.kind || "");
    const ic = document.createElement("span"); ic.className = "bic";
    ic.innerHTML = '<svg class="icn" aria-hidden="true"><use href="' + ico + '"/></svg>';
    const tx = document.createElement("span"); tx.className = "btx";
    const t1 = document.createElement("span"); t1.textContent = String(e.text || "");
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
    // SK-50: открытие журнала НЕ помечает всё прочитанным — «посмотреть» ≠
    // «ликвидировать». Прочтение: клик по конкретному событию или явная кнопка
    // «Прочитать всё» в подвале. Никаких таймеров автозакрытия у журнала нет и
    // быть не должно (проверка живьём 25.10: «закрывается через 10 с» — это
    // сворачивание ПАНЕЛИ эвристикой LONG_MS; SK-51 её убрал — панель сворачивает
    // только явный long:true агента или кнопка в шапке).
    bellRender();
    bellClose.focus();
  }
}
bellBtn.onclick = () => bellPanelOpen(bellpanel.hidden);
bellClose.onclick = () => { bellPanelOpen(false); bellBtn.focus(); };
if (bellReadAll) bellReadAll.onclick = () => {
  // Явное «прочитать всё» (SK-50): счётчик в ноль, звонки ЗАДАЧ сняты,
  // пермис-звонки живы до вердикта — bellAckIds на стороне фона трогает
  // только task_done/task_failed.
  try { port.postMessage({ type: "bell_read" }); } catch (e) { /* мост-фон моргнул — счётчик доживёт до следующего кадра */ }
};
document.addEventListener("mousedown", (e) => {
  if (!bellpanel.hidden && !bellpanel.contains(e.target) && !bellBtn.contains(e.target)) bellPanelOpen(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !bellpanel.hidden) { e.preventDefault(); bellPanelOpen(false); bellBtn.focus(); }
});
// Клик по событию журнала: снять прочитанное и показать человеку источник.
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
// Ручное сворачивание панели (жест человека — но и без жеста window.close()
// работает, проверено пробдой: панель закрывается, окно живёт).
collapseBtn.onclick = () => window.close();
function flashTaskCard(id) {
  const c = TASK_CARDS.get(String(id));
  const el = c && c.card;
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
}

// SK-30: тема — система → светлая → тёмная (html[data-theme] переопределяет
// prefers-color-scheme в theme.css; выбор живёт в storage.local).
const THEME_KEY = "sk30Theme";
const THEME_MODES = ["system", "light", "dark"];
const THEME_LBL = { system: "Тема: как в системе", light: "Тема: светлая", dark: "Тема: тёмная" };
let THEME = "system";
function applyTheme(mode) {
  THEME = THEME_MODES.includes(mode) ? mode : "system";
  if (THEME === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = THEME;
  themeLbl.textContent = THEME_LBL[THEME];
}
themeBtn.onclick = () => {
  applyTheme(THEME_MODES[(THEME_MODES.indexOf(THEME) + 1) % THEME_MODES.length]);
  browser.storage.local.set({ [THEME_KEY]: THEME });
};
function answer(id, choice) {
  port.postMessage({ type: "perm_answer", id, choice }); // background forwards + greys out
}
// SK-34 п.4: чипы причины deny в один тап (аудит 21.10 п.8: 16 deny без причины —
// будущие аудиты должны понимать, что человек имел в виду). Ключи стабильны —
// их пишет audit.jsonl, перевод только в UI/audit.js.
const DENY_CHIPS = [["wrong_target", "не туда жмёшь"], ["wrong_field", "не то поле"],
  ["stop_task", "стоп задача"]];
function addPermAsk(m) {
  if (PERM_BLOCKS.has(m.id)) return; // reconnect re-push must not duplicate
  const d = document.createElement("div"); d.className = "m perm"; d.dataset.id = m.id;
  const why = String(m.reason || "").split("+").map((r) => REASON_RU[r] || r).filter(Boolean).join(" · ");
  const hd = document.createElement("div"); hd.className = "hd";
  hd.textContent = "Hermes просит: " + (ACTION_RU[m.action] || m.action);
  const sub = document.createElement("div"); sub.className = "sub";
  // SK-37: имя цели (m.name — куда именно смотрит рука агента) — домена мало,
  // человек должен видеть ПРЕДМЕТ, а не только сайт.
  sub.textContent = [(m.domain || "неизвестный сайт"), m.name || "", why, m.hint || "", m.url || ""].filter(Boolean).join(" — ");
  d.append(hd, sub);
  renderPermShot(d, m.shot); // SK-37: миниатюра кадра вкладки (прилетит отдельным кадром, если ещё нет)
  const btns = document.createElement("div"); btns.className = "btns";
  // SK-34 п.1: «Разрешить для {domain} всегда» — только когда мост прислал
  // show_always (первое «разово» на этот тип уже было; на чувствительном пути
  // флага нет никогда — bridge снимает его и понижает «всегда» до разово).
  const opts = [["once", "Разрешить разово", "ok"]];
  if (m.show_always && m.domain)
    opts.push(["always", "Разрешить для " + m.domain + " всегда", "alw"]);
  opts.push(["deny", "Отклонить", "no"]);
  for (const [choice, label, cls] of opts) {
    const b = document.createElement("button"); b.className = cls; b.textContent = label;
    b.onclick = () => answer(m.id, choice);
    btns.appendChild(b);
  }
  d.append(btns);
  log.appendChild(d); log.scrollTop = log.scrollHeight;
  PERM_BLOCKS.set(m.id, d);
}
// SK-37: миниатюра кадра задачи в карточке пермиса. Кадр приносит мост отдельным
// кадром perm_shot (съёмка асинхронная, карточка не ждёт) — или replay паркованного
// кадра для позднего сайдбара. Пустой shot/ошибка съёмки — карточка без картинки,
// вердикты те же.
function renderPermShot(d, shot) {
  if (!shot || !String(shot).startsWith("data:")) return;
  let img = d.querySelector("img.pshot");
  if (!img) {
    img = document.createElement("img"); img.className = "pshot"; img.alt = "кадр задачи";
    const sub = d.querySelector(".sub");
    sub ? sub.after(img) : d.prepend(img);
  }
  if (img.getAttribute("src") !== shot) img.src = shot;
}
// SK-37: клик по уведомлению — background уже открыл сайдбар; осталось прокрутить
// к карточке и подсветить её (flash пересоздаётся рефлоу: повторный клик триггерит
// анимацию заново).
// SK-35: звонок задачи живёт в чате задачи — при необходимости переключаемся и
// подсвечиваем карточку после приезда её ленты (replay + chats_data — раундтрип).
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
  const label = { once: "✓ разрешено разово", always: "✓ всегда для сайта",
    deny: "✗ отклонено", timeout: "⏱ время вышло — заблокировано" }[choice] || "ответ отправлен";
  const btns = d.querySelector(".btns");
  const res = Object.assign(document.createElement("span"), { className: "res" });
  setGlyphText(res, label); // SK-30: ✓/✗/⏱ → SVG-кандидаты
  btns.replaceChildren(res);
  if (choice !== "deny") return;
  // SK-34 п.4: вердикт уже ушёл (агент разморожен), причина — следующим тапом.
  // «стоп задача» — не только причина: это и ■ Stop (chip = deny + stop).
  for (const [reason, text] of DENY_CHIPS) {
    const c = document.createElement("button");
    c.className = "chip"; c.textContent = text;
    c.onclick = () => {
      port.postMessage({ type: "perm_reason", id, reason });
      if (reason === "stop_task") port.postMessage({ type: "stop", task: TL.task });
      btns.replaceChildren(Object.assign(document.createElement("span"),
        { className: "res", textContent: label.replace(/^\S+\s*/, "") + " · причина: " + text }));
    };
    btns.appendChild(c);
  }
}
// ---- SK-08: hand-off «нужен ты». The agent is blocked on the bridge (hub/paused —
// page.*/previews are refused there); the human acts in the browser, then presses the
// one green button. Never a modal; the block lives in the chat feed and survives a
// late sidebar open via the background's PENDING_HO replay.
function addHandoff(m) {
  // SK-48: ре-пуш той же руки после неудачной expect-верификации — НЕ дублируем блок,
  // а обновляем существующий: note (что не так), кадр (что было на экране), попытка,
  // живая кнопка. Первый показ — обычный путь.
  const old = HO_BLOCKS.get(m.id);
  if (old) {
    // SK-48: ре-пуш той же руки после неудачной expect-верификации — НЕ дублируем блок,
    // а обновляем существующий: note (что не так), кадр (что было на экране), попытка,
    // живая кнопка. Фон оптимистично гасит баннер по клику «Готово» (мол, рука закрыта) —
    // ре-пуш с пометкой значит «мост цель не подтвердил», и блок оживает.
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
  const hd = document.createElement("div"); hd.className = "hd";
  setGlyphText(hd, "🖐 Нужен ты" + (m.attempt ? ` · попытка ${+m.attempt + 1}` : ""));
  const tx = document.createElement("div"); tx.className = "tx2";
  tx.textContent = m.what || "займи браузер: заверши вручную и нажми «Готово, продолжай»";
  const sub = document.createElement("div"); sub.className = "sub";
  sub.textContent = [m.url || "", m.task ? "задача: " + m.task : ""].filter(Boolean).join(" — ");
  d.append(hd, tx);
  if (sub.textContent) d.append(sub);
  if (m.note) { // SK-48: мост проверил ожидаемое состояние — не нашёл; человек видит почему
    const nt = Object.assign(document.createElement("div"), { className: "note" });
    setGlyphText(nt, "⚠ " + m.note);
    d.append(nt);
  }
  if (typeof m.shot === "string" && m.shot.startsWith("data:image/")) {
    const im = Object.assign(document.createElement("img"), { className: "shot", alt: "кадр страницы" });
    im.src = m.shot;
    d.append(im);
  }
  const b = document.createElement("button"); b.textContent = "Готово, продолжай";
  b.onclick = () => {
    // SK-48: до вердикта моста (дешёвое чтение страницы) кнопка засыпает: «Готово» —
    // заявка на проверку, а не рубильник; офлайн-клик оживляет её снова (markHoOffline)
    b.disabled = true; b.textContent = "Проверяю…";
    port.postMessage({ type: "resume", id: m.id, task: m.task || TL.task });
  };
  d.append(b);
}
function markHoResolved(id, choice) {
  const d = HO_BLOCKS.get(id); if (!d || d.classList.contains("done")) return;
  d.classList.add("done");
  const label = { resumed: "✓ продолжено — агент вернулся",
    timeout: "⏱ время вышло — агент разблокирован",
    superseded: "↪ заменено новым запросом" }[choice] || "закрыто";
  const b = d.querySelector("button");
  if (b) {
    const res = Object.assign(document.createElement("span"), { className: "res" });
    setGlyphText(res, label); // SK-30: ✓/⏱/↪ → SVG-кандидаты
    b.replaceWith(res);
  }
  const w = d.querySelector(".warn"); if (w) w.remove();
}
function markHoOffline(id) { // the click never reached the bridge — keep the button live, say so
  const d = HO_BLOCKS.get(id); if (!d || d.classList.contains("done")) return;
  const b = d.querySelector("button"); // SK-48: «Проверяю…» без ответа моста = не нажималось
  if (b && b.disabled) { b.disabled = false; b.textContent = "Готово, продолжай"; }
  if (d.querySelector(".warn")) return;
  const w = Object.assign(document.createElement("div"), { className: "warn" });
  setGlyphText(w, "⚠ мост офлайн — нажми ещё раз после переподключения");
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
    ? SKILLS.length + " сохранено · ↑↓ — выбор · Enter — вставить · Esc — закрыть"
    : (SKILLS.length ? "нет совпадений · Esc — закрыть" : "пока пусто: напиши промпт и сохрани через «⋯»");
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
  add("sys", "скилл «" + r.name + "» " + (r.action === "updated" ? "обновлён" : "сохранён") + " — жми «/»");
}
skNameOk.onclick = skConfirm;
skNameCancel.onclick = () => { skName.hidden = true; inp.focus(); };
skNameIn.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); skConfirm(); }
  else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); skName.hidden = true; inp.focus(); } // SK-38: Esc в поле имени — только отмена, не стоп
});
skIoBtn.onclick = () => { skIo.hidden = !skIo.hidden; skIoMsg.textContent = ""; };
skExp.onclick = () => { // blob download — the file is the export format (import accepts it back)
  const blob = new Blob([Skills.exportJson(SKILLS)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "sidekick-skills.json";
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  skIoMsg.textContent = "выгружено скиллов: " + SKILLS.length;
};
function skApplyImport(txt) {
  const r = Skills.importJson(txt, SKILLS);
  skIoMsg.textContent = r.ok
    ? "импорт: +" + r.added + ", ⟳ " + r.updated + (r.skipped ? ", пропущено " + r.skipped : "")
    : "импорт не удался: " + r.err;
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
// jsonl (card: «лента полная — сверить с jsonl»). Lines are re-masked locally
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
  catch (e) { AUD.err = "мост офлайн"; audRender(); } // port died between reconnects
}
function audRender() {
  const lines = (Array.isArray(AUD.lines) ? AUD.lines : []).map(Audit.mask);
  if (AUD.raw) { // «открыть сырой файл»: the same tail monospace, masked twice over
    audList.hidden = true; audRaw.hidden = false;
    audRaw.textContent = lines.length ? lines.join("\n") : "— пусто —";
  } else {
    audRaw.hidden = true; audList.hidden = false;
    audList.replaceChildren(...lines.map((ln) => {
      const e = Audit.parseLine(ln);
      const d = document.createElement("div");
      d.className = "arow " + e.tone; d.title = e.sub || e.title;
      const t = document.createElement("span"); t.className = "at"; t.textContent = e.time;
      const c = document.createElement("span"); c.className = "aic";
      // SK-30: audit.js отдаёт стабильный ключ иконки ("lock"/"eye"/…) — рисуем
      // SVG из спрайта; неизвестный ключ честно остаётся текстом.
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
    + "показано " + lines.length + " · в файле " + (AUD.total || 0)
    + (AUD.truncated ? " · показан хвост (фильтр ищет по всему файлу)" : "")
    + (AUDQ ? " · фильтр «" + AUDQ + "»" : "");
}
function audOpen(on) {
  if (on && HIST.open) histOpen(false); // SK-23: second views are exclusive — one at a time
  if (on) menuOpen(false); // SK-30: открытие второго вида закрывает меню «⋯»
  AUD.open = on;
  aud.hidden = !on;
  audBtn.classList.toggle("on", on);
  log.hidden = on || HIST.open; // the second view: the chat log steps aside, the trail takes its place
  if (on) { audFetch(); AUDTIMER = setInterval(() => { if (!document.hidden) audFetch(); }, 2000); }
  else if (AUDTIMER) { clearInterval(AUDTIMER); AUDTIMER = null; }
  syncEmpty(); // SK-40: второй вид прячет пустое состояние, закрытие — возвращает
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
// pulled over WS chats_get — so «история и активный чат на месте» after a reload.
// «＋» = new chat (clean feed; agentd has no session for it → fresh hermes session);
// «🕘» = the history list (title, date, last utterance; the current chat is marked).
// Frames without a chat field belong to "default" (п.4 compatibility).
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
  syncEmpty(); // SK-40: чистая лента честно показывает пустое состояние
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
  setChatTitle(""); // SK-38: до приезда ленты чата шапка честно говорит «Новый чат»
  loadChat(cid);
  port.postMessage({ type: "replay" }); // pending perm/handoff/tl re-render into the fresh feed
  if (HIST.open) histRender();
}
newBtn.onclick = () => { // чистая лента + новая сессия Hermes (нет записи в sessions.json — без resume)
  switchChat("c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6));
  inp.focus();
};
function histRender() {
  histList.replaceChildren(...HIST.chats.map((c) => {
    const row = document.createElement("div");
    row.className = "hrow" + (c.id === activeChat ? " cur" : "");
    const tx = document.createElement("div"); tx.className = "htx";
    const t = document.createElement("span"); t.className = "ht";
    t.textContent = c.title || "(без названия)"; t.title = c.title || c.id;
    const dt = document.createElement("span"); dt.className = "hdate"; dt.textContent = fmtDate(c.ts);
    tx.append(t, dt);
    if (c.id === activeChat) {
      const mk = document.createElement("span"); mk.className = "hcur"; setGlyphText(mk, "✓ текущий");
      tx.append(mk);
    }
    const last = document.createElement("div"); last.className = "hlast";
    last.textContent = c.last ? (c.last.role === "agent" ? "Hermes: " : "ты: ") + c.last.text : "";
    row.append(tx, last);
    row.onclick = () => { switchChat(c.id); histOpen(false); };
    return row;
  }));
  histN.textContent = (HIST.err ? HIST.err + " · " : "")
    + "чатов: " + HIST.chats.length + (HIST.chats.length >= 30 ? " · показаны последние 30" : "");
}
function histFetch() {
  if (!HIST.open || !port) return;
  HIST.req = "hst" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  try { port.postMessage({ type: "chats_get", id: HIST.req }); }
  catch (e) { HIST.err = "мост офлайн"; histRender(); } // port died between reconnects
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

// ---- SK-38 п.1: заголовок чата в шапке — первый запрос с truncation и «…», пустой
// чат — «Новый чат». Строга режем до 60 символов (иначе гигантский первый запрос
// уехал бы и в DOM, и в title-тултип), CSS-ellipsis — фолбэк узкой панели.
let chatTitle = "";
function setChatTitle(t) {
  chatTitle = String(t == null ? "" : t).replace(/\s+/g, " ").trim();
  const cut = chatTitle.length > 60 ? chatTitle.slice(0, 60).trimEnd() + "…" : chatTitle;
  ttl.textContent = cut || "Новый чат";
  ttl.title = chatTitle;
}
function submit() {
  const t = inp.value.trim(); if (!t) return;
  if (!chatTitle) setChatTitle(t); // SK-38: шапка = первый запрос чата
  port.postMessage({ type: "chat", text: t, chat: activeChat }); inp.value = "";
}
// SK-38 п.4: одна кнопка на два состояния (морф CSS-ом, класс .running).
// Running — клик это ■ стоп (тот же stop-кадр, что пункт меню и Esc). Enter в
// поле остаётся «отправить» и во время задачи (параллельные задачи SK-25 живы).
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
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); skMenu.hidden = true; return; } // SK-38: Esc в скилл-меню — только закрыть, не стоп
    if (e.key === "Enter") { e.preventDefault(); if (SKSHOWN.length) skPick(SKHL); return; }
  }
  if (e.key === "Enter") submit();
});
// ---- SK-38 п.5: живой плейсхолдер композера — ротация фраз с fade (150 мс CSS,
// оверлей #phin над пустым полем). Пауза на фокусе (тикающий стопор не трогает
// видимую фразу) и на вводе (оверлей скрыт, пока есть текст). prefers-reduced-motion
// → без ротации и без оверлея: статичная первая фраза в нативном placeholder-е.
const phin = document.getElementById("phin");
const PH_RU = ["спроси что угодно", "могу закрыть вкладки", "найду и очищу корзину",
  "открою YouTube", "соберу вкладки в группу", "объясню эту страницу"];
const PH_MS = 3200, PH_FADE = 170; // fade 150 мс (CSS) + запас на смену текста
const RMOTION = matchMedia("(prefers-reduced-motion: reduce)");
let PHI = 0, PHT = null;
function phTick() {
  if (document.activeElement === inp) return; // пауза на фокусе — фраза стоит
  phin.classList.add("off");
  setTimeout(() => {
    if (document.activeElement !== inp) { PHI = (PHI + 1) % PH_RU.length; phin.textContent = PH_RU[PHI]; }
    phin.classList.remove("off");
  }, PH_FADE);
}
function phSync() { phin.hidden = !!inp.value || RMOTION.matches; }
function phStart() {
  if (RMOTION.matches) { // статичный режим: оверлей выключен, говорит нативный placeholder
    if (PHT) { clearInterval(PHT); PHT = null; }
    inp.placeholder = PH_RU[0]; phin.hidden = true;
    return;
  }
  inp.placeholder = ""; // нативный молчит: живую фразу рисует оверлей
  phin.textContent = PH_RU[PHI];
  phSync();
  if (!PHT) PHT = setInterval(phTick, PH_MS);
}
inp.addEventListener("input", phSync); // пауза/скрытие на вводе
if (RMOTION.addEventListener) RMOTION.addEventListener("change", phStart);
phStart();
// SK-13: the «bridge: …» footer became a status DOT in the header — green = online,
// amber = the human must fix something (token/protocol), red = offline/reconnecting,
// gray = nothing seen yet. The full text lives in the dot's tooltip (title).
// SK-28: retry (◌ идёт перебор адресов) — янтарь; offline (✗ ни один адрес не
// отвечает) — красный с инструкцией; к вердикту подмешивается адрес, чтобы человек
// видел, КУДА именно не доходит (поле «Сервер» в настройках).
const BRIDGE_DOT = { online: "online", retry: "warn", offline: "err", no_token: "warn",
  token_rejected: "warn", proto_mismatch: "warn" };
const BRIDGE_HINT = { online: "bridge: на связи",
  retry: "bridge: переподключаюсь — адрес моста не отвечает",
  offline: "bridge: сервер недоступен — проверь мост (sk status) и поле «Сервер» в настройках",
  no_token: "bridge: токен не задан — открой настройки аддона",
  token_rejected: "bridge: токен отклонён (ротация?) — обнови в настройках",
  proto_mismatch: "bridge: несовпадение протокола с мостом" };
function setBridge(state, url) {
  st.dataset.s = BRIDGE_DOT[state] || "idle";
  st.title = (BRIDGE_HINT[state] || ("bridge: " + state))
    + (url && state !== "online" ? " · " + url : "");
}
// ---- SK-21/SK-22: think-поток от sidekick-agentd. С SK-22 — полноценный блок:
// один на задачу, по умолчанию свёрнут («💭 Думает…»), клик разворачивает
// скролл-контейнер, после финала — «💭 Подумал (N сек)». Автоскролл к хвосту
// только в развёрнутом виде — лента чата рассуждением не дёргается. Логика — feed.js.
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
function thinkDone(task) { // финальный ответ/терминальный кадр — блок больше не «думает»
  const b = THINK_BLOCKS.get(task);
  if (b) Feed.thinkFinish(b, Date.now());
}
function connect() {
  port = browser.runtime.connect({ name: "sidebar" });
  port.onMessage.addListener((m) => {
    // SK-23: chat/think frames belong to one chat — while another chat is open they
    // must not paint over its feed (frames without chat = "default", п.4 compat).
    if (m.type === "chat") { if (!inChat(m)) return; add(m.role === "me" ? "me" : "hm", m.text, m.memory === true); if (m.task) thinkDone(m.task); } // SK-31: memory-флаг — глиф «🗑 из памяти»
    else if (m.type === "think") { if (inChat(m)) addThink(m); } // SK-21 stub
    else if (m.type === "tl") { TL = m; renderTL();
      // SK-21: терминальный task-кадр тоже закрывает «думает…» — если chat-кадр
      // потерялся (WS-морг расширения), заголовок не залипает навечно
      if (m.state === "done" || m.state === "failed") thinkDone(m.task);
    } // SK-06 snapshot (task state + steps)
    else if (m.type === "agent_state") { AST = m.state || "unknown"; } // SK-38: шапка статуса не рисует (бейдж — на стороне фона, bell — SK-37)
    else if (m.type === "perm_ask") addPermAsk(m);
    else if (m.type === "perm_resolved") markResolved(m.id, m.choice);
    else if (m.type === "perm_shot") { // SK-37: миниатюра догнала карточку
      const d = PERM_BLOCKS.get(m.id);
      if (d && !d.classList.contains("done")) renderPermShot(d, m.shot);
    }
    else if (m.type === "bell_focus") bellFocus(m.id, m.kind, m.chat); // SK-37: клик по уведомлению — показать карточку; SK-35: chat — чат задачи
    else if (m.type === "bell_log") { BELL_LOG = Array.isArray(m.events) ? m.events : []; bellRender(); } // SK-35: журнал шапки
    else if (m.type === "sidebar_collapse") { window.close(); } // SK-35: долгая задача начала работу — панель сворачивается (window.close() из этого документа работает без жеста, пробда firefox-158)
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
      AUD.err = "мост офлайн"; audRender();
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
          // SK-38 п.1: заголовок чата = первый пользовательский запрос (тот же
          // источник, что title в chats_records на мосту)
          const firstUser = msgs.find((x) => x.role === "user" && String(x.text || "").trim());
          setChatTitle(firstUser ? firstUser.text : "");
          port.postMessage({ type: "replay" });
        }
      }
    }
    else if (m.type === "chats_err") { // background could not reach the bridge
      if (m.id === HIST.req && HIST.open) { HIST.err = "⚠ мост офлайн"; histRender(); }
      else if (m.id === CHATREQ.id) CHATREQ = { id: "", chat: "" }; // startup load — empty feed is honest
    }
    else if (m.type === "bridge") {
      setBridge(m.state, m.url); // SK-28: url — куда именно не доходит (в тултип при retry/offline)
    }
  });
  port.onDisconnect.addListener(() => { setBridge("offline"); setTimeout(connect, 1200); });
}
// (SK-10) submit()/go.onclick and the Enter fork moved up to the composer section —
// the old unconditional Enter-submits listener is gone: with the «/»-menu open, Enter inserts.
// SK-38: шапка стартует честно («Новый чат» в разметке, чипов нет) — статусный
// рендер из шапки убран, остаётся только прогон снапшота задач (морф кнопки, карточки).
renderTL();
connect();
syncEmpty(); // SK-40: старт — пустая лента, пустое состояние видно сразу
// SK-23: the active chat_id survives an add-on reload (storage.local); the feed
// itself is re-pulled from the bridge (chats.jsonl is the source of truth). The
// port exists already — connect() above ran synchronously, this is a microtask.
browser.storage.local.get(CHAT_KEY).then((m) => {
  if (typeof m[CHAT_KEY] === "string" && m[CHAT_KEY]) activeChat = m[CHAT_KEY];
  loadChat(activeChat);
});
// SK-30: выбранный тема-режим переживает перезагрузку аддона (storage.local).
browser.storage.local.get(THEME_KEY).then((m) => applyTheme(m[THEME_KEY]));
