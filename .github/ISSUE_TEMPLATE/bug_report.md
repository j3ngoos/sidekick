---
name: Bug report
about: Something misbehaves (bridge, extension, agentd, MCP adapter)
title: "[BUG] "
labels: bug
assignees: ''
---

<!-- Never paste real secrets: bridge token, MCP key, cookies, auth headers.
     Redact; synthetic reproductions are preferred. See SECURITY.md. -->

**Component** (check one):
- [ ] bridge (`bridge/sidekick_bridge.py`)
- [ ] extension (`extension/`)
- [ ] agentd (`agentd/sidekick_agentd.py`)
- [ ] MCP adapter / client configs
- [ ] gallery/docs

**Environment**
- Firefox version / OS:
- Bridge host OS / Python version:
- Mode: tunnel (empty Server field) / LAN (`ws://<your-host>:8791`) / stdio MCP / HTTP MCP:

**Describe the bug**


**Steps to reproduce**
1.

**Expected behavior**


**Actual behavior + evidence**
<!-- `sk status`, relevant lines from bridge/hub/events.jsonl, bridge/hub/health.json,
     extension console (about:debugging → Inspect), screenshots. Redact personal data. -->

**Impact on the safety model?** (gate bypass / audit gap / token exposure / none)
