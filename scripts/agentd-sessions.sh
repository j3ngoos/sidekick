#!/usr/bin/env bash
# agentd-sessions.sh — история сессий agentd для человека (SK-29).
#
# Показывает именованные сессии профиля sidekick (sidekick/<chat_id> — текущая
# сессия чата; «sidekick/<chat_id> · дата» — закрытые задачи того же чата):
# имя, даты, число сообщений, первая реплика (без конверта агентда).
# Только чтение: state.db открывается mode=ro, ничего не пишет.
#
# Использование:
#   agentd-sessions.sh              живые сессии sidekick/* (свежие сверху)
#   agentd-sessions.sh --archived   вместе с архивными (ротация не удаляет молча)
#   agentd-sessions.sh --check      согласованность hub/sessions.json (карта
#                                   агентда chat→sid) и state.db: тот же sid, то
#                                   же имя. Выход: 0 согласовано | 1 есть
#                                   расхождения | 2 db/файлы недоступны
#
# Продолжить диалог из CLI:  hermes -p sidekick --resume '<имя>'
# env: SIDEKICK_AGENTD_PROFILE (sidekick), SIDEKICK_STATE_DB, SIDEKICK_SESSIONS_JSON,
#      HERMES_HOME, SIDEKICK_HOME (hub — для sessions.json).
set -euo pipefail

MODE="list"
case "${1:-}" in
  "") ;;
  --archived) MODE="archived" ;;
  --check) MODE="check" ;;
  -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "неизвестный флаг: $1 (есть --archived, --check)" >&2; exit 2 ;;
esac

MODE="$MODE" python3 - <<'PY'
import json, os, sqlite3, sys, time

mode = os.environ["MODE"]
profile = os.environ.get("SIDEKICK_AGENTD_PROFILE") or "sidekick"
home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
prof_home = home if profile in ("", "default") else os.path.join(home, "profiles", profile)
db_path = os.environ.get("SIDEKICK_STATE_DB") or os.path.join(prof_home, "state.db")
hub = os.environ.get("SIDEKICK_HOME") or os.path.expanduser("~/sidekick/bridge/hub")
sessions_json = os.environ.get("SIDEKICK_SESSIONS_JSON") or f"{hub}/sessions.json"
PREFIX = "sidekick/"


def die(msg, rc=2):
    print(msg, file=sys.stderr)
    sys.exit(rc)


def strip_envelope(text):
    """Конверт агентда («[sidekick-agentd task=…] …\n---\n<тело>») → тело."""
    t = str(text or "")
    head, sep, rest = t.partition("\n---\n")
    return rest if sep else t


def fmt(ts):
    try:
        return time.strftime("%d.%m %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


if not os.path.exists(db_path):
    die(f"state.db не найден: {db_path} (профиль {profile})")
try:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    q = ("SELECT id, title, started_at, COALESCE(last_activity_at, started_at), "
         "COALESCE(message_count,0), COALESCE(archived,0) FROM sessions "
         "WHERE title LIKE ?")
    if mode != "archived":
        q += " AND COALESCE(archived,0)=0"
    q += " ORDER BY started_at DESC"
    rows = db.execute(q, (PREFIX + "%",)).fetchall()
except sqlite3.Error as e:
    die(f"state.db не читается: {e}")

if mode == "check":
    # карта агентда ↔ заголовки в db: тот же chat_id, тот же sid, то же имя
    try:
        with open(sessions_json, encoding="utf-8") as fh:
            smap = json.load(fh)
        if not isinstance(smap, dict):
            smap = {}
    except (OSError, json.JSONDecodeError):
        smap = {}
    by_id = {r[0]: r for r in db.execute(
        "SELECT id, title, COALESCE(archived,0) FROM sessions WHERE title LIKE ?",
        (PREFIX + "%",))}
    problems = 0
    for chat, ent in sorted(smap.items()):
        sid = ent.get("sid") if isinstance(ent, dict) else None
        want = PREFIX + chat
        if not sid or sid not in by_id:
            print(f"MISMATCH чат {chat}: sid {sid or '—'} нет среди sidekick/* в state.db")
            problems += 1
        elif by_id[sid][1] != want:
            print(f"MISMATCH чат {chat}: сессия {sid} озаглавлена «{by_id[sid][1]}», "
                  f"ожидалось «{want}»")
            problems += 1
        elif by_id[sid][2]:
            print(f"MISMATCH чат {chat}: текущая сессия {sid} в архиве (ротация не должна)")
            problems += 1
    live_bare = {r[0]: r[1] for r in db.execute(
        "SELECT id, title FROM sessions WHERE title LIKE ? "
        "AND COALESCE(archived,0)=0 AND title NOT LIKE ?", (PREFIX + "%", "% · %"))}
    mapped = {e.get("sid") for e in smap.values() if isinstance(e, dict)}
    for sid, title in sorted(live_bare.items()):
        if sid not in mapped:
            print(f"ORPHAN сессия {sid} «{title}» — живой чат в db, но её нет в "
                  f"{sessions_json}")
            problems += 1
    if problems:
        print(f"SK-29 --check: РАСХОЖДЕНИЙ {problems}")
        sys.exit(1)
    print(f"SK-29 --check: согласовано (карта {len(smap)} чат(ов), "
          f"живых голоименных {len(live_bare)})")
    sys.exit(0)

if not rows:
    print(f"сессион sidekick/* пока нет (профиль {profile}, {db_path})")
    sys.exit(0)

print(f"{'ИМЯ сессии (resume-имя)':<44} {'начало':<12} {'послед.':<12} "
      f"{'сообщ.':>6}  {'арх':>3}  первая реплика")
print("─" * 118)
for sid, title, started, last, msgs, arch in rows:
    first = db.execute(
        "SELECT COALESCE(api_content, content) FROM messages WHERE session_id=? "
        "AND role='user' AND COALESCE(active,1)=1 "
        "AND COALESCE(_compressed_summary,0)=0 ORDER BY id LIMIT 1", (sid,)).fetchone()
    reply = " ".join(strip_envelope(first[0] if first else "").split())
    if len(reply) > 60:
        reply = reply[:59] + "…"
    flag = "арх" if arch else ""
    print(f"{title[:44]:<44} {fmt(started):<12} {fmt(last):<12} {msgs:>6}  "
          f"{flag:>3}  {reply}")
print(f"─ всего строк: {len(rows)} (живые скрыты без --archived; "
      f"продолжить: hermes -p {profile} --resume '<имя>')")
PY
