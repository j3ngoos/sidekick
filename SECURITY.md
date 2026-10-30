# Security Policy

## Reporting a vulnerability

Report security issues **privately**, not in public issues:

- GitHub → *Security* tab → *Report a vulnerability* (private draft advisory), or
- the contact listed in the repo's Security tab (maintainer channel).

Include: component (`bridge/`, `extension/`, `agentd/`, `mcp/`), reproduction
steps, impact. We aim to respond within 72 hours and will coordinate
disclosure timing with you.

**Never paste real secrets into any report:** bridge token
(`bridge/hub/token`), MCP bearer key, `~/.hermes/.env` contents, cookies,
authorization headers, screenshots of real pages containing personal data.
Redact them; synthetic reproductions are preferred.

## Scope

In scope:

- Authentication/authorization bypass of the bridge (`:8791`) — token
  handling, peer limits (4003/4009), Origin checks.
- Bypass of human gates: pre-action preview, permission cards,
  `ask_timeout`-as-denial, `hub/paused` page reads, ■ Stop, task↔tab
  isolation.
- MCP adapter HTTP hardening: bearer comparison, session lifecycle,
  Origin allowlist (DNS rebinding), rate limiting, protocol validation.
- Injection/escalation through the hub: frames that let an agent reach the
  browser outside the bridge contract (CDP/Marionette/selenium paths),
  audit-evasion, token leakage into logs/frames/RPC.
- Extension privilege issues (host permissions beyond declared narrow
  permissions, `tabs` leak).

Out of scope:

- Vulnerabilities of websites the agent browses; phishing content.
- Attacks that require physical access to the machine, control of the
  user's Firefox profile, the user's Hermes installation, the `hub/`
  directory, or the user's systemd user units — that is owning the box.
- Social engineering of the human (tricking a human into approving a
  permission card is a UX problem, file it as one).

## Known design limitations (not vulnerabilities)

- `ws://` (no TLS) is the transport; the token travels in plaintext.
  Documented: loopback (default) or trusted LAN only; remote setups use an
  SSH tunnel (`ssh -R 8791:127.0.0.1:8791 <agent-user>@<agent-host>`, which
  carries real encryption).
- The agent runs locally with the user's own privileges — Sidekick gates
  *browser actions*, not the OS. The agent has no browser channel at all:
  killing the bridge kills browsing, `hub/paused` freezes page reads.
- A human who habitually approves every permission card defeats the gates —
  by design the gates need an awake human (60 s silence = denial).

## Hardening checklist (self-hosted)

- Keep the bridge on `127.0.0.1`; `SIDEKICK_BIND=0.0.0.0` only on a trusted
  network.
- `bridge/hub/token` is generated 0600; keep `hub/` private (it contains
  page snapshots and audit).
- MCP HTTP key: `openssl rand -hex 32`, env/systemd drop-in 0600, never a
  URL, never git. Set `SIDEKICK_MCP_ALLOWED_ORIGINS` for browser-fronted
  setups; leave the default for local stdio only.
- Review `bridge/hub/audit.jsonl` (`perm_decisions`, `agent_blocked`) — the
  verdicts of your gates are auditable.
