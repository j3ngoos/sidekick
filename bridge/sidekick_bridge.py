#!/usr/bin/env python3
"""Hermes Sidekick bridge: authenticated WS hub between the Firefox extension and Hermes.

Contract v1 (SK-02; runbook «Архитектура: контракт моста v1» is the source of truth):
  * every frame carries "v" (int, 1..PROTOCOL_VERSION). Post-handshake frames
    without v are rejected: {"v":1,"type":"error","err":"no_version"} + close 4002;
  * first frame within HELLO_TIMEOUT_S (5 s):
      {"v":1,"type":"hello","token":"<contents of $SIDEKICK_HOME/token>"}
    ("v" may be omitted = v1 client, root-plan decision №7);
    answer: {"v":1,"type":"hello_ack","ok":true}
        or: {"v":1,"type":"hello_ack","ok":false,"err":"bad_token"|"bad_version"};
  * close codes: 4002 version/frame, 4003 token,
    (SK-41: no/late/malformed hello закрывается МОЛЧА — RST без close-frame,
     клиент видит 1006: страница-щуп не получает опознавательный reason);
    4009 replaced (SK-49, see single-connection policy below);
  * SK-49 single-connection policy: at most ONE extension socket is registered at
    a time. A second hello with a VALID token evicts the previous socket —
    close 4009 "replaced by newer connection" — so RPC routing is never a coin
    flip over set order (incident 23.10: page.dom без task ушёл в браузер
    пользователя, пока агентские вкладки открывались в тест-Firefox). Last valid
    hello wins; a hello with a BAD token never evicts (4003 as before). Every
    connect/disconnect/replace lands in audit.jsonl (ev "ext") and the active
    peer (ip:port) rides in health.json — «агент бьёт не туда» диагностируется
    по факту, а не по догадке;
  * before handshake a client takes no part in outbox broadcast;
  * token file: 0600, hex 32 bytes, stable across restarts — rotate manually:
    rm $SIDEKICK_HOME/token && systemctl --user restart sidekick-bridge;
  * env: SIDEKICK_HOME (default ~/sidekick/bridge/hub), SIDEKICK_PORT (default 8791),
    SIDEKICK_BIND (default 127.0.0.1; SK-28: 0.0.0.0 opens the bridge to the LAN —
    the token then rides over ws in CLEAR TEXT, trusted home network only, wss is
    out of scope);
  * SK-28: token rejections are rate-limited per source IP — more than
    SIDEKICK_TOKEN_FAIL_LIMIT (default 5) bad tokens within SIDEKICK_TOKEN_FAIL_WINDOW
    (default 60 s) and further connections from that IP are closed with 4003
    WITHOUT a handshake ("token rate limit"). A successful handshake clears the
    counter (a proven token is not a brute-forcer). Brute-forcing a 64-hex token is
    unrealistic — the limit exists to cut the log/retry noise (audit 21.10);
  * SK-46 legacy_hello (tolerant acceptance, audit 22.10 storm): an OLD extension
    build may race a heartbeat in front of hello on reconnect. If the FIRST frame
    is {type:"hb"} from a source IP that completed a token handshake within
    SIDEKICK_LEGACY_HELLO_WINDOW_S (default 120 s; pairings persist in
    $SIDEKICK_HOME/.paired_ips across bridge restarts — the storm scenario IS a
    restart), it is accepted as hello v1 and logged as "handshake legacy_hello".
    Any other first frame is refused silently (SK-41: RST, client sees 1006).
    The window is deliberately
    short: it only papers over the reconnect gap of already-paired machines;
    unpaired IPs never get a token-free pass;
  * SK-46 channel stability: health.json carries reconnects_per_hour (successful
    handshakes in the last 3600 s — flapping reads as a number, not by grepping
    bridge.log); a run that ends without the clean-exit mark (SIGKILL/crash/boot;
    SIGTERM/SIGINT stop marks first) makes the NEXT start append
    {"type":"bridge_unclean_exit"} to events.jsonl — agentd turns it into a chat
    alert on the next human message.

Inbox  (extension -> Hermes): $SIDEKICK_HOME/inbox.jsonl  (appended)
Outbox (Hermes -> extension): $SIDEKICK_HOME/outbox.jsonl (drained & truncated)

RPC bus (SK-03), file-side of the contract (runbook «RPC агент↔расширение»):
  * agent appends to $SIDEKICK_HOME/rpc_in.jsonl:
      {"v":1,"id":"<uuid>","method":"page.text","params":{...}}
    (v mandatory here — decision №7 leniency is hello-frames only);
  * bridge (100 ms poller) forwards to a connected extension client:
      {"v":1,"type":"rpc","id":...,"method":...,"params":...}
  * extension answers {"v":1,"type":"rpc_result","id":...,"ok":true,"result":...};
  * bridge appends the verdict to rpc_out.jsonl (agent polls by id):
      {"v":1,"id":...,"ok":true,"result":...} | {"v":1,"id":...,"ok":false,"err":...}
    errors: no_extension (immediate), timeout (RPC_TIMEOUT_S, env
    SIDEKICK_RPC_TIMEOUT, default 10), bad_request, no_version, bad_version;
  * every rpc with a known method is audited to audit.jsonl (ts, method, ok, err).
    Secrets are masked on audit writes only — page.text results stay verbatim.
  * rpc_result frames are consumed by the bridge and never reach inbox.jsonl.

Permission model (SK-04; runbook «Grants — на стороне моста», root decision №2):
  * grants live HERE only — $SIDEKICK_HOME/grants.json (0600, atomic write):
      {"example.com": {"level": "allow|ask|block", "ts": 1792160000}}
    Never in the extension's storage (Origin hole). Lookup: longest matching key,
    host==key or host endswith "."+key; missing entry = level "ask".
  * bridge-local rpc methods (no extension round-trip for allow/block):
      grants.get {} -> {grants:{...}}
      grants.set {domain, level} -> {ok:true}        (audited; level: allow|ask|block)
      perm.check {domain|url, action, url?, hint?, perm?} -> {decision:"allow"|"block", via:str}
  * SK-43 п.1 (аддитивно): с параметром `perm` поле action — ГЛАГОЛ действия
      (click|input|submit|key|scroll|...), а perm — ТИП пермиса. Мост валидирует
      семантику: click/rightclick/dblclick/hover/move/key/scroll с perm
      sensitive_input/send_message/send_email -> err perm_action_mismatch + hint
      (карточка человеку НЕ уходит; ALWAYS_HUMAN-формулировки для не-чувствительных
      действий не показываются НИКОГДА). Без perm — легаси-форма {action:"<тип>"}.
  * capabilities {} -> полный контракт моста (SK-43 п.2): методы, глаголы action.do,
      типы пермисов с границами — агент получает границы от моста, а не из grep
      исходников. Отвечает даже в hand-off паузе (статика, без данных браузера).
  * perm.check decision table:
      level block                          -> block (via grant_block)
      action in ALWAYS_HUMAN_ACTIONS (defaults sensitive_input|payment|oauth|2fa|
        send_message|send_email; env SIDEKICK_ALWAYS_HUMAN=a,b extends the set)
                                           -> ask, even at level allow
      bank domain (BANK_DOMAINS + env SIDEKICK_BANK_DOMAINS) -> ask, even at allow
      url path matches /login|oauth|authorize|signin|2fa|mfa|otp|password|payment|checkout|billing -> ask
      level allow                          -> allow (via grant_hit)   (SK-34: renamed)
      level ask / no entry                 -> ask   (via level_ask / no_grant)
    "ask" is resolved live: push {type:"perm_ask", id, domain, action, reason, url,
    hint, show_always?, name?, tab?, shot?} to the extension, wait for
    {type:"perm_answer", id, choice} —  (SK-37 additive: `name` — имя цели для
    человека; `tab` — вкладка задачи, из которой мост асинхронно снимает миниатюру
    page.screenshot {max_width:320}; `shot` — dataUrl миниатюры, если съёмка уже
    успела к моменту re-push'а. Доноса: {type:"perm_shot", id, shot} — отдельный
    кадр, когда кадр докарточкился; вердикты и аудит от этой ноги не меняются.)
      once   -> allow (no write) + remember (domain, action) in perm_once.json;
                the NEXT ask of the same kind carries show_always:true — the sidebar
                renders «Разрешить для <domain> всегда» (one tap -> grants.set allow).
                Sensitive paths (ALWAYS_HUMAN/bank/auth-path) NEVER get the button:
                an "always" answer there is downgraded to once (bank always re-asked).
      always -> allow + grants.json[level=allow] (only on non-sensitive paths)
      deny   -> block; an optional `reason` (chip key, <=80) rides along into the
                audit; a LATE {type:"perm_reason", id, reason} frame (the human picks
                a chip after the verdict already went out) audits {ev:"deny_reason"}
                against the remembered deny (RECENT_DENIES, TTL 10 min, one reason
                per ask).
      no answer within PERM_TIMEOUT_S -> block (ask_timeout). SK-34 п.3: two
                CONSECUTIVE ask_timeouts for the same (task, domain, action) arm a
                streak — further identical asks answer block via "ask_timeout_streak"
                WITHOUT bothering the human; each timeout appends
                {type:"perm_timeout", id, domain, action, task, streak} to
                events.jsonl, and agentd pauses the task with an explicit question
                in the chat on streak>=2. A human answer of any kind clears the streak.
    env SIDEKICK_PERM_TIMEOUT (default 60 s — was 300; the audit 21.10 п.8 counted
    8 ask_timeouts of dead time). Pending asks are re-pushed to the client after a
    reconnect handshake. No extension connected + ask needed -> err no_extension
    (the human UI is the only way to answer).
  * perm_answer frames are appended to $SIDEKICK_HOME/events.jsonl (agent-readable)
    and consumed — they never reach inbox.jsonl.
  * every perm decision is audited: {ts, ev:"perm", domain, action, decision, via}.

Pre-action preview (SK-05; card: «рамка цели + подпись за 300-500 мс до действия»):
  * rpc method action.preview {selector, action?, name?, label?, ms?} — forwarded to the
    extension like page.*; the content script draws the ring + «кликну <name>» bubble
    (SK-37: NO buttons on the cursor overlay — ring + human label only; verdicts and
    Stop live in the chat window) and holds it ms (default 450, clamped 0..5000)
    before answering:
      {shown:true, cancelled:false, held_ms, label}   — timer expired, agent MAY act;
      {shown:true, cancelled:true, reason:"user_stop"} — legacy vocabulary (SK-37):
        the overlay stopped emitting it (its Stop button is gone); the agent-side
        handling is unchanged — cancelled is a SUCCESSFUL rpc, not an err, skip it;
      {shown:false, cancelled:false, reason:"no_element"|"bad_selector"} — stale/invalid
        selector: the agent must re-snapshot (page.dom) before acting, never blind-click;
      reason:"superseded" — a newer preview replaced this one (skip the old action too).
  * this is the ONLY cancellation channel: MCP clicks bypass the bridge, so enforcement
    is the agent contract (AGENTS.md): preview -> await verdict -> act only if not
    cancelled. Since SK-37 the human's stop-the-task lever is the chat's ■ Stop
    (SK-06/SK-38), not a button on the preview frame; Stop after the verdict cannot
    undo the click — that is SK-06's step Stop.
  * push mode {type:"preview",...} via outbox stays for manual UI checks (runbook) —
    visual only, its verdict is discarded.
  * every verdict is audited: {ts, ev:"preview", shown, cancelled, reason?, label?}.

Step timeline + inline Stop (SK-06; card: «лента шагов, Stop различим с завершением»):
  * agent-authored push frames via outbox (the bridge only broadcasts them as-is):
      {type:"task","id":"<tid>","title"?,"state":"running|paused|done|failed","long"?:true}
      {type:"step","task":"<tid>","n":int,"text"?,"state"?:
         "queued|active|done|failed","shot"?:"data:image/...;base64,..."}
    n is 1-based and task-scoped; shot is an optional page.screenshot dataUrl the
    sidebar shows on click ("что агент видел"). The extension caches the latest
    snapshot and replays it to sidebars that open late.
    SK-51: `long:true` (additive, only on a running frame) is the agent's EXPLICIT
    "I am going into long/autonomous work" signal — the extension collapses the
    sidebar + the agent's tab group (⏳ prefix), the terminal frame brings bell+✓.
    There is NO time heuristic anymore: an interactive dialog never auto-collapses.
    The bridge is a transparent relay and never interprets the field.
  * the sidebar's ■ Stop sends {type:"stop","task"?} up the WS. The bridge consumes it:
    appends the raw frame to events.jsonl (agent-readable), touches $SIDEKICK_HOME/stop
    (the pause flag the agent checks between steps — AGENTS.md), audits {ev:"stop"}.
    It never reaches inbox.jsonl. Touching is idempotent; the AGENT removes hub/stop
    when the human says continue, and resets it at the start of every new task.
  * "paused" (human Stop) and "done" (task finished) are distinct UI states: the Stop
    frame never produces a task frame — only the agent marks a task done/failed.

Agent tab groups (SK-07; AGENTS.md п.6: agent tabs open ONLY through tabs.open_grouped,
so the human sees where the agent works). The extension executes these (the bridge just
forwards + audits them like page.*); group «Sidekick», blue, FF 138/139 API:
  * tabs.open_grouped {url, focus?, windowId?, title?, color?} -> opens the tab
    (background by default — no focus steal), adds it to the agent group (creating it
    with title/color when absent; un-collapses on new activity) and answers
    {tabId, windowId, url, grouped, groupId, groupCreated, title, color}. If grouping
    fails the tab still exists: ok:true with grouped:false + groupErr — the agent needs
    the tabId either way. Errors: url_required, create_failed.
  * tabs.use_tab {tabId?, title?, color?} — an EXISTING tab the agent starts to work in
    (default: the active tab) joins the group; {already:true} when it is inside already;
    fromGroup is reported when the tab leaves a FOREIGN group (research п.9: never
    shuffle human groups silently).
  * tabs.highlight_group {tabId?} — native highlight of every group tab; tabId (the
    source tab) is placed first, and the first highlighted index becomes the active tab
    (MDN tabs.highlight) — that is the Firefox analogue of Claude's orange underline.
    Errors: no_group, tab_not_in_group.
  * tabs.park_group {mode?} — after the task the group must not stand out: collapse
    (default; an active tab may stay inside a collapsed group in Firefox), ungroup
    (tabs stay open) or none. mode omitted -> the extension's sk07AfterTask setting.
    Auto-park with that setting also fires when a SK-06 task frame reaches done/failed.
  * groupId is NOT a durable identity (not preserved across session restore, ids may be
    reused) — the extension re-adopts its group by title every run; the bridge stores
    nothing about groups.
  * while hub/paused (SK-08) all four are refused with paused_for_human like any other
    browser-facing rpc — the human has the browser.

Hand-off to the human (SK-08; card: «пауза на логин/капчу, "Готово, продолжай", без
скриншотов»; AGENTS.md п.4 is the agent side of this contract):
  * the agent calls the bridge-local rpc handoff.request {what, task?, url?, expect?} when only a
    human may act (login/captcha/2FA/OAuth — the SK-04 always-human classes are the
    natural triggers). The bridge touches $SIDEKICK_HOME/paused (the HAND-OFF flag —
    distinct from the SK-06 hub/stop) and pushes {type:"handoff", id, what, task?, url?}
    to the extension; the sidebar renders «🖐 Нужен ты: <what>» with a «Готово, продолжай»
    button. The rpc itself BLOCKS until the human answers (or the timeout below).
  * SK-48 (аудит №2 22.10, проблема 10): «Готово, продолжай» — слово человека, а не
    измеренный факт (handoff «нажми Play» → «Готово» → видео на паузе → задача уехала
    виртуально выполненной). Аддитивно: handoff.request принимает
    expect {kind:"video_playing"} | {kind:"dom", name, role?, checked?, near?} —
    ожидаемое состояние, и после resume мост ПРОВЕРЯЕТ его дешёвым чтением (внутренний
    rpc-лег page.media / page.dom по вкладке задачи; в rpc_out агента этих строк нет).
    Недостиг — баннер возвращается с note (что не так) и кадром (page.screenshot),
    hub/paused держится, rpc агента остаётся заблокированным; повторов
    HANDOFF_VERIFY_MAX_RETRY (2), дальше вердикт наружу. Ответ resume аддитивен:
    {resumed:true, verified:true|false[, verify_err]}; verified:false — задача НЕ
    выполнена, агент обязан решить сам (повторный handoff.request или стоп).
    handoff.status отдаёт expect/note/attempt (shot — dataUrl кадра — в статус НЕ
    доезжает, он нужен только баннеру).
  * while hub/paused exists the bridge refuses every other rpc — page.*, action.preview,
    perm.check, grants.* alike — with err paused_for_human + audit {ev:"handoff_block",
    method}: no screenshot/preview/context can reach the browser while the human types
    secrets (privacy guarantee, ChatGPT-agent pattern). Only handoff.status and
    handoff.request (supersede path) answer while paused.
  * the button sends {type:"resume", id, task?} up the WS: consumed (raw line ->
    events.jsonl, never inbox), removes hub/paused, resolves the waiting handoff.request
    with {resumed:true}, audits {ev:"resume"}. A resume whose handoff is already gone
    (timed out) still clears the flag — idempotent.
  * no answer within HANDOFF_TIMEOUT_S (env SIDEKICK_HANDOFF_TIMEOUT, default 600) ->
    rpc err handoff_timeout, flag removed, banner greyed via handoff_resolved{timeout}.
    The agent must then re-request or stop — never resume page.* while the human may
    still be at the keyboard (AGENTS.md п.4).
  * a second handoff.request supersedes the first (rpc err superseded + banner replaced)
    — same vocabulary as SK-05 previews. No extension connected -> err no_extension and
    the flag is NOT touched (the banner is the only way the human learns of the pause).
  * a stale hub/paused left by a bridge crash is removed at startup: without a live
    waiter nothing can ever resolve it.

Agent state (SK-09; card: «состояния агента в шапке сайдбара, источник истины —
события моста; при обрыве WS — unknown; смена < 500 мс после события»):
  * the bridge DERIVES the agent state from its own event stream and pushes
    {v:1,type:"state",state,ts,reason?,task?} to every client on every CHANGE;
    a freshly handshaked client is replayed the current frame (same pattern as
    perm_ask/handoff parks). The extension only renders — it never computes.
  * wire vocabulary: idle | thinking | acting | needs_you | paused. The extension
    adds the local "unknown" while its WS to the bridge is down (no frame can pass,
    so nobody may claim a state). research pin: sidebarAction.open() needs a user
    gesture — the toolbar badge mirrors the same state for the closed sidebar.
  * derivation priority (first match wins):
      needs_you  a hand-off is open (HANDOFFS) or a perm ask awaits the human
      paused     hub/stop exists (human pressed ■ Stop) or a task frame says "paused"
      acting     a browser-facing rpc (page.*/action.*/tabs.*) is in flight
      thinking   a SK-06 task is running (or another rpc is pending) without browser I/O
      idle       none of the above
  * transitions fire from event hooks (rpc forward/result, perm ask/resolve, handoff
    open/close, stop frame, resume, task/step frames) — sub-100 ms; a 0.25 s tick
    additionally catches the EXTERNAL removal of hub/stop (the agent rm's the flag
    itself when the human says continue). The outbox pump runs at 0.15 s so even
    task-frame-driven changes stay inside the 500 ms chip-change budget.
  * the outbox pump parses task/step frames for the task half of the derivation but
    still broadcasts them verbatim; agent-authored {type:"state"} frames are DROPPED
    — the bridge owns this vocabulary, two authors would fight over the chip.
  * every change is audited {ev:"state", state, reason?, task?}; state frames are
    bridge->extension only and never enter inbox.jsonl.

Audit feed (SK-11; card: «аудит-лог действий в сайдбаре: время, действие, домен,
решение; фильтр по домену; сырой файл; секреты маскировать»):
  * the feed's source of truth is audit.jsonl — every rpc verdict, perm decision,
    preview verdict, Stop, hand-off, grants.set and state change is already there.
    The bridge only READS it for the feed; the writer side is unchanged.
  * rpc audit.tail {limit=50, domain?} — bridge-local (no extension round-trip,
    refused with paused_for_human during a hand-off like every non-handoff method):
    {lines:[...], total, truncated}. limit clamped 1..AUDIT_TAIL_MAX; domain is a
    case-insensitive substring matched against the WHOLE file before tailing, so a
    rare domain in old lines survives the tail window. total = file lines (the
    sidebar's «сверить с jsonl» counter), truncated = more matches than limit.
  * WS frame audit_get {id, limit?, domain?} from the extension (the sidebar's pull
    channel — the rpc bus is agent->extension, the human UI needs its own leg):
    answered {v:1,type:"audit_data",id,lines,total,truncated}. Consumed like stop/
    resume — never inbox. NOT audited: the sidebar polls it every couple of seconds
    and auditing the poll would feed the audit with itself.
  * masking is applied on READ too (mask_secrets), not only on write: lines written
    by an older bridge or appended by hand must still never show a secret in the
    feed. SECRET_RE covers token/password/secret/cvv/cvc/value keys and the literal
    session TOKEN string anywhere in the line (card: «поле value -> ***»).
  * page.*/tabs.* audit lines carry the domain extracted from the extension's
    result.url — the feed's «домен» column and the domain filter need it; the
    bridge never saw the URL before the answer came back.

Action execution (SK-17; card: «чистый режим = полный агент — click/input/submit без
BiDi, preview-гейт на мосту»):
  * rpc action.do {selector, action:"click"|"input"|"submit", value?, name?, ms?} —
    the agent's execution channel in the clean mode. The bridge NEVER forwards the
    method raw: dispatch chains action.preview (the SAME SK-05 conveyor — frame +
    human-readable label; SK-37: verdicts/Stop live only in the chat, the cursor
    overlay is buttonless) and only sends the execution leg to the
    extension after a clean verdict, on the agent's own rpc id. One action.do =
    exactly one preview frame (audit ev:"preview" + via:"action.do").
  * gate refusals — the action is NOT executed, the rpc is a SUCCESSFUL answer
    (SK-05 vocabulary, the agent decides):
      cancelled/superseded   -> {shown:true, cancelled:true, executed:false, reason};
      no_element/bad_selector/cross_origin_frame
                             -> {shown:false, cancelled:false, executed:false, reason}
        — the agent must re-snapshot (page.dom), never blind-retry.
    A failing gate leg (extension err / timeout) -> rpc err, nothing executed.
    Execution result passes verbatim: {done:true, executed:true, action,
    synthetic:true, url} (extension errors: no_element, bad_selector, not_editable,
    no_form, no_content_script...). SK-48: клик-цель, читаемая как тумблер, дополнительно
    несёт post_state {before, after, changed} — cheap before/after без vision;
    executed:true + changed:false = silent-noop (Ozon «Выбрать все»), агент обязан
    переснять/сменить цель, а не рапортовать; в аудит ложится отдельной записью.
  * synthetic:true is the documented boundary: el.click()/dispatchEvent produce
    isTrusted=false events — anti-bot sites may refuse them. When isTrusted=true is
    needed the agent escalates to BiDi agent mode or a hand-off, not around action.do.
  * input uses the PROTOTYPE value setter (React's instance tracker swallows plain
    assignment) + input/change events; submit uses form.requestSubmit() with a
    submit() fallback. perm.check (SK-04) is unchanged — sensitive fields still
    require the agent's own perm.check first (AGENTS.md п.2/п.3: action.do carries
    the preview inside, replacing the separate preview->act step).
  * while hub/paused (SK-08) action.do is refused with paused_for_human like every
    browser-facing rpc — the human holds the browser. Contract v1 stays additive:
    hello remains v:1, action.preview keeps its standalone round-trip.

SK-24 (card: «action.do координаты: клик/hover по (x,y), клавиши, опора для SoM») —
the same gated method, three target modes, additive to the SK-17 shape:
  * {x, y, action:"click"|"rightclick"|"hover"|"dblclick"|"move", frame?} — coordinate
    execution for targets no selector reaches (canvas, overlays, same-origin iframe
    widgets). Coords are viewport-relative (page.screenshot space). frame is the
    optional page.dom frame-chain ("iframeSel > inner"); without it the extension
    auto-descends same-origin iframes under the point; a cross-origin frame answers
    cross_origin_frame (escalation: BiDi/hand-off, not around the gate). Selector and
    coordinates are mutually exclusive — an ambiguous target is bad_request, never a
    silent priority. Selector clicks may also carry frame (page.dom gives selector and
    frame as a pair).
  * {action:"key", key:"Enter"|..., modifiers?:["ctrl"|"shift"|"alt"|"meta"]} —
    KeyboardEvent on the focused element (dropdowns close/select from the keyboard).
    No selector/coords/frame may ride along: the focus IS the target.
  * п.3: selector clicks walk the FULL pointer path (pointerover/enter/move +
    pointerdown/mousedown/pointerup/mouseup at the element centre, then el.click() for
    the click event and the default action) — «клик есть, сайт не видит» was a missing
    mousedown/mouseup, not a missing click.
  * п.5: every click/hover/key answer carries hit:{tag,text≤60} — what REALLY sits
    under the point / received the key, the anti-«промахнулся» report.
  * the preview gate covers all of it (п.4): point previews draw the circle+cursor at
    (x,y), key previews ring the focused element; verdict vocabulary is untouched —
    frame chains resolve in the preview leg too, so a cross-origin frame refuses with
    shown:false reason:cross_origin_frame BEFORE the human ever sees a ring.
  * audit addendum 21.10 (а): hit-tests SKIP the extension's own overlay — the shadow
    host is hidden for the hit frame (elementFromPoint would otherwise return the
    preview bubble / Stop button: a real incident, session 20261021_104857).
  * audit addendum (б): click/rightclick/dblclick onto body/html/root container ->
    reason/err not_clickable (a miss is NOT a success — the audit caught shown:true on
    a body click). Preview refuses BEFORE the ring; hover/move over the background
    stay legitimate. The hit report (п.5) still answers for real non-shell elements.
  * audit addendum (в): {name, role?} target mode for click/rightclick/dblclick/hover
    — the extension resolves accessible names (aria-label/text/value/placeholder/alt/
    title/aria-labelledby) against role (button/link/checkbox/... or [role=...]).
    Unique -> the same full pointer path at the element centre; ambiguous ->
    {shown:false, reason:ambiguous, candidates:[{tag,text,rect}]} (the agent picks a
    coordinate, we never guess); none -> no_element. name is simultaneously the target
    and the human label («кликну «Удалить»»). name+selector/x/y -> the SK-17 label
    reading wins; role without name / name+frame -> bad_request.

SoM screenshots (SK-26; card: «SoM-скриншоты: номера-указатели на кнопках + клики по
номеру» — vision видит кнопку, но не может её адресовать, агент слеп после скрина):
  * rpc page.screenshot {marks:true} — content.js РИСУЕТ перед снимком номерные бейджи
    1..N у интерактивных элементов вьюпорта (тот же фильтр, что у page.dom; максимум
    40, приоритет площадь × близость к центру, нумерация reading order; плашка «номер +
    имя ≤18симв» у угла, НЕ перекрывая элемент; вне вьюпорта не маркируется; изолированный
    слой pointer-events:none — hit-тесты и так пропускают хост). Кадр берётся сразу
    после отрисовки (capture видит DOM контент-скрипта — тот же механизм, что делает
    видимым preview-кольцо), метки убираются сразу за кадром (страховка 2 с). Ответ:
    {dataUrl, marks:[{n, selector, tag, name, rect}], url, tabId} — карта номер→селектор.
    Скрин БЕЗ marks:true остаётся чистым (флаг — единственная причина рисовать).
  * action.do {mark:N} — цель «номер с последнего скрина задачи»: карту держит РАСШИРЕНИЕ
    (content-замыкание вкладки; задача↔вкладка — SK-25). Мост валидирует форму: int 1..64,
    глаголы click/input/submit/rightclick/dblclick/hover, взаимоисключение с selector/x-y/
    frame (name рядом — только подпись), key/move без цели → bad_request локально.
    Оба лега цепи резолвят метку ОДНИМ резолвером: превью рисует кольцо на элементе
    («кликну «Удалить» [7]»), исполнение — полный указательный путь по центру.
  * истечение карты: навигация/SPA (url), перестройка DOM (селектор исчез), скролл/сдвиг
    вёрстки (rect разошёлся > 2px) -> mark_stale. Отказ превью — успешный SK-05-вердикт
    {shown:false, executed:false, reason:"mark_stale"} (агент обязан ПЕРЕСНЯТЬ, а не
    зондить DOM), гонка между легами -> rpc err mark_stale, исполнения нет.
  * marks:true на вкладке без живого content.js -> marks_unavailable (лечится SK-18
    self-heal через page.dom, затем повтор); capture-сбой с уже нарисованными метками
    всё равно убирает их (catch-leg шлёт som_marks_clear).

Speed & addressing v2 (SK-27; card: «аудит живой сессии: 26 rpc на задачу, 40% имён
пустые, 41 no_element против 102 ok, 12 скроллей вслепую»):
  * page.dom {near?} — near:[x0,y0,x1,y1] (координаты вьюпорта) отсекает кандидатов вне
    региона ДО сортировки; мост валидирует форму локально (иначе bad_request, как флаг
    marks). Отбор: приоритет оверлей/диалог/портал (role=dialog/alertdialog/aria-modal/
    <dialog> или fixed-предок с z-index>=100) > элемент в вьюпорте > остальное; внутри
    — близость к центру near, затем reading order. Срез MAX_ELS=800 уходит в elements,
    хвост — в more:{count, elements:[{sid,role,name,rect}]} (сжатые записи; элемент всё
    равно резолвится по sid — реестр жив). truncated:true = хвост есть.
    SK-48: каждый тумблер (нативный checkbox/radio, label→control, aria
    checkbox/switch/pressed/checked/expanded) несёт checked:true|false|"mixed" —
    пост-чек клика без vision; у не-тумблеров ключа нет (undefined выпадает на
    сериализации).
  * page.media {} — SK-48, дешёвое чтение видео без превью и без vision:
    {url, count, vp:{w,h}, videos:[{paused, ended, muted, volume, currentTime, duration,
    readyState, rect:{x,y,w,h}, name?}]} (топ-8 видео). name —
    aria-label/title (подсказка, не идентичность); src НЕ отдаётся (может нести
    токенты). Плейбук «запусти видео» (tabs.activate — в фоновой вкладке Firefox
    держит play() на удержании — затем mute→Play) и handoff expect
    {kind:"video_playing"} проверяют paused/currentTime, а не угадывают по кадру;
    «играет» считается только видимое в вьюпорте видео (vp) — превью-карусель
    за кадром не считается;
    «загрузка» — readyState/duration.
  * nameSrc + hint: каждое имя несёт источник (aria-label|text|title|alt|placeholder|
    value|aria-labelledby|label|legend|sibling|svg|src|none); пусто — nameSrc:"none" и
    hint ("svg-icon class=…"), агент видит чем кликать, а не гадает (аудит: 40% пустых).
  * sid — детерминированный id элемента (FNV-1a от путь|tag|имя, 8 символов base36):
    один элемент в повторных снапшотах получает тот же sid. action.do {sid, action} —
    цель из последнего page.dom (карта меток SoM хранит тот же sid, mark-клик отдаёт его
    в ответе). Реестр живёт в content-замыкании вкладки; элемент умер/отсоединён или url
    сменился -> sid_stale — успешный вердикт {shown:false, executed:false, reason:
    "sid_stale"} (агент переснимает page.dom, не угадывает). Мост валидирует форму
    ([a-z0-9]{4,12}) и взаимоисключение с selector/x-y/frame/mark — локально, bad_request.
  * hitTarget: у элементов <2×2 px (чекбоксы-пиксели) page.dom отдаёт селектор и rect
    кликабельного label — агент бьёт по нему, а не мажет по input.
  * css-пути больше НЕ обрываются на 8 частях (баг аудита: обрезанные пути не уникальны,
    «селекторы сломаны»): полный nth-of-type-путь от html уникален по построению.
  * аддитивно к v1: вызовы без near/sid отвечают прежней формой (плюс nameSrc/hitTarget —
    новые поля старых записей), hello остаётся v:1.

SK-42 (card: «примитивы моста — ввод в contenteditable, scroll, координатный ввод,
хинты ошибок»; аудит 22.10, инциденты A/B — композер web.telegram.org не поддавался,
прокрутка делалась спамом клавиш через 8 превью-гейтов):
  * input теперь понимает contenteditable: резолвер цели идёт ВВЕРХ до ближайшего
    [contenteditable]-хоста (клик/селектор попадают в span внутри композера), ввод —
    beforeinput (cancelable: обработчик страницы может вето, err input_prevented) →
    execCommand('insertText') (браузер сам мутирует DOM и шлёт нативный input с
    inputType:'insertText' — событие, которому верят React/Vue) → change; если
    execCommand недоступен/отказал — ручной write + InputEvent(inputType:'insertText').
    Нативные input/textarea/select — прежний прототипный сеттер (SK-17). Семантика
    ввода — ЗАМЕНА содержимого (как value у нативных полей).
  * input без цели — фокус-режим (тот же принцип, что у key: цель — activeElement);
    input по координатам {x,y,value|text} — хит-тест elementFromPoint с пропуском
    собственного оверлея (SK-24 аддендум а), затем фокус и ввод. value остаётся именем
    v1, text — алиас (оба и разные — неоднозначность, bad_request).
  * новый глагол scroll: {dy} = window.scrollBy, {to:"top"|"bottom"}, {sid|mark} =
    scrollIntoView по центру (прокручивает и внутренний контейнер — лента/чат).
    scroll — ЕДИНСТВЕННЫЙ action.do без превью-гейта: движение вьюпорта ничего на
    странице не меняет (класс чтения, сосед page.*), а гейтинг = превью-спам
    (инцидент B). Ответ несёт scrolled:{dx,dy,y} или note «окно не сдвинулось» —
    агент видит эффект без пересъёмки.
  * п.4 самодостаточные отказы: bad_request моста — с hint и примером допустимой
    формы (валидатор знает, ЧТО именно не так); not_editable расширения — с tag и
    contenteditable цели; отказ гейта (reason) — с hint из REASON_HINTS. Поле hint
    аддитивно в вердикте rpc (контракт v1: старые клиенты его не читают). Агент не
    обязан читать исходники моста, чтобы починить вызов (инцидент A: 13 tool-вызовов
    на греп).
  * SK-24/SK-26/SK-27 матрицы не изменены: все прежние цели/глаголы/вердикты те же.

Task↔tab binding (SK-25; card: «агент бьёт по своей вкладке, пользователь серфит свободно»):
  * the bug this kills: page.*/action.do used to target the ACTIVE tab — the human switched
    tabs/windows (two windows of one profile!) and the agent started clicking someone else's
    page. Since SK-25 a task owns a tab: every browser-facing rpc may carry
    params {task:"<tid>"} (and optionally {tab:<int>}); the bridge resolves the target:
    explicit tab wins, else the tab bound to the task (injected into the forwarded params —
    the extension executes in a CONCRETE tab, never in the active one), else the active tab
    (п.6 backward compat: callers without task/tab behave exactly as before).
  * binding: tabs.open_grouped {task} binds the task to the opened tab (FIRST binding wins —
    a task owns one tab; opening more tabs does not move it); tabs.use_tab {task, tabId?}
    RE-binds (explicit move; also the recovery verb after tab_moved). The bridge records
    {tab, window, url} and refreshes url from every tabbed result carrying result.url —
    perm.check/handoff.request with task and no url get the bound url backfilled.
  * tab_gone / tab_moved are extension-side machine errors (the extension owns browser
    events): a closed bound tab answers "tab_gone:…" and the bridge marks the binding DEAD
    — every further rpc of that task answers tab_gone locally (never falls back to the
    active tab; open_grouped/use_tab revive the task on a new tab); a tab whose URL changed
    outside the agent-navigation grace window answers "tab_moved:…" (sticky until
    tabs.use_tab re-binds). Both relay verbatim as rpc errors — the AGENTS.md п.9 contract
    makes the agent stop and re-ask in chat.
  * every tabbed audit line carries "tab":<int> (acceptance: «все rpc задачи с tab=A»).
  * on bind/rebind/unbind the bridge pushes {v:1,type:"task_tab",task,tab,window?,url?}
    (tab:null = unbound) — the extension renders the tab title in the task card (п.4; the
    card click focuses the tab — the human's own gesture). No pinning: temp-add-on cannot
    guarantee it; the tab stays in the Sidekick group.
  * page.active {} — the ONLY active-tab query left: an explicit method the agent calls to
    see what the human has open; a `tab` param on it is bad_request (contradictory target).
  * windowId of the bound tab is recorded (п.5 multi-window); the extension resolves the
    window from the tab itself for capture (tabs.captureTab when present, else
    captureVisibleTab(tab.windowId) — a background tab in an unfocused window is honest
    only through captureTab).

Tab inventory & management (SK-32; card: «tabs.list/close/activate — провал задачи
закрытия вкладок», аудит 21.10 п.5: агент 7 минут копал CDP/Marionette/selenium, потому
что вкладочных глаголов не было):
  * rpc tabs.list {} — bridge forwards to the extension (it owns browser.tabs):
    {tabs:[{tabId,url,title,active,pinned,groupId,windowId,index}]} — ALL windows.
    The bridge annotates every tab bound to a live task (SK-25 TASK_TAB, not dead):
    {protected:true, task:"<tid>"} — the agent sees protection at inventory time,
    not at refusal time.
  * rpc tabs.close {tabId | tabIds:[...], force?} — batch close, the bridge gates it
    (the raw method reaches the extension only after a clean verdict — the SK-17
    conveyor principle). Validation is local: tabId XOR tabIds (both = bad_request,
    an ambiguous target is never prioritised — SK-24 precedent), ints >= 0, dedup,
    cap TABS_CLOSE_MAX, force must be a bool. Refusals BEFORE any ask:
      protected_tab — a tab bound to a live task (SK-25) without force:true;
    then ONE human permission per call (card: «массово, с пермиссией»): perm_decide
    on the synthetic domain «вкладки» / action close_tabs -> ask -> perm_ask frame
    («Hermes просит: закрыть вкладки», hint = count + ids or the agent's own hint).
    once -> forward close on the AGENT's rid (the answer is the close result, not
    {decision}); always -> persistent grant «вкладки» (further closes answer without
    an ask); deny -> SUCCESSFUL answer {closed:[],denied:true,reason:user_denied}
    (SK-05 vocabulary — a human veto is not an rpc error); ask_timeout -> same shape
    with reason "ask_timeout". grant block -> {denied:true,reason:grant_block}.
    The extension executes per-tab: closed:[ids], skipped:[{tabId,reason:
    no_tab|last_in_window|remove_failed}] — the last tab of a window survives
    without force (closing it would close the window — never silently).
  * rpc tabs.activate {tabId} | {task} — focus the tab and its window; the target is
    explicit (tabId) or the task's bound tab (SK-25 resolver: dead binding -> tab_gone,
    no target -> bad_request, never the active tab as a guess). The answer confirms:
    {activated:true, tabId, windowId, url, title}.
  * all three are browser-facing (BROWSER_RPC_RE tabs.*) -> state "acting", and the
    SK-08 PAUSED gate refuses them with paused_for_human like every page.*/action.*.
  * the bypass ban (AGENTS.md п.6, SOUL): CDP :9222 / Marionette :2828 / selenium /
    geckodriver / MCP browser tools are NOT agent channels — the bridge RPC is the
    only one; a missing method is reported to the human, never worked around.

Chat history (SK-23; card: «история чатов: новый чат, возврат к прошлому с контекстом»):
  * $SIDEKICK_HOME/chats.jsonl mirrors BOTH sides of every sidebar conversation:
    the human's chat frames (WS -> inbox leg) and the agent's chat frames (outbox
    pump leg) are appended as one line per message {chat_id, role:user|agent, text, ts}
    (text capped CHATS_TEXT_MAX, empty never logged). The spec record shape
    {chat_id, title, ts, msgs:[{role,text,ts}]} is ASSEMBLED ON READ (chats_records):
    title = first user utterance clipped to 60, ts = first message. Append-only keeps
    the writer O(1) and crash-safe; malformed lines are skipped, never fatal.
  * chat attribution: the extension stamps {chat:"<id>"} on user frames and agentd
    stamps chat on every task frame (both additive — frames without it belong to the
    "default" chat, п.4 compatibility). The bridge never invents a chat id.
  * rpc chats.list {} -> {chats:[{id,title,ts,last}]} — the newest CHATS_LIST_MAX (30)
    chats, sorted by ts desc (ties keep first-seen order); last = last utterance
    {role,text,ts}. rpc chats.get {chat_id} -> {chat:{chat_id,title,ts,msgs}} (unknown
    chat = empty msgs, not an error). Both bridge-local (chats.jsonl lives HERE) and
    behind the SK-08 PAUSED gate like audit.tail: the history is the human's own
    conversation text — while they hold the browser, the agent reads nothing.
  * WS frame chats_get {id, chat_id?} -> chats_data {id, chats:[...]} (no chat_id =
    list) | {id, chat:{...}} (with chat_id) — the sidebar's pull leg, consumed like
    audit_get (never inbox) and NOT audited (the panel pulls; auditing the poll feeds
    the audit with itself). Works during a hand-off pause — the human at the keyboard
    may read their own history.

Hub plumbing (SK-33; audit 21.10 п.7: rpc_out 20 МБ без ротации, ложный «мост лежит»
из-за systemctl без XDG_RUNTIME_DIR, голый jsonl-хаб без обвязки):
  * ротация по записи rpc_out.jsonl / audit.jsonl / events.jsonl: порог 5 МБ
    (env SIDEKICK_ROTATE_MAX, байт), хранить 2 бэкапа (.1, .2), переменование при
    записи — читатель по пути никогда не видит файл длиннее порога надолго;
    audit_tail/agentd (tail_lines) ротацию по пути переживают, sk следит за inode.
  * hub/health.json — пишет мост, читают все (agentd по SK-28, sk status):
      {"bridge":"active","extension":"connected|offline","peer":"<ip:port>|null",
       "ts":<epoch>,"ws_count":N}
    атомарно (tmp+replace); peer — адрес АКТИВНОГО расширения (SK-49: их никогда
    не больше одного, peer отвечает на «куда именно уходят RPC»); обновление на
    connect/disconnect, на старте и каждые
    10 с (env SIDEKICK_HEALTH_TICK). Правило свежести для читателей: ts не старше
    ~30 с = мост жив; systemctl --user для статуса НЕ нужен (без XDG_RUNTIME_DIR
    он врёт «мост лежит» — аудит 21.10 п.7).
  * events.jsonl: мост дописывает ts в рамку, если своего нет (sk events --since
    фильтрует по ts; agentd читает только "type" — поле аддитивно, формат не ломается).
  * CLI агента — ~/sidekick/bin/sk (sk rpc / sk status / sk events), контракт в
    AGENTS.md п.11; агентам хелперы руками писать нечем.
"""
import asyncio, collections, hmac, json, math, os, re, secrets, signal, time
from urllib.parse import urlsplit
import websockets

DIR = os.path.expanduser(os.environ.get("SIDEKICK_HOME", "~/sidekick/bridge/hub"))
PORT = int(os.environ.get("SIDEKICK_PORT", "8791"))
# SK-28: bind-адрес. 127.0.0.1 (default) — ничего не ломаем: чистый запуск = туннельный
# режим, как раньше. 0.0.0.0 — мост смотрит в LAN (домашняя сеть), токен по ws летит
# открытым текстом (README «Подключение без ssh», предупреждение в AGENTS.md п.8).
BIND = os.environ.get("SIDEKICK_BIND", "127.0.0.1")
# SK-28: rate-limit отказов токена (4003) на source IP — «шум отрезать», не защиту
# от брутфорса 64-hex изображать. Успешный handshake счётчик снимает.
TOKEN_FAIL_LIMIT = int(os.environ.get("SIDEKICK_TOKEN_FAIL_LIMIT", "5"))
TOKEN_FAIL_WINDOW_S = float(os.environ.get("SIDEKICK_TOKEN_FAIL_WINDOW", "60"))
INBOX, OUTBOX, TOKEN_FILE = f"{DIR}/inbox.jsonl", f"{DIR}/outbox.jsonl", f"{DIR}/token"
RPC_IN, RPC_OUT, AUDIT = f"{DIR}/rpc_in.jsonl", f"{DIR}/rpc_out.jsonl", f"{DIR}/audit.jsonl"
GRANTS_FILE, EVENTS = f"{DIR}/grants.json", f"{DIR}/events.jsonl"
# SK-34 п.1: память «на этом домене человек уже разрешал разово» — (domain|action) ->
# {ts}. Персистентна (мост перезапускаются редко, но кнопка «всегда» должна жить и
# после рестарта), атомарная запись как у grants.
PERM_ONCE_FILE = f"{DIR}/perm_once.json"
PERM_ONCE_MAX = 512  # записей; старше — выпариваются первыми
STOP = f"{DIR}/stop"  # SK-06 pause flag: touched by the Stop button, checked by the agent between steps
PAUSED = f"{DIR}/paused"  # SK-08 hand-off flag: human holds the browser — page.*/preview refused
CHATS = f"{DIR}/chats.jsonl"  # SK-23: append-only mirror of every sidebar conversation
CHATS_LIST_MAX = 30   # chats.list answers the newest 30 chats (card: «35 чатов → режет до 30»)
CHATS_TITLE_MAX = 60  # card: «title (первая реплика ≤60симв)»
CHATS_TEXT_MAX = 4000
CHATS_ID_MAX = 80
RPC_TIMEOUT_S = float(os.environ.get("SIDEKICK_RPC_TIMEOUT", "10"))
PERM_TIMEOUT_S = float(os.environ.get("SIDEKICK_PERM_TIMEOUT", "60"))  # SK-34 п.3: 300 -> 60
HANDOFF_TIMEOUT_S = float(os.environ.get("SIDEKICK_HANDOFF_TIMEOUT", "600"))
# SK-33: hub plumbing — health для читателей без systemctl + ротация jsonl-потоков
HEALTH = f"{DIR}/health.json"
ROTATE_MAX = int(os.environ.get("SIDEKICK_ROTATE_MAX", 5 * 1024 * 1024))
ROTATE_KEEP = 2  # бэкапов: file.1, file.2 (третьего нет — «хранить 2 ротации»)
HEALTH_TICK_S = float(os.environ.get("SIDEKICK_HEALTH_TICK", "10"))
PROTOCOL_VERSION = 1
HELLO_TIMEOUT_S = 5
# SK-46: «уже спаренный» IP для legacy_hello (первый фрейм hb вместо hello у
# старых сборок расширения). Окно короткое — закрывает только gap реконнекта
# после рестарта моста, не токен-фри проход для чужаков. 0 = приём выключен.
LEGACY_HELLO_WINDOW_S = float(os.environ.get("SIDEKICK_LEGACY_HELLO_WINDOW_S", "120"))
PAIRED_FILE = f"{DIR}/.paired_ips"   # ip -> ts успешного handshake (переживает рестарт)
CLEAN_MARK = f"{DIR}/.clean_exit"    # «прошлый ран завершился чисто» (SIGTERM/SIGINT)
RECONNECT_WINDOW_S = 3600.0          # окно счётчика reconnects_per_hour (health.json)
os.makedirs(DIR, exist_ok=True)
CLIENTS = set()
# SK-46: флэппинг-метрика — ts успешных handshake за последний час. Считаем все
# успешные handshake (первый после рестарта тоже): смысл не в букве «re-»,
# а в «сколько раз за час канал пересобирался»; шторм 22.10 = 12 переходов/70 мин.
RECONNECTS = collections.deque()
PAIRED_IPS = {}                      # SK-46: см. LEGACY_HELLO_WINDOW_S
# SK-49: ws -> "ip:port" активного расширения (политика одного соединения —
# элементов всегда ≤1; словарь, а не атрибут сокета, чтобы не жить в websockets).
CLIENT_PEER = {}
TOKEN_FAILS = {}  # SK-28: source ip -> [ts плохих токенов] (окно TOKEN_FAIL_WINDOW_S)
PENDING = {}   # rpc id -> (timeout task, method)
PERM_ASKS = {} # perm.check rpc id -> {domain, action} (audit + reconnect replay)
# SK-34 п.3: подряд идущие ask_timeout по (task, domain, action) — после двух
# одинаковый пермес больше НЕ беспокоит человека (block via ask_timeout_streak),
# агентд ставит задачу на паузу по событию perm_timeout из events.jsonl.
TIMEOUT_STREAK = {}  # (task, domain, action) -> int
# SK-34 п.4: недавние deny — чтобы запоздалая причина из чипа (perm_reason) попала
# в аудит, а не в «orphan ignored». TTL 10 мин, одна причина на ask (pop при выдаче).
RECENT_DENIES = {}  # ask id -> {domain, action, ts}
RECENT_DENIES_TTL_S = 600.0
HANDOFFS = {}  # SK-08 handoff.request rpc id -> {what, task, url, ts} (reconnect replay)
# SK-48: верификация handoff — «Готово» не есть результат. expect хранится в записи
# HANDOFFS, незакрытые проверки (внутренние rpc-леги) — здесь: internal id ->
# {rid, expect, attempt, shot_leg?}. Ответ агента аддитивен: {resumed:true, verified:...}.
HANDOFF_VERIFY = {}
HANDOFF_EXPECT_KINDS = {"video_playing", "dom"}
HANDOFF_VERIFY_MAX_RETRY = 2   # «Готово» при недостиге: баннер с кадром ещё N раз, дальше — вердикт наружу
HANDOFF_VERIFY_AUDIT = "handoff_verify"
HANDOFF_EXPECT_NAME_MAX = 120
AGENT_STATE = None  # SK-09: last broadcast agent state (None = nothing sent yet)
TASK_STATE = ""     # SK-09: from SK-06 task frames — running|paused|done|failed ("" = never seen)
TASK_ID = ""        # SK-09: current task id (from task/step frames), for the state frame
BROWSER_RPC_RE = re.compile(r"^(page\.|action\.|tabs\.)")  # SK-09: browser I/O = "acting"
ACTION_DO_ACTIONS = {"click", "input", "submit",  # SK-17: the gated executor vocabulary
                     "rightclick", "hover", "dblclick", "move", "key",  # SK-24: coords + keys
                     "scroll"}  # SK-42 п.2: viewport movement (the only gate-free verb)
ACTION_DO_COORDS = {"rightclick", "hover", "dblclick", "move"}  # SK-24 п.1: coord-only verbs
ACTION_DO_NAME = {"click", "rightclick", "dblclick", "hover"}  # SK-24 аддендум (в): name/role verbs
ACTION_DO_MODIFIERS = {"ctrl", "control", "shift", "alt", "meta"}  # SK-24 п.2 ("ctrlKey" suffix optional)
ACTION_DO_FRAME_MAX = 500  # page.dom frame-chains are css paths — 500 is generous
ACTION_DO_NAME_MAX = 200  # accessible names are short; 200 is generous
ACTION_DO_ROLE_MAX = 40
# SK-26: mark — цель «номер с последнего SoM-скрина». Глаголы: все, у которых есть
# элемент-цель (key — фокус, move — координаты: метки там не при чём).
ACTION_DO_MARK = {"click", "input", "submit", "rightclick", "dblclick", "hover"}
# SK-27: sid — стабильный id из page.dom (content-реестр резолвит; мост — форма и
# взаимоисключение). Тот же набор глаголов, что у mark: цель есть, key/move — без цели.
ACTION_DO_SID_RE = re.compile(r"[a-z0-9]{4,12}\Z", re.I)
ACTION_DO_MARK_MAX = 64  # SoM-карта ~40 меток (content SOM_MAX); 64 — потолок с запасом
ACTION_DO_CHAIN = {}  # SK-17: internal preview-leg rpc id -> {rid, action, params, client}

# ---- SK-25: task↔tab (реестр на мосту; расширение — исполнитель и свидетель) --------
TASK_TAB = {}  # task id -> {"tab": int, "window": int|None, "url": str, "ts": int}
RPC_REQ = {}   # rid -> {"task": str, "tab": int|None} — для биндинга/аудита на rpc_result
TAB_METHODS = {"page.text", "page.dom", "page.media", "page.screenshot", "action.preview",
               "action.do", "tabs.use_tab"}  # методы, принимающие task/tab (инъекция — только не BIND)
BIND_METHODS = {"tabs.open_grouped", "tabs.use_tab"}  # ответы, привязывающие задачу к вкладке
TAB_TASK_MAX = 64  # cap: мост не разрастается за счёт брошенных задач (stale-запись дёшева tab_gone)
ERR_TAB_GONE = ("tab_gone: вкладка задачи закрыта — переспроси человека в чате, "
                "не бей по чужой вкладке; tabs.open_grouped/use_tab перепривяжут")
# ---- SK-32: tabs.list/close/activate -------------------------------------------------
TABS_CLOSE_MAX = 64      # пачка закрытия: больше — явный признак агента-пылесоса, bad_request
TABS_CLOSE_DOMAIN = "вкладки"  # синтетический домен перм-конвейера (grant «всегда» = ключ grants)
TABS_CLOSE_CHAIN = {}    # ask rid -> {rid, params, client} — гейт-вердикт -> лег закрытия

def tab_resolve(params, inject=True):
    """(err, task, tab) — SK-25 п.1/п.6. Явный `tab` (int>=0) побеждает; иначе вкладка,
    привязанная к `task` (инжектится в params — расширение исполняет в КОНКРЕТНОЙ вкладке);
    иначе (task, None) = активная вкладка (обратная совместимость). Неверный `tab` —
    bad_request локально, до раунд-трипа к браузеру."""
    t = params.get("task")
    task = str(t)[:TAB_TASK_MAX] if isinstance(t, str) and t.strip() else ""
    tab = params.get("tab")
    if tab is not None and (isinstance(tab, bool) or not isinstance(tab, int) or tab < 0):
        return "bad_request", task, None
    if tab is None and inject and task and task in TASK_TAB:
        if TASK_TAB[task].get("dead"):
            # вкладка задачи умерла: задача НЕ имеет права на чужую (активную) страницу —
            # каждый rpc честно отвечает tab_gone, пока человек не перепривяжет (п.9)
            return ERR_TAB_GONE, task, None
        tab = TASK_TAB[task]["tab"]
        params["tab"] = tab
    return None, task, tab

def push_task_tab(task):
    """SK-25 п.4: bind/rebind/unbind — карточка задачи в сайдбаре должна видеть вкладку
    (заголовок резолвит расширение, у него browser.tabs). tab:null = отвязана/мертва."""
    e = TASK_TAB.get(task)
    frame = {"v": 1, "type": "task_tab", "task": task,
             "tab": (None if e.get("dead") else e["tab"]) if e else None,
             **({"window": e["window"]} if e and e.get("window") is not None else {}),
             **({"url": e["url"]} if e and e.get("url") else {})}
    for c in list(CLIENTS):
        asyncio.create_task(safe_send(c, frame))

def bind_task_tab(task, res, rebind):
    """Биндинг по ответу вкладочного метода. rebind=False (open_grouped) — первая
    привязка выигрывает; rebind=True (use_tab) — явное перемещение задачи.
    Мёртвая запись (после tab_gone) перетирается обеими ногами — задача перерождается."""
    if not task or not isinstance(res, dict) or not isinstance(res.get("tabId"), int):
        return False
    if task not in TASK_TAB and len(TASK_TAB) >= TAB_TASK_MAX:
        return False
    if task in TASK_TAB and not rebind and not TASK_TAB[task].get("dead"):
        return False  # open_grouped: первая привязка выигрывает, задача владеет одной вкладкой
    TASK_TAB[task] = {"tab": res["tabId"],
                      "window": res.get("windowId") if isinstance(res.get("windowId"), int) else None,
                      "url": str(res.get("url") or "")[:2000], "ts": int(time.time())}
    push_task_tab(task)
    log(f"SK-25: task {task[:12]} -> tab {res['tabId']}"
        + (f" (window {TASK_TAB[task]['window']})" if TASK_TAB[task]["window"] is not None else ""))
    return True

# ---- SK-32: вкладочные глаголы (инвентаризация, массовое закрытие с гейтом, фокус) ----

def live_bound_tabs():
    """{tabId: task} — вкладки живых задач (не dead): защита от tabs.close без force."""
    return {e["tab"]: t for t, e in TASK_TAB.items() if not e.get("dead") and isinstance(e.get("tab"), int)}

def tabs_close_validate(params):
    """(err, tab_ids, force) — валидация ЛОКАЛЬНАЯ (bad_request и без браузера, как
    action.do). tabId XOR tabIds: оба поля — неоднозначная цель, не приоритизируется
    (прецедент SK-24). Целые >= 0, дедуп с сохранением порядка, кап TABS_CLOSE_MAX,
    force — только bool (не «да» строкой и не 1)."""
    force = params.get("force")
    if force is None:
        force = False
    if not isinstance(force, bool):
        return "bad_request", [], False
    raw, single = params.get("tabIds"), params.get("tabId")
    if raw is not None and single is not None:
        return "bad_request", [], False
    if raw is None:
        if single is None:
            return "bad_request", [], False  # цели нет вообще
        raw = [single]
    if not isinstance(raw, list) or not raw:
        return "bad_request", [], False
    ids = []
    for x in raw:
        if isinstance(x, bool) or not isinstance(x, int) or x < 0:
            return "bad_request", [], False
        if x not in ids:
            ids.append(x)
    if not ids or len(ids) > TABS_CLOSE_MAX:
        return "bad_request", [], False
    return None, ids, force

def tabs_activate_resolve(params):
    """SK-32: цель tabs.activate — явный tabId ИЛИ вкладка задачи (SK-25). Мутом
    досытывает params["tabId"]; активной вкладкой НЕ угадывает (п.9: у задачи — своя
    вкладка, у агента без задачи — явный tabId). -> err-строка или None."""
    tab = params.get("tabId")
    if tab is not None:
        if isinstance(tab, bool) or not isinstance(tab, int) or tab < 0:
            return "bad_request"
        return None
    t = str(params.get("task") or "")[:TAB_TASK_MAX]
    e = TASK_TAB.get(t)
    if e is not None:
        if e.get("dead"):
            return ERR_TAB_GONE
        params["tabId"] = e["tab"]
        return None
    return "bad_request"  # ни tabId, ни живой task — цели нет, активную не угадываем

def tabs_close_forward_params(tab_ids, force):
    p = {"tabIds": tab_ids}
    if force:
        p["force"] = True
    return p

def handle_tabs_close(rid, tab_ids, force, params, client):
    """Гейт на мосту: защищённые вкладки -> отказ ДО переспроса; затем ОДНО человеческое
    разрешение на всю пачку (perm_decide по синтетическому домену «вкладки» — обычно ask;
    grant «всегда» после always — allow без переспроса). Разрешение — и закрытие уходит
    расширению на rid агента: ответом будет результат закрытия, а не {decision}."""
    live = live_bound_tabs()
    hits = [i for i in tab_ids if i in live]
    if hits and not force:
        rpc_respond(rid, False,
                    err=("protected_tab: " + ",".join(map(str, hits)) +
                         " — вкладки, привязанные к задачам (SK-25): исключи из списка "
                         "или force:true (и объясни человеку)"),
                    method="tabs.close")
        log(f"tabs.close ({str(rid)[:8]}): REJECTED protected {hits} (без force)")
        return
    decision, via, _sensitive = perm_decide(TABS_CLOSE_DOMAIN, "close_tabs")
    # SK-34 п.3: тот же streak, что и у perm.check: два молчаливых таймаута —
    # третий вызов получает честный denied без нового вопроса.
    t = str(params.get("task") or "")[:TAB_TASK_MAX]
    if TIMEOUT_STREAK.get((t, TABS_CLOSE_DOMAIN, "close_tabs"), 0) >= 2:
        rpc_respond(rid, True, {"closed": [], "denied": True,
                                "reason": "ask_timeout_streak"}, method="tabs.close")
        audit({"ts": int(time.time()), "ev": "perm", "domain": TABS_CLOSE_DOMAIN,
               "action": "close_tabs", "decision": "block", "via": "ask_timeout_streak"})
        log(f"tabs.close ({str(rid)[:8]}): streak таймаутов — не беспокою, denied")
        return
    fwd = tabs_close_forward_params(tab_ids, force)
    if decision == "block":
        rpc_respond(rid, True, {"closed": [], "denied": True, "reason": "grant_block"},
                    method="tabs.close")
        audit({"ts": int(time.time()), "ev": "perm", "domain": TABS_CLOSE_DOMAIN,
               "action": "close_tabs", "decision": "block", "via": via})
        return
    if decision == "allow":
        PENDING[rid] = (asyncio.create_task(rpc_timeout(rid, "tabs.close")), "tabs.close")
        asyncio.create_task(safe_send(client, {"v": 1, "type": "rpc", "id": rid,
                                               "method": "tabs.close", "params": fwd}))
        push_state()
        log(f"tabs.close ({str(rid)[:8]}): grant {via} -> closing {len(tab_ids)} tab(s)")
        return
    # ask — одно переспрашивание на пачку; hint: свой от агента (подпись) или счёт+id
    hint = str(params.get("hint") or "")[:200].strip()
    if not hint:
        hint = (f"закрыть {len(tab_ids)} вкладок(и): " + ",".join(map(str, tab_ids[:12])))[:200]
    info = {"domain": TABS_CLOSE_DOMAIN, "action": "close_tabs", "reason": "close_tabs",
            "url": "", "hint": hint, "task": t}
    PERM_ASKS[rid] = info
    TABS_CLOSE_CHAIN[rid] = {"rid": rid, "params": fwd, "client": client}
    PENDING[rid] = (asyncio.create_task(perm_timeout(rid)), "tabs.close")
    # SK-34 п.1: «всегда» для вкладок — та же схема в один тап, что и для доменов
    # (grant-ключ — синтетический домен «вкладки», п.6 SK-32).
    asyncio.create_task(safe_send(client, perm_frame(
        rid, info, show_always=once_seen(TABS_CLOSE_DOMAIN, "close_tabs"))))
    asyncio.create_task(perm_thumb(rid, info))  # SK-37: миниатюра вкладки задачи
    push_state()  # SK-09: ждём человека = needs_you
    audit({"ts": int(time.time()), "ev": "perm", "domain": TABS_CLOSE_DOMAIN,
           "action": "close_tabs", "decision": "ask", "via": via, "n": len(tab_ids)})
    log(f"tabs.close ({str(rid)[:8]}): {len(tab_ids)} tab(s) — переспрашиваю человека")

def tabs_list_annotate(res):
    """SK-32: инвентаризация с пометкой защиты — агент видит protected/task до отказа,
    а не после. Мутит результат на месте (rpc_respond отдаёт тот же объект)."""
    if not isinstance(res, dict) or not isinstance(res.get("tabs"), list):
        return
    live = live_bound_tabs()
    for tb in res["tabs"]:
        if isinstance(tb, dict) and tb.get("tabId") in live:
            tb["protected"] = True
            tb["task"] = live[tb["tabId"]]


# SK-04 «always human» classes (card: payment forms, password/card fields,
# OAuth redirects, 2FA — blocked until explicit confirmation, even at Allow).
# SK-34 п.1: «отправка формы с текстом письма наружу» — send_message/send_email тоже
# здесь: текст наружу — приватность, «всегда» не предлагается. Расширение через env
# SIDEKICK_ALWAYS_HUMAN=a,b (список чувствительного — в конфиге, дефолт расширен).
ALWAYS_HUMAN_ACTIONS = {"sensitive_input", "payment", "oauth", "2fa",
                        "send_message", "send_email"} \
    | {a.strip().lower() for a in os.environ.get("SIDEKICK_ALWAYS_HUMAN", "").split(",") if a.strip()}

# ---- SK-43 п.1: семантика perm.check — action это ГЛАГОЛ действия, perm это ТИП пермиса.
# Инцидент B (аудит №2 22.10): агент «для надёжности» пометил обычный клик по аватару
# YouTube как sensitive_input (таблицу ALWAYS_HUMAN он вычитал из исходников моста) —
# человек получил карточку «действие с полем пароля/карты» на youtube.com. Мост больше
# не крутит карточку на бессмыслице: обычный клик/клавиша/скролл не могут нести текст
# наружу или ввод в поле пароля. Границы типов — rpc capabilities (п.2), а не grep.
PERM_OPS = set(ACTION_DO_ACTIONS)  # глаголы реального действия (словарь action.do)
PERM_OPS_NOISE = PERM_OPS - {"input", "submit"}  # текст не вводят и наружу не несут
PERM_TEXT_OUT = {"sensitive_input", "send_message", "send_email"}
# RU banks + payment endpoints (user geography); extend via env SIDEKICK_BANK_DOMAINS=a,b
BANK_DOMAINS = {
    "sberbank.ru", "sbolbank.ru", "vtb.ru", "alfabank.ru", "alfaimvest.ru",
    "tinkoff.ru", "tbank.ru", "tpbank.ru", "gazprombank.ru", "bankpsb.ru", "psb.ru",
    "rsbi.ru", "mtsbank.ru", "akbars.ru", "avangabank.ru", "sovcombank.ru",
    "pochtabank.ru", "ubrr.ru", "paypal.com", "stripe.com", "yoomoney.ru",
    "oplazaru.ru", "gosuslugi.ru", "nalog.ru",
} | {d.strip().lower() for d in os.environ.get("SIDEKICK_BANK_DOMAINS", "").split(",") if d.strip()}
OAUTH_PATH_RE = re.compile(
    r"(oauth|authorize|signin|sign-in|signup|sign-up|login|log-in|password|2fa|mfa|otp|payment|checkout|billing)", re.I)

def log(msg): print(f"[bridge {time.strftime('%H:%M:%S')}] {msg}", flush=True)

def load_token():
    """Persist a per-install session token; regenerate only if the file is absent."""
    try:
        tok = open(TOKEN_FILE).read().strip()
        if tok:
            os.chmod(TOKEN_FILE, 0o600)
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_hex(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    log(f"generated new session token -> {TOKEN_FILE} (chmod 600)")
    return tok

_HAD_STATE = os.path.exists(TOKEN_FILE)  # SK-46: первый ли это ран хаба (до load_token)
TOKEN = load_token()

def valid_v(v):
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= PROTOCOL_VERSION

def peer_ip(ws):
    """SK-28: source IP для rate-limit'а (без порта — один нарушитель ≠ один сокет)."""
    try:
        return str(ws.remote_address[0]) if ws.remote_address else "?"
    except Exception:
        return "?"

def token_fails_pruned(ip):
    """Свежие отказы токена ip в окне; протухшие чипим на месте (словарь не растёт)."""
    now = time.time()
    fails = TOKEN_FAILS.get(ip)
    if not fails:
        return []
    fresh = [t for t in fails if now - t <= TOKEN_FAIL_WINDOW_S]
    if fresh:
        TOKEN_FAILS[ip] = fresh
    else:
        TOKEN_FAILS.pop(ip, None)
    return fresh

def token_rate_limited(ip):
    """SK-28: > TOKEN_FAIL_LIMIT плохих токенов за TOKEN_FAIL_WINDOW_S с одного IP."""
    return len(token_fails_pruned(ip)) >= TOKEN_FAIL_LIMIT

def note_token_fail(ip):
    TOKEN_FAILS.setdefault(ip, []).append(time.time())
    token_fails_pruned(ip)

# ---- SK-46: память «этот IP уже спарен» (legacy_hello) --------------------------

def paired_load():
    """Спаренные IP переживают рестарт моста: сценарий шторма — мост перезапустили
    (SIGKILL/панель), старая сборка переподключается hb-первым фреймом, а памяти о
    токене в новой памяти процесса уже нет. Грязные/просроченные записи >сутков
    выбрасываем; честное окно проверяет paired_recent."""
    global PAIRED_IPS
    try:
        with open(PAIRED_FILE) as f:
            d = json.load(f)
        now = time.time()
        PAIRED_IPS = {str(ip): float(ts) for ip, ts in d.items()
                      if isinstance(ts, (int, float)) and now - float(ts) <= 86400}
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"SK-46: paired load err: {e}")

def paired_note(ip):
    """Успешный handshake = IP спарена. Пишем файл сразу (атомарно, как grants):
    handshake'и редки, файл — горстка ip->ts; рестарт не должен слепить мост."""
    PAIRED_IPS[ip] = time.time()
    now = time.time()
    for stale in [k for k, t in PAIRED_IPS.items() if now - t > 86400]:
        PAIRED_IPS.pop(stale, None)
    try:
        tmp = PAIRED_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(PAIRED_IPS, f)
        os.replace(tmp, PAIRED_FILE)
    except OSError as e:
        log(f"SK-46: paired write err: {e}")

def paired_recent(ip):
    ts = PAIRED_IPS.get(ip)
    return (ts is not None and LEGACY_HELLO_WINDOW_S > 0
            and time.time() - ts <= LEGACY_HELLO_WINDOW_S)

async def safe_send(ws, obj):
    try: await ws.send(json.dumps(obj))
    except Exception: pass


def abort_silent(ws):
    """SK-41: до-аутентификационные отказы закрываются МОЛЧА (RST, без close-frame).

    Страница-щуп (new WebSocket('ws://127.0.0.1:8791')) видит 1006 — как у любого
    сдохшего сервиса, — а не опознавательный close(4001, "hello timeout"). Пути
    расширения сюда не доходят: контракт v1 — hello ВСЕГДА первый фрейм (SK-46),
    а легальные вердикты 4002/4003/4009, на которые ветвится background.js, не
    тронуты. transport.abort() шлёт RST — браузеры показывают это как 1006.
    """
    try:
        ws.transport.abort()
    except Exception:
        pass  # транспорт уже мёртв — сокет всё равно доживает последние секунды


async def handshake(ws):
    """Return negotiated protocol version, or None (socket already closed)."""
    peer = f"{ws.remote_address[0]}:{ws.remote_address[1]}" if ws.remote_address else "?"
    ip = peer_ip(ws)
    # SK-28: IP отрезан после серии плохих токенов — закрываем БЕЗ handshake
    # (ни hello_ack, ни чтения hello: шум из одного источника не кормим).
    if token_rate_limited(ip):
        log(f"handshake REJECTED from {peer}: token rate limit "
            f"(> {TOKEN_FAIL_LIMIT} bad tokens in {TOKEN_FAIL_WINDOW_S:.0f}s) — closed without handshake")
        await ws.close(4003, "token rate limit"); return None
    try:
        raw = await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT_S)
    except asyncio.TimeoutError:
        log(f"handshake REJECTED from {peer}: no hello within {HELLO_TIMEOUT_S}s (anonymous socket aborted)")
        abort_silent(ws); return None
    except websockets.ConnectionClosed:
        log(f"handshake: anonymous connection from {peer} closed before hello"); return None
    try:
        msg = json.loads(raw)
        if not isinstance(msg, dict): raise ValueError("not an object")
    except Exception:
        log(f"handshake REJECTED from {peer}: malformed hello frame"); abort_silent(ws); return None
    if msg.get("type") != "hello":
        # SK-46 постельный приём: старые сборки расширения на реконнекте гонят hb
        # раньше hello (шторм 22.10: «first frame type='hb' != hello», 12 переходов
        # ext_online↔offline за 70 мин). Первый фрейм hb от УЖЕ СПАРЕННОГО ip
        # (успешный hello-handshake по токену в окне LEGACY_HELLO_WINDOW_S, память
        # переживает рестарт моста) трактуется как hello v1 — обратная совместимость
        # со старыми сборками, лог legacy_hello. Не-спаренный IP токено-фри прохода
        # не получает: отказ как раньше, но молчаливый (SK-41: RST → 1006).
        if msg.get("type") == "hb" and paired_recent(ip):
            lv = msg.get("v", 1)  # v missing = v1 client (decision №7, hello only)
            if not valid_v(lv):
                log(f"handshake REJECTED from {peer}: v={msg.get('v')!r} unsupported "
                    f"(bridge supports 1..{PROTOCOL_VERSION})")
                await safe_send(ws, {"v": PROTOCOL_VERSION, "type": "hello_ack",
                                     "ok": False, "err": "bad_version"})
                await ws.close(4002, "unsupported v"); return None
            log(f"handshake legacy_hello from {peer}: first frame hb accepted as hello v1 "
                f"(paired {int(time.time() - PAIRED_IPS[ip])}s ago)")
            paired_note(ip)
            RECONNECTS.append(time.time())
            await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "hello_ack", "ok": True}))
            return 1
        log(f"handshake REJECTED from {peer}: first frame type={msg.get('type')!r} != hello")
        abort_silent(ws); return None
    v = msg.get("v", 1)  # v missing = v1 client (decision №7, hello only)
    if not valid_v(v):
        log(f"handshake REJECTED from {peer}: v={msg.get('v')!r} unsupported (bridge supports 1..{PROTOCOL_VERSION})")
        await safe_send(ws, {"v": PROTOCOL_VERSION, "type": "hello_ack", "ok": False, "err": "bad_version"})
        await ws.close(4002, "unsupported v"); return None
    tok = msg.get("token")
    if not isinstance(tok, str) or not hmac.compare_digest(tok.encode(), TOKEN.encode()):
        note_token_fail(ip)  # SK-28: считаем ШУМ по IP, не только «правильно ли токен»
        log(f"handshake REJECTED from {peer}: bad or missing token "
            f"({len(token_fails_pruned(ip))}/{TOKEN_FAIL_LIMIT} в окне {TOKEN_FAIL_WINDOW_S:.0f}s)")
        await safe_send(ws, {"v": PROTOCOL_VERSION, "type": "hello_ack", "ok": False, "err": "bad_token"})
        await ws.close(4003, "bad token"); return None
    TOKEN_FAILS.pop(ip, None)  # SK-28: доказанный токен = не брутфорсер, шум снимаем
    paired_note(ip)            # SK-46: доказанный токен = IP спарен (legacy_hello окно)
    RECONNECTS.append(time.time())  # SK-46: флэппинг-метрика (reconnects_per_hour)
    log(f"handshake OK from {peer} (v={v})")
    await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "hello_ack", "ok": True}))
    return v

async def pump_outbox():
    while True:
        try:
            if os.path.exists(OUTBOX):  # SK-09: drains even with no clients — task frames
                lines = open(OUTBOX).read().splitlines()  # keep TASK_STATE fresh, late clients
                if lines:               # get the CURRENT truth replayed, not a stale backlog
                    open(OUTBOX, "w").close()
                    n = 0
                    for ln in lines:
                        if not ln.strip(): continue
                        if note_frame(ln) == "drop":  # SK-09: agent-authored state frames die here
                            log("outbox {type:state} frame dropped — the bridge owns the state vocabulary")
                            continue
                        n += 1
                        for c in list(CLIENTS):
                            try: await c.send(ln)
                            except Exception: CLIENTS.discard(c)
                    if n: log(f"sent {n} msg(s) from outbox")
                    push_state()  # SK-09: task/step frames may have moved the state
        except Exception as e: log(f"outbox err: {e}")
        await asyncio.sleep(0.15)  # SK-09: 0.4 -> 0.15 — task frames stay inside the 500 ms budget

async def pump_state():
    """SK-09 safety net: event hooks cover every bridge-side transition; the tick exists
    for the one EXTERNAL transition — the agent removing hub/stop itself on resume."""
    while True:
        try: push_state()
        except Exception as e: log(f"state tick err: {e}")
        await asyncio.sleep(0.25)

# ---- RPC bus (SK-03): rpc_in.jsonl -> WS "rpc" frame -> "rpc_result" -> rpc_out.jsonl ----

# SK-11: "value" joined the family — the card pins «поле value -> ***»; the audit
# vocabulary has no legitimate "value" field, so anything wearing that key is a secret.
# The value half matches a full JSON string (escapes included) and the replacement
# RESTORES the closing quote — a masked line stays valid JSON, so the feed can still
# render it as a row (with value: ***) instead of falling back to raw text.
SECRET_RE = re.compile(r'(token|password|secret|cvv|cvc|value)("\s*:\s*)"(?:[^"\\]|\\.)*"', re.I)

def mask_secrets(line):
    line = SECRET_RE.sub(r'\1\2"***"', line)
    if TOKEN and TOKEN in line:  # the literal session token anywhere (not only "token": "...")
        line = line.replace(TOKEN, "***")
    return line

def audit(obj):
    """Audit lines are the only place we mask — page.text results stay verbatim."""
    try:
        rotate_if_big(AUDIT)  # SK-33: порог по записи, читатель по пути всегда в норме
        with open(AUDIT, "a") as f:
            f.write(mask_secrets(json.dumps(obj, ensure_ascii=False)) + "\n")
    except Exception as e: log(f"audit err: {e}")

# ---- SK-33: ротация по записи + health.json + events с ts ------------------------

def rotate_if_big(path):
    """SK-33: rename-ротация порога (5 МБ по умолчанию), хранить ROTATE_KEEP бэкапов.
    Вызывается ПЕРЕД append: вердикт/строка всегда долетают до свежего файла,
    перелистывание — переименование, не копирование. Мост — единственный писатель
    этих файлов (asyncio, пишущие секции синхронны), гонки переименования нет."""
    try:
        if os.path.getsize(path) < ROTATE_MAX:
            return
    except FileNotFoundError:
        return
    try:
        for i in range(ROTATE_KEEP - 1, 0, -1):  # .1 -> .2 (старый .2 умирает), current -> .1
            if os.path.exists(f"{path}.{i}"):
                os.replace(f"{path}.{i}", f"{path}.{i + 1}")
        os.replace(path, f"{path}.1")
        log(f"SK-33: rotate {os.path.basename(path)} -> .1 (>= {ROTATE_MAX // (1024 * 1024)} MB)")
    except OSError as e:
        log(f"SK-33: rotate {os.path.basename(path)} err: {e}")

def health_write():
    """SK-33: hub/health.json — мост пишет, читатели (agentd SK-28, sk status) только
    читают. Атомарно (tmp+replace): получатель никогда не съест пол-файла. ts — часы
    читателя к свежести не применяются, это метка записи."""
    try:
        # SK-49: peer активного расширения — «агент бьёт не туда» читается отсюда
        # за минуту, до чтения bridge.log (ws_count ≤ 1 по политике соединения).
        peer = CLIENT_PEER.get(next(iter(CLIENTS))) if CLIENTS else None
        # SK-46: флэппинг — числом, а не грепом bridge.log: шторм 22.10 это
        # «12 переходов за 70 мин», глазами — «что-то мигает». Порог приёмки <2/ч.
        now = time.time()
        while RECONNECTS and now - RECONNECTS[0] > RECONNECT_WINDOW_S:
            RECONNECTS.popleft()
        rec = {"bridge": "active",
               "extension": "connected" if CLIENTS else "offline",
               "peer": peer,
               "reconnects_per_hour": len(RECONNECTS),
               "ts": int(time.time()), "ws_count": len(CLIENTS)}
        tmp = HEALTH + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(rec) + "\n")
        os.replace(tmp, HEALTH)
    except Exception as e:
        log(f"SK-33: health write err: {e}")

async def pump_health():
    while True:
        try: health_write()
        except Exception as e: log(f"health tick err: {e}")
        await asyncio.sleep(HEALTH_TICK_S)

def event_log(raw):
    """SK-33: append WS-события (stop/resume/perm_answer) в events.jsonl с ts: рамку
    без ts мост штампует (sk events --since фильтрует по ts; agentd читает только
    "type" — поле аддитивно). Ротация — как у audit."""
    line = raw if raw.endswith("\n") else raw + "\n"
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "ts" not in obj:
            obj["ts"] = int(time.time())
            line = json.dumps(obj, ensure_ascii=False) + "\n"
    except Exception:
        pass  # некадровая строка — пишем как есть, читатель её честно пропустит
    try:
        rotate_if_big(EVENTS)
        with open(EVENTS, "a") as f:
            f.write(line)
    except Exception as e:
        log(f"SK-33: events write err: {e}")

# ---- SK-46: чистый/грязный выход процесса ----------------------------------------

def mark_clean_exit():
    """Марк пишется ПЕРВЫМ делом по SIGTERM/SIGINT (до закрытия сервера): даже
    если systemd добьёт по таймауту, выход уже засчитан чистым — человек/панель
    остановили мост намеренно, алертить не о чем."""
    try:
        tmp = CLEAN_MARK + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(int(time.time())))
        os.replace(tmp, CLEAN_MARK)
    except OSError as e:
        log(f"SK-46: clean mark write err: {e}")

def check_unclean_exit():
    """Старт: маркера нет, а хаб не первый ран — прошлый процесс умер молча
    (SIGKILL — как два харнесс-убийства 22.10, — crash-loop, reboot). Событие в
    events.jsonl; агентд превратит его в алерт чату при следующем сообщении."""
    if os.path.exists(CLEAN_MARK):
        try:
            os.remove(CLEAN_MARK)
        except OSError:
            pass
        return
    if not _HAD_STATE:
        return  # первый ран хаба: «грязного выхода» не было — не о чем сообщать
    event_log(json.dumps({
        "type": "bridge_unclean_exit",
        "note": "прошлый ран моста не оставил clean-exit mark (SIGKILL/крах/reboot); "
                "systemd поднимет мост restart'ом"}))
    log("SK-46: previous exit was UNCLEAN — event bridge_unclean_exit written")

# ---- SK-11: the audit feed's read side (rpc audit.tail + WS audit_get share this) ----

AUDIT_TAIL_MAX = 2000  # a sidebar poll must never drag megabytes over the WS

def audit_tail(limit=50, domain=""):
    """(lines, total, truncated) — the tail of audit.jsonl, masked on READ (defense
    in depth: hand-appended or old-bridge lines still may not leak into the feed).
    The domain filter runs over the WHOLE file before tailing, so matches survive a
    busy audit trail; total is always the file's line count (completeness counter)."""
    try:
        raw = open(AUDIT, encoding="utf-8", errors="replace").read().splitlines()
    except FileNotFoundError:
        return [], 0, False
    total = len(raw)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, AUDIT_TAIL_MAX))
    d = str(domain or "").strip().lower()
    if d:
        raw = [ln for ln in raw if d in ln.lower()]
    truncated = len(raw) > limit
    return [mask_secrets(ln) for ln in raw[-limit:]], total, truncated

# ---- SK-23: chat history (chats.jsonl mirror + chats.list/chats.get) ----------

def clip_title(s, n=CHATS_TITLE_MAX):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"

def chats_log(chat_id, role, text):
    """Append one conversation line to chats.jsonl (append-only; the spec record
    {chat_id,title,ts,msgs} is assembled on read). WS side = the human, outbox
    side = the agent; anything else normalizes to "user". Empty text never logs."""
    text = str(text or "")[:CHATS_TEXT_MAX]
    if not text.strip():
        return
    rec = {"chat_id": str(chat_id or "default")[:CHATS_ID_MAX],
           "role": "agent" if str(role or "") == "agent" else "user",
           "text": text, "ts": int(time.time())}
    try:
        with open(CHATS, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e: log(f"chats err: {e}")

def chats_records():
    """Aggregate chats.jsonl into {id,title,ts,msgs:[{role,text,ts}]} records in
    first-seen order. Malformed/short lines are skipped (crash tail), never fatal."""
    try:
        raw = open(CHATS, encoding="utf-8", errors="replace").read().splitlines()
    except FileNotFoundError:
        return []
    order, recs = [], {}
    for ln in raw:
        if not ln.strip(): continue
        try: f = json.loads(ln)
        except Exception: continue
        if not isinstance(f, dict): continue
        cid = str(f.get("chat_id") or "default")[:CHATS_ID_MAX]
        r = recs.get(cid)
        if r is None:
            r = recs[cid] = {"id": cid, "title": "", "ts": None, "msgs": []}
            order.append(cid)
        msg = {"role": "agent" if f.get("role") == "agent" else "user",
               "text": str(f.get("text") or ""), "ts": f.get("ts")}
        r["msgs"].append(msg)
        if r["ts"] is None and isinstance(f.get("ts"), (int, float)):
            r["ts"] = f["ts"]
        if not r["title"] and msg["role"] == "user" and msg["text"].strip():
            r["title"] = clip_title(msg["text"])
    for r in recs.values():  # an agent-only chat still needs a title (honest fallback)
        if not r["title"] and r["msgs"]:
            r["title"] = clip_title(r["msgs"][0]["text"])
    return [recs[c] for c in order]

def chats_list():
    """The newest CHATS_LIST_MAX chats, ts desc (stable: ties keep first-seen order).
    Entry = {id,title,ts,last} — last is the last utterance {role,text,ts}."""
    recs = chats_records()
    recs.sort(key=lambda r: r["ts"] or 0, reverse=True)
    return [{"id": r["id"], "title": r["title"], "ts": r["ts"],
             "last": r["msgs"][-1] if r["msgs"] else None} for r in recs[:CHATS_LIST_MAX]]

def chats_get(chat_id):
    """The spec record for one chat; unknown chat = empty msgs (not an error — the
    sidebar opens a fresh/absent chat without a special case)."""
    for r in chats_records():
        if r["id"] == chat_id:
            return {"chat_id": r["id"], "title": r["title"], "ts": r["ts"], "msgs": r["msgs"]}
    return {"chat_id": chat_id, "title": "", "ts": None, "msgs": []}

# ---- SK-04: grants (bridge-side, 0600) + perm.check --------------------------

def norm_host(s):
    """'https://WWW.Example.com:8899/x' | 'Example.com' | '127.0.0.1:8899' -> lowercase host."""
    s = (s or "").strip().lower()
    if not s: return ""
    if "://" not in s: s = "//" + s.lstrip("/")
    try: return (urlsplit(s).hostname or "").rstrip(".")
    except ValueError: return ""

def url_path(s):
    if not s or "://" not in s and not s.startswith("/"): return ""
    try: return urlsplit(s if "://" in s else "//_/" + s.lstrip("/")).path
    except ValueError: return ""

def domain_matches(host, key):
    """Exact host, or subdomain of a dotted key ('example.com' matches 'www.example.com').
    Single-label keys never suffix-match — 'com' must not blanket the whole TLD."""
    if not host or not key: return False
    if host == key: return True
    return "." in key and host.endswith("." + key)

def grants_load():
    try:
        g = json.loads(open(GRANTS_FILE).read())
        return g if isinstance(g, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"grants.json unreadable ({e}) — treating as empty"); return {}

def grants_save(g):
    tmp = GRANTS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(g, ensure_ascii=False, indent=1) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, GRANTS_FILE)  # atomic: readers never see a half-written file

def level_for(host):
    """Longest matching grant key wins; None = no entry (Nanobrowser default is Ask)."""
    best = None
    grants = grants_load()
    for key, rec in grants.items():
        if isinstance(rec, dict) and rec.get("level") in ("allow", "ask", "block") \
           and domain_matches(host, key) and (best is None or len(key) > len(best)):
            best = key
    return grants[best]["level"] if best else None

def perm_decide(host, action, url=""):
    """(decision, via, sensitive) — decision 'ask' means: go through the human,
    whatever the grant says. `sensitive` (SK-34 п.1) marks asks that are caused by
    the always-human classes (action/bank/auth-path), not by a missing grant:
    on sensitive paths the «всегда» button must never appear and an «always»
    answer must not write a grant."""
    level = level_for(host)
    if level == "block": return "block", "grant_block", False
    why = []
    if action in ALWAYS_HUMAN_ACTIONS: why.append(f"action:{action}")
    if any(domain_matches(host, b) for b in BANK_DOMAINS): why.append("bank_domain")
    path = url_path(url)
    if path and OAUTH_PATH_RE.search(path): why.append("auth_or_payment_path")
    if why: return "ask", "+".join(why), True
    if level == "allow": return "allow", "grant_hit", False  # SK-34: via renamed (grant -> grant_hit)
    return "ask", "level_ask" if level == "ask" else "no_grant", False

# ---- SK-34 п.1: once-hits — «человек уже жаловал разово на (домен, тип)» ---------

def once_key(domain, action):
    return f"{domain}|{action}"

def once_load():
    try:
        d = json.loads(open(PERM_ONCE_FILE).read())
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"perm_once.json unreadable ({e}) — treating as empty"); return {}

def once_record(domain, action):
    """Человек ответил «разово» — запоминаем (домен, тип): следующий такой же вопрос
    получит show_always. Ничего секретного в файле нет, но манера та же, что у grants."""
    if not domain:
        return
    d = once_load()
    d[once_key(domain, action)] = {"ts": int(time.time())}
    if len(d) > PERM_ONCE_MAX:  # evict oldest first
        for k in sorted(d, key=lambda k: d[k].get("ts", 0))[:len(d) - PERM_ONCE_MAX]:
            d.pop(k, None)
    tmp = PERM_ONCE_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, PERM_ONCE_FILE)  # atomic, как grants_save

def once_seen(domain, action):
    return once_key(domain, action) in once_load()

def perm_frame(rid, info, show_always=False):
    t = TASK_TAB.get(str(info.get("task") or ""))
    return {"v": 1, "type": "perm_ask", "id": rid, "domain": info["domain"],
            "action": info["action"], "reason": info.get("reason", ""),
            **({"url": info["url"]} if info.get("url") else {}),
            **({"hint": info["hint"]} if info.get("hint") else {}),
            # SK-34 п.1: один тап до «всегда» — сайдбар рисует кнопку
            # «Разрешить для <domain> всегда» только когда флаг взведён.
            **({"show_always": True} if show_always else {}),
            # SK-37 (аддитивно): имя цели — человек видит ПРЕДМЕТ, а не только
            # домен; tab — вкладка задачи (из неё мост снимает миниатюру); shot —
            # уже снятая миниатюра (re-push после реконнекта — карточка сразу с
            # картинкой). Старый сайдбар незнакомые поля молча игнорирует.
            **({"name": info["name"]} if info.get("name") else {}),
            **({"tab": t["tab"]} if t and not t.get("dead")
               and isinstance(t.get("tab"), int) else {}),
            **({"shot": info["shot"]} if info.get("shot") else {})}

def audit_perm(info, decision, via, deny_reason=""):
    audit({"ts": int(time.time()), "ev": "perm", "domain": info["domain"],
           "action": info["action"], "decision": decision, "via": via,
           "reason": info.get("reason"),
           **({"deny_reason": deny_reason} if deny_reason else {})})

def resolve_perm(rid, decision, via, ok=True, err=None, deny_reason=""):
    info = PERM_ASKS.pop(rid, None)
    perm_thumb_cancel(rid)  # SK-37: ask закрыт — нога миниатюры больше не нужна
    entry = PENDING.pop(rid, None)
    if entry: entry[0].cancel()
    chain = TABS_CLOSE_CHAIN.pop(rid, None)
    if chain:
        # SK-32: это гейт tabs.close — вердикт НЕ ответ rpc (им будет результат
        # закрытия). allow -> закрытие уходит на rid агента; deny/ask_timeout ->
        # успешный ответ {denied:true} (лексика SK-05: человеческое «нет» — не ошибка).
        audit_perm(info or {"domain": TABS_CLOSE_DOMAIN, "action": "close_tabs"},
                   decision, via, deny_reason)
        if decision == "allow":
            PENDING[rid] = (asyncio.create_task(rpc_timeout(rid, "tabs.close")), "tabs.close")
            asyncio.create_task(safe_send(chain["client"], {"v": 1, "type": "rpc", "id": rid,
                                                            "method": "tabs.close",
                                                            "params": chain["params"]}))
            log(f"tabs.close ({str(rid)[:8]}): человек разрешил ({via}) -> закрываем "
                f"{len(chain['params']['tabIds'])} tab(s)")
        else:
            rpc_respond(rid, True, {"closed": [], "denied": True,
                                    "reason": "user_denied" if via == "human_deny" else str(via)[:40]},
                        method="tabs.close")
            log(f"tabs.close ({str(rid)[:8]}): гейт сказал НЕТ ({via}) — ничего не закрыто")
        push_state()
        return
    if not ok:
        rpc_respond(rid, False, err=err, method="perm.check")
    else:
        rpc_respond(rid, True, {"decision": decision, "via": via}, method="perm.check")
    audit_perm(info or {"domain": "?", "action": "?"}, decision, via, deny_reason)
    push_state()  # SK-09: the human answered — needs_you lifts (if no other ask/handoff is open)

async def perm_timeout(rid):
    try:
        await asyncio.sleep(PERM_TIMEOUT_S)
    except asyncio.CancelledError:
        return
    if PENDING.get(rid) is not None:
        info = PERM_ASKS.get(rid) or {}
        # SK-34 п.3: считаем подряд идущие таймауты одного и того же пермиса.
        # Совершенно неважно, почему агент переспрашивает — важно, что человек
        # каждый раз молчит; третий такой же вопрос его только бесит.
        key = (str(info.get("task") or ""), str(info.get("domain") or ""),
               str(info.get("action") or ""))
        streak = TIMEOUT_STREAK.get(key, 0) + 1
        TIMEOUT_STREAK[key] = streak
        resolve_perm(rid, "block", "ask_timeout")
        # grey out the dead prompt in every UI, drop it from the extension's replay list
        for c in list(CLIENTS):
            await safe_send(c, {"v": 1, "type": "perm_resolved", "id": rid, "choice": "timeout"})
        # событие для агентда (пауза задачи с явным вопросом при streak>=2);
        # event_log штампует ts и гоняет через ротацию, как остальные события
        event_log(json.dumps({"v": 1, "type": "perm_timeout", "id": rid,
                              "domain": key[1], "action": key[2], "task": key[0],
                              "streak": streak}, ensure_ascii=False))
        log(f"perm.check ({str(rid)[:8]}): no human answer in {PERM_TIMEOUT_S:.0f}s -> block "
            f"(streak {streak} на {key[1] or '?'}/{key[2]})")

# ---- SK-37: миниатюра кадра в карточке пермиса -----------------------------------------
# Съёмка — асинхронная нога через существующий page.screenshot (max_width 320, на
# стороне расширения JPEG q80 — SK-47). Карточку не ждём и не тормозим: не удалось —
# карточка без картинки. Вердикты/аудит/таймауты от этой ноги НЕ зависят; в rpc_out
# агента её строки не выходят (тот же паттерн, что HANDOFF_VERIFY у SK-48).
PERM_THUMB = {}            # internal leg rid («{ask}~thumb») -> {"rid": ask, "to": timer}
PERM_THUMB_W = 320         # ширина миниатюры, px
PERM_THUMB_MAX = 400_000   # потолок dataUrl, байт (JPEG q80 @320w ~30-60 КБ; мусор отсекаем)
PERM_THUMB_TIMEOUT_S = 10

async def perm_thumb_timeout(sid):
    try:
        await asyncio.sleep(PERM_THUMB_TIMEOUT_S)
    except asyncio.CancelledError:
        return
    PERM_THUMB.pop(sid, None)  # кадр не дошёл — карточка остаётся без картинки

async def perm_thumb(rid, info):
    t = TASK_TAB.get(str(info.get("task") or ""))
    if not t or t.get("dead") or not isinstance(t.get("tab"), int) or not CLIENTS:
        return  # нет вкладки задачи — нечего снимать (в активную бить нельзя, п.9)
    sid = f"{rid}~thumb"
    PERM_THUMB[sid] = {"rid": rid, "to": asyncio.create_task(perm_thumb_timeout(sid))}
    if rid not in PERM_ASKS:  # гонка: человек ответил раньше, чем нога стартовала
        perm_thumb_cancel(rid)
        return
    await safe_send(next(iter(CLIENTS)), {"v": 1, "type": "rpc", "id": sid,
                                          "method": "page.screenshot",
                                          "params": {"tab": t["tab"], "max_width": PERM_THUMB_W}})

def perm_thumb_result(rid, msg):
    """Внутренняя лег миниатюры: вердикт человеку не агенту — только perm_shot в
    клиенты (и shot в PERM_ASKS для re-push'а после реконнекта)."""
    ent = PERM_THUMB.pop(rid, None)
    if not ent:
        return
    ent["to"].cancel()
    res = msg.get("result") if msg.get("ok") else None
    if not isinstance(res, dict):
        return
    shot = res.get("dataUrl")
    if not isinstance(shot, str) or not shot.startswith("data:") or len(shot) > PERM_THUMB_MAX:
        return
    info = PERM_ASKS.get(ent["rid"])
    if info is None:
        return  # ask уже закрыт (вердикт/таймаут) — догонять карточку нечем
    info["shot"] = shot
    for c in list(CLIENTS):
        asyncio.create_task(safe_send(c, {"v": 1, "type": "perm_shot", "id": ent["rid"], "shot": shot}))

def perm_thumb_cancel(rid):
    ent = PERM_THUMB.pop(f"{rid}~thumb", None)
    if ent:
        ent["to"].cancel()

def handle_grants_set(rid, params):
    host, level = norm_host(params.get("domain")), params.get("level")
    if not host or level not in ("allow", "ask", "block"):
        rpc_respond(rid, False, err="bad_request", method="grants.set"); return
    g = grants_load()
    g[host] = {"level": level, "ts": int(time.time())}
    grants_save(g)
    audit({"ts": int(time.time()), "ev": "grants.set", "domain": host, "level": level})
    rpc_respond(rid, True, {"ok": True, "domain": host, "level": level}, method="grants.set")
    log(f"grants.set {host} -> {level}")

def perm_semantics_validate(op, perm):
    """(err, hint) — SK-43 п.1: action обязан соответствовать действию. Обычный
    click/rightclick/dblclick/hover/move/key/scroll НЕ может быть sensitive_input/
    send_message/send_email: текст наружу несут только input/submit, в поле пароля
    печатают через input. «Надёжный» пермис на обычном клике (инцидент B) — это
    пугающая карточка человеку вместо работы, поэтому бессмысленная комбинация
    отклоняется ДО всякого вопроса человеку."""
    if op not in PERM_OPS:
        return "perm_action_mismatch", (
            "perm.check: action — глагол действия (click|rightclick|dblclick|hover|move|"
            "key|scroll|input|submit), perm — тип пермиса. Пример: "
            '{"action":"input","perm":"sensitive_input","domain":"…","task":"…"}. '
            "Обычному клику пермис не нужен вовсе — гейт уже внутри action.do "
            "(превью: кольцо+подпись, кнопок на курсоре нет — SK-37). "
            "Границы типов: sk capabilities")
    if op in PERM_OPS_NOISE and perm in PERM_TEXT_OUT:
        right = "input" if perm == "sensitive_input" else "input/submit"
        return "perm_action_mismatch", (
            f"'{op}' — обычное действие, оно не может быть '{perm}': sensitive_input "
            f"бывает только у input (поле пароля/карты), send_message/send_email — "
            f"только у отправки текста ({right}). Обычному клику perm.check не нужен: "
            'гейт уже внутри action.do; если нужен вопрос по домену — {"action":'
            '"click"} без perm. Карточка «поле пароля/карты» на обычный клик не '
            "показывается НИКОГДА. Границы типов: sk capabilities")
    return None, None

def handle_perm_check(rid, params, task=""):
    host = norm_host(params.get("domain") or params.get("url"))
    action = str(params.get("action") or "").strip().lower() or "action"
    # SK-43 п.1 (аддитивно к v1): perm — тип пермиса, action — глагол действия.
    # Без perm остаётся легаси-форма {action:"sensitive_input"} (матрица SK-34).
    perm = str(params.get("perm") or "").strip().lower()
    if perm:
        verr, vhint = perm_semantics_validate(action, perm)
        if verr:
            # отказа достаточно в rpc_out-аудите (rpc_respond пишет method/ok/err):
            # отдельной ev:perm строки нет — карточка человеку НЕ уходила.
            rpc_respond(rid, False, err=verr, hint=vhint, method="perm.check")
            log(f"perm.check ({str(rid)[:8]}): {action}+{perm} — семантический отказ "
                f"{verr}, человека не беспокою")
            return
        action = perm  # решение — по типу пермиса (card/audit/grants ключуются им)
    # SK-34 п.3: два таймаута подряд на одном пермисе — человек всё равно молчит,
    # третий вопрос его добьёт. Отказ без кадра, аудит тот же (ev:perm).
    key = (task, host, action)
    if TIMEOUT_STREAK.get(key, 0) >= 2:
        rpc_respond(rid, True, {"decision": "block", "via": "ask_timeout_streak"},
                    method="perm.check")
        audit({"ts": int(time.time()), "ev": "perm", "domain": host, "action": action,
               "decision": "block", "via": "ask_timeout_streak"})
        log(f"perm.check ({str(rid)[:8]}): {host or '?'}/{action} — streak таймаутов, "
            f"человека не беспокою")
        return
    decision, via, sensitive = perm_decide(host, action, params.get("url") or "")
    if decision != "ask":
        rpc_respond(rid, True, {"decision": decision, "via": via}, method="perm.check")
        audit({"ts": int(time.time()), "ev": "perm", "domain": host, "action": action,
               "decision": decision, "via": via, "reason": via})
        return
    if not CLIENTS:  # the human UI is the only answer channel — without it, deny loudly
        rpc_respond(rid, False, err="no_extension", method="perm.check")
        audit({"ts": int(time.time()), "ev": "perm", "domain": host, "action": action,
               "decision": "block", "via": "no_extension"})
        return
    info = {"domain": host, "action": action, "reason": via,
            "url": params.get("url") or "", "hint": str(params.get("hint") or "")[:200],
            "task": task}
    # SK-37 (аддитивно): name — имя цели для человека («Кнопка „Войти“», «поле
    # письма»); карточка показывает предмет, а не только домен.
    pname = str(params.get("name") or "").strip()[:120]
    if pname:
        info["name"] = pname
    PERM_ASKS[rid] = info
    PENDING[rid] = (asyncio.create_task(perm_timeout(rid)), "perm.check")
    # SK-34 п.1: «Разрешить для {domain} всегда» в один тап — кнопка появляется на
    # СЛЕДУЮЩЕМ запросе того же типа после первого «разово»; на чувствительном пути
    # (платежи/2FA/bank/текст наружу) — никогда.
    show_always = (not sensitive) and once_seen(host, action)
    asyncio.create_task(safe_send(next(iter(CLIENTS)), perm_frame(rid, info, show_always)))
    asyncio.create_task(perm_thumb(rid, info))  # SK-37: миниатюра — отдельной ногой
    push_state()  # SK-09: waiting on a human answer = needs_you
    log(f"perm_ask {host or '?'} / {action} ({rid[:8]}) — {via}"
        f"{' +кнопка «всегда»' if show_always else ''}, waiting ≤{PERM_TIMEOUT_S:.0f}s")


# ---- SK-43 п.2: capabilities — границы агент получает из моста, а не из grep ---------
# Инцидент B: агент вычитал ALWAYS_HUMAN-таблицу из исходников моста и на её основе
# выбрал неверный пермис. Мост отдаёт полный контракт сам: методы, глаголы action.do,
# типы пермисов с границами. Собирается из собственных констант — документ не может
# уехать от кода (drift-гард в tests/test_perm_semantics_sk43.py).
CAP_BRIDGE_LOCAL = {"audit.tail", "capabilities", "chats.get", "chats.list",
                    "grants.get", "grants.set", "handoff.request", "handoff.status",
                    "perm.check"}
CAP_FORWARDED = {"action.preview", "page.active", "page.dom", "page.media", "page.screenshot",
                 "page.text", "tabs.highlight_group", "tabs.list", "tabs.open_grouped",
                 "tabs.park_group", "tabs.use_tab"}
CAP_GATED = {"action.do": "гейт превью внутри моста (scroll — единственный без гейта); "
                          "цели: selector | sid | mark | {x,y}[+frame] | {name,role?} | "
                          "фокус (key/input без цели); selector+координаты — bad_request",
             "tabs.close": "одно разрешение человека на пачку; protected_tab — вкладка задачи",
             "tabs.activate": "цель — явный tabId или вкладка задачи, не активная вкладка"}
# Типы пермисов: ops — какими глаголами пермис вообще бывает; always_human — вопрос
# человеку даже при Allow-гранте; always_button — предлагается ли «всегда для сайта».
PERM_TYPE_DOC = {
    "sensitive_input": {"means": "ввод в поле пароля/карты/CVC (sensitive:true из page.dom)",
                        "ops": ["input", "submit"], "always_button": False},
    "payment": {"means": "платёж/checkout (банк-домены — тоже)",
                "ops": ["click", "input", "submit"], "always_button": False},
    "oauth": {"means": "OAuth/выдача доступа (auth-путь в url)",
              "ops": ["click", "input", "submit"], "always_button": False},
    "2fa": {"means": "код 2FA/MFA/OTP", "ops": ["input", "submit"], "always_button": False},
    "send_message": {"means": "отправка текста наружу: сообщение/комментарий/публикация",
                     "ops": ["input", "submit"], "always_button": False},
    "send_email": {"means": "отправка письма наружу", "ops": ["input", "submit"],
                   "always_button": False},
}
PERM_ROUTINE_DOC = {
    "click": {"means": "обычный клик — perm.check обычно НЕ нужен (гейт внутри action.do)",
              "ops": ["click", "rightclick", "dblclick"], "always_button": True},
    "input": {"means": "ввод в обычное поле", "ops": ["input"], "always_button": True},
    "submit": {"means": "отправка обычной формы", "ops": ["submit"], "always_button": True},
    "navigate": {"means": "переход", "ops": [], "always_button": True},
    "action": {"means": "прочее действие (дефолт, когда action не задан)",
               "ops": [], "always_button": True},
    "close_tabs": {"means": "закрытие вкладок (у tabs.close свой гейт, не perm.check)",
                   "ops": [], "always_button": True},
}

def capabilities_doc():
    types = []
    for p in sorted(ALWAYS_HUMAN_ACTIONS):
        d = PERM_TYPE_DOC.get(p) or {"means": "свой always-human тип (env SIDEKICK_ALWAYS_HUMAN)",
                                     "ops": [], "always_button": False}
        types.append({"perm": p, "always_human": True, **d})
    for p in sorted(PERM_ROUTINE_DOC):
        types.append({"perm": p, "always_human": p == "close_tabs", **PERM_ROUTINE_DOC[p]})
    return {
        "v": 1,
        "methods": {"bridge_local": sorted(CAP_BRIDGE_LOCAL),
                    "forwarded": sorted(CAP_FORWARDED),
                    "gated": CAP_GATED},
        "action_do": {"actions": sorted(ACTION_DO_ACTIONS),
                      "verbs_ru": {"click": "кликну", "input": "введу", "submit": "отправлю",
                                   "navigate": "перейду", "rightclick": "кликну ПКМ",
                                   "hover": "наведу", "dblclick": "двойной клик",
                                   "move": "двину", "key": "нажму", "scroll": "прокручу"},
                      "notes": ["scroll — единственный глагол без превью-гейта",
                                "name/label — русская человеческая цель + ожидаемое изменение, "
                                "не ASCII-ярлык элемента (SK-43)",
                                "hit:{tag,text} в ответе — куда реально попал; промах — свежий page.dom",
                                "post_state {before,after,changed} у клик-целей-тумблеров: "
                                "executed:true + changed:false = silent-noop — переснять/сменить "
                                "цель, не рапортовать (SK-48, п.14)"]},
        "handoff": {
            "expect": "handoff.request принимает expect {kind:'video_playing'} | "
                      "{kind:'dom', name, role?, checked?, near?} — ожидаемое состояние, "
                      "которым мост проверит «Готово, продолжай» человека дешёвым чтением "
                      "(page.media/page.dom); при недостиге баннер возвращается человеку с "
                      "note+кадром, пауза держится (SK-48, п.14)",
            "verdict": "resume-вердикт {resumed:true, verified:true|false[, verify_err]}; "
                       "verified:false — задача НЕ выполнена: повторный handoff.request или стоп",
            "page_media": "page.media {} — видео-состояние {count,videos:[{paused,"
                          "currentTime,readyState,duration,rect,…}]} без vision; src не "
                          "отдаётся (токены)"},
        "perms": {
            "form": "perm.check {domain|url, action:<глагол>, perm:<тип>, url?, task, hint?, name?} — task всегда; "
                    "name — человекочитаемое имя цели (в карточке: предмет + миниатюра кадра, SK-37)",
            "ops": sorted(PERM_OPS),
            "rule": "обычный click/key/scroll/hover/move пермиса не требуют (гейт уже внутри "
                    "action.do: превью — кольцо+подпись, кнопок на курсоре нет, вердикты в чате — SK-37); "
                    "отдельный perm.check — только чувствительные "
                    "типы. click/key/scroll/hover/move × sensitive_input/send_message/"
                    "send_email — err perm_action_mismatch (карточка человеку не уходит)",
            "mismatch_err": "perm_action_mismatch",
            "types": types,
        },
        "preview": {
            "label": "подпись по-русски: цель по-человечески + ожидаемое изменение; "
                     "ASCII-only подпись мост заменяет читаемой из DOM (aria-label/"
                     "visible text), label_src:'dom' в ev:preview",
            "stop": "кнопок на превью-оверлее нет (SK-37): кольцо+подпись, вердикты и ■ Stop — "
                    "в окне чата; legacy-вердикт cancelled:true/user_stop агент исполняет как "
                    "отмену (успешный ответ, не ошибка)"},
    }


# ---- SK-08: hand-off to the human (login/captcha/2FA/OAuth) ---------------------
# SK-48 (аудит №2 22.10, проблема 10): «Готово, продолжай» — это слово человека, а не
# измеренный факт: handoff «нажми Play», человек нажал «Готово», видео на паузе, а задача
# уехала «виртуально выполненной». Аддитивно к протоколу: handoff.request принимает
# expect {kind:"video_playing"|"dom",…}, и после resume мост ПРОВЕРЯЕТ ожидаемое состояние
# дешёвым чтением (page.media / page.dom — существующие каналы, внутренний лег, агент
# его не видит). Недостиг — баннер возвращается с note и кадром (page.screenshot),
# hub/paused держится, rpc агента остаётся заблокированным. Вердикт resume аддитивен:
# {resumed:true, verified:true|false[, verify_err]}; verified:false — задача НЕ выполнена.

def handoff_frame(rid, info):
    return {"v": 1, "type": "handoff", "id": rid, "what": info["what"],
            **({"task": info["task"]} if info["task"] else {}),
            **({"url": info["url"]} if info["url"] else {}),
            # SK-48: ре-пуш баннера после недостигнутой верификации — человек видит,
            # ПОЧЕМУ его зовут снова (note) и что было на экране в момент проверки (shot)
            **({"note": info["note"]} if info.get("note") else {}),
            **({"shot": info["shot"]} if info.get("shot") else {}),
            **({"attempt": info["attempt"]} if info.get("attempt") else {})}

def close_handoff(rid, ok, err=None, broadcast=None, extra=None):
    """Pop the waiter + answer the blocked rpc; optionally grey banners via broadcast.
    SK-48: extra — аддитивные поля успешного вердикта (verified/verify_err)."""
    HANDOFFS.pop(rid, None)
    entry = PENDING.pop(rid, None)
    if entry: entry[0].cancel()
    for vid in [v for v, e in HANDOFF_VERIFY.items() if e["rid"] == rid]:
        ent = HANDOFF_VERIFY.pop(vid)  # незакрытые проверочные леги этой руки — долой
        t = ent.get("to")
        if t: t.cancel()
    rpc_respond(rid, ok, ({"resumed": True, **(extra or {})} if ok else None),
                err=err, method="handoff.request")
    if broadcast:
        for c in list(CLIENTS):
            asyncio.create_task(safe_send(c, {"v": 1, "type": "handoff_resolved",
                                              "id": rid, "choice": broadcast}))
    push_state()  # SK-09: resume/timeout/supersede — recompute (a supersede re-push below
                  # lands needs_you again in the same ms; the chip never sees the blip order flip)

async def handoff_timeout(rid):
    try:
        await asyncio.sleep(HANDOFF_TIMEOUT_S)
    except asyncio.CancelledError:
        return
    if rid not in HANDOFFS:
        return
    try:
        os.remove(PAUSED)  # the agent got an error — it must re-request or stop, never screenshot on its own
    except FileNotFoundError:
        pass
    close_handoff(rid, False, err="handoff_timeout", broadcast="timeout")
    audit({"ts": int(time.time()), "ev": "handoff", "decision": "timeout", "id": str(rid)[:40]})
    log(f"handoff ({str(rid)[:8]}): no human in {HANDOFF_TIMEOUT_S:.0f}s -> unblocked, banner greyed")

def handoff_expect_validate(expect):
    """SK-48: (None, None) — форма верна, иначе ("bad_request", hint). expect — чем мост
    проверит «Готово» человека:
      {kind:"video_playing"} — на вкладке задачи играет видео (не paused, не ended, данные
        готовы; читается page.media);
      {kind:"dom", name, role?, checked?, near?} — в page.dom (срез elements) есть элемент,
        доступное имя которого содержит name (без регистра), с нужной ролью и checked."""
    if expect is None:
        return None, None
    if not isinstance(expect, dict):
        return "bad_request", ('expect — {"kind":"video_playing"} | {"kind":"dom",'
                               '"name":"Выбрать все","checked":true}; пример: {"kind":"video_playing"}')
    kind = expect.get("kind")
    if kind == "video_playing":
        return None, None
    if kind == "dom":
        name = expect.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > HANDOFF_EXPECT_NAME_MAX:
            return "bad_request", ('expect {kind:"dom"} требует name — непустую строку ≤120 '
                                   '(подстрока доступного имени); пример: {"kind":"dom","name":"Выбрать все","checked":true}')
        role = expect.get("role")
        if role is not None and (not isinstance(role, str) or not role.strip() or len(role) > 40):
            return "bad_request", 'expect.role — непустая строка ≤40 ("checkbox", "button", …)'
        checked = expect.get("checked")
        if checked is not None and checked is not True and checked is not False and checked != "mixed":
            return "bad_request", 'expect.checked — true | false | "mixed"'
        near = expect.get("near")
        if near is not None and (not isinstance(near, list) or len(near) != 4
                                 or not all(_num(v) and abs(v) <= 100000 for v in near)):
            return "bad_request", "expect.near — [x0,y0,x1,y1] координаты вьюпорта (4 числа)"
        return None, None
    return "bad_request", ('expect.kind — "video_playing" | "dom"; '
                           'пример: {"kind":"video_playing"}')

def handoff_expect_clean(expect):
    """Только известные ключи — мусор из expect дальше не едет."""
    if not expect:
        return None
    kind = expect["kind"] if "kind" in expect else expect.get("kind")
    out = {"kind": kind}
    for k in ("name", "role", "checked", "near"):
        if expect.get(k) is not None:
            out[k] = expect[k]
    return out

def handoff_expect_note(expect):
    """Человеческая строка в ре-пушащемся баннере: что проверялось и что не так."""
    if expect.get("kind") == "video_playing":
        return "проверка: видео всё ещё на паузе — нажми Play и затем «Готово, продолжай»"
    bits = [f"проверка: в странице нет элемента «{expect.get('name')}»"]
    if expect.get("role"): bits.append(f"роли {expect['role']}")
    if expect.get("checked") is not None: bits.append(f"в состоянии checked={expect['checked']}")
    bits.append("— доделай и нажми «Готово, продолжай»")
    return " ".join(bits)

def handoff_expect_reached(expect, res):
    """Дешёвая проверка ожидаемого состояния по ответу page.media / page.dom."""
    if not isinstance(res, dict):
        return False
    if expect.get("kind") == "video_playing":
        vp = res.get("vp") if isinstance(res.get("vp"), dict) else None
        for v in (res.get("videos") or []):
            # видимое видео в вьюпорте: превью-сетка/hover-автоплей за кадром и
            # карусель за пределами экрана — не «запущено»
            r = v.get("rect") if isinstance(v, dict) else None
            if not isinstance(r, dict) or (r.get("w") or 0) < 2 or (r.get("h") or 0) < 2:
                continue
            if isinstance(vp, dict):
                vw, vh = vp.get("w") or 0, vp.get("h") or 0
                if vw and (r.get("x") or 0) >= vw:
                    continue
                if vw and (r.get("x") or 0) + (r.get("w") or 0) <= 0:
                    continue
                if vh and (r.get("y") or 0) >= vh:
                    continue
                if vh and (r.get("y") or 0) + (r.get("h") or 0) <= 0:
                    continue
            if (isinstance(v, dict) and not v.get("paused") and not v.get("ended")
                    and (v.get("readyState") or 0) >= 2):
                return True
        return False
    want = str(expect.get("name") or "").strip().casefold()
    role = str(expect.get("role")).strip().lower() if expect.get("role") else None
    checked = expect.get("checked")
    els = list(res.get("elements") or []) + list((res.get("more") or {}).get("elements") or [])
    for e in els:
        if not isinstance(e, dict):
            continue
        if want and want not in str(e.get("name") or "").casefold():
            continue
        if role and str(e.get("role") or "").lower() != role:
            continue
        if checked is not None and e.get("checked") != checked:
            continue
        return True
    return False

def handle_handoff_request(rid, params, expect=None):
    what = str(params.get("what") or "").strip()[:300] \
        or "займи браузер: заверши вручную и нажми «Готово, продолжай»"
    task = str(params.get("task") or "")[:60]
    url = str(params.get("url") or "")[:300]
    if not CLIENTS:  # the banner is the only way the human learns of the pause — fail loudly
        rpc_respond(rid, False, err="no_extension", method="handoff.request")
        audit({"ts": int(time.time()), "ev": "handoff", "decision": "err", "via": "no_extension"})
        log(f"handoff.request ({rid[:8]}) REJECTED: no extension, flag untouched")
        return
    for oid in list(HANDOFFS):  # one active hand-off: a newer request supersedes (SK-05 vocabulary)
        close_handoff(oid, False, err="superseded", broadcast="superseded")
        audit({"ts": int(time.time()), "ev": "handoff", "decision": "superseded", "id": str(oid)[:40]})
    open(PAUSED, "w").close()  # touch: the bridge refuses page.*/preview while the human acts
    HANDOFFS[rid] = {"what": what, "task": task, "url": url, "ts": int(time.time()),
                     **({"expect": expect} if expect else {})}
    PENDING[rid] = (asyncio.create_task(handoff_timeout(rid)), "handoff.request")
    asyncio.create_task(safe_send(next(iter(CLIENTS)), handoff_frame(rid, HANDOFFS[rid])))
    push_state()  # SK-09: the human holds the browser = needs_you
    audit({"ts": int(time.time()), "ev": "handoff", "decision": "pause",
           **({"task": task} if task else {}), "what": what[:120],
           **({"expect": expect["kind"]} if expect else {})})
    log(f"HANDOFF {rid[:8]}: paused «{what[:60]}»"
        + (f" +expect:{expect['kind']}" if expect else "")
        + f" — waiting ≤{HANDOFF_TIMEOUT_S:.0f}s for «Готово, продолжай»")

# ---- SK-48: верификация «Готово» ---------------------------------------------------

def _verify_tab_params(info):
    """(params|None, err) — внутренний лег верификации бьёт по вкладке задачи (SK-25),
    tab_resolve инжектит tab; tab_gone честно всплывает как verify_err."""
    params = {}
    if info.get("task"):
        err, _t, tab = tab_resolve({"task": info["task"]})
        if err:
            return None, str(err).split(":")[0]
        if isinstance(tab, int):
            params["tab"] = tab
    return params, None

def start_handoff_verify(rid, info):
    """«Готово» нажал человек — мост читает ожидаемое состояние (внутренний rpc-лег на
    page.media/page.dom; в rpc_out агента этих строк нет). hub/paused до вердикта держится."""
    exp = info["expect"]
    params, err = _verify_tab_params(info)
    if err or params is None:
        finish_handoff_verify(rid, verified=False, verify_err=err or "verify_internal")
        return
    if exp["kind"] == "dom" and exp.get("near"):
        params["near"] = exp["near"]
    method = "page.dom" if exp["kind"] == "dom" else "page.media"
    attempt = int(info.get("attempt") or 0) + 1
    vid = f"{rid}~vf{attempt}"
    HANDOFF_VERIFY[vid] = {"rid": rid, "expect": exp, "attempt": attempt,
                           "to": asyncio.create_task(handoff_verify_timeout(vid))}
    asyncio.create_task(safe_send(next(iter(CLIENTS)), {"v": 1, "type": "rpc", "id": vid,
                                                        "method": method, "params": params}))
    log(f"HANDOFF {rid[:8]}: resume — верифицирую expect:{exp['kind']} (attempt {attempt})")

async def handoff_verify_timeout(vid, shot_leg=False):
    try:
        await asyncio.sleep(RPC_TIMEOUT_S + 2)
    except asyncio.CancelledError:
        return
    ent = HANDOFF_VERIFY.pop(vid, None)
    if not ent:
        return
    rid = ent["rid"]
    if shot_leg:  # кадра нет — баннер всё равно должен вернуться, просто без картинки
        if rid in HANDOFFS:
            repush_handoff(rid, HANDOFFS[rid])
        return
    if rid in HANDOFFS:
        finish_handoff_verify(rid, verified=False, verify_err="verify_timeout")

def handoff_verify_result(vid, msg):
    """Внутренний лег верификации/кадра ответил — вердикт формируется на мосту."""
    ent = HANDOFF_VERIFY.pop(vid, None)
    if not ent:
        return
    t = ent.get("to")
    if t: t.cancel()
    rid = ent["rid"]
    if rid not in HANDOFFS:  # timeout/supersede случились, пока ждали лег
        return
    info = HANDOFFS[rid]
    if ent.get("shot_leg"):
        res = msg.get("result") if msg.get("ok") and isinstance(msg.get("result"), dict) else {}
        if str(res.get("dataUrl") or "").startswith("data:image/"):
            info["shot"] = res["dataUrl"]
        repush_handoff(rid, info)
        return
    if not msg.get("ok"):
        finish_handoff_verify(rid, verified=False,
                              verify_err=str(msg.get("err") or "extension_error")[:60])
        return
    reached = handoff_expect_reached(ent["expect"], msg.get("result"))
    audit({"ts": int(time.time()), "ev": HANDOFF_VERIFY_AUDIT, "id": str(rid)[:40],
           "kind": ent["expect"]["kind"], "attempt": ent["attempt"],
           "decision": "reached" if reached else "not_reached"})
    if reached:
        finish_handoff_verify(rid, verified=True)
    elif ent["attempt"] <= HANDOFF_VERIFY_MAX_RETRY:
        handoff_verify_retry(rid, info, ent)
    else:
        finish_handoff_verify(rid, verified=False, verify_err="not_reached")

def handoff_verify_retry(rid, info, ent):
    """Недостиг: сначала кадр (page.screenshot, vision-дефолт 1280 — дёшево), потом ре-пуш
    баннера с note. Кадр не получился — баннер вернётся без картинки, но вернётся."""
    info["attempt"] = ent["attempt"]
    info["note"] = handoff_expect_note(ent["expect"])
    params, err = _verify_tab_params(info)
    if err:
        finish_handoff_verify(rid, verified=False, verify_err=err)
        return
    sid = f"{rid}~shot{ent['attempt']}"
    HANDOFF_VERIFY[sid] = {"rid": rid, "expect": ent["expect"], "attempt": ent["attempt"],
                           "shot_leg": True,
                           "to": asyncio.create_task(handoff_verify_timeout(sid, shot_leg=True))}
    asyncio.create_task(safe_send(next(iter(CLIENTS)), {"v": 1, "type": "rpc", "id": sid,
                                                        "method": "page.screenshot",
                                                        "params": params}))
    log(f"HANDOFF {rid[:8]}: expect не достигнут — беру кадр для повторного баннера")

def repush_handoff(rid, info):
    if not CLIENTS:
        finish_handoff_verify(rid, verified=False, verify_err="no_extension")
        return
    asyncio.create_task(safe_send(next(iter(CLIENTS)), handoff_frame(rid, info)))
    entry = PENDING.get(rid)  # переожидание человека: старый таймер руки пересобираем
    if entry and entry[1] == "handoff.request":
        entry[0].cancel()
        PENDING[rid] = (asyncio.create_task(handoff_timeout(rid)), "handoff.request")
    audit({"ts": int(time.time()), "ev": "handoff", "decision": "reverify",
           "id": str(rid)[:40], "attempt": info.get("attempt", 1)})
    log(f"HANDOFF {rid[:8]}: баннер вернулся с кадром и note (attempt {info.get('attempt')}) — пауза держится")

def finish_handoff_verify(rid, verified, verify_err=""):
    """Закрыть руку с честной отметкой проверки. verified:false — мост НЕ подтвердил цель
    (ошибка чтения/таймаут/недостиг после N повторов): агент обязан не считать задачу
    сделанной и решить дальше сам (повторный handoff.request или стоп)."""
    try:
        os.remove(PAUSED)
    except FileNotFoundError:
        pass
    extra = {"verified": verified}
    if verify_err:
        extra["verify_err"] = verify_err
    close_handoff(rid, True, extra=extra)
    log(f"HANDOFF {rid[:8]}: verified={verified}"
        + (f" ({verify_err})" if verify_err else "") + " -> hub/paused removed, agent unblocked")

def handle_resume(msg, raw):
    """«Готово, продолжай» from the sidebar: consumed (events, never inbox), flag down, agent unblocked.
    SK-48: у руки с expect пауза НЕ снимается, пока мост не проверит ожидаемое состояние."""
    event_log(raw)  # SK-33: ts-штамп, ротация при пороге
    hid = msg.get("id")
    verifying = (isinstance(hid, str) and hid in HANDOFFS
                 and bool(HANDOFFS[hid].get("expect")) and bool(CLIENTS))
    if not verifying:
        try:
            os.remove(PAUSED)  # idempotent: also clears a flag whose waiter already timed out
        except FileNotFoundError:
            pass
    rt = str(msg.get("task") or "")[:60]
    audit({"ts": int(time.time()), "ev": "resume",
           **({"id": str(hid)[:40]} if isinstance(hid, str) else {}),
           **({"task": rt} if rt else {})})
    if isinstance(hid, str) and hid in HANDOFFS:
        if verifying:
            start_handoff_verify(hid, HANDOFFS[hid])
            push_state()  # состояние не меняется (needs_you), но пересчитываем честно
            return
        exp = HANDOFFS[hid].get("expect")
        close_handoff(hid, True,
                      extra=({"verified": False, "verify_err": "no_extension"} if exp else None))
        log(f"RESUME {hid[:8]}: human done -> hub/paused removed, agent unblocked")
    else:
        log("RESUME: no pending handoff (timed out/stale banner) — flag cleared only")
    push_state()  # SK-09: the human let go — recompute (needs_you -> thinking/idle)


# ---- SK-09: derived agent state (the bridge is the single source of truth) --------

def agent_compute():
    """(state, reason) — priority needs_you > paused > acting > thinking > idle."""
    if HANDOFFS: return "needs_you", "handoff"
    if PERM_ASKS: return "needs_you", "perm_ask"
    if os.path.exists(STOP): return "paused", "stop"
    if TASK_STATE == "paused": return "paused", "task_paused"
    if any(m and BROWSER_RPC_RE.match(m) for _, m in PENDING.values()):
        return "acting", "browser_rpc"
    if TASK_STATE == "running": return "thinking", "task_running"
    if PENDING: return "thinking", "rpc_pending"
    return "idle", ""

def state_frame():
    state, reason = agent_compute()
    return {"v": 1, "type": "state", "state": state, "ts": int(time.time()),
            **({"reason": reason} if reason else {}),
            **({"task": TASK_ID} if TASK_ID else {})}

def push_state():
    """Broadcast on CHANGE only — the chip must not flicker on every rpc round-trip."""
    global AGENT_STATE
    frame = state_frame()
    if frame["state"] == AGENT_STATE:
        return
    AGENT_STATE = frame["state"]
    for c in list(CLIENTS):
        asyncio.create_task(safe_send(c, frame))
    audit({"ts": frame["ts"], "ev": "state", "state": frame["state"],
           **({"reason": frame["reason"]} if "reason" in frame else {}),
           **({"task": frame["task"]} if "task" in frame else {})})
    log(f"agent state -> {frame['state']}"
        + (f" ({frame.get('reason')})" if frame.get("reason") else ""))

def note_frame(raw):
    """SK-09: read task/step frames for the state derivation — never mutate the line.
    Returns "drop" for agent-authored state frames (the bridge owns that vocabulary)."""
    global TASK_STATE, TASK_ID
    try: f = json.loads(raw)
    except Exception: return
    if not isinstance(f, dict): return
    t = f.get("type")
    if t == "state":
        return "drop"
    if t == "task":
        if isinstance(f.get("id"), str) and f["id"] and f["id"] != TASK_ID:
            TASK_ID = f["id"][:60]; TASK_STATE = ""  # a new task starts stateless
        if f.get("state") in ("running", "paused", "done", "failed"):
            TASK_STATE = f["state"]
    elif t == "step":
        if isinstance(f.get("task"), str) and f["task"]: TASK_ID = f["task"][:60]
        if not TASK_ID: TASK_ID = "t"
        if not TASK_STATE: TASK_STATE = "running"  # steps without a task frame = implicit running
        if f.get("state") == "active" and TASK_STATE == "paused": TASK_STATE = "running"
    elif t == "chat":
        # SK-23: the agent side of the conversation (agentd stamps chat on every
        # frame; frames without it belong to "default" — п.4 compatibility). The
        # line still broadcasts verbatim — the mirror is a side effect, not a filter.
        chats_log(f.get("chat"), "agent", f.get("text"))


def rpc_respond(rid, ok, result=None, err=None, method=None, audit_extra=None, hint=None):
    out = {"v": 1, "id": rid, "ok": bool(ok)}
    if ok: out["result"] = result
    else: out["err"] = err or "error"
    # SK-42 п.4: hint — аддитивное поле отказа (контракт v1: старые клиенты его просто
    # не читают). Самодостаточная подсказка: форма ошибки + следующий шаг.
    if not ok and hint: out["hint"] = str(hint)[:600]
    try:
        rotate_if_big(RPC_OUT)  # SK-33: вердикт пишется уже в свежий файл после листания
        with open(RPC_OUT, "a") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
    except Exception as e: log(f"rpc_out err: {e}")
    if method:
        # SK-11: audit_extra = {"domain": ...} from the RESULT url (page.*/tabs.*) —
        # the feed's domain column/filter needs it; mask_secrets still runs on the line.
        audit({"ts": int(time.time()), "method": method, "ok": bool(ok),
               **({} if ok else {"err": out["err"]}), **(audit_extra or {})})

async def rpc_timeout(rid, method):
    try:
        await asyncio.sleep(RPC_TIMEOUT_S)
    except asyncio.CancelledError:
        return
    if PENDING.pop(rid, None) is not None:
        RPC_REQ.pop(rid, None)  # SK-25: заявка на биндинг/аудит протухла вместе с rpc
        rpc_respond(rid, False, err="timeout", method=method)
        log(f"rpc {method} ({str(rid)[:8]}): timeout after {RPC_TIMEOUT_S:.0f}s")

# ---- SK-17: action.do — preview gate on the bridge, execution in the extension ----
# The gate is the whole point: the raw method never reaches the extension. The chain
# is action.preview (internal leg id "<rid>~pv", its verdict never enters rpc_out)
# -> clean verdict -> the execution leg on the AGENT's own rpc id. Refusals answer
# ok:true with the SK-05 verdict shape + executed:false (the agent decides, SK-05).

def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

def _input_value_ok(params):
    """(ok, why) — SK-42: value остаётся именем v1, text — алиас (пример карточки п.3).
    Оба поля одновременно с РАЗНЫМИ значениями — неоднозначность: отказ, не приоритет."""
    v, t = params.get("value"), params.get("text")
    if v is not None:
        if not isinstance(v, str):
            return False, "value должно быть строкой"
        if t is not None and (not isinstance(t, str) or t != v):
            return False, "value и text — алиасы одного поля; пришли оба и разные — оставь один"
        return True, ""
    if t is not None:
        if not isinstance(t, str):
            return False, "text должно быть строкой"
        return True, ""
    return False, 'input требует value:"текст" (или text:"текст")'

# SK-42 п.4: отказ гейта (shown:false reason=…) самодостаточен — агент обязан понять
# следующий шаг, не читая исходники моста и не гугля словарь причин (аудит 22.10,
# инцидент A: 5 попыток not_editable + 132 события аудита, потому что «not_editable»
# не говорил НИЧЕГО). Хинты короткие и машинно-стабильные по ключу причины.
REASON_HINTS = {
    "not_editable": "цель не редактируема; попробуй ближайший input/textarea по page.dom "
                    "или клик по полю и input без цели (фокус); не помогло — handoff с причиной",
    "no_element": "элемента нет — пересними page.dom и возьми свежий sid/selector",
    "bad_selector": "селектор невалиден — возьми путь из page.dom",
    "mark_stale": "карта меток протухла (навигация/скролл/DOM) — пересними page.screenshot marks:true",
    "sid_stale": "элемент из page.dom умер или url сменился — пересними page.dom",
    "not_clickable": "под точкой фон (body/html) — возьми координату реального элемента",
    "ambiguous": "имя далилось — смотри candidates[].rect и бей координатой в центр",
    "cross_origin_frame": "кросс-доменный фрейм — синтетика туда не ходит: BiDi или handoff",
    "no_form": "у элемента нет form — отправляй кнопкой submit кликом",
}

def action_do_validate(params):
    """(None, None) = valid, else ("bad_request", hint). bad_request for anything the
    executor could not act on faithfully — decided LOCALLY, before the CLIENTS check, so
    a malformed call gets the same error whether or not a browser is online. SK-24: target
    modes — selector, coordinates, key, name; SK-26/27: mark/sid; SK-42: scroll
    (dy|to|sid|mark), input по координатам и input в focused-элемент. Неоднозначная цель
    отказывается, не приоритизируется. С SK-42 каждый отказ несёт hint с примером
    допустимой формы — аудит 22.10 поймал агента, грепавшего исходники моста ради формы."""
    action = str(params.get("action") or "").strip().lower()
    if action not in ACTION_DO_ACTIONS:
        return "bad_request", ('action должен быть одним из click|input|submit|rightclick|'
                               'hover|dblclick|move|key|scroll; пример: {"action":"click","selector":"#go"}')
    selector = params.get("selector")
    has_xy = params.get("x") is not None or params.get("y") is not None
    frame = params.get("frame")
    if frame is not None and (not isinstance(frame, str) or len(frame) > ACTION_DO_FRAME_MAX):
        return "bad_request", ('frame — строка-цепочка page.dom ("iframeSel > inner") ≤500 символов; '
                                               'пример: {"action":"click","selector":"#go","frame":"iframe#x > body"}')
    mark = params.get("mark")
    sid = params.get("sid")
    # SK-42 п.2: scroll — три режима, РОВНО ОДИН: dy (window.scrollBy), to (top/bottom),
    # цель sid|mark (scrollIntoView по центру вьюпорта). Чужие цели (selector/x-y/frame)
    # не едут. Гейт превью scroll НЕ проходит — это единственное исключение SK-17:
    # скролл ничего на странице не меняет (класс чтения, сосед page.*), а гейтинг
    # порождает превью-спам (инцидент B: 8 кадров, scrollY=0).
    if action == "scroll":
        if selector is not None or has_xy or frame is not None:
            return "bad_request", ('scroll принимает dy|to|sid|mark без selector/x/y/frame; '
                                   'пример: {"action":"scroll","dy":800}')
        dy, to = params.get("dy"), params.get("to")
        if [dy is not None, to is not None, mark is not None, sid is not None].count(True) != 1:
            return "bad_request", ('scroll требует РОВНО ОДНО из dy:800 | to:"top"|"bottom" | '
                                   'sid:"a1b2c3" | mark:3; пример: {"action":"scroll","dy":800}')
        if dy is not None and not _num(dy):
            return "bad_request", ('dy — конечное число пикселей (отрицательное крутит вверх); ''пример: {"action":"scroll","dy":-600"}')
        if to is not None and (not isinstance(to, str) or to.strip().lower() not in ("top", "bottom")):
            return "bad_request", ('to принимает только "top" или "bottom"; пример: {"action":"scroll","to":"bottom"}')
        if mark is not None and (isinstance(mark, bool) or not isinstance(mark, int)
                                 or not 1 <= mark <= ACTION_DO_MARK_MAX):
            return "bad_request", 'mark — целое 1..64 с последнего скрина marks:true; пример: {"action":"scroll","mark":3}'
        if sid is not None and (not isinstance(sid, str) or not ACTION_DO_SID_RE.match(sid)):
            return "bad_request", 'sid — строка [a-z0-9]{4,12} из последнего page.dom; пример: {"action":"scroll","sid":"a1b2c3"}'
        return None, None
    # SK-26: mark — цель «номер с последнего скрина marks:true». Карта живёт в content
    # (расширение резолвит и сверяет url/rect: скролл/навигация -> mark_stale уже в легах).
    # Мост валидирует ФОРМУ и взаимоисключение целей: mark — ОДНА цель, как selector/x-y;
    # name рядом остаётся человеческой подписью превью (SK-17 семантика).
    if mark is not None:
        if isinstance(mark, bool) or not isinstance(mark, int) or not 1 <= mark <= ACTION_DO_MARK_MAX:
            return "bad_request", "mark — целое 1..64 с последнего скрина marks:true; bool — не номер"
        if action not in ACTION_DO_MARK:
            return "bad_request", ('mark — цель; у key/move цели нет (key бьёт по фокусу, move — по координатам); '
                                     'пример: {"action":"click","mark":3}')
        if selector is not None or has_xy or frame is not None or sid is not None:
            return "bad_request", ('mark — единственная цель: selector/x-y/frame/sid рядом запрещены; '
                                     'пример: {"action":"click","mark":3}')
        if action == "input" and not _input_value_ok(params)[0]:
            return "bad_request", _input_value_ok(params)[1] + '; пример: {"action":"input","mark":3,"value":"тест"}'
        return None, None
    # SK-27: sid — цель «элемент из последнего page.dom» (реестр живёт в content вкладки;
    # умерший элемент/смена url -> sid_stale уже в легах, обеими резолверами цепи).
    # Мост валидирует ФОРМУ и взаимоисключение: sid — ОДНА цель, как mark/selector/x-y;
    # name рядом — человеческая подпись превью (SK-17 семантика, как у mark).
    if sid is not None:
        if not isinstance(sid, str) or not ACTION_DO_SID_RE.match(sid):
            return "bad_request", "sid — строка [a-z0-9]{4,12} из последнего page.dom"
        if action not in ACTION_DO_MARK:
            return "bad_request", 'sid — цель; у key/move цели нет; пример: {"action":"click","sid":"a1b2c3"}'
        if selector is not None or has_xy or frame is not None or mark is not None:
            return "bad_request", ('sid — единственная цель: selector/x-y/frame/mark рядом запрещены; '
                                     'пример: {"action":"click","sid":"a1b2c3"}')
        if action == "input" and not _input_value_ok(params)[0]:
            return "bad_request", _input_value_ok(params)[1] + '; пример: {"action":"input","sid":"a1b2c3","value":"тест"}'
        return None, None
    if action == "key":  # SK-24 п.2: the focus IS the target — no other target may be claimed
        if selector is not None or has_xy or frame is not None:
            return "bad_request", ('key бьёт по фокусу: selector/x/y рядом запрещены (фокус и есть цель); '
                                     'пример: {"action":"key","key":"Enter"}')
        key = params.get("key")
        if not isinstance(key, str) or not key.strip() or len(key) > 40:
            return "bad_request", 'key требует key:"Enter" (1..40 символов); пример: {"action":"key","key":"Enter"}'
        mods = params.get("modifiers")
        if mods is not None:
            if not isinstance(mods, list) or not all(
                    isinstance(s, str) and s.strip().lower().removesuffix("key") in ACTION_DO_MODIFIERS
                    for s in mods):
                return "bad_request", ('modifiers — список из ctrl|shift|alt|meta (суффикс Key необязателен); '
                                       'пример: {"action":"key","key":"a","modifiers":["ctrl"]}')
        return None, None
    # SK-42 п.1: input без цели — вставка в focused-элемент (тот же принцип, что у key:
    # фокус и есть цель). Револьвер not_editable с подсказкой — на стороне расширения.
    if action == "input" and selector is None and not has_xy:
        if frame is not None:
            return "bad_request", ('input без цели бьёт по фокусу топ-документа: frame рядом запрещён; ''пример: {"action":"input","value":"тест"}')
        ok, why = _input_value_ok(params)
        if not ok:
            return "bad_request", why + '; пример: {"action":"input","value":"тест"}'
        return None, None
    # SK-24 аддендум (в): name/role — резолвер на стороне расширения. Целевой режим
    # ТОЛЬКО когда других целей не заявлено (selector/x/y); с ними name остаётся
    # человеческой подписью (SK-17). Роль без имени — нечего резолвить.
    if selector is None and not has_xy and action in ACTION_DO_NAME:
        name, role = params.get("name"), params.get("role")
        if role is not None and (not isinstance(role, str) or not role.strip()
                                 or len(role) > ACTION_DO_ROLE_MAX):
            return "bad_request", 'role — непустая строка ≤40 символов; пример: {"action":"click","name":"Удалить","role":"button"}'
        if role is not None and not isinstance(name, str):
            return "bad_request", ('role без name нечего резолвить: name — цель, role — фильтр; '
                                     'пример: {"action":"click","name":"Удалить","role":"button"}')
        if isinstance(name, str) and name.strip():
            if len(name) > ACTION_DO_NAME_MAX or frame is not None:
                return "bad_request", ('name-резолвер топ-документный (name ≤200); кадры — selector+frame; '
                                     'пример: {"action":"click","name":"Удалить"}')
            return None, None
        return "bad_request", ('ни selector, ни координат, ни имени — цели нет; примеры: '
                               '{"action":"click","selector":"#go"} | {"action":"click","x":120,"y":340} | '
                               '{"action":"key","key":"Enter"} | {"action":"scroll","dy":800}')
    # SK-42 п.3: input по координатам — хит-тест резолвит цель на стороне расширения
    # (elementFromPoint с пропуском собственного оверлея), затем фокус и ввод.
    if action in ACTION_DO_COORDS or ((action == "click" or action == "input") and has_xy):
        if selector is not None:
            return "bad_request", ('selector и x/y взаимоисключающи: неоднозначная цель не приоритизируется; '
                                     'оставь одно: {"action":"click","selector":"#go"} | {"action":"click","x":120,"y":340}')
        if not _num(params.get("x")) or not _num(params.get("y")):
            return "bad_request", 'x и y — обязательные числа вьюпортных координат (space of page.screenshot); пример: {"action":"click","x":120,"y":340}'
        if action == "input" and not _input_value_ok(params)[0]:
            return "bad_request", _input_value_ok(params)[1] + '; пример: {"action":"input","x":400,"y":520,"text":"тест"}'
        return None, None
    # selector leg (SK-17 shape): click/input/submit
    if has_xy:
        return "bad_request", ('selector и x/y взаимоисключающи: неоднозначная цель не приоритизируется; '
                                     'оставь одно: {"action":"click","selector":"#go"} | {"action":"click","x":120,"y":340}')
    if not isinstance(selector, str) or not selector.strip():
        return "bad_request", ('selector — непустая css-строка из page.dom; примеры: '
                               '{"action":"click","selector":"#go"} | {"action":"input","selector":"#q","value":"тест"}')
    if action == "input" and not _input_value_ok(params)[0]:
        return "bad_request", _input_value_ok(params)[1] + '; пример: {"action":"input","selector":"#q","value":"тест"}'
    return None, None

def action_do_preview_params(params, action):
    """SK-05 gate params for the three target modes (SK-24): the extension rings the
    selector, draws the circle at (x,y), or rings the focused element for keys.
    SK-25: `tab` rides along — the gate must draw on the SAME tab the action executes on."""
    ms = params.get("ms")
    pv = {"action": action,
          **({"name": str(params["name"])[:120]} if params.get("name") else {}),
          **({"tab": params["tab"]} if isinstance(params.get("tab"), int) else {}),
          **({"ms": ms} if isinstance(ms, (int, float)) and not isinstance(ms, bool) else {})}
    if action == "key":
        pv["key"] = str(params["key"]).strip()
        if not params.get("name"):
            pv["name"] = pv["key"][:120]  # «нажму «Enter»» — the key itself is the label
        if params.get("modifiers"):
            pv["modifiers"] = params["modifiers"]
        return pv
    if params.get("mark") is not None:
        # SK-26: гейт рисует кольцо на элементе МЕТКИ — резолвер карты живёт в content
        # (там же, где и карта). name, если агент принёс, — человеческая подпись (SK-17);
        # без него content соберёт сам: «кликну «Удалить» [7]» из карты скрина.
        pv["mark"] = params["mark"]
        return pv
    if params.get("sid") is not None:
        # SK-27: кольцо на элементе реестра page.dom — резолвер реестра тоже в content
        # (один и тот же на оба лега: превью и исполнение отказывают одинаково). name —
        # человеческая подпись; без него content возьмёт имя из реестра (как mark).
        pv["sid"] = str(params["sid"])
        return pv
    if (action in ACTION_DO_NAME and params.get("selector") is None
            and params.get("x") is None and params.get("y") is None
            and isinstance(params.get("name"), str) and params["name"].strip()):
        # SK-24 аддендум (в): имя — ОДНОВРЕМЕННО цель резолвера и человеческая подпись
        # («кликну «Удалить»»). Никаких selector/x/y — цель только именем и задаём.
        if params.get("role") is not None:
            pv["role"] = str(params["role"]).strip()
        return pv
    if action in ACTION_DO_COORDS or ((action == "click" or action == "input") and
                                      (params.get("x") is not None or params.get("y") is not None)):
        # SK-42 п.3: input по координатам превьюкается точкой (кружок в (x,y), глагол
        # «введу») — расширение резолверит хит-тест тем же sk17HitPoint, что и исполнение.
        pv.update({"x": params["x"], "y": params["y"]})
        if params.get("frame") is not None:
            pv["frame"] = params["frame"]
        return pv
    if action == "input" and params.get("selector") is None:
        # SK-42 п.1: input без цели — фокус-режим (тот же принцип, что у key: кольцо на
        # activeElement; нечего кольцевать — гейт честно откажет not_editable ДО кадра).
        return pv
    pv["selector"] = params["selector"]
    if params.get("frame") is not None:  # page.dom hands selector+frame as a pair
        pv["frame"] = params["frame"]
    return pv

def handle_action_do(rid, params, client):
    if action_do_validate(params)[0]:  # defense in depth — dispatch already validated
        rpc_respond(rid, False, err="bad_request", method="action.do"); return
    action = str(params.get("action") or "").strip().lower()
    pv_params = action_do_preview_params(params, action)
    pid = f"{rid}~pv"  # internal leg — the gate verdict is chain-internal, not an rpc_out line
    ACTION_DO_CHAIN[pid] = {"rid": rid, "action": action, "params": params, "client": client}
    PENDING[rid] = (asyncio.create_task(action_do_preview_timeout(rid, pid)), "action.do")
    asyncio.create_task(safe_send(client, {"v": 1, "type": "rpc", "id": pid,
                                           "method": "action.preview", "params": pv_params}))
    push_state()  # SK-09: the gate is already drawing the overlay in the browser = acting
    log(f"action.do {action} ({str(rid)[:8]}): preview gate first ({pid[:13]}…)")

async def action_do_preview_timeout(rid, pid):
    """The gate leg must answer within RPC_TIMEOUT_S — a silent extension means the
    human never saw a frame, so the action is refused, not blindly executed."""
    try:
        await asyncio.sleep(RPC_TIMEOUT_S)
    except asyncio.CancelledError:
        return
    if ACTION_DO_CHAIN.get(pid, {}).get("rid") == rid and rid in PENDING:
        ACTION_DO_CHAIN.pop(pid, None)
        PENDING.pop(rid, None)
        RPC_REQ.pop(rid, None)  # SK-25
        rpc_respond(rid, False, err="timeout", method="action.do")
        log(f"action.do ({str(rid)[:8]}): preview gate silent {RPC_TIMEOUT_S:.0f}s -> timeout, NOT executed")

def action_do_verdict(pid, msg):
    """The gate verdict came back on the internal preview leg: decide, never half-decide."""
    chain = ACTION_DO_CHAIN.pop(pid, None)
    rid = chain["rid"]
    entry = PENDING.pop(rid, None)
    if entry: entry[0].cancel()
    ok = bool(msg.get("ok"))
    pv = msg.get("result") if ok and isinstance(msg.get("result"), dict) else {}
    # SK-25: вкладка цели — из params (таб инжектирован резолвером до входа в цепь)
    tab = chain["params"].get("tab")
    # exactly one preview audit frame per action.do (SK-05 shape + via marker for the feed)
    audit({"ts": int(time.time()), "ev": "preview", "via": "action.do",
           "shown": bool(pv.get("shown")), "cancelled": bool(pv.get("cancelled")),
           **({"tab": tab} if isinstance(tab, int) else {}),
           **({"reason": str(pv.get("reason"))[:40]} if pv.get("reason") else {}),
           # SK-43 п.4: откуда подпись — от агента или мост обогатил из DOM
           **({"label_src": str(pv.get("label_src"))[:12]} if pv.get("label_src") else {}),
           **({"label": str(pv.get("label"))[:120]} if pv.get("label") else {})})
    if not ok:
        RPC_REQ.pop(rid, None)  # SK-25: цепь закрыта отказом лега — заявка больше не нужна
        rpc_respond(rid, False, err=msg.get("err") or "extension_error", method="action.do")
        log(f"action.do {chain['action']} ({str(rid)[:8]}): gate leg failed "
            f"({msg.get('err')}) — NOT executed")
    elif pv.get("cancelled") or not pv.get("shown"):
        # human veto / superseded / stale selector: a SUCCESSFUL answer, action skipped
        RPC_REQ.pop(rid, None)  # SK-25
        reason = str(pv.get("reason"))[:40] if pv.get("reason") else ""
        res = {"shown": bool(pv.get("shown")), "cancelled": bool(pv.get("cancelled")),
               "executed": False, "action": chain["action"],
               **({"reason": reason} if reason else {}),
               # SK-24 аддендум (в): ambiguous name → кандидаты с rect — агент выберет
               # координатой; не заставляем угадывать по «нет элемента».
               **({"candidates": [c for c in pv["candidates"] if isinstance(c, dict)][:8]}
                  if isinstance(pv.get("candidates"), list) else {}),
               # SK-42 п.4: отказ гейта самодостаточен — причина + следующий шаг, без
               # чтения исходников моста (агент обязан быть переносим между версиями).
               **({"hint": REASON_HINTS[reason]} if reason in REASON_HINTS else {})}
        rpc_respond(rid, True, res, method="action.do")
        log(f"action.do {chain['action']} ({str(rid)[:8]}): gate said NO "
            f"({pv.get('reason') or 'cancelled'}) — skipped")
    else:
        # clean verdict — execute now, same socket, the agent's own rpc id
        asyncio.create_task(safe_send(chain["client"], {"v": 1, "type": "rpc", "id": rid,
                                                        "method": "action.do", "params": chain["params"]}))
        PENDING[rid] = (asyncio.create_task(rpc_timeout(rid, "action.do")), "action.do")
        log(f"action.do {chain['action']} ({str(rid)[:8]}): gate clean -> executing")
    push_state()  # SK-09: refusal lifts the acting state; execution keeps it (new PENDING)

def dispatch_rpc(line):
    try:
        req = json.loads(line)
        if not isinstance(req, dict): raise ValueError("not an object")
    except Exception:
        rpc_respond(None, False, err="bad_request"); return
    rid, method = req.get("id"), req.get("method")
    if "v" not in req:
        rpc_respond(rid, False, err="no_version", method=method); return
    if not valid_v(req["v"]):
        rpc_respond(rid, False, err="bad_version", method=method); return
    if not isinstance(rid, str) or not rid or not isinstance(method, str) or not method:
        rpc_respond(rid, False, err="bad_request"); return
    params = req.get("params")
    if not isinstance(params, dict): params = {}
    # SK-08: while a human holds the browser, NOTHING visual may pass the bridge —
    # no page.* context, no previews, no screenshots (the human may be typing secrets).
    # Only the hand-off protocol itself answers; everything else gets a loud refusal
    # that is also audited, so the log proves nothing was forwarded during the pause.
    if method == "handoff.status":
        hid, hinfo = next(iter(HANDOFFS.items()), (None, None))
        # SK-48: shot (dataUrl кадра) — в статус не отдаём: это мегабайты для чипа,
        # он нужен только баннеру сайдбара
        hres = {k: v for k, v in (hinfo or {}).items() if k not in ("shot",)}
        rpc_respond(rid, True, {"paused": os.path.exists(PAUSED),
                                **({"id": hid, **hres} if hinfo else {})}, method=method)
        return
    # SK-43 п.2: контракт моста — статические границы, никаких данных браузера, поэтому
    # отвечает и во время hand-off паузы (агенту он нужен в том числе чтобы понять,
    # почему сейчас paused_for_human и что вообще умеет мост).
    if method == "capabilities":
        rpc_respond(rid, True, capabilities_doc(), method=method); return
    if method == "handoff.request":
        # SK-25: задача стоит на вкладке — handoff-баннер показывает её URL, даже когда
        # агент его не понёс (приватность: url задачи — тот же, что агент и так видел).
        t = str(params.get("task") or "")[:TAB_TASK_MAX]
        if not params.get("url") and t in TASK_TAB and TASK_TAB[t].get("url"):
            params = {**params, "url": TASK_TAB[t]["url"]}
        # SK-48: expect — ожидаемое состояние, которым мост проверит «Готово» человека;
        # форма отсекается локально (bad_request + hint), до касания паузы
        verr, vhint = handoff_expect_validate(params.get("expect"))
        if verr:
            rpc_respond(rid, False, err=verr, hint=vhint, method="handoff.request"); return
        handle_handoff_request(rid, params,
                               expect=handoff_expect_clean(params.get("expect"))); return
    if os.path.exists(PAUSED):
        rpc_respond(rid, False, err="paused_for_human", method=method)
        audit({"ts": int(time.time()), "ev": "handoff_block", "method": str(method)[:60]})
        log(f"rpc {method} BLOCKED ({str(rid)[:8]}): hand-off pause — human has the browser")
        return
    # SK-04: bridge-local methods — decided without the extension (allow/block fast path;
    # perm.check escalates to the human via a perm_ask frame only when the model says ask).
    if method == "grants.get":
        rpc_respond(rid, True, {"grants": grants_load()}, method=method); return
    if method == "grants.set":
        handle_grants_set(rid, params); return
    if method == "perm.check":
        # SK-25: perm для задачи без url решается по URL её вкладки (иначе домен
        # неизвестен и честный ответ только no_grant/ask — хуже, чем знание с моста).
        t = str(params.get("task") or "")[:TAB_TASK_MAX]
        if not params.get("url") and t in TASK_TAB and TASK_TAB[t].get("url"):
            params = {**params, "url": TASK_TAB[t]["url"]}
        handle_perm_check(rid, params, task=t); return
    # SK-11: the audit feed's agent-side leg (bridge-local — the file lives HERE).
    # Behind the PAUSED gate on purpose: during a hand-off the human holds the
    # browser and the trail may name the page they are typing secrets on.
    if method == "audit.tail":
        lines, total, trunc = audit_tail(params.get("limit", 50), params.get("domain") or "")
        rpc_respond(rid, True, {"lines": lines, "total": total, "truncated": trunc}, method=method)
        return
    # SK-23: chat history — bridge-local too (chats.jsonl lives HERE) and behind the
    # PAUSED gate for the same privacy reason as audit.tail: these answers carry the
    # human's own conversation text, which the agent must not read during a hand-off.
    if method == "chats.list":
        rpc_respond(rid, True, {"chats": chats_list()}, method=method); return
    if method == "chats.get":
        cid = str(params.get("chat_id") or "default")[:CHATS_ID_MAX]
        rpc_respond(rid, True, {"chat": chats_get(cid)}, method=method); return
    # SK-17: action.do NEVER passes through — the bridge chains preview (gate) ->
    # execution itself; forwarding the raw method would be an ungated execution hole.
    # Validation is local (bad_request without a browser), execution needs a client.
    if method == "action.do":
        verr, vhint = action_do_validate(params)
        if verr:
            rpc_respond(rid, False, err=verr, hint=vhint, method="action.do"); return
        # SK-25: цель задачи — её вкладка; tab инжектируется в params и проходит через
        # ОБА лега цепочки (гейт-превью рисуется на той же вкладке, что и исполнение).
        err, task, tab = tab_resolve(params)
        if err:
            rpc_respond(rid, False, err=err, method="action.do"); return
        if not CLIENTS:
            rpc_respond(rid, False, err="no_extension", method="action.do"); return
        RPC_REQ[rid] = {"task": task, "tab": tab}
        # SK-42 п.2: scroll — ЕДИНСТВЕННЫЙ глагол action.do без превью-гейта: он ничего
        # на странице не меняет (движение вьюпорта, класс чтения рядом с page.*), а
        # гейтинг порождает ровно то, против чего карточка: инцидент B — 8 превью-кадров
        # ради прокрутки, scrollY=0. Аудит-строка метода при этом обычная (rpc_respond).
        if str(params.get("action") or "").strip().lower() == "scroll":
            client = next(iter(CLIENTS))
            PENDING[rid] = (asyncio.create_task(rpc_timeout(rid, "action.do")), "action.do")
            asyncio.create_task(safe_send(client, {"v": 1, "type": "rpc", "id": rid,
                                                   "method": "action.do", "params": params}))
            push_state()  # SK-09: вкладка шевелится — acting
            log(f"rpc -> action.do scroll ({rid[:8]}): gate-free, no preview")
            return
        handle_action_do(rid, params, next(iter(CLIENTS))); return
    # SK-32: tabs.close — вкладочный аналог action.do: мост НЕ пропускает метод как есть.
    # Гейт (защищённые вкладки SK-25 + одно человеческое разрешение на пачку) живёт здесь,
    # расширению метод достаётся только после чистого вердикта (на rid агента).
    if method == "tabs.close":
        err, tab_ids, force = tabs_close_validate(params)
        if err:
            rpc_respond(rid, False, err=err, method="tabs.close"); return
        if not CLIENTS:
            rpc_respond(rid, False, err="no_extension", method="tabs.close"); return
        handle_tabs_close(rid, tab_ids, force, params, next(iter(CLIENTS))); return
    # SK-32: tabs.activate — цель явная (tabId) или вкладка задачи (SK-25 резолвер);
    # активной вкладкой метод НЕ угадывает. Дальше — обычный форвард.
    if method == "tabs.activate":
        err = tabs_activate_resolve(params)
        if err:
            rpc_respond(rid, False, err=err, method="tabs.activate"); return
    # SK-25: page.active {} — явный запрос «что у человека открыто»; вкладка-цель тут
    # противоречит смыслу метода (это НЕ путь нацеливания агента).
    if method == "page.active" and params.get("tab") is not None:
        rpc_respond(rid, False, err="bad_request", method=method); return
    # SK-26: marks — булев флаг SoM-съёмки (номерные метки + карта marks[] в ответе).
    # Мусор в флаге отсекается локально, до раунд-трипа: расширение ждёт строго true.
    if method == "page.screenshot" and params.get("marks") is not None \
            and not isinstance(params.get("marks"), bool):
        rpc_respond(rid, False, err="bad_request", method=method); return
    # SK-47: max_width — ширина vision-кадра (0 = «чистый» PNG без даунскейла; дефолт 1280
    # живёт в расширении). Форма проверяется локально (прецедент marks): строка/bool/отрицательное
    # означали бы тихий даунскейл «на глаз» или краш канваса, а агент ждал бы кадр.
    if method == "page.screenshot" and params.get("max_width") is not None:
        mw = params.get("max_width")
        if mw is None or not _num(mw) or mw < 0 or mw > 8192:  # _num уже режет bool/NaN/inf
            rpc_respond(rid, False, err="bad_request", method=method); return
    # SK-47: clip — регион кадра {x,y,w,h} в CSS-пикселях вьюпорта (та же система, что у
    # page.dom/marks rect). w/h строго положительны, координаты в разумных границах (near-прецедент).
    if method == "page.screenshot" and params.get("clip") is not None:
        clip = params.get("clip")
        if not isinstance(clip, dict) or not all(_num(clip.get(k)) for k in ("x", "y", "w", "h")) \
                or clip["w"] <= 0 or clip["h"] <= 0 \
                or any(abs(clip[k]) > 100000 for k in ("x", "y", "w", "h")):
            rpc_respond(rid, False, err="bad_request", method=method); return
    # SK-27: near — регион page.dom [x0,y0,x1,y1] (координаты вьюпорта, конечные числа).
    # Форма проверяем локально (прецедент marks): content ждёт ровно 4 числа, прочий
    # мусор означал бы тихую отдачу без фильтра — агент ждал бы region, получил бы всё.
    if method == "page.dom" and params.get("near") is not None:
        near = params.get("near")
        if not isinstance(near, list) or len(near) != 4 or not all(
                _num(v) and abs(v) <= 100000 for v in near):
            rpc_respond(rid, False, err="bad_request", method=method); return
    # SK-25: вкладочные методы — резолвер цели (инъекция вкладки задачи; для BIND-методов
    # только валидация явного tab — tabs.use_tab без tabId осознанно берёт активную).
    req_task, req_tab = "", None
    if method in TAB_METHODS or method in BIND_METHODS:
        err, req_task, req_tab = tab_resolve(params, inject=method not in BIND_METHODS)
        if err:
            rpc_respond(rid, False, err=err, method=method); return
    if not CLIENTS:
        rpc_respond(rid, False, err="no_extension", method=method); return
    # SK-49: не «предположение», а инвариант — handler вытесняет предыдущий сокет
    # (4009), поэтому CLIENTS ≤ 1 и next() детерминирован: RPC всегда уходят
    # последним подтверждённым hello, а не первому попавшему из порядка set().
    client = next(iter(CLIENTS))
    if method in TAB_METHODS or method in BIND_METHODS:
        RPC_REQ[rid] = {"task": req_task, "tab": req_tab}
    PENDING[rid] = (asyncio.create_task(rpc_timeout(rid, method)), method)
    asyncio.create_task(safe_send(client, {"v": 1, "type": "rpc", "id": rid,
                                           "method": method, "params": params}))
    push_state()  # SK-09: browser-facing rpc in flight = acting, everything else = thinking
    log(f"rpc -> {method} ({rid[:8]})")

async def pump_rpc():
    while True:
        try:
            if os.path.exists(RPC_IN):
                data = open(RPC_IN).read()
                if data.strip():
                    open(RPC_IN, "w").close()  # same drain pattern as outbox
                    for ln in data.splitlines():
                        if ln.strip(): dispatch_rpc(ln)
        except Exception as e: log(f"rpc_in err: {e}")
        await asyncio.sleep(0.1)

def peer_str(ws):
    """SK-49: «ip:port» сокета для health/audit (тот же формат, что в handshake-логе)."""
    try:
        return f"{ws.remote_address[0]}:{ws.remote_address[1]}" if ws.remote_address else "?"
    except Exception:
        return "?"

async def handler(ws):
    if await handshake(ws) is None:
        return
    # SK-49: политика одного соединения — второй валидный hello ВЫТЕСНЯЕТ первого
    # (close 4009), а не сажает два браузера на random-маршрутизацию через
    # next(iter(CLIENTS)) (инцидент 23.10: page.dom без task ушёл в браузер
    # пользователя, агентские вкладки открылись в тест-Firefox). Мутируем CLIENTS
    # СИНХРОННО до первого await — два конкурентных handshake не могут оставить
    # в множестве два сокета; плохой токен сюда не долетает (handshake закрыл 4003).
    evicted = [(c, CLIENT_PEER.pop(c, "?")) for c in CLIENTS if c is not ws]
    for c, _ in evicted:
        CLIENTS.discard(c)
    CLIENTS.add(ws)
    CLIENT_PEER[ws] = peer_str(ws)
    log(f"extension connected ({len(CLIENTS)}) peer={CLIENT_PEER[ws]}")
    audit({"ts": int(time.time()), "ev": "ext", "decision": "connect",
           "peer": CLIENT_PEER[ws]})
    health_write()  # SK-33: connect-переход пишется сразу, не дожидаясь тика
    for c, old_peer in evicted:  # await'ы — только после того как маршрут уже принадлежит нам
        try:
            audit({"ts": int(time.time()), "ev": "ext", "decision": "replaced",
                   "peer": old_peer, "by": CLIENT_PEER[ws]})
            log(f"SK-49: previous extension socket {old_peer} evicted "
                f"by {CLIENT_PEER[ws]} (close 4009)")
            await c.close(4009, "replaced by newer connection")
        except Exception:
            pass
    # SK-09: replay the current agent state to the fresh socket — the chip must not sit
    # on "unknown" (or an older value) while the bridge already knows the truth. If the
    # recompute drifted (hub/stop removed while nobody was connected) push_state
    # broadcasts to everyone; otherwise we answer just this client. Exactly one frame.
    prev_state = AGENT_STATE
    push_state()
    if AGENT_STATE == prev_state:
        await safe_send(ws, state_frame())
    # reconnect while a human was being asked: re-push open asks to the fresh socket
    for rid, info in PERM_ASKS.items():
        await safe_send(ws, perm_frame(rid, info))
        log(f"perm_ask {info['domain']}/{info['action']} ({rid[:8]}) re-pushed after reconnect")
    # SK-08: same for an open hand-off — the banner must survive a WS drop
    for rid, info in HANDOFFS.items():
        await safe_send(ws, handoff_frame(rid, info))
        log(f"handoff ({rid[:8]}) re-pushed after reconnect")
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict): raise ValueError("not an object")
            except Exception:
                await safe_send(ws, {"v": PROTOCOL_VERSION, "type": "error", "err": "bad_frame"})
                await ws.close(4002, "bad frame"); break
            if "v" not in msg:  # contract v1: version is mandatory on every frame
                log(f"frame REJECTED: no_version -> close 4002 ({raw[:80]!r})")
                await safe_send(ws, {"v": PROTOCOL_VERSION, "type": "error", "err": "no_version"})
                await ws.close(4002, "no_version"); break
            if not valid_v(msg["v"]):
                log(f"frame REJECTED: bad_version v={msg['v']!r} -> close 4002")
                await safe_send(ws, {"v": PROTOCOL_VERSION, "type": "error", "err": "bad_version"})
                await ws.close(4002, "bad_version"); break
            if msg.get("type") == "hb": continue
            if msg.get("type") == "rpc_result":  # agent-facing answer: consume, never inbox
                rid = msg.get("id")
                if isinstance(rid, str) and rid in ACTION_DO_CHAIN:
                    # SK-17: this is the GATE verdict of a chained action.do, not a
                    # standalone action.preview — it never gets its own rpc_out line.
                    action_do_verdict(rid, msg)
                    continue
                if isinstance(rid, str) and rid in HANDOFF_VERIFY:
                    # SK-48: внутренний лег верификации handoff (page.media/page.dom/кадр) —
                    # вердикт варится на мосту; в rpc_out агента эти строки не выходят
                    handoff_verify_result(rid, msg)
                    continue
                if isinstance(rid, str) and rid in PERM_THUMB:
                    # SK-37: внутренний лег миниатюры пермиса — только perm_shot в
                    # клиенты; агенту и в rpc_out здесь ничего не положено
                    perm_thumb_result(rid, msg)
                    continue
                entry = PENDING.pop(rid, None) if isinstance(rid, str) else None
                if entry:
                    task, method = entry
                    task.cancel()
                    ok = bool(msg.get("ok"))
                    # SK-25: заявка резолвера (task/tab) живёт до ответа этого rpc — на ней
                    # и биндинг по вкладочным методам, и «tab» в строке аудита.
                    req = RPC_REQ.pop(rid, None) or {}
                    res = msg.get("result") if ok and isinstance(msg.get("result"), dict) else None
                    if method == "tabs.list":
                        tabs_list_annotate(res)  # SK-32: protected/task — из реестра моста
                    if res is not None:
                        t = req.get("task") or ""
                        if method in BIND_METHODS:
                            bind_task_tab(t, res, rebind=(method == "tabs.use_tab"))
                        elif t in TASK_TAB and isinstance(res.get("url"), str) and res["url"]:
                            TASK_TAB[t]["url"] = res["url"][:2000]  # url задачи — свежий, perm/handoff по нему
                    elif req.get("task") in TASK_TAB and str(msg.get("err") or "").startswith("tab_gone"):
                        # вкладка задачи закрыта: запись помечается мёртвой (НЕ удаляется) —
                        # до перепривязки ЛЮБОЙ rpc задачи отвечает tab_gone на месте, а не
                        # падает в легаси-активную вкладку (задача не имеет права на чужую)
                        TASK_TAB[req["task"]]["dead"] = True
                        push_task_tab(req["task"])
                    # SK-11: the feed's «домен» column — page.*/tabs.* answers carry the
                    # URL the action touched (the request side rarely knows it: active tab).
                    dom = ""
                    if res is not None:
                        u = res.get("url")
                        if isinstance(u, str) and u: dom = norm_host(u)
                    extra = {}
                    if dom: extra["domain"] = dom
                    if isinstance(req.get("tab"), int): extra["tab"] = req["tab"]  # SK-25 приёмка
                    if method == "action.do" and res is not None \
                            and isinstance(res.get("post_state"), dict):
                        # SK-48 (аудит №2 22.10, проблема 11): silent-noop тумблера
                        # («Выбрать все» на Ozon при executed:true) виден в аудите grep'ом
                        # «post_state» — без разбора скриншотов и без доверия executed.
                        extra["post_state"] = res["post_state"]
                    rpc_respond(rid, ok, result=msg.get("result"),
                                err=None if ok else (msg.get("err") or "extension_error"),
                                method=method,
                                # SK-42 п.4: hint расширения (not_editable с tag/CE и т.п.)
                                # доезжает до агента аддитивным полем вердикта
                                **({} if ok else {"hint": msg.get("hint")}),
                                **({"audit_extra": extra} if extra else {}))
                    if method == "action.preview" and ok and isinstance(msg.get("result"), dict):
                        # SK-05: the human's veto verdict belongs in the audit trail (SK-11 feed)
                        # SK-34 п.2: via:"standalone" — отдельный превью-запрос вне
                        # action.do (панель/приёмка); в потоке задачи его быть не должно,
                        # по этой метке аудит и считает двойные вопросы.
                        pv = msg["result"]
                        audit({"ts": int(time.time()), "ev": "preview", "via": "standalone",
                               "shown": bool(pv.get("shown")), "cancelled": bool(pv.get("cancelled")),
                               **({"tab": req["tab"]} if isinstance(req.get("tab"), int) else {}),
                               **({"reason": str(pv.get("reason"))[:40]} if pv.get("reason") else {}),
                               **({"label_src": str(pv.get("label_src"))[:12]} if pv.get("label_src") else {}),
                               **({"label": str(pv.get("label"))[:120]} if pv.get("label") else {})})
                    log(f"rpc {method} ({str(rid)[:8]}) -> {'ok' if ok else 'err:' + str(msg.get('err'))}")
                else:
                    log(f"orphan/stale rpc_result id={rid!r} ignored")
                push_state()  # SK-09: the in-flight rpc is over — acting/thinking lift
                continue
            if msg.get("type") == "perm_answer":  # SK-04: human verdict from the sidebar
                aid, choice = msg.get("id"), msg.get("choice")
                event_log(raw)  # SK-33: agent-readable event log (ts-штамп) ... consumed: never inbox
                if not isinstance(aid, str) or aid not in PERM_ASKS:
                    log(f"orphan/stale perm_answer id={aid!r} ignored")
                    continue
                if choice not in ("once", "always", "deny"):
                    resolve_perm(aid, "block", "bad_choice", ok=False, err="bad_request")
                    continue
                info = PERM_ASKS[aid]
                # SK-34 п.3: любой живой ответ человека обнуляет счётчик таймаутов —
                # streak наказывает именно молчание.
                TIMEOUT_STREAK.pop((str(info.get("task") or ""),
                                    str(info.get("domain") or ""),
                                    str(info.get("action") or "")), None)
                deny_reason = str(msg.get("reason") or "").strip()[:80]
                if choice == "once":  # SK-34 п.1: запоминаем (домен, тип) — на следующем
                    once_record(info["domain"], info["action"])  # вопросе того же типа
                if choice == "always":  # «Всегда для сайта» -> persistent grant (bridge-side)
                    # SK-34 п.1: приватность — на чувствительном пути (платежи, 2FA,
                    # bank/*, текст наружу) кнопки «всегда» не существовало; пришедший
                    # всё равно «always» (старый UI, кривой клиент) понижается до once.
                    _d, _v, sensitive = perm_decide(info["domain"], info["action"],
                                                    info.get("url") or "")
                    if sensitive:
                        once_record(info["domain"], info["action"])
                        log(f"perm_answer ({str(aid)[:8]}): «всегда» на чувствительном "
                            f"пути ({info['domain']}/{info['action']}) -> понижено до разово")
                        resolve_perm(aid, "allow", "human_once")
                        continue
                    host = info["domain"]
                    if host:
                        g = grants_load()
                        g[host] = {"level": "allow", "ts": int(time.time())}
                        grants_save(g)
                if choice == "deny":  # SK-34 п.4: помним недавние deny — запоздалая
                    RECENT_DENIES[aid] = {"domain": info["domain"],  # причина из чипа
                                          "action": info["action"], "ts": time.time()}
                    for k in [k for k, v in RECENT_DENIES.items()
                              if time.time() - v["ts"] > RECENT_DENIES_TTL_S]:
                        RECENT_DENIES.pop(k, None)
                resolve_perm(aid, "block" if choice == "deny" else "allow",
                             f"human_{choice}", deny_reason=deny_reason)
                log(f"perm_answer ({str(aid)[:8]}): {choice}"
                    + (f" reason={deny_reason}" if deny_reason else ""))
                continue
            if msg.get("type") == "perm_reason":  # SK-34 п.4: чип-причина ПОСЛЕ вердикта
                event_log(raw)  # агент тоже видит (events.jsonl), но аудит — главное
                rid_ = msg.get("id")
                reason = str(msg.get("reason") or "").strip()[:80]
                hit = RECENT_DENIES.pop(rid_, None) if isinstance(rid_, str) else None
                if not hit or not reason:
                    log(f"orphan/stale perm_reason id={rid_!r} ignored")
                    continue
                audit({"ts": int(time.time()), "ev": "deny_reason",
                       "domain": hit["domain"], "action": hit["action"], "reason": reason})
                log(f"deny_reason {hit['domain'] or '?'}/{hit['action']}: {reason}")
                continue
            if msg.get("type") == "bell_event":  # SK-50: диагностика звонка (create/fail) —
                event_log(raw)  # events.jsonl (причина не глотается catch-ом), consumed (never inbox)
                continue
            if msg.get("type") == "stop":  # SK-06: human's ■ Stop — pause flag + agent event, consumed (never inbox)
                event_log(raw)  # SK-33: ts-штамп, ротация при пороге
                open(STOP, "w").close()  # touch: idempotent; the agent checks hub/stop between steps
                stask = str(msg.get("task") or "")[:60]
                audit({"ts": int(time.time()), "ev": "stop", **({"task": stask} if stask else {})})
                push_state()  # SK-09: hub/stop up = paused (the chip flips on the way in)
                log(f"STOP pressed{(' task=' + stask) if stask else ''} -> hub/stop + events.jsonl")
                continue
            if msg.get("type") == "resume":  # SK-08: «Готово, продолжай» — human finished, agent unblocked
                handle_resume(msg, raw)
                continue
            if msg.get("type") == "audit_get":  # SK-11: the sidebar's audit-feed pull —
                # consumed (never inbox) and deliberately NOT audited: the panel polls
                # this every couple of seconds, and auditing the poll feeds the audit
                # with itself. Works during a hand-off pause — the human at the
                # keyboard is exactly who may want to read the trail.
                aid = msg.get("id")
                lines, total, trunc = audit_tail(msg.get("limit", 200), msg.get("domain") or "")
                await safe_send(ws, {"v": 1, "type": "audit_data",
                                     **({"id": aid} if isinstance(aid, str) else {}),
                                     "lines": lines, "total": total, "truncated": trunc})
                continue
            if msg.get("type") == "chats_get":  # SK-23: the sidebar's chat-history pull —
                # same legs as audit_get: consumed (never inbox), not audited (the panel
                # pulls), answered during a hand-off pause (the human reads their own chat).
                # No chat_id -> the history list; with chat_id -> that chat's messages.
                cid = msg.get("chat_id")
                out = {"v": 1, "type": "chats_data",
                       **({"id": msg["id"]} if isinstance(msg.get("id"), str) else {})}
                if isinstance(cid, str) and cid:
                    out["chat"] = chats_get(cid[:CHATS_ID_MAX])
                else:
                    out["chats"] = chats_list()
                await safe_send(ws, out)
                continue
            if msg.get("type") == "chat":  # SK-23: mirror the human side of the
                # conversation into chats.jsonl — additive: the frame still lands in
                # inbox.jsonl for agentd exactly as before (chat field rides along).
                chats_log(msg.get("chat"), "user", msg.get("text"))
            with open(INBOX, "a") as f:
                f.write(raw + "\n")
            log(f"<< {raw[:160]}")
    except websockets.ConnectionClosed:
        pass
    finally:
        # SK-49: вытесненный 4009 сокет сюда долетает уже удалённым — его уход
        # уже описан в replaced-строке, дубль «disconnect» ленту не плодит.
        was_active = ws in CLIENTS
        CLIENTS.discard(ws)
        peer = CLIENT_PEER.pop(ws, None)
        if was_active:
            log(f"extension disconnected ({len(CLIENTS)})")
            audit({"ts": int(time.time()), "ev": "ext", "decision": "disconnect",
                   "peer": peer or "?"})
            health_write()  # SK-33: disconnect-переход — сразу (agentd на паузе не ждёт тика)

async def main():
    # SK-08: a hand-off cannot survive us — the waiter died with the old process, so a
    # leftover flag would block page.* forever with nobody left to resolve it.
    if os.path.exists(PAUSED):
        os.remove(PAUSED)
        log("startup: stale hub/paused removed (no live hand-off after a restart)")
    paired_load()      # SK-46: память о спаренных IP (legacy_hello) — с первой секунды
    check_unclean_exit()  # SK-46: «прошлый ран убит» — событие до первой минуты жизни
    lan = BIND not in ("127.0.0.1", "localhost", "::1", "[::1]")
    log(f"listening on ws://{BIND}:{PORT} (contract v{PROTOCOL_VERSION}, token auth"
        + (", SK-28 LAN-bind: токен по ws открытым текстом — только доверенная сеть)" if lan else ")"))
    health_write()  # SK-33: «мост жив» — с первой секунды, читателю не за что не зацепиться
    asyncio.create_task(pump_outbox())
    asyncio.create_task(pump_rpc())
    asyncio.create_task(pump_state())  # SK-09: hub/stop removal safety net
    asyncio.create_task(pump_health())  # SK-33: health tick (connect/disconnect — свои хуки)
    # SK-46: SIGTERM/SIGINT = намеренная остановка (systemd stop/restart, человек) —
    # сначала clean-exit mark, потом штатное закрытие. Без этого каждый штатный
    # рестарт выглядел бы как «грязный выход» и будил алерт.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    def _on_stop():
        mark_clean_exit()
        stop.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_stop)
        except NotImplementedError:
            pass  # не-Unix (стенды на Windows не гоняем, но и не роняем мост)
    global GRACEFUL
    async with websockets.serve(handler, BIND, PORT, max_size=2**22):
        await stop.wait()
    GRACEFUL = True
    log("SK-46: graceful stop (signal), clean-exit mark left")

GRACEFUL = False

if __name__ == "__main__":
    while not GRACEFUL:
        try:
            asyncio.run(main())
        except Exception as e:
            log(f"CRASH: {e!r}, restarting in 2s")
            time.sleep(2)
