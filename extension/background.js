// Hermes Sidekick — bridge client (event page). WS to the bridge.
// SK-02: token handshake (contract v1), options page for token entry.
// SK-04: permission decisions live on the BRIDGE (hub/grants.json) — this page only
// relays perm_ask to the sidebar and the human's perm_answer back. No local grants store.
// SK-28: адрес сервера настраивается — прямой LAN-коннект без ssh-туннеля.
// Порядок fallback (спека п.3): сохранённый bridgeServer → ws://127.0.0.1:8791
// (туннельная совместимость) → ошибка с инструкцией (бейдж ✗ + подсказка в сайдбаре).
// Ретраи экспоненциалом 2с..60с; бейдж: ● зелёный (агентский) / ◌ ретрай / ✗ offline.
const FALLBACK_SERVER = "ws://127.0.0.1:8791"; // чистый профиль = туннельный режим, как до SK-28
const PROTO_V = 1;
const BACKOFF_MIN = 2000, BACKOFF_MAX = 60000; // спека SK-28: 2с..60с
// SK-49: нас вытеснил новый hello (close 4009) — немедленный ре-коннект устроил бы
// пинг-понг с активным сокетом (два браузера по 2 с переключали бы маршруты).
// Пауза 15 с: стендовый Firefox после приёмки харнесс убивает, живой браузер
// пользователя возвращается к мосту сам и надолго.
const REPLACED_BACKOFF_MS = 15000;
const OPEN_TIMEOUT_MS = 4000;   // кандидат, на котором open не наступил — мёртвый
const ACK_TIMEOUT_MS = 1500;    // открылся, но hello_ack не пришёл — это не наш мост
let ws = null, wsUrl = "", backoff = BACKOFF_MIN, optionsShown = false;
let trying = false, failStreak = 0; // SK-28: одна попытка за раз; ретрай ◌ пока <3 циклов, дальше ✗
const clients = new Set(); // sidebar ports
const PENDING_PERM = new Map(); // perm_ask id -> frame (replayed to sidebars that open late)
// SK-08: open hand-offs («нужен ты») — same park/replay pattern as PENDING_PERM.
// The bridge holds hub/paused while we hold these; «Готово, продолжай» sends {type:"resume"}.
const PENDING_HO = new Map(); // handoff id -> frame
// SK-09: agent state — the BRIDGE derives it from its event stream ({type:"state"}
// frames: idle/thinking/acting/needs_you/paused) and replays the current one on every
// handshake; this page only renders, it never computes. While the WS is down no frame
// can pass, so the honest local value is "unknown". research pin: sidebarAction.open()
// only works from a user gesture — the toolbar badge is the signal the human sees with
// the sidebar CLOSED; the header chip is for when it is already open.
let AGST = "unknown";
let BRIDGE_ST = "offline"; // online|offline|no_token|token_rejected|proto_mismatch
const AG_BADGE = { idle: ["H", "#7c3aed"], thinking: ["…", "#2563eb"], acting: ["▶", "#059669"],
  needs_you: ["!", "#f59e0b"], paused: ["‖", "#dc2626"], unknown: ["?", "#6b7280"] };
function agState(s) { AGST = s; badge(); sendToUi({ type: "agent_state", state: AGST }); }
function badge() {
  // SK-37: колокольчик (неотвеченный пермис/незавершённая задача) приоритетнее
  // состояния агента: пропущенный пермис дороже, чем «агент думает».
  if (typeof BELL !== "undefined" && BELL.pending.size) {
    browser.action.setBadgeText({ text: bellBadgeText(BELL.pending.size) });
    browser.action.setBadgeBackgroundColor({ color: BELL_BADGE_COLOR });
    return;
  }
  let t = "?", c = "#6b7280";
  if (BRIDGE_ST === "online") [t, c] = AG_BADGE[AGST] || AG_BADGE.unknown;
  else if (BRIDGE_ST === "no_token") [t, c] = ["?", "#dc2626"];
  else if (BRIDGE_ST === "token_rejected" || BRIDGE_ST === "proto_mismatch") [t, c] = ["!", "#dc2626"];
  else if (BRIDGE_ST === "retry") [t, c] = ["◌", "#f59e0b"];    // SK-28: перебор адресов/цикл ретраев идёт
  else if (BRIDGE_ST === "offline") [t, c] = ["✗", "#dc2626"];  // SK-28: ни один адрес не отвечает (ретрай продолжается)
  else [t, c] = ["", "#6b7280"]; // unknown bridge state: no badge
  browser.action.setBadgeText({ text: t });
  browser.action.setBadgeBackgroundColor({ color: c });
}
// SK-06: latest step-timeline snapshot, cached in the event page so a sidebar that
// opens late (or after a reconnect) still shows the live task. state: running|paused|
// done|failed; "paused" is set locally the moment a human presses Stop (the abort frame
// goes to the bridge), an "active" step or a running task frame (agent resumed) clears it.
// handoff: {id, what} while a human holds the browser — the chip outranks every state.
// SK-25 п.3: PARALLEL tasks — TASKS is the per-task store (steps/chat/tab never mix),
// TLK names the «active» task (the one that got the last task frame — the chip follows
// it). tlPush sends the legacy single-task fields (old sidebars/tests keep working)
// PLUS a `tasks` array so the feed renders one card per task.
const TASKS = new Map(); // task -> {task,title,state,steps:Map,chat,tab,window,url,tabTitle,tabGone,ts}
let TLK = "";            // active task key (chip/header follow task frames, not stray steps)
const TLW = { warn: "", handoff: null }; // global UI fields: Stop-delivery warning, open hand-off
function taskOf(id) {
  let e = TASKS.get(id);
  if (!e) {
    e = { task: id, title: "", state: "", steps: new Map(), chat: "",
          tab: null, window: null, url: "", tabTitle: "", tabGone: false, ts: Date.now() };
    TASKS.set(id, e);
    if (TASKS.size > 24) { // cap: drop the OLDEST terminal task, never a running one
      for (const [k, v] of TASKS) if (k !== id && (v.state === "done" || v.state === "failed")) { TASKS.delete(k); break; }
    }
  }
  return e;
}
function tlPush() {
  const cur = TASKS.get(TLK);
  const tasks = [];
  for (const e of TASKS.values()) tasks.push({ task: e.task, title: e.title, state: e.state,
    chat: e.chat, tab: e.tab, window: e.window, url: e.url, tabTitle: e.tabTitle,
    tabGone: e.tabGone, steps: [...e.steps.values()] });
  sendToUi({ type: "tl",
    task: cur ? cur.task : "", title: cur ? cur.title : "", state: cur ? cur.state : "",
    chat: cur ? cur.chat : "", steps: cur ? [...cur.steps.values()] : [],
    warn: TLW.warn, handoff: TLW.handoff, tasks });
}
function tlTask(msg) { // {type:"task",id,title?,state,chat?,long?} — authoritative task-level state
  const id = String(msg.id || TLK || "t").slice(0, 60);
  const e = taskOf(id);
  if (msg.chat != null) e.chat = String(msg.chat).slice(0, 80); // SK-23: which chat this task belongs to
  if (msg.title != null) e.title = String(msg.title).slice(0, 120);
  if (["running", "paused", "done", "failed"].includes(msg.state)) e.state = msg.state;
  TLK = id; // a task frame is a task-level touch: the chip follows the task the human drives
  // SK-35/SK-51: долгая задача — ТРИГГЕР только явный кадр агента long:true
  // (аддитивно к v1-контракту). Десятисекундной эвристики больше нет: интерактивный
  // диалог не сворачивается НИКОГДА, сколько бы ни шёл (v0.7, пользователь 25.10).
  // Терминальный кадр долгой — bell + ✓ (механика SK-35 целиком).
  if (msg.state === "running") {
    if (LONGS.has(id)) bellClear(id); // новая фаза: старый звонок «сделана» той же id не нужен
    // SK-51: «ухожу в долгую» — сайдбар+группа сворачиваются; повторный long:true
    // идемпотентен (LONGS.has guard — sidebar_collapse не шлётся второй раз).
    if (msg.long === true && !LONGS.has(id)) markLong(id).catch(() => {});
  }
  // SK-07: the task is over -> park the agent's tab group (collapse/ungroup/none by
  // setting) so it stops standing out. Fire-and-forget: a timeline frame never fails.
  if (msg.state === "done" || msg.state === "failed") {
    if (LONGS.has(id)) finishLong(id, msg.state).catch(() => {}); // SK-35: bell + ✓ до парковки
    if (AG) parkAgentGroup().catch(() => {});
  }
}
function tlStep(msg) { // {type:"step",task,n,text?,state?,shot?,chat?} — upsert one step
  const n = parseInt(msg.n, 10);
  if (!Number.isInteger(n) || n < 1) return false;
  const e = taskOf(String(msg.task || TLK || "t").slice(0, 60));
  if (msg.chat != null) e.chat = String(msg.chat).slice(0, 80); // SK-23 (steps may arrive before the task frame)
  if (!e.state) e.state = "running"; // steps without a task frame = an implicit running task
  if (msg.state === "active" && e.state === "paused") e.state = "running"; // agent resumed after Stop
  const prev = e.steps.get(n) || {};
  const shot = typeof msg.shot === "string" && msg.shot.startsWith("data:") && msg.shot.length <= 2000000
    ? msg.shot : ""; // cap: a step frame must not flood the WS with megabytes
  e.steps.set(n, { n, text: String(msg.text ?? prev.text ?? "").slice(0, 300),
    state: ["queued", "active", "done", "failed"].includes(msg.state) ? msg.state : (prev.state || "queued"),
    shot: shot || prev.shot || "",
    // SK-36: raw — сырая команда под кат «подробнее»; caption — подпись агента
    // (приоритетный текст). Пустые поля НЕ затирают уже пришедшие (рефайн-кадр
    // refine_step несёт только text).
    raw: typeof msg.raw === "string" ? msg.raw.slice(0, 1200) : (prev.raw || ""),
    caption: typeof msg.caption === "string" ? msg.caption.slice(0, 160) : (prev.caption || "") });
  if (e.steps.size > 100) e.steps.delete(e.steps.keys().next().value);
  return true;
}

// ---- SK-35/SK-51: долгая задача прячет UI, по завершении зовёт человека -------------
// SK-51 (v0.7, пользователь 25.10): «долгая» — ТОЛЬКО по явному сигналу агента:
// кадр {type:"task",state:"running",long:true} (аддитивно к v1-контракту).
// Эвристика LONG_MS=10000 убрана: обычный интерактивный диалог НЕ сворачивается
// НИКОГДА, сколько бы ни шёл. Механика SK-35 сохранена целиком: панель сворачивается
// кадром sidebar_collapse (window.close() из sidebar-документа, см. sidebar.js:
// программное закрытие панели без пользовательского жеста схема Firefox отклоняет —
// проверено на сборке firefox-158 пробдой 23.10), группа вкладок сворачивается с
// префиксом ⏳. Терминальный кадр долгой задачи: bell (уведомление+бейдж решит
// bellRefresh) + запись в журнал шапки + переименование группы в ✓ (если других
// долгих не осталось). Разворачивание группы — ТОЛЬКО на перерыве на пермисс
// (perm_ask): человек идёт решать. Ручное сворачивание — кнопка в шапке, жива.
const LONGS = new Set(); // task -> в режиме долгой работы
async function markLong(id) {
  const e = TASKS.get(id);
  if (!e || e.state !== "running") return; // long:true без running — не триггер
  LONGS.add(id);
  // Сайдбар закрывает САМ себя кадром: у расширения нет жеста, а window.close() из
  // moz-extension-документа панели работает (пробда 23.10: isOpen true->false).
  // Кадр летит всем портам — в многооконной сборке свернутся все панели; для одного
  // рабочего окна (реальный пользователь) это ровно «закрыть сайдбар».
  sendToUi({ type: "sidebar_collapse" });
  const gid = await ownGroupId();
  if (gid == null) return;
  try {
    const g = await browser.tabGroups.get(gid);
    await browser.tabGroups.update(gid, { collapsed: true, title: bellGroupTitle(g.title, "long") });
  } catch (err) { /* группа могла уйти — не роняет таймлайн */ }
}
async function finishLong(id, state) {
  LONGS.delete(id);
  const e = TASKS.get(id);
  const title = (e && e.title) || id;
  const chat = (e && e.chat) || "";
  bellRing(id, { kind: "task_" + state, title: "Hermes: задача " + (state === "done" ? "сделана" : "не сделана"),
                 message: title, chat });
  bellLogPush({ kind: "task_" + state, id, text: state === "done" ? "Задача сделана" : "Задача не сделана",
                sub: title, chat, ts: Date.now() });
  const gid = await ownGroupId();
  if (gid == null) return;
  try {
    const g = await browser.tabGroups.get(gid);
    const upd = {};
    if (!LONGS.size) upd.title = bellGroupTitle(g.title, "done"); // другая долгая ещё пашет — ⏳ держим
    if (Object.keys(upd).length) await browser.tabGroups.update(gid, upd);
  } catch (err) { /* non-fatal */ }
}
async function expandGroupForPerm() {
  if (!LONGS.size) return; // не долгая задача — группа и так на виду (ensureGroup)
  const gid = await ownGroupId();
  if (gid == null) return;
  try { await browser.tabGroups.update(gid, { collapsed: false }); } catch (e) { /* non-fatal */ }
}

// ---- SK-25: задача↔вкладка (мост владеет привязкой, страница — исполнитель и свидетель) --
// TAB_TRACK хранит для каждой вкладки задачи URL, который агент видел последним, и ставит
// moved, когда вкладка сменила URL ВНЕ окна агентской навигации (grace после исполненного
// click/submit/key/dblclick — навигацию сделал агент). resolveTargetTab превращает это в
// честные машинные ошибки tab_gone/tab_moved: агент по п.9 переспрашивает человека, а не
// бьёт по чужой странице. Никакого pin: temp-add-on его не гарантирует, вкладка живёт в
// группе Sidekick (п.4).
const NAV_GRACE_MS = 4000;      // базовое окно агентской навигации после исполненного глагола
const NAV_GRACE_MAX_MS = 15000; // SK-44: потолок динамического окна SPA-цепочки после exec:True
const TAB_TRACK = new Map(); // tabId -> {url, moved, graceUntil, navOpenedAt}
const ERR_TAB_GONE = "tab_gone: вкладка задачи закрыта — переспроси человека в чате, не бей по чужой вкладке";
const ERR_TAB_MOVED = "tab_moved: вкладка задачи ушла на другой URL не агентом — переспроси человека; tabs.use_tab {tabId, task} перепривяжет";
// ЧИСТАЯ функция (контракт гоняет её под node): что свидетель знает о вкладке сейчас.
function tabGate(track, now) {
  if (!track) return "fresh";
  if (track.moved) return "moved";
  if (now < track.graceUntil) return "grace";
  return "ok";
}
// ЧИСТАЯ функция SK-44 (контракт гоняет её под node): вердикт свидетеля на ОЧЕРЕДНУЮ
// навигацию главного кадра вкладки задачи. Кормят её оба глаза — tabs.onUpdated (полные
// загрузки) и webNavigation (pushState/SPA, которых onUpdated не видит вовсе; аудит №2,
// инцидент B: собственный SPA-клик агента через несколько секунд давал ложный tab_moved).
//   url === эталон  — человек ВЕРНУЛСЯ на эталон (tab_returned снимает moved) либо
//                     подтвердил агентскую цель;
//   в окне агентской навигации (базовый grace ИЛИ динамическое окно navOpenedAt+MAX после
//     exec:True от action.do/use_tab) — навигировал агент: эталон следует за URL, а окно
//     продлевается от последней навигации (grace динамический, а не фиксированные 4 с;
//     ответ с exec:True уже несёт свежий url — noteUrl якорит эталон им), но НЕ дальше
//     потолка MAX от последнего exec-глагола — за ним снова правит честность;
//   вне окон — уход человека: moved липкий, эталон НЕ перетирается (S3b: возврат на
//     эталонный URL обязан совпасть, иначе tab_returned не сработает никогда).
function witnessNav(tr, url, now, graceMs, maxMs) {
  if (!tr || typeof url !== "string" || !url) return tr;
  if (url === tr.url) return tr.moved ? { ...tr, moved: false } : tr;
  const inGrace = now < tr.graceUntil;
  const inNav = tr.navOpenedAt > 0 && now < tr.navOpenedAt + maxMs;
  if (inGrace || inNav) return { ...tr, url, graceUntil: Math.max(tr.graceUntil, now + graceMs) };
  return { ...tr, moved: true };
}
function seedTrack(tabId, url) { // fresh bind/rebind: свежие претензии, grace на первую загрузку; SK-44: окно тоже открывается (use_tab — свидетель из спеки)
  const now = Date.now();
  TAB_TRACK.set(tabId, { url: String(url || ""), moved: false, graceUntil: now + NAV_GRACE_MS, navOpenedAt: now });
}
function noteUrl(tabId, resUrl) { if (tabId == null) return; const tr = TAB_TRACK.get(tabId); if (tr && typeof resUrl === "string" && resUrl) tr.url = resUrl; }
function noteNavGrace(tabId) { if (tabId == null) return; const tr = TAB_TRACK.get(tabId) || { url: "", moved: false, graceUntil: 0, navOpenedAt: 0 }; const now = Date.now(); tr.graceUntil = now + NAV_GRACE_MS; tr.navOpenedAt = now; TAB_TRACK.set(tabId, tr); }
// SK-44: единый свидетель URL вкладки задачи — диспетчер между двумя глазами навигаций.
// Без трека (вкладка не задача) — свидетеля нет и быть не может.
function witnessTabUrl(tabId, url) {
  const tr = TAB_TRACK.get(tabId);
  if (!tr) return false;
  const next = witnessNav(tr, url, Date.now(), NAV_GRACE_MS, NAV_GRACE_MAX_MS);
  if (next === tr) return true;
  if (next.moved && !tr.moved) log("SK-25: tab", tabId, "ушёл от задачи ->", String(url).slice(0, 90));
  else if (!next.moved && tr.moved) log("SK-25: tab", tabId, "вернулся к задаче");
  else if (next.url !== tr.url) log("SK-44: свидетель tab", tabId, "признал агентскую навигацию ->", String(url).slice(0, 90));
  TAB_TRACK.set(tabId, next);
  return true;
}
const NAV_ACTIONS = ["click", "submit", "key", "dblclick"]; // способны увести вкладку с URL
async function resolveTargetTab(tabId) { // -> {tab, legacy?} | {err}
  if (tabId == null) { // п.6 обратная совместимость: без tab — активная вкладка (как до SK-25)
    const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    return tab ? { tab, legacy: true } : { err: "no_tab" };
  }
  let tab;
  try { tab = await browser.tabs.get(tabId); } catch (e) { return { err: ERR_TAB_GONE }; }
  if (tabGate(TAB_TRACK.get(tabId), Date.now()) === "moved")
    return { err: ERR_TAB_MOVED + " · current=" + String(tab.url || "").slice(0, 160) };
  return { tab };
}
function taskTabFrame(m) { // {type:"task_tab",task,tab,window?,url?} — tab:null = отвязана (tab_gone)
  const t = String(m.task || "").slice(0, 60); if (!t) return;
  const e = taskOf(t);
  if (m.tab == null) { e.tab = null; e.tabGone = true; }
  else {
    e.tab = m.tab; e.tabGone = false;
    if (Number.isInteger(m.window)) e.window = m.window;
    if (m.url) e.url = String(m.url);
    seedTrack(m.tab, m.url);
    browser.tabs.get(m.tab).then((tb) => { e.tabTitle = tb.title || ""; e.window = tb.windowId; tlPush(); }).catch(() => {});
  }
  tlPush();
}
function refreshTabTitle(tabId) { // заголовок вкладки в карточке задачи живёт вместе с ней
  for (const e of TASKS.values()) if (e.tab === tabId) {
    browser.tabs.get(tabId).then((tb) => { e.tabTitle = tb.title || ""; if (tb.url) e.url = tb.url; tlPush(); }).catch(() => {});
  }
}

function log(...a) { console.log("[sidekick]", ...a); }
async function getToken() {
  const { bridgeToken } = await browser.storage.local.get("bridgeToken");
  return (bridgeToken || "").trim();
}

// SK-28: список кандидатов: сохранённый bridgeServer → localhost (туннельная
// совместимость). Пустое поле = ровно один localhost-кандидат = поведение до SK-28.
async function serverList() {
  const { bridgeServer } = await browser.storage.local.get("bridgeServer");
  const saved = String(bridgeServer || "").trim().replace(/\/+$/, "");
  const urls = [];
  for (const u of [saved, FALLBACK_SERVER]) if (u && !urls.includes(u)) urls.push(u);
  return urls;
}

// SK-46-BEGIN ws-client (hello-first контракт: node-юнит tests/test_channel_sk46.py
// прогоняет этот блок с mock WebSocket; менять globals-имена нельзя — харнесс их
// объявляет на globalThis). Контракт: hello — ВСЕГДА первый фрейм сокета (в onopen,
// синхронно, до любого hb); promote() только после hello_ack ok:true; send() до
// promote не пропускает ни один кадр. Реконнект = новый wsAttempt = снова hello.
function connect() {
  if (trying || (ws && ws.readyState <= 1)) return; // не плодим сокеты: alarm+timer не должны дать второй
  trying = true;
  (async () => {
    const token = await getToken();
    if (!token) { // нечем аутентифицироваться — спросим один раз, мост не спамим
      trying = false;
      status("no_token"); agState("unknown"); sendToUi({ type: "bridge", state: "no_token" });
      if (!optionsShown) { optionsShown = true; browser.runtime.openOptionsPage().catch(() => {}); }
      schedule();
      return;
    }
    const urls = await serverList();
    for (const url of urls) {
      const res = await wsAttempt(url, token);
      if (res === "open") { trying = false; return; } // сокет живой, цикл продолдит onclose
      if (res === "token_rejected" || res === "proto_mismatch" || res === "replaced") {
        trying = false; schedule(); return; // проблема не в адресе: любой мост отвергнет то же
      }
      log("SK-28: адрес", url, "недоступен — следующий кандидат");
    }
    // ни один адрес не ответил — человекочитаемый вердикт (бейдж ✗ + сайдбар-подсказка),
    // ретрай НЕ прекращаем: экспоненциал 2с..60с живёт дальше.
    trying = false; failStreak++;
    const st = failStreak >= 3 ? "offline" : "retry";
    status(st); sendToUi({ type: "bridge", state: st, url: urls[0] });
    schedule();
  })().catch(() => { trying = false; schedule(); });
}

// Один кандидат: open + hello + ждём hello_ack. «open» — только при ok:true:
// сокет без нашего ack — не наш мост (молчун умерщвляется таймаутом).
function wsAttempt(url, token) {
  return new Promise((resolve) => {
    let sock, done = false;
    try { sock = new WebSocket(url); } catch (e) { resolve("dead"); return; }
    const finish = (r) => {
      if (done) return; done = true; clearTimeout(to);
      if (r !== "open") { try { sock.close(); } catch (e) {} }
      resolve(r);
    };
    const to = setTimeout(() => finish("dead"), OPEN_TIMEOUT_MS + ACK_TIMEOUT_MS + 500);
    sock.onopen = () => {
      // SK-46: hello — ПЕРВЫЙ кадр этого сокета, синхронно в onopen. Никакой ветки
      // «открылся, а hello потом/через await»: шторм 22.10 вырос из сборок, где
      // heartbeat успевал прилететь раньше hello («first frame type='hb' != hello»).
      sock.send(JSON.stringify({ v: PROTO_V, type: "hello", token, ext: "hermes-sidekick", ver: "0.2" }));
      log("socket open, hello sent →", url);
    };
    sock.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "hello_ack") {
        if (msg.ok) { finish("open"); promote(sock, url); } // дальше кадры ведёт обычный route()
        return; // не-ok: вердикт придёт кодом закрытия (4003/4002), ждём onclose
      }
      (sock._pre = sock._pre || []).push(msg); // кадр между ack и promote — не терять
    };
    sock.onclose = (ev) => {
      let verdict = "dead";
      if (!done) { // живой сокет умер ИЛИ кандидат отказал — вердикт по коду
        if (ev.code === 4003) { // токен отвергнут: ретраи не помогут, мост ротировал токен
          status("token_rejected"); sendToUi({ type: "bridge", state: "token_rejected", url });
          backoff = 15000; verdict = "token_rejected";
          if (!optionsShown) { optionsShown = true; browser.runtime.openOptionsPage().catch(() => {}); }
        } else if (ev.code === 4002) {
          status("proto_mismatch"); sendToUi({ type: "bridge", state: "proto_mismatch", url });
          backoff = 15000; verdict = "proto_mismatch";
        } else if (ev.code === 4009) { // SK-49: мост замещён новым hello — перебор
          // адресов бессилен (тот же мост вытеснит снова), ждём и пробуем тем же списком.
          log("SK-49: сокет вытеснен новым подключением (4009), пауза", REPLACED_BACKOFF_MS, "мс");
          backoff = REPLACED_BACKOFF_MS; verdict = "replaced";
        } else log("bridge connection lost (code", ev.code + "), retry in", backoff, "ms");
      }
      finish(verdict);
    };
    sock.onerror = () => { try { sock.close(); } catch (e) {} };
  });
}

// Акк получен — сокет становится «тем самым», переводим на обычную маршрутизацию.
function promote(sock, url) {
  ws = sock; wsUrl = url; backoff = BACKOFF_MIN; failStreak = 0;
  sock._handshaked = true; // SK-46: пропуск send(); отсутствие флага = «handshake не
                           // подтверждён» = ни один кадр, кроме hello, не уходит
  status("online"); sendToUi({ type: "bridge", state: "online" });
  ws.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch { return; } route(m); };
  ws.onclose = (ev) => {
    if (ev.code === 4009) backoff = REPLACED_BACKOFF_MS; // SK-49: вытеснены — не стучимся сразу
    log("bridge connection lost (code", ev.code + "), retry in", backoff, "ms");
    agState("unknown"); // SK-09: no frames can pass a dead WS — nobody may claim a state
    status("retry"); sendToUi({ type: "bridge", state: "retry", url: wsUrl });
    schedule();
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
  for (const m of (sock._pre || [])) route(m); // state/ask'и между ack и promote
}

function schedule() {
  const d = backoff + Math.random() * 500;
  backoff = Math.min(backoff * 2, BACKOFF_MAX); // SK-28: 2с..60с
  setTimeout(connect, d);
}
// SK-15: «Проверить подключение» с options-страницы. НОВЫХ rpc-методов моста нет —
// переиспользуем уже согласованный hello-handshake: если сокет лежит, форсируем
// немедленное переподключение и ждём вердикт. Ответ никогда не содержит токен.
function wsAddr() { return (wsUrl || FALLBACK_SERVER).split(/[?#]/)[0]; } // адрес без токена (токен и не живёт в URL — strip на всякий случай)
async function wsCheck(sendResponse) {
  const token = await getToken();
  if (!token) { sendResponse({ state: "not_connected", reason: "токен не задан" }); return; }
  if (ws && ws.readyState === 1 && BRIDGE_ST === "online") { sendResponse({ state: "connected", url: wsAddr() }); return; }
  backoff = BACKOFF_MIN;
  if (ws) { try { ws.close(); } catch {} ws = null; }
  connect();
  const t0 = Date.now();
  while (Date.now() - t0 < 6000) {
    await new Promise((r) => setTimeout(r, 250));
    if (ws && ws.readyState === 1 && BRIDGE_ST === "online") { sendResponse({ state: "connected", url: wsAddr() }); return; }
    if (BRIDGE_ST === "token_rejected") { sendResponse({ state: "not_connected", reason: "мост отверг токен (ротация?) — вставь свежий" }); return; }
    if (BRIDGE_ST === "proto_mismatch") { sendResponse({ state: "not_connected", reason: "несовместимость протокола" }); return; }
  }
  sendResponse({ state: "not_connected", reason: BRIDGE_ST === "no_token" ? "токен не задан" : "мост не отвечает" });
}
function send(msg) {
  // SK-46: structural hello-first — этот путь живёт только для сокета с
  // подтверждённым handshake (флаг ставит promote). Даже если будущий правщик
  // дёрнет send() до ack, мост не увидит «first frame type='hb'».
  if (ws && ws.readyState === 1 && ws._handshaked) { ws.send(JSON.stringify({ v: PROTO_V, ...msg })); return true; } // contract v1: every frame carries v
  return false;
}
// SK-46-END ws-client
function status(t) { BRIDGE_ST = t; badge(); } // SK-09: bridge health picks the badge family, agent state the value
async function toActiveTab(msg) {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  try { await browser.tabs.sendMessage(tab.id, msg); } catch (e) { /* no content script (chrome pages) */ }
}
async function route(msg) {
  if (msg.type === "hello_ack") {
    if (msg.ok) {
      log("handshake ok (bridge v" + msg.v + ")");
      status("online"); sendToUi({ type: "bridge", state: "online" });
    } else {
      log("handshake refused:", msg.err); // onclose carries the code
    }
    return;
  }
  if (msg.type === "rpc") { handleRpc(msg); return; }
  if (msg.type === "task") { tlTask(msg); tlPush(); return; } // SK-06: timeline frames arrive via outbox broadcast
  if (msg.type === "step") { if (tlStep(msg)) tlPush(); return; }
  if (msg.type === "state") { // SK-09: the bridge's derived agent state (it is the ONLY author
                              // of this frame — the bridge drops agent-authored state pushes)
    agState(["idle", "thinking", "acting", "needs_you", "paused"].includes(msg.state) ? msg.state : "unknown");
    return;
  }
  if (msg.type === "perm_ask") { // SK-04: park + show in every sidebar (also late-openers)
    PENDING_PERM.set(msg.id, msg);
    sendToUi(msg);
    // SK-37: не видно карточку — звени (уведомление+бейдж решит bellRefresh).
    bellRing(msg.id, { kind: "perm", title: "Hermes нужен ты", message: bellPermMessage(msg) });
    // SK-35: пермисс — единственный повод развернуть свёрнутую долгую задачу
    // группу: человек идёт решать, вкладки агента должны быть на виду.
    bellLogPush({ kind: "perm", id: msg.id, text: "Нужно разрешение",
                  sub: bellPermMessage(msg), chat: "", ts: Date.now() });
    expandGroupForPerm().catch(() => {});
    return;
  }
  if (msg.type === "perm_shot") { // SK-37: миниатюра кадра с моста — в паркованный
    // кадр (поздний сайдбар увидит её при replay) и во все открытые карточки.
    const f = PENDING_PERM.get(msg.id);
    if (f && typeof msg.shot === "string") f.shot = msg.shot.slice(0, 500000);
    sendToUi(msg);
    return;
  }
  if (msg.type === "perm_resolved") { // SK-04: bridge closed the ask itself (e.g. ask_timeout)
    PENDING_PERM.delete(msg.id);
    bellClear(msg.id); // SK-37: ask закрыт мостом (timeout) — звонок больше не нужен
    sendToUi(msg);
    return;
  }
  if (msg.type === "handoff") { // SK-08: agent paused for the human — banner + chip; the bridge refuses page.* meanwhile
    PENDING_HO.set(msg.id, msg);  // SK-48: ре-пуш с note/shot/attempt перезаписывает кадр — replay отдаст свежий
    TLW.handoff = { id: msg.id, what: msg.what };  // SK-48: и ре-пуш обновляет what (рука та же)
    sendToUi(msg); tlPush(); return;
  }
  if (msg.type === "handoff_resolved") { // bridge closed it itself (timeout/superseded)
    PENDING_HO.delete(msg.id);
    if (TLW.handoff && TLW.handoff.id === msg.id) TLW.handoff = null;
    sendToUi(msg); tlPush(); return;
  }
  if (msg.type === "task_tab") { taskTabFrame(msg); return; } // SK-25: bind/rebind/unbind задачи к вкладке
  if (msg.type === "audit_data") { sendToUi(msg); return; } // SK-11: answer to a sidebar's audit_get
  if (msg.type === "chats_data") { sendToUi(msg); return; } // SK-23: answer to a sidebar's chats_get
  if (msg.type === "think") { // SK-21: agentd's thinking stream — stub render (full UI is SK-22)
    sendToUi({ type: "think", task: msg.task, text: msg.text, chat: msg.chat }); return;
  }
  if (msg.type === "cursor") toActiveTab({ type: "cursor", x: msg.x, y: msg.y, label: msg.label });
  else if (msg.type === "highlight") toActiveTab({ type: "highlight", selector: msg.selector });
  else if (msg.type === "preview") toActiveTab({ type: "preview", selector: msg.selector, label: msg.label, action: msg.action, name: msg.name, ms: msg.ms }); // SK-05 push mode (visual only — no verdict path; the agent path is rpc action.preview)
  else if (msg.type === "toast") toActiveTab({ type: "toast", text: msg.text });
  else if (msg.type === "chat") sendToUi({ type: "chat", role: "hermes", text: msg.text, task: msg.task, chat: msg.chat, ...(msg.memory ? { memory: true } : {}) }); // SK-21: task passthrough; SK-23: chat passthrough; SK-31: memory passthrough (все аддитивны — у старых кадров поля нет)
  else if (msg.type === "ping") send({ type: "pong" });
}

// ---- SK-03: page context RPC. Answers ONLY explicit bridge requests — never proactive. ----
function rpcResult(id, ok, payload, hint) {
  const msg = ok ? { type: "rpc_result", id, ok: true, result: payload }
                 : { type: "rpc_result", id, ok: false, err: String(payload || "error"),
                     ...(hint ? { hint: String(hint) } : {}) }; // SK-42 п.4: hint — аддитивно
  if (!send(msg)) log("rpc_result dropped (bridge offline):", id);
}
// ---- SK-18: self-healing page RPC (dead/missing content.js) -------------------------
// Вкладки, открытые ДО загрузки аддона (восстановленная сессия), и вкладки после
// перезагрузки/переустановки аддона живут без живого content.js: sendMessage падает и
// агент получал безликий отказ, неотличимый от болячки на служебных страницах.
// Теперь: сбой -> классифицировать URL -> на injectable-странице инжект + РОВНО ОДИН
// автоповтор исходного RPC; на служебной — честный internal_page; если после инжекта
// скрипт так и не ответил — inject_failed с подсказкой перезагрузки. Машинный код
// всегда префиксом err-строки (контракт v1 аддитивен, bridge/ не знает новых полей).
// ЧИСТАЯ ФУНКЦИЯ: контракт (tests/test_selfheal_sk18.py) вытащивает её дословно и гоняет
// под node — никаких внешних констант/браузерных API внутри.
function pageClass(url) {
  const u = String(url || "").trim().toLowerCase();
  return /^(https?|file):/.test(u) ? "injectable" : "internal";
}
const ERR_INTERNAL_PAGE = "internal_page: это служебная страница браузера (about:, moz-extension:, devtools:), content.js туда не инджектится — работай на http(s) или file";
const ERR_INJECT_FAILED = "inject_failed: автоинжект content.js не поднялся — перезагрузи вкладку (Cmd+R) и повтори запрос";
async function selfHealRpc(id, tab, msg, unwrapErr) {
  // Служебная страница опознаётся ДО любой попытки инжекта (никаких циклов: один
  // инжект, один повтор, дальше только честная ошибка — rpc_result ровно один раз).
  // Лог-строки («internal page», «inject threw», «retry failed», «self-heal: healed») —
  // якоря атрибуции живого харнесса, не менять без синхронной правки харнесса.
  if (pageClass(tab.url) === "internal") {
    log("self-heal: internal page, отказ от инжекта:", tab.id, String(tab.url || "").slice(0, 80));
    return rpcResult(id, false, ERR_INTERNAL_PAGE);
  }
  try {
    // bootstrap: сироты прошлой жизни — гард и хост от МЁРТВОГО content.js (аддон
    // перезагрузили при открытой вкладке): новый content.js на гард early-return-ится,
    // поэтому гард сбрасываем и мёртвый хост убираем ДО реинжекта.
    await browser.scripting.executeScript({ target: { tabId: tab.id }, func: () => { window.__hermesSidekick = false; const h = document.getElementById("hermes-sidekick-host"); if (h) h.remove(); } });
    await browser.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
  } catch (e) {
    log("self-heal: inject threw:", String(e && e.message || e).slice(0, 140));
    return rpcResult(id, false, ERR_INJECT_FAILED);
  }
  let res;
  try { res = await browser.tabs.sendMessage(tab.id, msg); } // РОВНО ОДИН автоповтор
  catch (e) {
    log("self-heal: retry failed — content.js после инжекта не отвечает:", tab.id);
    return rpcResult(id, false, ERR_INJECT_FAILED);
  }
  log("self-heal: healed", msg && msg.type, "tab", tab.id);
  noteUrl(tab.id, res && res.url); // SK-25: URL-свидетель обновляется и на залеченном пути
  if (msg && msg.type === "action_do" && res && res.done && NAV_ACTIONS.includes(msg.action)) noteNavGrace(tab.id);
  if (unwrapErr && res && res.err) return rpcResult(id, false, res.err, res.hint); // action_do: {err,hint?} -> rpc error (SK-42)
  rpcResult(id, true, res || { err: "no_response" });
}
// SK-25: tabId (params.tab, инжектированный мостом) — конкретная вкладка, а не активная
// вкладка. Без tabId — легаси-путь (п.6 обратная совместимость). tab_gone/tab_moved —
// машинные префиксы: агент обязан остановиться и переспросить человека (AGENTS.md п.9).
async function rpcToTab(id, msg, tabId) {
  const r = await resolveTargetTab(tabId);
  if (r.err) return rpcResult(id, false, r.err);
  const tab = r.tab;
  try {
    const res = await browser.tabs.sendMessage(tab.id, msg);
    noteUrl(tab.id, res && res.url); // SK-25: свидетель видел этот URL после исполнения
    rpcResult(id, true, res);
  } catch (e) {
    selfHealRpc(id, tab, msg); // SK-18: первый сбой — в самоизлечение, не в безликий отказ
  }
}
// SK-17: execution leg of action.do. The preview gate is enforced on the BRIDGE —
// this method only ever arrives after a clean SK-05 verdict (the raw method never
// passes through). The content script reports failures as {err:...}; they become
// rpc errors here. A success result passes verbatim (executed/synthetic/url).
// SK-25: исполнение — в конкретной вкладке задачи (resolveTargetTab), а навигационный
// click/submit/key оставляет grace: ушедший после него URL — дело рук агента, не человека.
async function actionDoRpc(id, params) {
  const r = await resolveTargetTab(params.tab);
  if (r.err) return rpcResult(id, false, r.err);
  const tab = r.tab;
  sk47Inval(tab.id); // SK-47 п.3: любое агентское действие (клик/ввод/скролл) снимает кэш кадра
  const msg = { type: "action_do", selector: params.selector,
                action: params.action, value: params.value, name: params.name,
                // SK-24: coordinates/keys/frames — additive, SK-17 callers send none of these
                x: params.x, y: params.y, frame: params.frame,
                key: params.key, modifiers: params.modifiers,
                role: params.role, // SK-24 аддендум (в): name/role resolver
                mark: params.mark, // SK-26: клик по номеру с последнего SoM-скрина (карта — в content)
                sid: params.sid, // SK-27: цель — стабильный id из последнего page.dom (реестр — в content)
                // SK-42: scroll (dy/to) и text-алиас value — аддитивно, старые вызовы не затронуты
                dy: params.dy, to: params.to, text: params.text };
  let res;
  try {
    res = await browser.tabs.sendMessage(tab.id, msg);
  } catch (e) { return selfHealRpc(id, tab, msg, true); } // SK-18: тот же путь самоизлечения
  if (res && res.err) return rpcResult(id, false, res.err, res.hint); // SK-42: hint доезжает до моста
  if (res && res.done) {
    noteUrl(tab.id, res.url);
    if (NAV_ACTIONS.includes(params.action)) noteNavGrace(tab.id); // навигацию сделал агент
  }
  rpcResult(id, true, res || { err: "no_response" });
}
// ---- SK-47: vision-скорость — даунскейл/кроп кадра, кэш 2 с, метка масштаба ----------------
// (Аудит №2 22.10, проблема 7: vision_analyze 33–120 с на кадрах PNG 2578×1898 DPR2 — модель
// задыхается на полном кадре; лечим на стороне расширения: кадр уходит с браузера ужатым.)
// SK-47-BEGIN transform (node-юнит tests/test_vision_sk47.py: чистая математика, без canvas/DOM)
// sk47Plan — план превращения кадра. cap — размер захвата (device-пиксели); vp — метрики
// вьюпорта {w,h} в CSS-пикселях (null — внутренняя страница без content-скрипта); clip —
// регион кроя в CSS-пикселях вьюпорта (та же система, что у page.dom/marks/action.do x,y),
// может быть null; mw — max_width выходного кадра (0 = без даунскейла, «чистый» PNG-путь).
// Возвращает {cx,cy,cw,ch, outW,outH, scale, jpeg, capW,capH} или {err}.
// scale = пикселей отправленного кадра на CSS-пиксель страницы: координаты, прочитанные с
// кадра, агент делит на него обратно (x_css = clip.x + x_frame/scale); rect карты marks/SoM
// остаются в CSS-пикселях (пространство клика action.do) и в масштаб кадра НЕ пересчитываются.
function sk47Plan(cap, vp, clip, mw) {
  const num = (v) => typeof v === "number" && isFinite(v);
  if (!num(cap.w) || !num(cap.h) || cap.w < 1 || cap.h < 1) return { err: "capture_metrics" };
  const hasVp = !!(vp && num(vp.w) && num(vp.h) && vp.w >= 1 && vp.h >= 1);
  if (clip) {
    if (!num(clip.x) || !num(clip.y) || !num(clip.w) || !num(clip.h) || clip.w <= 0 || clip.h <= 0)
      return { err: "bad_clip" };
    if (!hasVp) return { err: "no_metrics: clip задаётся в CSS-пикселях, метрик вьюпорта вкладки нет" };
  }
  const sx = hasVp ? cap.w / vp.w : 1, sy = hasVp ? cap.h / vp.h : 1; // device-пикселей на CSS-пиксель (DPR)
  let cx = 0, cy = 0, cw = cap.w, ch = cap.h;
  if (clip) { // кроп клипается границами кадра: диалог у края — честный обрезанный кадр
    cx = Math.max(0, Math.min(cap.w - 1, Math.round(clip.x * sx)));
    cy = Math.max(0, Math.min(cap.h - 1, Math.round(clip.y * sy)));
    cw = Math.max(1, Math.min(cap.w - cx, Math.round(clip.w * sx)));
    ch = Math.max(1, Math.min(cap.h - cy, Math.round(clip.h * sy)));
  }
  let outW = cw, outH = ch;
  if (mw > 0 && cw > mw) { outH = Math.max(1, Math.round((ch * mw) / cw)); outW = mw; }
  return { cx, cy, cw, ch, outW, outH,
           scale: hasVp ? (outW * sx) / cw : null, jpeg: mw > 0, capW: cap.w, capH: cap.h };
}
// SK-47-END transform
// SK-47: PNG-захват → кроп/даунскейл OffscreenCanvas → JPEG q80 (vision-путь) либо PNG.
// always-decode честен даже когда канвас не нужен: scale/capture считываются из реального
// кадра, а не из догадок. Сорванный декод — raw PNG с scale:null (честно, не выдумка).
async function sk47Xform(pngDataUrl, vp, clip, mw) {
  let bmp = null;
  try {
    const bin = atob(pngDataUrl.slice(pngDataUrl.indexOf(",") + 1));
    const u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    bmp = await createImageBitmap(new Blob([u], { type: "image/png" }));
    const p = sk47Plan({ w: bmp.width, h: bmp.height }, vp, clip, mw);
    if (p.err) return p;
    if (!clip && !p.jpeg) // «чистый» путь (max_width:0): канвас не нужен, только метки
      return { dataUrl: pngDataUrl, scale: p.scale, capW: p.capW, capH: p.capH };
    const cv = new OffscreenCanvas(p.outW, p.outH);
    const ctx = cv.getContext("2d");
    if (p.jpeg) { ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, p.outW, p.outH); } // у JPEG нет альфы
    ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
    ctx.drawImage(bmp, p.cx, p.cy, p.cw, p.ch, 0, 0, p.outW, p.outH);
    const blob = await cv.convertToBlob({ type: p.jpeg ? "image/jpeg" : "image/png",
                                          ...(p.jpeg ? { quality: 0.8 } : {}) });
    const buf = new Uint8Array(await blob.arrayBuffer());
    let s = "";
    for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return { dataUrl: "data:" + (p.jpeg ? "image/jpeg" : "image/png") + ";base64," + btoa(s),
             scale: p.scale, capW: p.capW, capH: p.capH };
  } catch (e) {
    return { dataUrl: pngDataUrl, scale: null, capW: null, capH: null, note: "transform_failed" };
  } finally { if (bmp && bmp.close) bmp.close(); }
}
// SK-47 п.3: кэш кадра 2 с — повторный вызов тех же параметров без скролла/навигации/
// action.do отдаёт ТОТ ЖЕ кадр (hit:"cache"), спасает от двойных vision-зондов. Свежесть —
// ленивая (та же дисциплина, что mark_stale): перед выдаче сверяются метрики вьюпорта с
// моментом съёмки; агентский action.do, навигация и закрытие вкладки чистят запись напрямую.
// Ручные DOM-мутации человека без скролла не ловятся — их ограничивает сам TTL 2 с.
const SHOT_CACHE = new Map(); // tabId -> {key, out, ts, vp}
const SHOT_CACHE_MS = 2000;
const sk47Inval = (tabId) => { SHOT_CACHE.delete(tabId); };
async function sk47Vp(tabId) { // метрики вьюпорта с content-скрипта; null = скрипта нет
  try { return await browser.tabs.sendMessage(tabId, { type: "sk47_vp" }); }
  catch (e) { return null; }
}
const vpSame = (a, b) => !!a && !!b && a.w === b.w && a.h === b.h && a.sx === b.sx && a.sy === b.sy;
// SK-25: снимок вкладки задачи. captureTab (сборка firefox-158+) честен и для ФОНОВОЙ
// вкладки; captureVisibleTab умеет только активную вкладку окна — для фоновой это
// честный отказ, а не чужой кадр.
// SK-26: {marks:true} — SoM-снимок: content.js РИСУЕТ номерные метки (изолированный слой,
// pointer-events:none), кадр берётся сразу после отрисовки, метки убираются сразу за ним
// (страховочный таймер 2 с — в самом content). Ответ несёт карту
// marks:[{n,selector,tag,name,rect}]: action.do {mark:N} резолвится через неё на стороне
// content (скролл/навигация -> mark_stale). Кадр снимается ВМЕСТЕ с метками — captureVisibleTab/
// captureTab видят DOM контент-скрипта (тот же механизм, что делает видимым preview-кольцо).
// SK-47: {max_width?} — даунскейл на стороне расширения (дефолт 1280, JPEG q80 — vision-путь;
// max_width:0 — «чистый» полномасштабный PNG); {clip:{x,y,w,h}} — кроп региона в CSS-пикселях.
// Ответ несёт scale (кадровых пикселей на CSS-пиксель) и capture:{w,h} (размер исходного
// захвата); повтор вызова ≤2 с тех же параметров на неизменном вьюпорте — тот же кадр hit:"cache".
async function pageScreenshot(id, params) {
  const wantMarks = params.marks === true;
  // SK-47: vision-дефолт 1280; 0 — явный «чистый» кадр. Мусор в параметре отсекает мост
  // (bad_request), сюда доезжает только число; нечисловой остаток — тоже дефолт, не краш.
  const mw = params.max_width === 0 ? 0
    : (typeof params.max_width === "number" && params.max_width > 0 ? params.max_width : 1280);
  const clip = params.clip || null;
  let marksTab = null;
  const dropMarks = () => { if (marksTab) browser.tabs.sendMessage(marksTab.id, { type: "som_marks_clear" }).catch(() => {}); };
  try {
    let tab = null;
    if (params.tab != null) {
      const r = await resolveTargetTab(params.tab);
      if (r.err) return rpcResult(id, false, r.err);
      tab = r.tab;
    }
    if (!tab) [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    // SK-47 п.3: чтение кэша — ДО отрисовки меток: хит это сохранённый кадр, бейджи
    // перерисовывать не за чем (карта marks жива: скролл/навигация сбивают и её, и кэш).
    const ckey = JSON.stringify([wantMarks ? 1 : 0, mw, clip || 0]);
    if (tab) {
      const ce = SHOT_CACHE.get(tab.id);
      if (ce && ce.key === ckey && Date.now() - ce.ts < SHOT_CACHE_MS) {
        if (vpSame(await sk47Vp(tab.id), ce.vp))
          return rpcResult(id, true, { ...ce.out, hit: "cache" });
        SHOT_CACHE.delete(tab.id);
      }
    }
    let marksRes = null;
    if (wantMarks) {
      if (!tab) return rpcResult(id, false, "no_tab");
      marksTab = tab;
      try {
        marksRes = await browser.tabs.sendMessage(tab.id, { type: "som_marks" });
      } catch (e) {
        return rpcResult(id, false, "marks_unavailable: content-скрипт вкладки не отвечает — залечи вкладку (page.dom/self-heal SK-18) и повтори скрин");
      }
      if (!marksRes || marksRes.err) return rpcResult(id, false, (marksRes && marksRes.err) || "marks_failed");
    }
    // SK-47: метрики — ДО capture (иначе страница «уедет» между кадром и scale); somDraw
    // несёт их с момента отрисовки — вторым раунд-трипом не плодим.
    let vp = marksRes && marksRes.vp ? marksRes.vp : (tab ? await sk47Vp(tab.id) : null);
    const out = {};
    const raw = tab && typeof browser.tabs.captureTab === "function"
      ? await browser.tabs.captureTab(tab.id, { format: "png" })
      : (!tab || tab.active)
        ? await browser.tabs.captureVisibleTab(tab ? tab.windowId : null, { format: "png" })
        : null;
    if (raw === null)
      return rpcResult(id, false, "capture_unsupported_background_tab: в этой сборке нет tabs.captureTab — активная вкладка окна доступна, фоновая нет");
    const x = await sk47Xform(raw, vp, clip, mw);
    if (x.err) return rpcResult(id, false, x.err);
    out.dataUrl = x.dataUrl;
    if (x.scale != null) out.scale = x.scale;
    if (x.capW) out.capture = { w: x.capW, h: x.capH };
    if (clip) out.clip = clip; // свидетель: кадр — этот регион (координаты с кадра: +clip, /scale)
    if (x.note) out.note = x.note;
    if (marksTab) {
      out.marks = Array.isArray(marksRes.marks) ? marksRes.marks : [];
      if (marksRes.url) { out.url = marksRes.url; noteUrl(marksTab.id, marksRes.url); } // SK-25: свидетель видел этот URL
      dropMarks(); // п.5: убрать метки СРАЗУ после снимка
    }
    if (params.tab != null) out.tabId = params.tab;
    // SK-47 п.3: пишем кэш только с живыми метриками и если вьюпорт за время съёмки не
    // сдвинулся (иначе кадр противоречил бы собственному scale).
    if (tab && vp) {
      const vpPost = await sk47Vp(tab.id);
      if (vpSame(vpPost, vp)) SHOT_CACHE.set(tab.id, { key: ckey, out: { ...out }, ts: Date.now(), vp: vpPost });
    }
    rpcResult(id, true, out);
  } catch (e) {
    dropMarks(); // сорванный capture не должен оставлять метки человеку
    rpcResult(id, false, "capture_failed");
  }
}
// SK-25 п.5: единственный легальный вопрос про активную вкладку — явный page.active {}.
async function pageActive(id) {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab) return rpcResult(id, false, "no_tab");
  rpcResult(id, true, { tabId: tab.id, windowId: tab.windowId, url: tab.url || "", title: tab.title || "" });
}
async function handleRpc(msg) {
  const { id, method } = msg, params = msg.params || {};
  if (!id) return; // nothing to answer to
  if (method === "page.text") rpcToTab(id, { type: "page_text", maxChars: params.maxChars }, params.tab);
  else if (method === "page.dom") rpcToTab(id, { type: "page_dom",
    // SK-27: {near:[x0,y0,x1,y1]} — регион снапшота; форму уже проверил мост (bad_request
    // локально), сюда долетает только валидный массив или undefined (весь экран).
    opts: params.near ? { near: params.near } : undefined }, params.tab);
  else if (method === "page.media") rpcToTab(id, { type: "page_media" }, params.tab); // SK-48: видео-состояние, таб резолверит мост (task)
  else if (method === "action.preview") rpcToTab(id, { type: "preview", selector: params.selector, label: params.label, action: params.action, name: params.name, ms: params.ms, x: params.x, y: params.y, frame: params.frame, key: params.key, role: params.role, mark: params.mark, sid: params.sid }, params.tab); // SK-05: verdict (cancelled?) comes back as the rpc result; SK-24: point/key/name preview modes; SK-25: preview рисуется на вкладке задачи; SK-26: mark — кольцо на элементе из карты скрина; SK-27: sid — кольцо на элементе реестра page.dom
  else if (method === "action.do") actionDoRpc(id, params); // SK-17: execution leg — the BRIDGE already passed the preview gate
  else if (method === "page.screenshot") pageScreenshot(id, params); // SK-25: tab-aware capture
  else if (method === "page.active") pageActive(id); // SK-25: «что у человека открыто»
  // ---- SK-07: agent tab groups (AGENTS.md п.6 — agent tabs go ONLY through these) ----
  else if (method === "tabs.open_grouped") tabsOpenGrouped(id, params);
  else if (method === "tabs.use_tab") tabsUseTab(id, params);
  else if (method === "tabs.highlight_group") tabsHighlightGroup(id, params);
  else if (method === "tabs.park_group") tabsParkGroup(id, params);
  // ---- SK-32: вкладки (инвентаризация/закрытие/фокус) — close доходит только после
  // гейта моста (защищённые SK-25 + разрешение человека на пачку) ----
  else if (method === "tabs.list") tabsList(id);
  else if (method === "tabs.close") tabsClose(id, params);
  else if (method === "tabs.activate") tabsActivate(id, params);
  else rpcResult(id, false, "unknown_method"); // grants.*/perm.* arrive with SK-04
}

// ---- SK-07: tab group «Sidekick» (blue) for the agent's tabs --------------------------
// Firefox API (research 2026-10-17-firefox-tabgroups-sidebar-action.md): tabs.group/
// tabs.ungroup/Tab.groupId — FF 138; browser.tabGroups (title/color/collapsed) — FF 139
// + the "tabGroups" manifest permission. groupId does NOT survive session restore and ids
// may be reused — the cache below is only valid within this run; the group's identity is
// its title, re-adopted via tabGroups.query. tabs.highlight takes only indexes and the
// FIRST index becomes the active tab (MDN) — that is how the source tab is emphasized.
const GROUP_TITLE = "Sidekick", GROUP_COLOR = "blue";
const GROUP_COLORS = ["blue", "cyan", "grey", "green", "orange", "pink", "purple", "red", "yellow"];
const AFTER_MODES = ["collapse", "ungroup", "none"];
let AG = null; // {groupId, windowId} — validated on every use via groupGet(); null = no group this run
const hasGroupApi = () => typeof browser.tabs.group === "function" && !!browser.tabGroups;

async function groupGet(gid) {
  if (gid == null) return null;
  try { return await browser.tabGroups.get(gid); } catch { return null; } // gone/restarted
}
async function findGroup(windowId, title) {
  const all = await browser.tabGroups.query({}); // query({windowId}) support unproven — filter here
  for (const g of all) if (g.windowId === windowId && g.title === title) return g.id;
  return null;
}
async function ensureGroup(tabIds, windowId, title, color) {
  // -> {groupId, created}; adds the tabs to the agent group (tabs.group moves them itself).
  // Re-branding + un-collapse on every agent action: the human must always SEE the group.
  let gid = AG && AG.windowId === windowId && (await groupGet(AG.groupId)) ? AG.groupId : null;
  if (gid == null) gid = await findGroup(windowId, title);
  let created = false;
  if (gid == null) { gid = await browser.tabs.group({ tabIds }); created = true; }
  else await browser.tabs.group({ tabIds, groupId: gid });
  AG = { groupId: gid, windowId };
  // SK-35: во время долгой задачи группу НЕ разворачиваем и не переименовываем —
  // новый таб посреди свёрнутой работы не должен выдёргивать UI человека обратно.
  const longNow = LONGS.size > 0;
  const upd = { title: bellGroupTitle(title, longNow ? "long" : ""), color };
  if (!longNow) upd.collapsed = false;
  try { await browser.tabGroups.update(gid, upd); } catch (e) { /* non-fatal */ }
  return { groupId: gid, created };
}
async function ownGroupId() { // cached id, re-validated; null when the group is gone
  if (AG && (await groupGet(AG.groupId))) return AG.groupId;
  AG = null;
  return null;
}
async function afterTaskMode() { // parking mode after a task: hub setting, default collapse
  const { sk07AfterTask } = await browser.storage.local.get("sk07AfterTask");
  return AFTER_MODES.includes(sk07AfterTask) ? sk07AfterTask : "collapse";
}
async function parkAgentGroup(mode) {
  const gid = await ownGroupId();
  if (gid == null) return { parked: false, reason: "no_group", mode: mode || "auto" };
  if ((mode || "auto") === "auto") mode = await afterTaskMode();
  if (!AFTER_MODES.includes(mode)) mode = "collapse";
  if (mode === "none") return { parked: false, mode };
  if (mode === "collapse") await browser.tabGroups.update(gid, { collapsed: true });
  else { // ungroup: tabs stay open, the group disappears (Firefox removes empty groups itself)
    const t = await browser.tabs.query({ groupId: gid });
    if (t.length) await browser.tabs.ungroup(t.map((x) => x.id));
    AG = null;
  }
  return { parked: true, mode, groupId: gid };
}
async function tabsOpenGrouped(id, params) {
  const url = String(params.url || "").trim().slice(0, 2000);
  if (!url) return rpcResult(id, false, "url_required");
  let tab;
  try {
    tab = await browser.tabs.create({ url, active: params.focus === true,
                                      ...(Number.isInteger(params.windowId) ? { windowId: params.windowId } : {}) });
  } catch (e) { return rpcResult(id, false, "create_failed"); }
  const out = { tabId: tab.id, url: tab.url && tab.url !== "about:blank" ? tab.url : url,
                windowId: tab.windowId, grouped: false };
  if (!hasGroupApi()) { out.groupErr = "unsupported"; return rpcResult(id, true, out); } // FF<138: tab opened anyway
  const title = String(params.title || GROUP_TITLE).slice(0, 40) || GROUP_TITLE;
  const color = GROUP_COLORS.includes(params.color) ? params.color : GROUP_COLOR;
  try {
    const g = await ensureGroup([tab.id], tab.windowId, title, color);
    Object.assign(out, { groupId: g.groupId, groupCreated: g.created, title, color, grouped: true });
  } catch (e) { out.groupErr = String(e && e.message || e).slice(0, 120); }
  rpcResult(id, true, out); // the tab exists whatever the group says — the agent needs its id
}
async function tabsUseTab(id, params) { // an EXISTING tab the agent starts to work in
  if (!hasGroupApi()) return rpcResult(id, false, "unsupported");
  let tab;
  if (params.tabId != null) {
    try { tab = await browser.tabs.get(params.tabId); } catch (e) { return rpcResult(id, false, "no_tab"); }
  } else {
    [tab] = await browser.tabs.query({ active: true, currentWindow: true });
    if (!tab) return rpcResult(id, false, "no_tab");
  }
  // SK-25: use_tab с task+tabId — глагол ПЕРЕПРИВЯЗКИ (восстановление после tab_moved):
  // явное присвоение вкладки задаче снимает претензию свидетеля. Без task — только группа.
  if (params.tabId != null && typeof params.task === "string" && params.task.trim())
    seedTrack(tab.id, tab.url);
  const own = await ownGroupId();
  if (own != null && tab.groupId === own) return rpcResult(id, true, { tabId: tab.id, windowId: tab.windowId, url: tab.url || "", groupId: own, already: true });
  const foreign = tab.groupId != null && tab.groupId !== -1; // research п.9: report, don't silently shuffle
  const title = String(params.title || GROUP_TITLE).slice(0, 40) || GROUP_TITLE;
  const color = GROUP_COLORS.includes(params.color) ? params.color : GROUP_COLOR;
  try {
    const g = await ensureGroup([tab.id], tab.windowId, title, color);
    rpcResult(id, true, { tabId: tab.id, windowId: tab.windowId, url: tab.url || "", groupId: g.groupId, already: false, ...(foreign ? { fromGroup: tab.groupId } : {}) });
  } catch (e) { rpcResult(id, false, "group_failed"); }
}
async function tabsHighlightGroup(id, params) { // «вкладка-источник подсвечивается»: native multi-select
  const gid = await ownGroupId();
  if (gid == null) return rpcResult(id, false, "no_group");
  const tabs = await browser.tabs.query({ groupId: gid });
  if (!tabs.length) return rpcResult(id, false, "no_group");
  let idx = tabs.map((t) => t.index), activeTabId = tabs[0].id;
  if (params.tabId != null) { // the source tab goes FIRST — the first highlighted index becomes active (MDN)
    const i = tabs.findIndex((t) => t.id === params.tabId);
    if (i < 0) return rpcResult(id, false, "tab_not_in_group");
    idx = [idx[i], ...idx.filter((_, j) => j !== i)];
    activeTabId = params.tabId;
  }
  try {
    await browser.tabs.highlight({ windowId: tabs[0].windowId, tabs: idx });
    rpcResult(id, true, { groupId: gid, highlighted: tabs.map((t) => t.id), activeTabId });
  } catch (e) { rpcResult(id, false, "highlight_failed"); }
}
async function tabsParkGroup(id, params) { // after the task the group must not stand out
  if (!hasGroupApi()) return rpcResult(id, false, "unsupported");
  try { rpcResult(id, true, await parkAgentGroup(AFTER_MODES.includes(params.mode) ? params.mode : "auto")); }
  catch (e) { rpcResult(id, false, "park_failed"); }
}
// ---- SK-32: tabs.list / tabs.close / tabs.activate (аудит 21.10 п.5: без вкладочных
// глаголов агент полез в обход моста). Инвентаризация — все окна; закрытие — пачкой,
// ГЕЙТ НА МОСТУ (защищённые вкладки SK-25 + одно разрешение человека на вызов), сюда
// метод доходит только после чистого вердикта; последняя вкладка окна без force не
// закрывается (это закрыло бы окно — никогда молча).
async function tabsList(id) {
  const tabs = await browser.tabs.query({}); // {} = все окна
  rpcResult(id, true, { tabs: tabs.map((t) => ({
    tabId: t.id, url: t.url || "", title: t.title || "", active: !!t.active,
    pinned: !!t.pinned, groupId: t.groupId == null || t.groupId === -1 ? null : t.groupId,
    windowId: t.windowId, index: t.index })) });
}
async function tabsClose(id, params) {
  const ids = Array.isArray(params.tabIds) ? params.tabIds
            : (params.tabId != null ? [params.tabId] : []); // мост нормализует; прямые вызовы — тоже
  if (!ids.length) return rpcResult(id, false, "tabs_required");
  const force = params.force === true;
  const closed = [], skipped = [];
  for (const tabId of ids) {
    let t;
    try { t = await browser.tabs.get(tabId); } catch (e) { skipped.push({ tabId, reason: "no_tab" }); continue; }
    if (!force) {
      const inWin = await browser.tabs.query({ windowId: t.windowId });
      if (inWin.length <= 1) { skipped.push({ tabId, reason: "last_in_window" }); continue; }
    }
    try { await browser.tabs.remove(tabId); closed.push(tabId); }
    catch (e) { skipped.push({ tabId, reason: "remove_failed" }); }
  }
  rpcResult(id, true, { closed, skipped, force });
}
async function tabsActivate(id, params) { // фокус вкладки + окна; подтверждение — в ответе
  if (!Number.isInteger(params.tabId)) return rpcResult(id, false, "bad_request");
  let t;
  try { t = await browser.tabs.update(params.tabId, { active: true }); }
  catch (e) { return rpcResult(id, false, "no_tab"); }
  try { await browser.windows.update(t.windowId, { focused: true }); } catch (e) { /* уже в фокусе */ }
  rpcResult(id, true, { activated: true, tabId: t.id, windowId: t.windowId,
                        url: t.url || "", title: t.title || "" });
}
if (browser.tabGroups) { // the human closed/ungrouped/renamed-away — drop the stale cache
  browser.tabGroups.onRemoved.addListener((g) => { if (AG && AG.groupId === g.id) AG = null; });
}
function sendToUi(msg) { for (const p of Array.from(clients)) { try { p.postMessage(msg); } catch { clients.delete(p); } } }

browser.runtime.onConnect.addListener((port) => {
  if (port.name !== "sidebar") return;
  clients.add(port);
  // SK-37: сайдбар открыли — уведомления не нужны (карточка перед глазами);
  // закрыли — самое время позвонить по всем висящим пермисам.
  bellRefresh();
  port.onDisconnect.addListener(() => { clients.delete(port); bellRefresh(); });
  port.onMessage.addListener((m) => {
    if (m.type === "chat") {
      // SK-23: the active chat rides along (additive — an old sidebar sends no chat,
      // the bridge then treats the message as the "default" chat).
      const chat = String(m.chat || "").slice(0, 80);
      sendToUi({ type: "chat", role: "me", text: m.text, ...(chat ? { chat } : {}) });
      send({ type: "chat", role: "user", text: m.text, ...(chat ? { chat } : {}) });
    } else if (m.type === "command") send({ type: "command", text: m.text });
    else if (m.type === "perm_answer") { // human verdict -> bridge (it resolves the rpc)
      if (!PENDING_PERM.has(m.id)) return;
      PENDING_PERM.delete(m.id);
      bellClear(m.id); // SK-37: человек ответил с карточки — будильник больше не нужен
      send({ type: "perm_answer", id: m.id, choice: m.choice });
      sendToUi({ type: "perm_resolved", id: m.id, choice: m.choice }); // grey out in all sidebars
    } else if (m.type === "perm_reason") { // SK-34 п.4: чип-причина ПОСЛЕ вердикта —
      send({ type: "perm_reason", id: String(m.id || "").slice(0, 80),  // только в аудит,
             reason: String(m.reason || "").slice(0, 80) });            // ask уже закрыт
    } else if (m.type === "stop") { // SK-06: ■ Stop — pause the timeline NOW, abort frame to the bridge
      const e = TASKS.get(String(m.task || "").slice(0, 60)) || taskOf(TLK || "t");
      e.state = "paused"; TLK = e.task; // paused-by-Stop stays visibly distinct from task-done
      const delivered = send({ type: "stop", ...(m.task ? { task: String(m.task).slice(0, 60) } : {}) });
      TLW.warn = delivered ? "" : "мост офлайн — агент не уведомлён";
      tlPush();
    } else if (m.type === "tab_focus") { // SK-25 п.4: клик по чипу вкладки карточки — ЖЕСТ ЧЕЛОВЕКА, фокус вкладку задачи
      const e = TASKS.get(String(m.task || "").slice(0, 60));
      if (e && e.tab != null) {
        browser.tabs.update(e.tab, { active: true })
          .then((tb) => { const w = (tb && tb.windowId) || e.window; if (Number.isInteger(w)) return browser.windows.update(w, { focused: true }); })
          .catch(() => {}); // вкладка могла закрыться между кликами — карточка обновится сама
      }
    } else if (m.type === "resume") { // SK-08: «Готово, продолжай» — human finished, unblock the agent
      if (!PENDING_HO.has(m.id)) return; // double click / stale banner
      const delivered = send({ type: "resume", id: m.id,
                               ...(m.task ? { task: String(m.task).slice(0, 60) } : {}) });
      if (delivered) {
        PENDING_HO.delete(m.id);
        if (TLW.handoff && TLW.handoff.id === m.id) TLW.handoff = null;
        tlPush();
        sendToUi({ type: "handoff_resolved", id: m.id, choice: "resumed" }); // grey out in all sidebars
      } else {
        sendToUi({ type: "handoff_offline", id: m.id }); // banner stays clickable — the bridge never heard the click
      }
    } else if (m.type === "audit_get") { // SK-11: the sidebar's audit-feed pull — the bridge
      // answers audit_data on the WS (consumed there, never inbox). No caching here:
      // the file is the single source of truth, and the feed must match it exactly.
      const delivered = send({ type: "audit_get", id: String(m.id || "").slice(0, 40),
                               ...(Number.isFinite(m.limit) ? { limit: m.limit } : {}),
                               ...(m.domain ? { domain: String(m.domain).slice(0, 120) } : {}) });
      if (!delivered) sendToUi({ type: "audit_err", id: m.id });
    } else if (m.type === "chats_get") { // SK-23: the sidebar's chat-history pull (list
      // without chat_id, that chat's messages with it) — chats_data answers on the WS.
      const delivered = send({ type: "chats_get", id: String(m.id || "").slice(0, 40),
                               ...(m.chat_id ? { chat_id: String(m.chat_id).slice(0, 80) } : {}) });
      if (!delivered) sendToUi({ type: "chats_err", id: m.id });
    } else if (m.type === "bell_read") { // SK-35: журнал открыли — всё прочитано, звонки задач сняты
      bellLogReadAll();
    } else if (m.type === "bell_read_id") { // SK-35: клик по событию журнала — точечное прочтение
      bellLogReadId(String(m.id || "").slice(0, 80));
    } else if (m.type === "replay") { // SK-23: after a chat switch the feed was cleared —
      // the pending global blocks (perm asks / hand-offs / live timeline) re-render here,
      // same replay as a fresh port connect.
      for (const ask of PENDING_PERM.values()) port.postMessage(ask);
      for (const ho of PENDING_HO.values()) port.postMessage(ho);
      if (TASKS.size) tlPush(); // SK-25: store был TL до параллельных задач — guard отстал
    }
  });
  port.postMessage({ type: "bridge", state: BRIDGE_ST, url: wsUrl }); // SK-28: честный текущий state (retry/offline/no_token…), а не угадка по readyState
  port.postMessage({ type: "agent_state", state: AGST }); // SK-09: late sidebar sees the current chip
  for (const ask of PENDING_PERM.values()) port.postMessage(ask); // sidebar opened after the ask
  for (const ho of PENDING_HO.values()) port.postMessage(ho); // SK-08: late sidebar sees the open hand-off too
  if (TASKS.size) tlPush(); // SK-06: late sidebar sees the live timeline too (SK-25: TL→TASKS)
  port.postMessage({ type: "bell_log", events: BELL_LOG }); // SK-35: поздний сайдбар видит журнал
});

browser.runtime.onMessage.addListener((m, _s, sendResponse) => {
  if (m.type === "sidekick-ready") toActiveTab({ type: "hello" });
  else if (m.type === "token-updated" || m.type === "server-updated") { // SK-28: смена сервера — тот же немедленный путь
    backoff = BACKOFF_MIN; optionsShown = false; failStreak = 0;
    if (ws) { try { ws.close(); } catch {} ws = null; }
    connect();
  } else if (m.type === "ws-check") { // SK-15: options «проверить подключение»
    wsCheck(sendResponse);
    return true; // async sendResponse
  } else if (m.type === "bell-test") { // SK-50: options «проверить звонок»
    bellTest(sendResponse);
    return true; // async sendResponse
  }
});

// SK-50 п.3: «Проверить звонок» — живой notifications.create (тот же путь, что
// bellRefresh: базовый тип, реальный iconUrl — см. комментарий в bell.js).
// Звук даёт ОС (у WebExtensions API звука нет), поэтому в ответ кладём и
// getPermissionLevel: «denied/prompt» честнее, чем «ок, но тихо».
async function bellTest(sendResponse) {
  const reply = (obj) => { try { sendResponse(obj); } catch (e) {} };
  if (!browser.notifications) { reply({ ok: false, error: "browser.notifications недоступен в этой сборке" }); return; }
  let level = "";
  try { level = String(await browser.notifications.getPermissionLevel()) || ""; }
  catch (e) { level = "unknown"; }
  const nid = "sk50test" + Date.now();
  try {
    await browser.notifications.create(nid, {
      type: "basic", title: "Hermes: проверка звонка",
      message: "Увидел и услышал — звонок работает. Клик открывает сайдбар.",
      iconUrl: browser.runtime.getURL("icons/icon-96.png"),
    });
    send({ type: "bell_event", ev: "notify_test", id: nid, kind: "test" });
    setTimeout(() => { browser.notifications.clear(nid).catch(() => {}); }, 8000);
    reply({ ok: true, level });
  } catch (err) {
    const why = String((err && err.message) || err);
    send({ type: "bell_event", ev: "notify_test_fail", id: nid, kind: "test", reason: why.slice(0, 200) });
    reply({ ok: false, level, error: why.slice(0, 200) });
  }
}

// SK-50 п.2: смена фокуса окна меняет правило показа всплывашек — пересобираем
// звонок на каждом onFocusChanged (вернулись в Firefox → тосты задач сняты;
// увели окно / ушли в другое приложение → тосты задач выходят; пермисы живут).
if (browser.windows && browser.windows.onFocusChanged) {
  browser.windows.onFocusChanged.addListener(() => { bellRefresh().catch(() => {}); });
}

// SK-46-BEGIN ws-alarm (node-юнит test_channel_sk46: heartbeat только по
// handshaked-сокету; open-but-not-acked сокет — ни hb, ни второго сокета)
browser.alarms.create("keepalive", { periodInMinutes: 0.4 });
browser.alarms.onAlarm.addListener(() => {
  // SK-46: hb уходит ТОЛЬКО по сокету с подтверждённым handshake (_handshaked
  // ставит promote). Сокт «открыт, но не ack'нут» — не повод слать hb: ровно так
  // старые сборки ловили «first frame type='hb' != hello» (шторм 22.10).
  if (!ws || ws.readyState > 1 || !ws._handshaked) connect();
  else send({ type: "hb" });
});
// SK-46-END ws-alarm

// SK-19: повторное открытие сайдбара. Крестик закрывает панель, а action в манифесте
// был только ради бейджа — клик по иконке не делал НИЧЕГО. onClicked приходит и от
// реального клика по иконке, и от команды _execute_action (Alt+Shift+S): у action нет
// popup, и Firefox ведёт команду в тот же onClicked. open() идемпотентен — повторный
// клик по открытому сайдбару его НЕ закрывает (toggle запрещён: «клик закрывает» —
// ровно тот баг, из-за которого карточка). catch глушит отказ в сборке без
// user-gesture контекста (замер на firefox-158: open() из onClicked работает).
browser.action.onClicked.addListener(() => {
  browser.sidebarAction.open().catch(() => {});
});

// v0.2: inject into tabs that existed before install
async function injectAll() {
  const tabs = await browser.tabs.query({});
  for (const t of tabs) {
    if (!/^https?:/.test(t.url || "")) continue;
    try {
      await browser.scripting.executeScript({ target: { tabId: t.id }, files: ["stealth.js"], world: "MAIN", injectImmediately: true });
      await browser.scripting.executeScript({ target: { tabId: t.id }, files: ["content.js"] });
    } catch (e) { /* protected pages */ }
  }
}
browser.runtime.onInstalled.addListener(injectAll);
browser.tabs.onUpdated.addListener((id, info) => {
  // SK-25/SK-44: URL-свидетель. Решение живёт в чистой witnessNav, лог — в witnessTabUrl
  // (общий с webNavigation: pushState-навигации, которых onUpdated не видит вовсе).
  // Смена URL внутри окна агентской навигации — навигировал агент (исполненный
  // click/submit/key), пишем URL молча. Смена вне окна — вкладка задачи ушла от задачи:
  // moved липкий до перепривязки (tabs.use_tab {tabId, task}), следующий rpc ответит
  // tab_moved и агент переспросит человека, а не продолжит бить по чужой странице.
  if (info.url) { witnessTabUrl(id, info.url); refreshTabTitle(id); }
  // SK-47 п.3: навигация/релоад снимает кэш кадра (строка свидетеля выше — пин SK-44, не трогать)
  if (info.url || info.status === "loading") sk47Inval(id);
  if (info.title) refreshTabTitle(id);
  if (info.status === "complete") setTimeout(() => browser.tabs.sendMessage(id, { type: "are-you-there" }).catch(() => {}), 1500);
});
// SK-44: pushState-свидетель (аудит №2, инцидент B). SPA-роутер меняет URL через
// history.pushState — tabs.onUpdated этого не видит, и собственный клик агента по
// Shorts-вкладке через несколько секунд превращался в ложный tab_moved (fixed 4 с grace
// истекал, фолбэк-глаз молчал). webNavigation ловит и onHistoryStateUpdated (pushState/
// replaceState), и onLocationChange (полные навигации, back/forward) — раньше onUpdated,
// без задержки на status. Фильтр жёсткий: только главный кадр (iframe-роутер не имеет
// права двигать эталон вкладки) и только http(s) (about:/data: — не навигация задачи).
if (browser.webNavigation) {
  const navMainFrame = (d) => d && d.frameId === 0 && d.tabId >= 0 && /^https?:/.test(d.url || "");
  browser.webNavigation.onHistoryStateUpdated.addListener((d) => { if (navMainFrame(d)) { witnessTabUrl(d.tabId, d.url); sk47Inval(d.tabId); } }); // SK-47: SPA-роутер тоже снимает кэш
  // SK-47-фикс: onLocationChange нет в сборке firefox-158 (20261007) — обращение к
  // undefined-событию роняло весь startup background (после SK-44 это молча ело witness
  // и убило бы коннект). Полные навигации и так видны tabs.onUpdated (info.url).
  if (browser.webNavigation.onLocationChange)
    browser.webNavigation.onLocationChange.addListener((d) => { if (navMainFrame(d)) { witnessTabUrl(d.tabId, d.url); sk47Inval(d.tabId); } });
}
browser.tabs.onRemoved.addListener((id) => { // SK-25: свидетель закрытую вкладку забывает; карточка — «закрыта»
  TAB_TRACK.delete(id);
  let hit = false;
  for (const e of TASKS.values()) if (e.tab === id) { e.tabGone = true; hit = true; }
  if (hit) tlPush();
});

connect();
injectAll();
bellLogLoad().catch(() => {}); // SK-35: журнал переживает моргание event-page
