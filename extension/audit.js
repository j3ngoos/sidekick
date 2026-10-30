// SK-11: audit feed — pure logic, no DOM, no transport.
// The bridge owns audit.jsonl (and masks on write AND on read); the sidebar owns
// fetching (WS audit_get via background) and rendering. This module is the contract
// between the two: line -> readable row model, domain filtering, and a defensive
// mask mirror so the feed never shows a secret even if some line escaped the
// bridge's masking (an old bridge, a hand-appended line).
// Loaded as a plain script in the sidebar (window.Audit) and require()'d by node tests.
(function (root) {
  // Mirror of the bridge's SECRET_RE (token/password/secret/cvv/cvc/value keys).
  // A full JSON string value (escapes included) is replaced by "***" WITH its
  // closing quote — masked lines stay parseable, the feed renders rows not raw.
  // The literal session-token string is the bridge's job — the sidebar cannot know
  // every token that ever passed through the file; key-shaped secrets are this regex's.
  const SECRET_RE = /(token|password|secret|cvv|cvc|value)("\s*:\s*)"(?:[^"\\]|\\.)*"/gi;
  function mask(line) {
    return String(line == null ? "" : line).replace(SECRET_RE, '$1$2"***"');
  }

  // "HH:MM:SS" local — the feed is the human's own machine, local time is the truth.
  // n > 1e11 (~year 5138) rejects ms-since-epoch and garbage without an Invalid Date.
  function timeOf(ts) {
    const n = Number(ts);
    if (!Number.isFinite(n) || n <= 0 || n > 1e11) return "";
    const d = new Date(n * 1000), p = (x) => String(x).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  const METHOD_RU = {
    "page.text": "прочитать текст страницы", "page.dom": "снимок DOM",
    "page.screenshot": "скриншот", "action.preview": "предпросмотр действия",
    "tabs.open_grouped": "открыть вкладку", "tabs.use_tab": "взять вкладку",
    "tabs.highlight_group": "подсветить вкладки", "tabs.park_group": "спрятать группу вкладок",
    "perm.check": "проверка разрешения", "grants.get": "список разрешений",
    "grants.set": "смена разрешения сайта", "handoff.request": "просьба к человеку",
    "audit.tail": "чтение журнала",
  };
  const ACTION_RU = {
    sensitive_input: "поле пароля/карты", payment: "платёж", oauth: "OAuth-вход",
    "2fa": "код 2FA", click: "клик", input: "ввод текста", submit: "отправка формы",
    navigate: "переход", action: "действие",
    send_message: "сообщение наружу", send_email: "письмо наружу", // SK-34
  };
  const VIA_RU = {
    grant_hit: "сайт в Allow (грант)", grant: "сайт в Allow (грант)", grant_block: "сайт в Block",
    level_ask: "уровень сайта Ask",
    no_grant: "сайт без разрешения", human_once: "человек: разово",
    human_always: "человек: всегда для сайта", human_deny: "человек: отклонено",
    ask_timeout: "никто не ответил — блок", no_extension: "нет расширения — блок",
    ask_timeout_streak: "молчание дважды — блок без нового вопроса", // SK-34 п.3
    bank_domain: "банковский/платёжный домен", auth_or_payment_path: "страница входа/оплаты",
    "action:sensitive_input": "чувствительное поле", "action:payment": "платёж",
    "action:oauth": "OAuth", "action:2fa": "2FA",
    "action:send_message": "текст наружу", "action:send_email": "письмо наружу", // SK-34
  };
  const DENY_RU = { wrong_target: "не туда жмёшь", wrong_field: "не то поле",
    stop_task: "стоп задача" }; // SK-34 п.4 — чипы сайдбара, ключи стабильны в аудите
  const viaRu = (v) => String(v || "").split("+").map((x) => VIA_RU[x] || x).filter(Boolean).join(" · ");
  const actRu = (a) => ACTION_RU[String(a || "").toLowerCase()] || String(a || "");

  // Row model: {kind, ts, time, icon, title, domain, verdict, tone, sub}
// SK-30: icon — стабильный ключ (lock/eye/stop/hand/play/key/state/ban/gear/
// dotsoft): сайдбар красит из inline-SVG спрайта; эмодзи в модели больше нет.
  // tone: ok | bad | warn | dim — the render color family. domain "" = not site-bound.
  function row(kind, o, icon, title, verdict, tone, sub) {
    const ts = Number(o.ts) || 0;
    return { kind, ts, time: timeOf(ts), icon, title: title, domain: String(o.domain || ""),
             verdict: verdict || "", tone: tone || "dim", sub: sub || "" };
  }

  function parseLine(raw) {
    const s = String(raw == null ? "" : raw);
    let o = null;
    try { o = JSON.parse(s); } catch (e) { /* fall through to raw */ }
    if (!o || typeof o !== "object" || Array.isArray(o))
      return row("raw", {}, "dotsoft", s.slice(0, 400), "", "dim", "");
    switch (o.ev) {
      case "perm": {
        const block = o.decision === "block";
        return row("perm", o, "lock",
          (block ? "Заблокировано: " : "Разрешено: ") + actRu(o.action),
          block ? "блок" : "разрешено", block ? "bad" : "ok",
          viaRu(o.via || o.reason));
      }
      case "preview": {
        if (o.cancelled) return row("preview", o, "eye", "Действие отменено человеком (Stop)", "отменено", "bad", o.label || "");
        if (!o.shown) return row("preview", o, "eye", "Предпросмотр: цель не найдена" + (o.reason ? " (" + o.reason + ")" : ""), "не показан", "warn", o.label || "");
        return row("preview", o, "eye", "Предпросмотр показан — человек не возражал", "показан", "dim", o.label || "");
      }
      case "stop":
        return row("stop", o, "stop", "Человек нажал Stop" + (o.task ? " (задача " + o.task + ")" : ""), "пауза", "bad", "");
      case "handoff": {
        const d = String(o.decision || "");
        const t = { pause: "Агент ждёт человека (hand-off)", timeout: "Hand-off: время вышло",
                    superseded: "Hand-off заменён новым", err: "Hand-off отклонён" }[d] || ("Hand-off: " + d);
        return row("handoff", o, "hand", t, d === "pause" ? "ждёт" : d,
                   d === "pause" ? "warn" : d === "timeout" || d === "err" ? "bad" : "dim",
                   String(o.what || o.via || ""));
      }
      case "resume":
        return row("resume", o, "play", "Человек продолжил — агент вернулся", "продолжено", "ok", o.task ? "задача " + o.task : "");
      case "deny_reason": // SK-34 п.4: причина deny из чипа (один тап, audit.jsonl)
        return row("perm", o, "lock", "Причина отказа: " + (DENY_RU[o.reason] || o.reason),
                   "отказ", "warn", actRu(o.action));
      case "grants.set":
        return row("grants", o, "key", "Разрешение сайта изменено: " + (o.level || "?"), o.level || "",
                   o.level === "block" ? "bad" : o.level === "allow" ? "warn" : "dim", "");
      case "state":
        return row("state", o, "state", "Состояние агента: " + (o.state || "?"), o.state || "", "dim", o.reason ? viaRu(o.reason) : "");
      case "handoff_block":
        return row("blocked", o, "ban", "Отклонено на паузе hand-off: " + (o.method || "?"), "блок", "warn", "");
      case "ext": { // SK-49: жизнь расширения — видно, ЧЕЙ сокет активен (peer),
        // когда «агент бьёт не туда»: connect/replaced/disconnect из моста.
        const d = String(o.decision || "");
        if (d === "connect")
          return row("ext", o, "dotsoft", "Расширение подключилось", "подключено", "ok", String(o.peer || ""));
        if (d === "replaced")
          return row("ext", o, "dotsoft", "Прежнее подключение расширения вытеснено новым", "заменено", "warn",
                     [o.peer, o.by].filter(Boolean).join(" → "));
        return row("ext", o, "dotsoft", "Расширение отключилось", "офлайн", "dim", String(o.peer || ""));
      }
      default:
        if (typeof o.method === "string" && o.method) {
          const okv = o.ok === true;
          return row("rpc", o, "gear", (METHOD_RU[o.method] || o.method) + (okv ? "" : " — ошибка"),
                     okv ? "ok" : "ошибка", okv ? "ok" : "bad",
                     okv ? "" : String(o.err || ""));
        }
        return row("raw", o, "dotsoft", s.slice(0, 400), "", "dim", "");
    }
  }

  // Domain filter for the rendered feed: substring, case-insensitive, against the
  // row's domain. Rows without a domain (state, Stop, rpc without a resolved URL)
  // disappear while a filter is active — the filter asks «what happened ON that site».
  function filterEntries(entries, query) {
    const q = String(query == null ? "" : query).trim().toLowerCase();
    const list = Array.isArray(entries) ? entries : [];
    if (!q) return list.slice();
    return list.filter((e) => e && e.domain && e.domain.toLowerCase().includes(q));
  }

  const api = { SECRET_RE, mask, timeOf, parseLine, filterEntries, METHOD_RU, ACTION_RU, VIA_RU };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Audit = api;
})(typeof window !== "undefined" ? window : globalThis);
