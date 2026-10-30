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
.venv/bin/pytest tests/            # full suite: 786 tests, ~3 min
```

- Unit/integration tests: `tests/test_*.py` — hermetic, isolated bridge
  port `:8797` via `tests/conftest.py` / `tests/serve.py`.
- Live acceptance harnesses: `tests/sk<NN>_acc.py` — drive a real test
  Firefox through Marionette (port 2828) against the **isolated** bridge
  `:8797`. Production `:8791` must never be touched by tests; harness
  processes are killed by PID afterwards (AGENTS.md п.8).
- UI text is Russian (the product speaks Russian to its human); code
  comments follow the surrounding style.

## Branches, commits, PRs

- Base branch: `main`.
- One card = one commit; message: `SK-<NN>: <what> — <acceptance>` style.
- PR body: what changed, how it was verified (pytest count, harness names),
  any contract additions quoted into `AGENTS.md`/spec in the same commit.
- Screenshots of UI changes: the gallery pages in `variants/` are the
  mockup source of truth; regenerate shots with headless Firefox
  (`--screenshot`, window 760×1375 for panels).

## Questions?

Open a regular issue for usage questions; use [SECURITY.md](SECURITY.md)
for anything that touches the safety model (gate bypass, token leak,
agent-channel escalation).
