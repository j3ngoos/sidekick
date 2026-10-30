#!/usr/bin/env bash
# Установка sidekick-caption (SK-36) — юнит малой CPU-модели подписей шагов.
#
#   scripts/install-caption.sh
#
# Делает:
#   1) проверяет модель Qwen2.5-1.5B-Instruct Q4_K_M (путь SIDEKICK_CAPTION_MODEL,
#      дефолт ~/models/caption/qwen2.5-1.5b-instruct-q4_k_m.gguf); нет — печатает
#      команду скачивания и выходит с ошибкой (сеть в инсталл-скриптах не трогаем)
#   2) юнит ~/.config/systemd/user/sidekick-caption.service (подстановка путей)
#   3) daemon-reload + enable --now + health-check /v1/models
#
# Агентд обращается к http://127.0.0.1:8087 (SIDEKICK_CAPTION_URL); юнит лёгкий
# (~1.2 ГБ RAM, CPU, Nice=10) и не мешает ни основной модели, ни knet-эмбеддингу.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUB="${SIDEKICK_HOME:-$HOME/sidekick/bridge/hub}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/sidekick-caption.service"
LLAMA="${SIDEKICK_CAPTION_LLAMA:-$HOME/src/llama.cpp/build/bin/llama-server}"
MODEL="${SIDEKICK_CAPTION_MODEL:-$HOME/models/caption/qwen2.5-1.5b-instruct-q4_k_m.gguf}"

echo "== sidekick-caption install =="
echo "llama=$LLAMA model=$MODEL"

[ -x "$LLAMA" ] || { echo "llama-server не найден: $LLAMA (SIDEKICK_CAPTION_LLAMA=...)" >&2; exit 1; }
if [ ! -f "$MODEL" ]; then
  echo "модель не найдена: $MODEL" >&2
  echo "скачай: curl -L -C - -o '$MODEL' 'https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf'" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"
sed -e "s|@LLAMA@|$LLAMA|g" -e "s|%MODEL%|$MODEL|g" -e "s|%HOME%|$HOME|g" -e "s|%HUB%|$HUB|g" \
  "$REPO/agentd/sidekick-caption.service" > "$UNIT"
echo "юнит: $UNIT"

systemctl --user daemon-reload
systemctl --user enable --now sidekick-caption.service
systemctl --user restart sidekick-caption.service
# модель грузится секунды — ждём готовности до 60 с
for _ in $(seq 1 60); do
  curl -sf -m 2 http://127.0.0.1:8087/v1/models >/dev/null 2>&1 && break
  sleep 1
done
curl -sf -m 3 http://127.0.0.1:8087/v1/models >/dev/null \
  || { echo "caption-сервер не поднялся: journalctl --user -u sidekick-caption -n" >&2; exit 1; }
echo "готово: http://127.0.0.1:8087 (alias sidekick-caption), лог: $HUB/caption.log"
