// SK-37: bell/badge — общий модуль «позови человека» (SK-50 уточнил правило
// показа: всплывашка задачи — только при НЕ-фокусе окна Firefox; пермис зовёт
// всегда). Пока карточка на глазах — бейдж+журнал; ушёл из окна — нативное
// уведомление со звуком ОС (browser.notifications) + колокольчик-бейдж на
// иконке. Клик по уведомлению открывает сайдбар и подсвечивает карточку
// (bell_focus в UI).
// Скрипт грузится ПЕРЕД background.js (manifest.background.scripts): чистое ядро
// ниже не трогает browser.* вообще — оно вырезается в node-юнит (SK-37-BEGIN/END);
// нечистые обёртки обращаются к background-глобалам (clients/badge/sendToUi)
// только в момент вызова, а не при загрузке.

// SK-37-BEGIN bell-core (tests/test_perm_bell_sk37.py вырезает блок в node-юнит)
const BELL = { pending: new Map() }; // id -> {kind, title, message, notified, ts}
const BELL_BADGE_COLOR = "#dc2626";

// Текст бейджа: один звонок — колокольчик, пачка — «🔔N» (Firefox режет бейдж
// после ~4 символов, поэтому потолок двузначный с клиппингом).
function bellBadgeText(n) {
  return n <= 0 ? "" : n === 1 ? "🔔" : ("🔔" + n).slice(0, 4);
}

// SK-50 (v0.7, пакет пользователя 25.10): правило «островка» — всплывающее
// системное уведомление task_* выходит ТОЛЬКО когда окно Firefox НЕ в фокусе
// (свёрнуто / другое приложение); при фокусе — бейдж-колокольчик + журнал.
// perm-звонок зовёт ВСЕГДА (SK-37 семантика: вердикт человека дороже тишины).
// Чистое: kind + флаг фокуса + число pending.
function bellShouldNotify(kind, windowFocused, pendingCount) {
  if (pendingCount <= 0) return false;
  return kind === "perm" ? true : !windowFocused;
}
// SK-37-END bell-core

// ---- SK-35: журнал «позови человека» + префиксы группы ---------------------------
// Задача со явным сигналом агента long:true (SK-51) сворачивает UI, по завершении
// человек получает колокольчик И запись в выпадающем журнале шапки. Журнал живёт
// в background,
// хранится в storage.local (переживает моргание event-page), ретранслируется
// каждому сайдбару кадром {type:"bell_log", events}. ЧИСТОЕ ЯДРО ниже не трогает
// browser.* — вырезается в node-юнит (SK-35-BEGIN/END), как bell-core.
// SK-35-BEGIN bell-log (tests/test_long_task_sk35.py вырезает блок в node-юнит)
const BELL_LOG_CAP = 50;
const BELL_LOG_KINDS = ["task_done", "task_failed", "perm"];

// Снять служебный префикс с заголовка группы: ⏳ «работает долго» / ✓ «сделано».
// Повторный префикс исключён по построению: базовый заголовок всегда чистый.
function bellStripPrefix(t) {
  const s = String(t == null ? "" : t);
  return s.startsWith("⏳ ") ? s.slice(2) : s.startsWith("✓ ") ? s.slice(2) : s;
}
// Новый заголовок группы по режиму долгой задачи: "" | "long" | "done".
function bellGroupTitle(title, mode) {
  const base = bellStripPrefix(title);
  return mode === "long" ? "⏳ " + base : mode === "done" ? "✓ " + base : base;
}

// Добавить событие в журнал (чистая: возвращает НОВЫЙ массив, cap с хвоста).
function bellLogAdd(log, ev, cap) {
  const c = Number.isInteger(cap) && cap > 0 ? cap : BELL_LOG_CAP;
  return [ev].concat(Array.isArray(log) ? log : []).slice(0, c);
}
// SK-50: есть ли в журнале запись с таким id — ре-пуш того же события
// (мост шлёт висящий пермис карточкой заново) НЕ новое событие журнала.
function bellLogHas(log, id) {
  return (Array.isArray(log) ? log : []).some((e) => e && String(e.id) === String(id));
}
function bellUnread(log) {
  return (Array.isArray(log) ? log : []).filter((e) => e && !e.read).length;
}
// Пометить всё прочитанным (новый массив) — счётчик колокольчика обнуляют все
// открытые сайдбары синхронно, поэтому read — свойство журнала, а не вкладки.
function bellMarkAllRead(log) {
  return (Array.isArray(log) ? log : []).map((e) => (e && !e.read ? { ...e, read: true } : e));
}
// Пометить прочитанным событие с данным id (вердикт пермиса снимает и звонок, и
// запись журнала — человек их видел).
function bellMarkIdRead(log, id) {
  return (Array.isArray(log) ? log : []).map(
    (e) => (e && String(e.id) === String(id) && !e.read ? { ...e, read: true } : e));
}
// id звонков-задач, которые надо снять при «прочитано всё»: пермисы живут до
// вердикта, звонок «сделана/не сделана» нужен ровно до взгляда в журнал.
function bellAckIds(log) {
  return (Array.isArray(log) ? log : [])
    .filter((e) => e && (e.kind === "task_done" || e.kind === "task_failed"))
    .map((e) => String(e.id));
}
// SK-35-END bell-log

// Нечистая часть журнала: персист + рассылка. Вызывается только из background
// (после загрузки bell.js); storage/отправка — в момент вызова, не при загрузке.
let BELL_LOG = [];
const BELL_LOG_KEY = "sk35BellLog";
async function bellLogLoad() {
  try {
    const got = await browser.storage.local.get(BELL_LOG_KEY);
    const v = got && got[BELL_LOG_KEY];
    if (Array.isArray(v)) BELL_LOG = v.filter((e) => e && BELL_LOG_KINDS.includes(e.kind)).slice(0, BELL_LOG_CAP);
  } catch (e) { /* пустой журнал — честный старт */ }
}
function bellLogPersist() {
  try { browser.storage.local.set({ [BELL_LOG_KEY]: BELL_LOG }); } catch (e) { /* storage — не критично */ }
}
// Позвонить записью: добавить событие, сохранить, разослать всем сайдбарам.
function bellLogPush(ev) {
  if (!ev || !BELL_LOG_KINDS.includes(ev.kind) || !ev.id) return;
  // SK-50: повтор того же события (мост заново шлёт висящий пермис) — НЕ новая
  // запись: иначе один пермис плодит десятки строк журнала.
  if (bellLogHas(BELL_LOG, ev.id)) return;
  BELL_LOG = bellLogAdd(BELL_LOG, { read: false, ...ev }, BELL_LOG_CAP);
  bellLogPersist();
  if (typeof sendToUi === "function") sendToUi({ type: "bell_log", events: BELL_LOG });
}
// Человек открыл журнал: всё прочитано, звонки «сделана/не сделана» сняты.
// Порядок важен: сначала read-all (иначе bellClear→bellLogReadId прочитает
// только задачи, и непрочитанные пермисы переживут «прочитано всё»).
function bellLogReadAll() {
  const ids = bellAckIds(BELL_LOG);
  if (bellUnread(BELL_LOG)) { BELL_LOG = bellMarkAllRead(BELL_LOG); bellLogPersist(); }
  for (const id of ids) bellClear(id);
  if (typeof sendToUi === "function") sendToUi({ type: "bell_log", events: BELL_LOG });
}
// Точечное прочтение (клик по событию / вердикт пермиса): снять и звонок, если
// это он. Рекурсия bellClear↔bellLogReadId безопасна: повторный bellClear по
// уже снятому id — no-op (pending.delete возвращает false).
function bellLogReadId(id) {
  const hit = BELL_LOG.some((e) => e && String(e.id) === String(id) && !e.read);
  if (hit) {
    BELL_LOG = bellMarkIdRead(BELL_LOG, id);
    bellLogPersist();
    if (typeof sendToUi === "function") sendToUi({ type: "bell_log", events: BELL_LOG });
  }
  const e = BELL.pending.get(String(id));
  if (e && (e.kind === "task_done" || e.kind === "task_failed")) bellClear(id);
}

// SK-50: «в фокусе» = ХОТЬ ОДНО обычное окно Firefox несёт фокус — человек
// внутри браузера (даже с закрытым сайдбаром: бейдж и журнал у него на глазах).
// Нет окон/нет API — честим в «в фокусе»: человека в браузере не всполошим,
// ложных всплывашек не плодим.
async function bellWindowFocused() {
  try {
    const wins = await browser.windows.getAll({ windowTypes: ["normal"] });
    return wins.some((w) => w.focused);
  } catch (e) { return true; }
}

// SK-50: диагностика звонка в events.jsonl (нога bell_event через мост, ts
// штампует мост). Причина падения create() НЕ глотается catch-ом — её видно
// на стороне диагностики/приёмки. Мост офлайн — честная тишина ноги: сам
// звонок живёт, следующий bellRefresh повторит попытку.
function bellEventLog(ev, id, e, why) {
  if (typeof send !== "function") return;
  try {
    send({ type: "bell_event", ev, id: String(id || ""), kind: String((e && e.kind) || ""),
           title: String((e && e.title) || "").slice(0, 120),
           ...(why ? { reason: String(why).slice(0, 200) } : {}) });
  } catch (err) { /* диагностическая нога не имеет права ронять звонок */ }
}

// Пересобрать состояние звонка по BELL.pending (SK-50 правило показа):
//  • perm — звоним всегда, уведомление живёт до вердикта (фокус его не снимает);
//  • task_* — всплывашка только при НЕ-фокусе окна; вернулись в Firefox —
//    уведомление снимаем (бейдж + журнал достаточно), запись журнала читается
//    кликом/«прочитать всё».
// Бейдж пересчитывает badge() в background.js (колокольчик приоритетнее состояния
// агента: проигнорированный пермис дороже, чем «агент думает»).
async function bellRefresh() {
  if (typeof badge === "function") badge();
  const focused = await bellWindowFocused();
  if (!browser.notifications) return;
  for (const [id, e] of BELL.pending) {
    if (!bellShouldNotify(e.kind, focused, BELL.pending.size)) {
      if (e.kind !== "perm" && e.notified) {
        e.notified = false;
        browser.notifications.clear(String(id)).catch(() => {});
      }
      continue;
    }
    if (e.notified) continue;
    e.notified = true;
    // create() может бросить синхронно (нет ОС-бэкенда уведомлений) — это не
    // должно ронять весь bellRefresh: бейдж и карточка важнее нативного тоста
    try {
      // Firefox (в т.ч. 158) НЕ принимает requireInteraction в create() —
      // схема отклоняет свойство синхронным TypeError и звонок глохнет
      // совсем (замер приёмкой SK-35: «Unexpected property "requireInteraction"»).
      // XUL-фолбэк и так не закрывается сам, а нативный тост — best effort.
      // iconUrl — РЕАЛЬНЫЙ PNG из icons/ через runtime.getURL (data: схема
      // отклоняется валидатором, а без валидной иконки Firefox молча не
      // показывает тост — SK-50 п.1).
      browser.notifications.create(String(id), {
        type: "basic", title: e.title || "Hermes", message: e.message || "",
        iconUrl: browser.runtime.getURL("icons/icon-96.png"),
      }).then(() => bellEventLog("notify", id, e))
        .catch((er) => { e.notified = false;
          bellEventLog("notify_fail", id, e, "promise:" + String((er && er.message) || er)); });
    } catch (err) {
      e.notified = false;
      bellEventLog("notify_fail", id, e, "sync:" + String((err && err.message) || err));
    }
  }
}

// Позвонить: зарегистрировать звонок (идемпотентно по id — ре-пуш пермиса не
// плодит второе уведомление) и пересобрать бейдж/уведомления.
function bellRing(id, { kind = "perm", title = "Hermes", message = "", chat = "" } = {}) {
  if (!id) return;
  const prev = BELL.pending.get(String(id));
  BELL.pending.set(String(id), { kind, title, message, chat,
    notified: prev ? prev.notified : false, ts: Date.now() });
  bellRefresh();
}

// Звонок закрыт (вердикт человека, resolve моста, задача сделана) — убрать
// уведомление и пересчитать бейдж.
function bellClear(id) {
  if (!BELL.pending.delete(String(id))) return;
  if (browser.notifications) browser.notifications.clear(String(id)).catch(() => {});
  if (typeof badge === "function") badge();
  // SK-35: вердикт/закрытие = человек это видел — запись журнала того же id
  // прочитана. Рекурсия с bellLogReadId безопасна: повторный bellClear по
  // снятому id — no-op (pending.delete выше возвращает false).
  if (typeof bellLogReadId === "function") bellLogReadId(String(id));
}

// SK-37: человекочитаемый текст уведомления. Источник истины карточки — ACTION_RU
// в sidebar.js; это копия для bell-текста (дрейф ловит pytest-пин: ключи сайдбара
// обязаны быть подмножеством этих).
const BELL_ACTION_RU = { sensitive_input: "действие с полем пароля/карты", payment: "платёж",
  oauth: "OAuth-вход / выдача доступа", "2fa": "код 2FA", click: "клик",
  input: "ввод текста", submit: "отправка формы", navigate: "переход", action: "действие",
  close_tabs: "закрыть вкладки",
  send_message: "отправить сообщение наружу", send_email: "отправить письмо наружу" };
function bellPermMessage(m) {
  const act = BELL_ACTION_RU[m.action] || m.action || "действие";
  return [act, m.domain || "неизвестный сайт"].filter(Boolean).join(" — ");
}

// Клик по уведомлению: открыть сайдбар, показать окно и подсветить карточку.
// Звонок НЕ снимаем — пермис всё ещё висит без ответа; бейдж доживёт до вердикта.
if (browser.notifications && browser.notifications.onClicked) {
  browser.notifications.onClicked.addListener((nid) => {
    const e = BELL.pending.get(nid);
    browser.sidebarAction.open().catch(() => {});
    browser.windows.getAll().then((ws) => {
      for (const w of ws) browser.windows.update(w.id, { focused: true }).catch(() => {});
    }).catch(() => {});
    if (typeof sendToUi === "function") sendToUi({ type: "bell_focus", id: nid, kind: e ? e.kind : "", chat: e && e.chat ? e.chat : "" }); // SK-35: chat — сайдбар переключится на чат задачи
  });
}
