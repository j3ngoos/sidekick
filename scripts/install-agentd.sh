#!/usr/bin/env bash
# Установка sidekick-agentd (SK-21) — идемпотентно.
#
#   scripts/install-agentd.sh
#
# Делает:
#   1) юнит ~/.config/systemd/user/sidekick-agentd.service (подстановка путей)
#   2) плагин sidekick-think: симлинк в плагины профиля sidekick +
#      plugins.enabled/plugins.stream_reasoning_deltas в конфиге профиля
#      (значения сливаются с существующими, не затирая другие плагины)
#   3) systemctl --user daemon-reload + enable --now + демонстрация статуса
#
# НЕ трогает cron sidekick-agent (07966c5f447d): снятие его с дежурства —
# решение приёмки (карточка SK-21, п.4).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUB="${SIDEKICK_HOME:-$HOME/sidekick/bridge/hub}"
PROFILE="${SIDEKICK_AGENTD_PROFILE:-sidekick}"
STATE="${SIDEKICK_AGENTD_STATE:-$HOME/.hermes/cache/sidekick-agentd}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/sidekick-agentd.service"
PY="${SIDEKICK_AGENTD_PY:-$(command -v python3)}"
HERMES="${SIDEKICK_HERMES_BIN:-$(command -v hermes || echo "$HOME/.local/bin/hermes")}"

echo "== sidekick-agentd install =="
echo "repo=$REPO hub=$HUB profile=$PROFILE python=$PY"

# 1. юнит ---------------------------------------------------------------------
mkdir -p "$UNIT_DIR" "$STATE/run"
sed -e "s|@PYTHON@|$PY|g" -e "s|%REPO%|$REPO|g" -e "s|%HOME%|$HOME|g" -e "s|%HUB%|$HUB|g" \
  "$REPO/agentd/sidekick-agentd.service" > "$UNIT"
echo "юнит: $UNIT"

# 2. плагин thinking ------------------------------------------------------------
PLUG_DIR="$HOME/.hermes/profiles/$PROFILE/plugins"
mkdir -p "$PLUG_DIR"
ln -sfn "$REPO/agentd/think_plugin" "$PLUG_DIR/sidekick-think"
echo "плагин: $PLUG_DIR/sidekick-think -> $REPO/agentd/think_plugin"

# plugins.enabled — список с сохранением уже включённых
CURRENT="$("$HERMES" -p "$PROFILE" config get plugins.enabled 2>/dev/null || true)"
ENABLED="$(python3 - "$CURRENT" <<'EOF'
import ast, json, sys
raw = sys.argv[1].strip()
try:
    cur = ast.literal_eval(raw) if raw and raw != "None" else []
except (ValueError, SyntaxError):
    cur = []
if not isinstance(cur, list):
    cur = []
if "sidekick-think" not in cur:
    cur = cur + ["sidekick-think"]
print(json.dumps(cur))
EOF
)"
"$HERMES" -p "$PROFILE" config set plugins.enabled "$ENABLED" >/dev/null
"$HERMES" -p "$PROFILE" config set plugins.stream_reasoning_deltas true >/dev/null
echo "конфиг профиля: plugins.enabled=$ENABLED stream_reasoning_deltas=true"

# 3. systemd --------------------------------------------------------------------
systemctl --user daemon-reload
systemctl --user enable --now sidekick-agentd.service
systemctl --user restart sidekick-agentd.service
sleep 1
systemctl --user --no-pager --full status sidekick-agentd.service | sed -n '1,4p' || true
echo "лог: $HUB/agentd.log"
echo "готово. Проверка: journalctl --user -u sidekick-agentd -n; хаб: $HUB"
