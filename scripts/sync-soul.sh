#!/usr/bin/env bash
# sync-soul.sh — перегенерация блока контракта в SOUL.md sidekick-профиля из
# ~/sidekick/AGENTS.md (SK-20). Блок лежит между маркерами SK20-CONTRACT; остальной
# SOUL — рукописный, скрипт его не трогает. Идемпотентен: при отсутствии изменений
# не пишет. Использование: sync-soul.sh [--check]. Env SK20_AGENTS / SK20_SOUL —
# переопределение путей (для тестов на копиях).
set -euo pipefail
AGENTS="${SK20_AGENTS:-$HOME/sidekick/AGENTS.md}"
SOUL="${SK20_SOUL:-$HOME/.hermes/profiles/sidekick/SOUL.md}"
MODE="write"
case "${1:-}" in
  "") ;;
  --check) MODE="check" ;;
  *) echo "неизвестный флаг: $1 (есть только --check)" >&2; exit 2 ;;
esac
[ -f "$AGENTS" ] || { echo "AGENTS.md не найден: $AGENTS" >&2; exit 2; }
[ -f "$SOUL" ]   || { echo "SOUL.md не найден: $SOUL" >&2; exit 2; }

AGENTS="$AGENTS" SOUL="$SOUL" MODE="$MODE" python3 <<'PY'
import os, sys, difflib

agents_path = os.environ["AGENTS"]
soul_path = os.environ["SOUL"]
mode = os.environ["MODE"]

BEGIN = "<!-- SK20-CONTRACT:BEGIN (generated from ~/sidekick/AGENTS.md by scripts/sync-soul.sh — не править руками) -->"
END = "<!-- SK20-CONTRACT:END -->"

soul = open(soul_path, encoding="utf-8").read()
if soul.count(BEGIN) != 1 or soul.count(END) != 1:
    sys.exit("в SOUL.md должен быть ровно один блок маркеров SK20-CONTRACT — "
             "не правь маркеры руками, восстановите секцию из эталона")

head, rest = soul.split(BEGIN, 1)
_old_body, tail = rest.split(END, 1)

agents = open(agents_path, encoding="utf-8").read().rstrip("\n")
new_block = ("\n## Контракт (сгенерировано из ~/sidekick/AGENTS.md — "
             "не редактировать руками)\n\n" + agents + "\n\n")
new_soul = head + BEGIN + new_block + END + tail

if new_soul == soul:
    print("актуально: блок контракта SOUL == AGENTS.md (идемпотентно, ничего не пишу)")
    sys.exit(0)

sys.stdout.writelines(difflib.unified_diff(
    soul.splitlines(keepends=True), new_soul.splitlines(keepends=True),
    fromfile=soul_path + " (сейчас)", tofile=soul_path + " (после синка)"))

if mode == "check":
    print("\n--check: контракт устарел (см. diff выше); запусти без --check для обновления")
    sys.exit(1)

tmp = soul_path + ".sync-soul.tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(new_soul)
os.replace(tmp, soul_path)
print(f"\nзаписан {soul_path} ({len(new_soul)} байт)")
PY
