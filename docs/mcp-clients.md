# Sidekick MCP — быстрые конфиги клиентов

Адаптер: `mcp/sidekick_mcp.py` (спека `docs/mcp-spec.md`). Ключ — плейсхолдер
`<MCP_KEY>`; сгенерируй свой: `openssl rand -hex 32` (env `SIDEKICK_MCP_KEY`,
в git ничего не попадает). Мост и хаб живут на машине Sidekick (host-a.local);
stdio-клиенты запускают адаптер локально там же, HTTP-клиенты идут на
`http://127.0.0.1:8796/mcp` (LAN/публичный — только осознанно, см. спеку п.5).

| Клиент | Транспорт | Конфиг |
|---|---|---|
| Hermes (на машине Sidekick) | stdio | `~/.hermes/config.yaml`, см. §1 |
| Claude Code (там же) | stdio | `claude mcp add …`, см. §2 |
| Claude Code (удалённо) | HTTP | `claude mcp add --transport http …`, см. §2 |
| Claude Desktop (macOS/Win/Linux) | stdio→HTTP через mcp-remote | §3 |
| Cursor | HTTP | `~/.cursor/mcp.json`, см. §4 |
| ChatGPT (developer mode) | HTTP по публичному HTTPS (cloudflared) | §5 |

## 1. Hermes (stdio)

`~/.hermes/config.yaml` (файл chmod 600 — ключ локальный; инвариант Hermes
«Secrets in .env, settings in config.yaml» — компромисс зафиксирован в спеке п.10.6):

```yaml
mcp_servers:
  sidekick:
    command: python3
    args: ["~/sidekick/mcp/sidekick_mcp.py"]
    env:
      SIDEKICK_MCP_KEY: "<MCP_KEY>"
    timeout: 620
```

`timeout` ≥ 615 с — таймаут `handoff_request` (мост ждёт человека дольше
обычного rpc-лимита). env-фильтр Hermes пропускает только перечисленное из
`env:` — stdio-адаптер без ключа отказывается стартовать. Перезапуск Hermes
обязателен. Тулы приходят с префиксом `mcp_sidekick_*`.

## 2. Claude Code

Локально (stdio, на машине с хабом):

```bash
claude mcp add sidekick -e SIDEKICK_MCP_KEY=<MCP_KEY> \
  -- python3 ~/sidekick/mcp/sidekick_mcp.py
```

Удалённо (HTTP через туннель/LAN):

```bash
claude mcp add --transport http sidekick https://mcp.tunnel.example/mcp \
  --header "Authorization: Bearer <MCP_KEY>"
```

Scopes: local (только этот проект) / project (`.mcp.json`, в git — БЕЗ ключа,
env-интерполяция) / user (весь пользователь). docs: code.claude.com/docs/en/mcp-quickstart.

## 3. Claude Desktop (прямой bearer не умеет → stdio-прокси mcp-remote)

Файл конфига: macOS `~/Library/Application Support/Claude/claude_desktop_config.json`,
Linux `~/.config/Claude/claude_desktop_config.json`, Windows `%APPDATA%\Claude\claude_desktop_config.json`.

```json
{
  "mcpServers": {
    "sidekick": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.tunnel.example/mcp",
               "--header", "Authorization: Bearer ${MCP_KEY}"],
      "env": {"MCP_KEY": "<MCP_KEY>"}
    }
  }
}
```

`mcp-remote` поддерживает `--header` и `${VAR}`-интерполяцию (npmjs.com/package/mcp-remote).
После правки — полный рестарт Claude Desktop (Cmd-Q), не «перезагрузка окна».

## 4. Cursor

`~/.cursor/mcp.json` (глобал) или `<проект>/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "sidekick": {
      "url": "https://mcp.tunnel.example/mcp",
      "headers": {"Authorization": "Bearer ${env:MCP_KEY}"}
    }
  }
}
```

LAN-вариант (доверенная домашняя сеть, демон с `SIDEKICK_MCP_BIND`):
`"url": "http://<host-machine-lan-ip>:8796/mcp"`. docs: cursor.com/docs/mcp.

## 5. ChatGPT (developer mode, публичный HTTPS обязателен)

1. Демон на машине Sidekick: `python3 mcp/sidekick_mcp.py --http --port 8796`
   (unit `mcp/sidekick-mcp-http.service`, ключ — env drop-in 0600).
2. cloudflared **named** tunnel (не quick-туннель — рандомный URL без контроля):

```yaml
# ~/.cloudflared/config.yml
tunnel: <tunnel-uuid>
credentials-file: ~/.cloudflared/<tunnel-uuid>.json
ingress:
  - hostname: mcp.<your-domain>
    service: http://127.0.0.1:8796
  - service: http_status:404
```

```bash
cloudflared tunnel create sidekick-mcp
cloudflared tunnel route dns sidekick-mcp mcp.<your-domain>
# systemd --user unit: cloudflared tunnel run sidekick-mcp
```

3. ChatGPT → Settings → Apps and Connectors → ➕ Developer → Add custom MCP
   server → URL `https://mcp.<your-domain>/mcp` → Authentication: **Bearer
   Token** `<MCP_KEY>` → создать.
4. Бейсик: Origin `https://chatgpt.com` уже в allowlist адаптера
   (`SIDEKICK_MCP_ORIGINS`), bearer проверяется на адаптере, rate-limit
   публичного режима (`SIDEKICK_MCP_PUBLIC=1`) — 10 вызовов/мин.

Ограничения планов (help.openai.com/articles/12584461): полный MCP —
Business/Enterprise/Edu; Pro — read/fetch; список тулов — frozen snapshot
(«Refresh metadata» после смены версии адаптера); локальный сервер напрямую
нельзя — «use Secure MCP Tunnel». Долгие `handoff_request` (615 с) через
HTTP-гейты ChatGPT обрезаются клиентским таймаутом — для интерактивных
human-gated сценариев предпочтительны stdio-клиенты (спека п.10.3).

## Заметки

- stdio: `python3` (stdlib, никаких pip/npm-зависимостей в продукт).
- Ключ ротации: новый `SIDEKICK_MCP_KEY` → env демона + `systemctl --user
  restart sidekick-mcp-http` + конфиг клиента; HTTP-сессии умирают (401),
  stdio-клиенты перезапускаются.
- Тесты стенда: изолированный мост `:8797` (AGENTS п.8); прод `:8791` конфиги
  клиентов не трогают — адаптер говорит с хаб-файлами, не к ws-порту.
- Расширение и мост: установка — README/AGENTS (about:debugging, поле «Сервер»).
