// SK-11: audit feed — pure logic, no DOM, no transport.
// The bridge owns audit.jsonl (and masks on write AND on read); the sidebar owns
// fetching (WS audit_get via background) and rendering. This module is the contract
// between the two: line -> readable row model, domain filtering, and a defensive
// mask mirror so the feed never shows a secret even if some line escaped the
// bridge's masking (an old bridge, a hand-appended line).
// Loaded as a plain script in the sidebar (window.Audit) and require()'d by node tests.
// SK-71: row labels live in the dictionary (aud.* keys). The page injects the
// active-language translator via Audit.setT(I18N.t); the node contracts render the
// built-in EN pack, whose values mirror extension/locales/en.json aud.* byte-for-byte
// (parity pinned by tests/test_i18n_sk71.py).
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

  const EN_PACK = {
"aud.method.page.text": "read the page text", "aud.method.page.dom": "DOM snapshot",
  "aud.method.page.screenshot": "screenshot", "aud.method.action.preview": "action preview",
  "aud.method.tabs.open_grouped": "open a tab", "aud.method.tabs.use_tab": "take a tab",
  "aud.method.tabs.highlight_group": "highlight tabs",
  "aud.method.tabs.park_group": "hide the tab group",
  "aud.method.perm.check": "permission check", "aud.method.perm.list": "permissions list",
  "aud.method.grants.set": "site permission change",
  "aud.method.handoff.request": "ask the human", "aud.method.audit.tail": "journal read",
  "aud.type.sensitive_input": "password/card field", "aud.type.payment": "payment",
  "aud.type.oauth": "OAuth sign-in", "aud.type.2fa": "2FA code", "aud.type.click": "click",
  "aud.type.input": "typing text", "aud.type.submit": "form submit",
  "aud.type.navigate": "navigation", "aud.type.action": "action",
  "aud.type.send_message": "message out", "aud.type.send_email": "email out",
  "aud.via.grant_allow": "site in Allow (grant)", "aud.via.block": "site in Block",
  "aud.via.site_ask": "site Ask level", "aud.via.no_perm": "site without permission",
  "aud.via.once": "human: once", "aud.via.always": "human: always for the site",
  "aud.via.deny": "human: denied", "aud.via.timeout": "nobody answered — block",
  "aud.via.no_ext": "no extension — block",
  "aud.via.silence": "silence twice — block without a new ask",
  "aud.via.bank": "banking/payment domain", "aud.via.login": "login/payment page",
  "aud.via.sensitive": "sensitive field", "aud.via.payment": "payment",
  "aud.via.text": "outgoing text", "aud.via.mail": "outgoing email",
  "aud.via.wrong_target": "wrong target", "aud.via.wrong_field": "wrong field",
  "aud.via.stop": "stop task", "aud.title.blocked": "Blocked: ",
  "aud.title.allowed": "Allowed: ", "aud.v.blocked": "block", "aud.v.allowed": "allowed",
  "aud.title.cancelled": "Action cancelled by the human (Stop)",
  "aud.v.cancelled": "cancelled", "aud.title.preview_nofind": "Preview: target not found",
  "aud.v.not_shown": "not shown",
  "aud.title.preview_ok": "Preview shown — the human did not object", "aud.v.shown": "shown",
  "aud.title.stop": "The human pressed Stop", "aud.title.stop_task": " (task {{task}})",
  "aud.v.pause": "pause", "aud.title.ho_wait": "The agent is waiting for the human (hand-off)",
  "aud.title.ho_timeout": "Hand-off: timed out",
  "aud.title.ho_replaced": "Hand-off replaced by a new one",
  "aud.title.ho_rejected": "Hand-off rejected", "aud.v.waiting": "waiting",
  "aud.title.ho_resume": "The human continued — the agent is back", "aud.v.resumed": "resumed",
  "aud.title.task": "task {{task}}", "aud.title.denied_reason": "Denial reason: ",
  "aud.v.denied": "denied", "aud.title.grant_changed": "Site permission changed: ",
  "aud.title.agent_state": "Agent state: ",
  "aud.title.ho_paused": "Rejected during hand-off pause: ",
  "aud.title.ext_on": "Extension connected", "aud.v.connected": "connected",
  "aud.title.ext_replaced": "The previous extension connection was evicted by a new one",
  "aud.v.replaced": "replaced", "aud.title.ext_off": "Extension disconnected",
  "aud.v.offline": "offline", "aud.title.err": " — error", "aud.v.error": "error",
  };
  let T = null;
  function setT(tfn) { T = typeof tfn === "function" ? tfn : null; }
  function L(key, args) {
    let s = T ? T(key, args) : (EN_PACK[key] !== undefined ? EN_PACK[key] : key);
    if (args) s = s.replace(/\{\{(\w+)\}\}/g, (_, k) => (Object.prototype.hasOwnProperty.call(args, k) ? String(args[k]) : ""));
    return s;
  }

  // method / perm-type / via-reason / deny-reason: structural key -> dictionary key.
  const METHOD_RU = {
    "page.text": "aud.method.page.text", "page.dom": "aud.method.page.dom",
    "page.screenshot": "aud.method.page.screenshot", "action.preview": "aud.method.action.preview",
    "tabs.open_grouped": "aud.method.tabs.open_grouped", "tabs.use_tab": "aud.method.tabs.use_tab",
    "tabs.highlight_group": "aud.method.tabs.highlight_group", "tabs.park_group": "aud.method.tabs.park_group",
    "perm.check": "aud.method.perm.check", "grants.get": "aud.method.perm.list",
    "grants.set": "aud.method.grants.set", "handoff.request": "aud.method.handoff.request",
    "audit.tail": "aud.method.audit.tail",
  };
  const ACTION_RU = {
    sensitive_input: "aud.type.sensitive_input", payment: "aud.type.payment", oauth: "aud.type.oauth",
    "2fa": "aud.type.2fa", click: "aud.type.click", input: "aud.type.input", submit: "aud.type.submit",
    navigate: "aud.type.navigate", action: "aud.type.action",
    send_message: "aud.type.send_message", send_email: "aud.type.send_email", // SK-34
  };
  const VIA_RU = {
    grant_hit: "aud.via.grant_allow", grant: "aud.via.grant_allow", grant_block: "aud.via.block",
    level_ask: "aud.via.site_ask",
    no_grant: "aud.via.no_perm", human_once: "aud.via.once",
    human_always: "aud.via.always", human_deny: "aud.via.deny",
    ask_timeout: "aud.via.timeout", no_extension: "aud.via.no_ext",
    ask_timeout_streak: "aud.via.silence", // SK-34 p.3
    bank_domain: "aud.via.bank", auth_or_payment_path: "aud.via.login",
    "action:sensitive_input": "aud.via.sensitive", "action:payment": "aud.via.payment",
    "action:oauth": "OAuth", "action:2fa": "2FA",
    "action:send_message": "aud.via.text", "action:send_email": "aud.via.mail", // SK-34
  };
  const DENY_RU = { wrong_target: "aud.via.wrong_target", wrong_field: "aud.via.wrong_field",
    stop_task: "aud.via.stop" }; // SK-34 p.4 — sidebar chips, the keys are stable in the audit
  const viaRu = (v) => String(v || "").split("+").map((x) => L(VIA_RU[x] || x)).filter(Boolean).join(" · ");
  const actRu = (a) => L(ACTION_RU[String(a || "").toLowerCase()] || String(a || ""));

  // Row model: {kind, ts, time, icon, title, domain, verdict, tone, sub}
  // SK-30: icon — a stable key (lock/eye/stop/hand/play/key/state/ban/gear/
  // dotsoft): the sidebar paints from the inline-SVG sprite; emoji are gone from the model.
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
          L(block ? "aud.title.blocked" : "aud.title.allowed") + actRu(o.action),
          L(block ? "aud.v.blocked" : "aud.v.allowed"), block ? "bad" : "ok",
          viaRu(o.via || o.reason));
      }
      case "preview": {
        if (o.cancelled) return row("preview", o, "eye", L("aud.title.cancelled"), L("aud.v.cancelled"), "bad", o.label || "");
        if (!o.shown) return row("preview", o, "eye", L("aud.title.preview_nofind") + (o.reason ? " (" + o.reason + ")" : ""), L("aud.v.not_shown"), "warn", o.label || "");
        return row("preview", o, "eye", L("aud.title.preview_ok"), L("aud.v.shown"), "dim", o.label || "");
      }
      case "stop":
        return row("stop", o, "stop", L("aud.title.stop") + (o.task ? L("aud.title.stop_task", { task: o.task }) : ""), L("aud.v.pause"), "bad", "");
      case "handoff": {
        const d = String(o.decision || "");
        const HO = { pause: "aud.title.ho_wait", timeout: "aud.title.ho_timeout",
                     superseded: "aud.title.ho_replaced", err: "aud.title.ho_rejected" };
        const t = HO[d] ? L(HO[d]) : ("Hand-off: " + d);
        return row("handoff", o, "hand", t, d === "pause" ? L("aud.v.waiting") : d,
                   d === "pause" ? "warn" : d === "timeout" || d === "err" ? "bad" : "dim",
                   String(o.what || o.via || ""));
      }
      case "resume":
        return row("resume", o, "play", L("aud.title.ho_resume"), L("aud.v.resumed"), "ok", o.task ? L("aud.title.task", { task: o.task }) : "");
      case "deny_reason": // SK-34 p.4: the deny reason from a chip (one tap, audit.jsonl)
        return row("perm", o, "lock", L("aud.title.denied_reason") + L(DENY_RU[o.reason] || o.reason),
                   L("aud.v.denied"), "warn", actRu(o.action));
      case "grants.set":
        return row("grants", o, "key", L("aud.title.grant_changed") + (o.level || "?"), o.level || "",
                   o.level === "block" ? "bad" : o.level === "allow" ? "warn" : "dim", "");
      case "state":
        return row("state", o, "state", L("aud.title.agent_state") + (o.state || "?"), o.state || "", "dim", o.reason ? viaRu(o.reason) : "");
      case "handoff_block":
        return row("blocked", o, "ban", L("aud.title.ho_paused") + (o.method || "?"), L("aud.v.blocked"), "warn", "");
      case "ext": { // SK-49: the extension's life — you can see WHOSE socket is active (peer),
        // "the agent strikes the wrong spot": connect/replaced/disconnect from the bridge.
        const d = String(o.decision || "");
        if (d === "connect")
          return row("ext", o, "dotsoft", L("aud.title.ext_on"), L("aud.v.connected"), "ok", String(o.peer || ""));
        if (d === "replaced")
          return row("ext", o, "dotsoft", L("aud.title.ext_replaced"), L("aud.v.replaced"), "warn",
                     [o.peer, o.by].filter(Boolean).join(" → "));
        return row("ext", o, "dotsoft", L("aud.title.ext_off"), L("aud.v.offline"), "dim", String(o.peer || ""));
      }
      default:
        if (typeof o.method === "string" && o.method) {
          const okv = o.ok === true;
          return row("rpc", o, "gear", L(METHOD_RU[o.method] || o.method) + (okv ? "" : L("aud.title.err")),
                     okv ? "ok" : L("aud.v.error"), okv ? "ok" : "bad",
                     okv ? "" : String(o.err || ""));
        }
        return row("raw", o, "dotsoft", s.slice(0, 400), "", "dim", "");
    }
  }

  // Domain filter for the rendered feed: substring, case-insensitive, against the
  // row's domain. Rows without a domain (state, Stop, rpc without a resolved URL)
  // disappear while a filter is active — the filter asks "what happened ON that site".
  function filterEntries(entries, query) {
    const q = String(query == null ? "" : query).trim().toLowerCase();
    const list = Array.isArray(entries) ? entries : [];
    if (!q) return list.slice();
    return list.filter((e) => e && e.domain && e.domain.toLowerCase().includes(q));
  }

  const api = { SECRET_RE, mask, timeOf, parseLine, filterEntries,
    METHOD_RU, ACTION_RU, VIA_RU, setT, L };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Audit = api;
})(typeof window !== "undefined" ? window : globalThis);