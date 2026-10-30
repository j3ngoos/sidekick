# Contributing to Sidekick

Sidekick is a small, contract-driven project. Read this before your first PR —
a few rules keep the human-safety guarantees intact.

## Non-negotiables

1. **The bridge is the only path to the browser.** The agent talks to Firefox
   exclusively through the hub → bridge → WebSocket. Introducing CDP,
   Marionette, selenium, `browser_*` MCP tools or any other direct channel is
   a contract violation and will be rejected (AGENTS.md п.2). The Marionette
   instance in `tests/` is harness-only and never exposed to agents.
2. **Human gates are product surface.** Preview-before-action, permission
   cards for the sensitive classes, `ask_timeout` = denial, ■ Stop, handoff
   pause semantics may be extended (additive) but never weakened, bypassed
   or made configurable-per-agent.
3. **The v1 wire contract is additive.** New fields/methods in
   `bridge/hub/*.jsonl` frames, RPC methods and MCP tools are fine when
   documented in `AGENTS.md` (the source of truth for the agent contract)
   and `docs/mcp-spec.md` (for MCP); renaming/removing v1 semantics is not.
   `action.do` stays a single method (verdicts live in `results`, e.g.
   `cancelled` is a *successful* response).
4. **Zero dependencies discipline.** Bridge, agentd, `sk` and the MCP
   adapter are Python stdlib only (the bridge additionally uses
   `websockets`). No npm, no package sprawl in `extension/` (vanilla JS).
5. **No secrets, ever.** Tokens live in `bridge/hub/` (gitignored, 0600),
   MCP keys in env/systemd drop-ins. Docs use placeholders
   (`<MCP_KEY>`, `<your-host>`, `<agent-user>@<agent-host>`). PR CI should
   keep `git grep -E '\b[0-9a-f]{64}\b'` at zero hits.

## Dev loop

```bash
git clone <your-fork> && cd sidekick
python3 -m venv .venv && .venv/bin/pip install websockets pytest
.venv/bin/pytest tests/            # full suite: 819 tests, ~3 min
```

- Unit/integration tests: `tests/test_*.py` — hermetic, isolated bridge
  port `:8797` via `tests/conftest.py` / `tests/serve.py`.
- Live end-to-end harnesses: `tests/sk<NN>_acc.py` — drive a real test
  Firefox through Marionette (port 2828) against the **isolated** bridge
  `:8797`. Production `:8791` must never be touched by tests; harness
  processes are killed by PID afterwards (AGENTS.md п.8).
- UI text comes from the locale dictionaries, never from literals:
  `extension/locales/en.json` + `ru.json` (key parity is
  enforced by `tests/test_i18n_sk71.py`; EN is the default, RU is the
  same UI verbatim-mirrored). Zero Cyrillic in user-visible JS/HTML
  string literals — the only exception is the endonym "Русский" in the
  language switcher. Code comments follow the surrounding style.

## i18n rules

- `extension/i18n.js` is the shared core. Manifest loads it FIRST in
  `background.scripts` and in the `content.js` content-script entry.
- Lookups: JS uses `L("ns.key", {param: v})` (alias defined once in
  i18n.js); static HTML uses `data-i18n` / `data-i18n-title` /
  `data-i18n-placeholder` / `data-i18n-aria` and gets `I18N.apply(doc)`.
- Pure modules that run in node contracts (`feed.js`, `audit.js`) take a
  translator via `setT()` from the page and ship a built-in EN pack that
  mirrors `en.json` (mirror is asserted in tests).
- Language switch: options writes `storage.uiLang`
  ("en"|"ru"|"system"). Pages re-load packs in place via
  `I18N.watchStorage(cb)` and re-render — NO page reload. The background
  re-packs and broadcasts `i18n_changed`; content scripts cannot fetch
  extension URLs, so they pull the pack via the `i18n_get` runtime
  message and refresh on `i18n_changed`.
- Keys are namespaced (`sbt.` sidebar text, `opt.` options, `feed.`,
  `aud.`, `notif.`, `perm.`, `err.`, `prev.`, `cnt.`); a missing key
  renders as the key itself so it is greppable, never silently empty.

## Branches, commits, PRs

- Base branch: `main`.
- One logical change per commit; keep each PR to one logical commit
  (squash before merge). Message: imperative mood with a scope prefix —
  e.g. `bridge: add cursor replay on reconnect`, `docs: fix test count`.
- PR body: what changed, how it was verified (pytest count, harness names),
  any contract additions quoted into `AGENTS.md`/spec in the same commit.
- Screenshots of UI changes: the gallery pages in `variants/` are the
  mockup source of truth; regenerate shots with headless Firefox
  (`--screenshot`, window 760×1375 for panels).

## Questions?

Open a regular issue for usage questions; use [SECURITY.md](SECURITY.md)
for anything that touches the safety model (gate bypass, token leak,
agent-channel escalation).
