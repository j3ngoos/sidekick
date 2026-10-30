#!/usr/bin/env python3
"""sidekick-agentd — резидент живого агента сайдбара (SK-21).

Заменяет минутный cron-опрос (job sidekick-agent): слушает hub-файлы моста
(поллинг 0.2 с), на новое сообщение человека запускает

    hermes -p sidekick chat --format stream-json [--resume <sid>] --query-file -

и переводит его stdout-события в кадры hub/outbox.jsonl:
  * {"v":1,"type":"task",...} / {"v":1,"type":"step",...} — существующие типы
    контракта v1 (шаги — из tool_use/tool_result; первый кадр — сразу, ≤5 с);
  * {"v":1,"type":"think","task":...,"text":...} — аддитивный тип: дельты
    reasoning из плагина sidekick-think (файл из env SIDEKICK_THINK_FILE);
  * {"v":1,"type":"chat","role":"agent","task":...,"text":...} — финал из
    result-события.
  * каждый кадр несёт аддитивное поле chat (SK-23): по нему мост пишет зеркало
    беседы в hub/chats.jsonl, а сайдбар фильтрует ленту под просматриваемый чат.

Ленту человека ведёт ЕДИНСТВЕННЫЙ писатель — агентд; потомок явным конвертом
в сообщении освобождён от записи outbox-кадров (RPC-контракт, perm.check,
action.do, handoff.request и hub/stop — в силе).

Устойчивость:
  * курсор inbox сохраняется только после закрытия задачи — падение демона не
    «съедает» сообщение (после рестарта оно перечитывается и обрабатывается);
    при параллельных задачах коммитится граница ДО самого раннего незакрытого
    сообщения (очередь + активные) — ни один чат не теряется;
  * hub/sessions.json — карта chat → hermes-session (атомарная запись, битый
    файл = пусто); битый resume ретраится один раз без сессии;
  * hub/stop → SIGTERM группе КАЖДОГО живого потомка (тот печатает result rc=130)
    + шаг failed; hub/stop при этом потребляется агентдом;
  * SK-25 п.3: до MAX_ACTIVE задач параллельно (минимум: одна фоновая + одна во
    фронте), не более одной на чат — сообщение чата с живой задачей становится в
    очередь этого чата («сообщения из чата направляют активной задаче»); у каждой
    задачи своя лента шагов (task id в кадрах разделяет их на стороне моста);
  * SK-28: браузерный канал (health.json моста: bridge жив + extension connected) —
    пауза для задач. Офлайн дольше EXT_PAUSE_AFTER_S: потомка до SIGTERM (не долбить
    rpc_in штормом no_extension — 354 ошибки за 10 ч, аудит 21.10), задача в
    «paused» с подсказкой человеку («открой Firefox / подними мост»), новые задачи
    из очереди не стартуют. Канал ожил — авто-resume (sessions.json даёт контекст,
    человек ничего не пересылает). Пауза НЕ коммитит курсор — падение агентда с
    приостановленной задачей перечитывает её сообщение заново;
  * SK-45: watchdog зависания хода — для running-задачи трекатся момент последнего
    вывода потомка (любая строка stdout-стрима И think-дельты) и последнего rpc
    этой задачи; оба молчат дольше WATCHDOG_S (180 с) — зависание (аудит 22.10
    п.3: sk79745 «думала» 10 минут без единого rpc/stdout, умерла только от
    ручного Stop): SIGTERM потомку (не умер — SIGKILL после grace, переживает и
    SIGSTOP), задача в «paused» с явной строкой в чат «задача зависла (3 мин
    тишины) — остановил, вот что было последним: …»; резюмится репликой человека
    (путь idle-resume). Ожидание человека (hub/paused, висячий вердикт гейта) —
    не зависание; SIDEKICK_AGENTD_WATCHDOG_S=0 выключает надзор;
  * SK-45: гонка переименования сессий решена честно (аудит 22.10 п.9: rename
    успевал до коммита строки сессии hermes — rc=1 «No session» на каждой новой,
    именование SK-29 держалось на случайных ретраях): вместо слепых ударов —
    ожидание видимости строки в state.db по чекпоинтам 0/0.5/1.5/3.5/7.5/10 с
    (замер живой гонки: init→строка ≈ 1.4 с), затем rename — rc=0. Исчерпание —
    одна тихая пометка в journal, в чат ничего не пишется (автозаголовок жив,
    следующий rename перетрёт);
  * SK-29: именованные сессии профиля — текущая сессия чата всегда озаглавлена
    sidekick/<chat_id> (её видно в `hermes -p sidekick sessions list`, её продолжает
    `--resume sidekick/<chat_id>`), закрытая уходит в историю «sidekick/<chat> · дата».
    Гигиена контекста: новая ЗАДАЧА (окно диалога WINDOW_S просрочено) — новая сессия,
    фоллоу-апы внутри окна — resume той же. Idle-close: без stdout/rpc/чата IDLE_S —
    SIGTERM потомку и задача в paused (ожидание человека — handoff/гейт — не трогает);
    следующая реплика возобновляет или abandonит с новой задачей. Ротация: живых
    sidekick/* не более SESSION_KEEP, старейшие исторические — archive (soft-hide).

hub-файлы — общая территория: inbox/events только дочитываются, outbox только
дописывается (O_APPEND); sessions.json и агентд-курсор — собственные файлы
агентда (атомарная замена tmp+rename).

env: SIDEKICK_HOME (hub; по умолчанию ~/sidekick/bridge/hub),
     SIDEKICK_AGENTD_STATE (состояние; ~/.hermes/cache/sidekick-agentd),
     SIDEKICK_HERMES_BIN, SIDEKICK_AGENTD_PROFILE (по умолчанию sidekick),
     SIDEKICK_AGENTD_MAX (параллельных задач, по умолчанию 2),
     SIDEKICK_AGENTD_WINDOW_S (окно диалога, 1800), SIDEKICK_AGENTD_IDLE_S (idle-close,
     600), SIDEKICK_AGENTD_KEEP (ротация сессий, 30), SIDEKICK_AGENTD_WATCHDOG_S
     (watchdog зависания, 180; 0 — выключен), SIDEKICK_AGENTD_RENAME_BACKOFF_S
     (чекпоинты видимости rename, «0.5,1,2,4»), SIDEKICK_AGENTD_RENAME_MAX_S
     (бюджет rename, 10), SIDEKICK_STATE_DB / HERMES_HOME
     (путь state.db профиля для ротации; по умолчанию ~/.hermes/profiles/<p>/state.db),
     SIDEKICK_CAPTION_URL (SK-36: OpenAI-compat endpoint малой CPU-модели подписей;
     "" — выключить), SIDEKICK_CAPTION_BUDGET_MS (бюджет уточнения, 500),
     SIDEKICK_CAPTION_DISABLE=1 (полный выключатель), SIDEKICK_CAPTION_MODEL (alias).
"""
from __future__ import annotations

import collections
import concurrent.futures
import fcntl
import glob as globmod
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

POLL_S = 0.2
THINK_FLUSH_S = 0.4      # think-кадр не чаще; дельты копятся максимум столько
THINK_MAX_CHUNK = 400    # и не длиннее этого
STEP_TEXT_MAX = 90
TITLE_MAX = 60
CHAT_MAX = 4000
STOP_KILL_GRACE_S = 5.0  # SIGTERM -> SIGKILL потомку, если не умер
OLD_CHILD_GRACE_S = 10.0 # прошлый потомок после своего result обязан умереть сам
RUN_KEEP = 20            # хвост файлов stderr потомков в run/
MAX_ACTIVE = max(1, int(os.environ.get("SIDEKICK_AGENTD_MAX", "2")))  # SK-25 п.3
# SK-28: надзор за браузерным каналом по hub/health.json (пишет мост, SK-33 контракт:
# connect/disconnect + тик 10 с; «мост жив» = now - ts <= 30; ws_count — НЕ сигнал).
HEALTH_READ_S = 3.0      # свой тик чтения health (контракт SK-33: не чаще 2–5 с)
HEALTH_FRESH_S = 30.0    # порог свежести health (тот же контракт)
EXT_PAUSE_AFTER_S = 8.0  # подтверждение офлайна: блипы и короткие рестарты не дёргаем
CHAN_HINT = {
    "ext_offline": "⏸ Браузер офлайн: открой Firefox и загрузи расширение Sidekick — "
                   "задача продолжится автоматически",
    "bridge_down": "⏸ Мост не отвечает: sidekick-bridge лежит, задача ждёт восстановления "
                   "канала (sk status покажет причину)",
}

# SK-29: именованные сессии, гигиена контекста, idle-close, ротация.
# Окно диалога: ре-юз сессии чата, пока последняя активность не старше окна; дальше
# новая ЗАДАЧА получает новую сессию (аудит 21.10: 82 сообщения, 15 задач в одном
# контексте). Env-переопределения — для приёмки (окно/простой в секундах).
WINDOW_S = float(os.environ.get("SIDEKICK_AGENTD_WINDOW_S", "1800"))
IDLE_S = float(os.environ.get("SIDEKICK_AGENTD_IDLE_S", "600"))
SESSION_KEEP = int(os.environ.get("SIDEKICK_AGENTD_KEEP", "30"))
# SK-45: watchdog зависания хода (аудит 22.10 п.3). Порог — не «много времени»,
# а «оба канала молчат»: у штатного хода либо стримит stdout/think, либо идёт rpc.
# Долгие инструменты (>порога) — осознанный риск спеки; порог крутится env-ом
# (приёмка гоняет на маленьком, прод — 180).
WATCHDOG_S = float(os.environ.get("SIDEKICK_AGENTD_WATCHDOG_S", "180"))
# SK-45: чекпоинты ожидания видимости строки сессии в state.db перед rename
# (сумма шагов 7.5 с, последний чекпоинт — ровно бюджет 10 с; замер гонки
# 23.10: init→строка ≈ 1.4 с).
RENAME_BACKOFF_S = [float(x) for x in
                    os.environ.get("SIDEKICK_AGENTD_RENAME_BACKOFF_S", "0.5,1,2,4")
                    .split(",") if x.strip()]
RENAME_MAX_WAIT_S = float(os.environ.get("SIDEKICK_AGENTD_RENAME_MAX_S", "10"))
SESS_PREFIX = "sidekick/"
HIST_SEP = " · "  # разделитель исторического имени: «sidekick/<chat> · <дата>»

HOME_DIR = os.path.expanduser("~")
HUB = os.environ.get("SIDEKICK_HOME") or os.path.join(HOME_DIR, "sidekick/bridge/hub")
STATE = os.environ.get("SIDEKICK_AGENTD_STATE") or os.path.join(
    HOME_DIR, ".hermes/cache/sidekick-agentd")
RUN = os.path.join(STATE, "run")
INBOX, EVENTS, OUTBOX = f"{HUB}/inbox.jsonl", f"{HUB}/events.jsonl", f"{HUB}/outbox.jsonl"
STOP = f"{HUB}/stop"
HEALTH_F = f"{HUB}/health.json"  # SK-28: канал-надзор (писатель — мост)
PAUSED_F = f"{HUB}/paused"       # SK-08 handoff-флаг: рука у человека — idle не трогает
RPC_IN_F, RPC_OUT_F = f"{HUB}/rpc_in.jsonl", f"{HUB}/rpc_out.jsonl"  # SK-29: висячие вердикты
SESSIONS = f"{HUB}/sessions.json"
CURSOR_F = f"{STATE}/cursor.json"
LOCK_F = f"{STATE}/agentd.lock"
PROFILE = os.environ.get("SIDEKICK_AGENTD_PROFILE") or "sidekick"
# state.db профиля — источник заголовков для ротации (читаем mode=ro, не пишем)
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.join(HOME_DIR, ".hermes")
_PROFILE_HOME = HERMES_HOME if PROFILE in ("", "default") \
    else os.path.join(HERMES_HOME, "profiles", PROFILE)
STATE_DB = os.environ.get("SIDEKICK_STATE_DB") or os.path.join(_PROFILE_HOME, "state.db")


def hermes_bin() -> str:
    return (os.environ.get("SIDEKICK_HERMES_BIN")
            or shutil.which("hermes")
            or os.path.join(HOME_DIR, ".local/bin/hermes"))


def log(*a) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "agentd:", *a, flush=True)


def clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------- SK-28: канал -------

def channel_state(health, now: float) -> str:
    """Снимок health.json -> состояние браузерного канала (ЧИСТАЯ функция, тест).

    Правило свежести (now - ts <= HEALTH_FRESH_S) — контракт SK-33; ws_count —
    НЕ сигнал (его не читаем намеренно). ext_offline = мост жив, расширения нет;
    bridge_down = мост молчит/протух/файл битый (отличить «нет файла» от «мост
    умер» по одному health нельзя — и не нужно: действие одно)."""
    if not isinstance(health, dict):
        return "bridge_down"
    ts = health.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or (now - ts) > HEALTH_FRESH_S:
        return "bridge_down"
    return "ext_online" if health.get("extension") == "connected" else "ext_offline"


def read_health(path: str = HEALTH_F):
    """health.json или None (нет файла / битый JSON — читатель не падает)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------- SK-29: sessions ----

def session_title(chat: str) -> str:
    """Имя ТЕКУЩЕЙ сессии чата: sidekick/<chat_id> (SK-29). Chat id приходит с
    моста/сайдбара — санитизация страховочная (холостые символы, длина)."""
    c = " ".join(str(chat or "default").split())
    return SESS_PREFIX + (c[:64] or "default")


def history_title(chat: str, ts: float) -> str:
    """Имя ЗАКРЫТОЙ сессии чата: sidekick/<chat_id> · <ГГГГ-ММ-ДД ЧЧ:ММ:СС>.
    Уникальность — по секунде; задачи одного чата сериализованы (одна на чат)."""
    return session_title(chat) + HIST_SEP + time.strftime(
        "%Y-%m-%d %H:%M:%S", time.localtime(ts))


def new_session_needed(entry, now: float, window_s=None) -> bool:
    """Граница гигиены (SK-29): новая ЗАДАЧА — новая сессия. Ре-юз только внутри
    окна диалога (последняя активность в чате): фоллоу-апы остаются в контексте,
    а несвязанные задачи через окно не смешиваются в один контекст (аудит 21.10:
    82 сообщения, ≥15 задач). Пауза задачи — НЕ просрочка окна: живую/paused
    задачу вызывает не этот предикат, а пути resume (SK-28 / idle)."""
    if not entry or not entry.get("sid"):
        return True
    w = WINDOW_S if window_s is None else window_s
    try:
        upd = float(entry.get("updated"))
    except (TypeError, ValueError):
        return True  # нет/мусор в updated — консервативно новая (не тащить контекст вслепую)
    return (now - upd) > w


def pick_archive_targets(rows, keep=None) -> list:
    """Ротация (SK-29 п.5): если живых sidekick/* больше keep — в архив старейшие
    ИСТОРИЧЕСкие (с « · »). Голоименные ТЕКУЩИЕ не архивятся никогда: текущая
    сессия чата — его точка resume и лицо в `sessions list`; если перебор дали
    только текущие — оставляем как есть (в лог, не удаляем молча).
    rows: [{id,title,started_at,archived}] → строки-кандидаты, старые первыми."""
    k = SESSION_KEEP if keep is None else keep
    live = [r for r in rows
            if str(r.get("title") or "").startswith(SESS_PREFIX) and not r.get("archived")]
    if len(live) <= k:
        return []
    hist = sorted((r for r in live if HIST_SEP in str(r.get("title") or "")),
                  key=lambda r: r.get("started_at") or 0)
    return hist[:len(live) - k]


def strip_envelope(text: str) -> str:
    """Конверт агентда («[sidekick-agentd task=…] …\\n---\\n<тело>») → тело.
    Человек в списке сессий должен видеть свою реплику, а не служебный текст."""
    t = str(text or "")
    head, sep, rest = t.partition("\n---\n")
    return rest if sep else t


def idle_expired(last_event: float, now: float, blocked: bool, idle_s=None) -> bool:
    """Idle-close (SK-29 аддендум): без stdout-событий потомка, rpc задачи и
    сообщений чата дольше IDLE_S — задача зависла, пауза. blocked = ждём ЧЕЛОВЕКА
    (hub/paused handoff или незакрытый rpc-вердикт гейта) — это не зависание."""
    s = IDLE_S if idle_s is None else idle_s
    return (not blocked) and (now - last_event) > s


def hang_expired(last_out: float, last_rpc: float, now: float, blocked: bool,
                 watchdog_s=None) -> bool:
    """Watchdog зависания хода (SK-45, аудит 22.10 п.3): МОЛЧАТ ОБА канала жизни
    задачи дольше WATCHDOG_S — и stdout-стрим потомка (включая think-дельты:
    модель жива, просто думает — это вывод), и rpc этой задачи. Отличие от
    idle_expired: часы зависания НЕ сбрасываются репликой человека (спека: «если
    оба молчат») и порог втрое короче. blocked = ждём человека (hub/paused или
    висячий вердикт гейта) — не зависание, та же экзепция, что у idle.
    watchdog_s <= 0 — надзор выключен."""
    s = WATCHDOG_S if watchdog_s is None else watchdog_s
    if s <= 0:
        return False
    return (not blocked) and (now - max(last_out, last_rpc)) > s


def hang_text(last_seen: str, watchdog_s=None) -> str:
    """Явная строка в чат при зависании (SK-45): не тишина, а «остановил, вот что
    было последним». Человек резюмит репликой (путь idle_paused, SK-29)."""
    s = WATCHDOG_S if watchdog_s is None else watchdog_s
    span = f"{int(s // 60)} мин" if s >= 60 else f"{int(s)} с"
    return (f"⏸ Задача зависла ({span} тишины) — остановил, вот что было "
            f"последним: {clip(last_seen or 'ничего (потомок молчал с самого старта)', 120)}. "
            "Напиши реплику — продолжу сессию (или начну новую задачу)")


def perm_timeout_target(ev, actives) -> str | None:
    """SK-34 п.3: событие perm_timeout (с мостового счётчика молчаливых
    ask_timeout) → tid задачи, которую надо ставить на паузу, или None.

    Пауза — со второго подряд таймаута ОДНОГО пермиса: первый ещё может быть
    случайностью (человек отошёл на минуту), второй — уже допрос с пристрастием.
    Атрибуция: task из params (контракт AGENTS.md п.2 — агент кладёт task в
    perm.check); если задачи в событии нет (старый контракт/ручной вызов),
    допускаем единственную активную задачу — при нескольких угадывать нельзя."""
    try:
        streak = int(ev.get("streak") or 1)
    except (TypeError, ValueError):
        streak = 1
    if streak < 2:
        return None
    t = str(ev.get("task") or "")
    if t:
        return t if t in actives else None
    if len(actives) == 1:
        return next(iter(actives))
    return None


def perm_pause_text(ev) -> str:
    """Явный вопрос в чат вместо восьмого подряд карточного вопроса (аудит 21.10
    п.8: задача умирала после 8 минут переспрашиваний)."""
    dom = str(ev.get("domain") or "?")
    act = str(ev.get("action") or "?")
    return (f"⏸ Вопрос «{act}» на домене «{dom}» повис без ответа дважды — "
            "ставлю задачу на паузу и больше его не повторяю. Напиши в чат: "
            "разрешаю (что именно), отказываю, или что делать вместо этого.")


def child_cmd(sid) -> list:
    """Команда потомка: resume по sid — только когда решение «продолжить сессию»
    уже принято (окно/пауза); новая задача стартует БЕЗ resume и получает имя
    после init (name_new_session)."""
    cmd = [hermes_bin(), "-p", PROFILE, "chat", "--format", "stream-json"]
    if sid:
        cmd += ["--resume", sid]
    cmd += ["--query-file", "-"]
    return cmd


def _cli_sync(*args, timeout: float = 30.0, quiet: bool = False) -> int:
    """hermes CLI синхронно (для последовательных цепочек в воркере-потоке)."""
    try:
        r = subprocess.run([hermes_bin(), "-p", PROFILE, *[str(a) for a in args]],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 and not quiet:
            log(f"cli `{' '.join(str(a) for a in args[:3])}…` rc={r.returncode}: "
                f"{clip((r.stderr or r.stdout or '').strip(), 160)}")
        return r.returncode
    except (OSError, subprocess.TimeoutExpired) as e:
        if not quiet:
            log(f"cli err `{' '.join(str(a) for a in args[:3])}…`: {e}")
        return -1


def _cli(*args) -> None:
    """hermes CLI в фоне (rename/archive не должны блокировать цикл событий)."""
    threading.Thread(target=_cli_sync, args=args, daemon=True,
                     name="agentd-cli").start()


def state_db_rows(db_path=None) -> list:
    """Read-only снимок sidekick/* сессий state.db: [{id,title,started_at,archived}].
    Любая ошибка (нет файла/схемы, залочен) → []: ротатор не трогает то, чего не
    видит, — это лучше, чем падение или слепое архивирование."""
    path = db_path or STATE_DB
    try:
        import sqlite3
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            cur = db.execute(
                "SELECT id, title, started_at, COALESCE(archived,0) FROM sessions "
                "WHERE title LIKE ?", (SESS_PREFIX + "%",))
            return [{"id": r[0], "title": r[1], "started_at": r[2],
                     "archived": bool(r[3])} for r in cur]
    except Exception:
        return []


def rename_checkpoints(backoff=None, max_wait=None) -> list:
    """Моменты (с от старта, сек) проверять видимость строки сессии в state.db:
    0, затем шагами backoff (0.5/1/2/4 с), последний чекпоинт — ровно бюджет
    (10 с). Чистая функция — контракт расписания тестируется (SK-45)."""
    back = list(RENAME_BACKOFF_S) if backoff is None else list(backoff)
    budget = RENAME_MAX_WAIT_S if max_wait is None else max_wait
    pts, t = [0.0], 0.0
    for d in back:
        if t + d >= budget:
            break
        t += d
        pts.append(round(t, 3))
    if pts[-1] < budget:
        pts.append(float(budget))
    return pts


def _rename_when_visible(new_sid: str, title: str, *, visible=None,
                         backoff=None, max_wait=None) -> bool:
    """Честный retry переименования (SK-45) вместо слепых ударов `_cli_retry`
    (аудит 22.10 п.9: rename успевал до коммита строки сессии hermes — rc=1
    «No session» на каждой новой, rc=1 спамил в journal; именование SK-29
    держалось на случайных ретраях). Ждём ВИДИМОСТЬ строки в state.db по
    чекпоинтам (rename_checkpoints; замер 23.10: init→строка ≈ 1.4 с), затем
    rename — rc=0. Исчерпание — одна тихая пометка в journal; в чат-кадр ничего
    не идёт (автозаголовок остаётся, следующий rename перетрёт). Ретраи только
    в воркере-очереди (имена уникальны — параллельные rename конкурировали бы)."""
    vis = visible or (lambda: new_sid in session_titles_map())
    pts = rename_checkpoints(backoff, max_wait)
    prev = 0.0
    for i, pt in enumerate(pts):
        if i:
            time.sleep(pt - prev)
        prev = pt
        if vis() and _cli_sync("sessions", "rename", new_sid, title, quiet=True) == 0:
            return True
    log(f"SK-45: сессия {new_sid} не появилась в state за {pts[-1]:.0f} с — имя "
        f"«{clip(title, 40)}» не отдано (пометка тихая, автозаголовок жив)")
    return False


_NAME_Q: "queue.Queue[tuple]" = queue.Queue()


def _name_work_once(chat: str, prev_sid, prev_ts: float, new_sid: str) -> None:
    """Одна цепочка переименований: prev→история, new→голое имя (ожидание
    видимости строки в state, SK-45), ротация — после. Отдельная функция =
    тестируемый контракт."""
    if prev_sid:
        _cli_sync("sessions", "rename", prev_sid,
                  history_title(chat, prev_ts or time.time()))
    _rename_when_visible(new_sid, session_title(chat))
    rotate_sessions()


def _name_worker() -> None:
    """Единственный поток переименований (SK-29): порядок prev→new строго один,
    иначе две задачи одного чата дерутся за голое имя sidekick/<chat>."""
    while True:
        try:
            _name_work_once(*_NAME_Q.get())
        except Exception as e:  # воркер не должен умирать от одной цепочки
            log(f"SK-29 rename-воркер: {e}")


def name_new_session(chat: str, new_sid: str, prev_sid, prev_ts: float = 0) -> None:
    """Именование (SK-29): ТЕКУЩАЯ сессия чата всегда голое sidekick/<chat>;
    предыдущая уходит в историю «· дата». Порядок строгий и в ОДНОМ потоке:
    сначала освобождаем имя (переименовать старую), потом отдаём новой — заголовки
    уникальны, паралельные rename конкурировали бы за имя. Ротация — после."""
    _NAME_Q.put((chat, prev_sid, prev_ts or time.time(), new_sid))


def rotate_sessions() -> list:
    """Ротация (SK-29 п.5): не более SESSION_KEEP живых sidekick/*; старейшие
    исторические — soft-hide через `sessions archive --title` (не удаляем молча:
    видны в скрипте с --archived и в `hermes sessions`). Возвращает id в архиве."""
    rows = state_db_rows()
    targets = pick_archive_targets(rows)
    for r in targets:
        log(f"SK-29 ротация: архивирую сессию {r['id']} «{clip(r['title'], 60)}» "
            f"(keep={SESSION_KEEP})")
        _cli("sessions", "archive", "--title", r["title"], "--yes")
    return [r["id"] for r in targets]


def session_titles_map(db_path=None) -> dict:
    """id → title ВСЕХ сессий state.db (read-only; {} при любой ошибке)."""
    path = db_path or STATE_DB
    try:
        import sqlite3
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            return {r[0]: r[1] for r in db.execute("SELECT id, title FROM sessions")}
    except Exception:
        return {}


def migrate_legacy_names(sessions: "SessionsMap") -> int:
    """Одноразовая миграция (SK-29): legacy-записи sessions.json (без name) — это
    сессии агентда с мусорным авто-заголовком «[sidekick-agentd task=…]». Давим
    текущим сессиям канонические имена, чтобы история профиля стала видимой, а
    --resume sidekick/<chat> работал для заведомых чатов сразу. Синхронно ДО
    основного цикла (до конкурентных sessions.set). Только сессии, реально
    существующие в db; занятые другим владельцем имена не вышибаем (rename не
    удался — попробуем при следующем старте). Повторный запуск ничего не делает.
    Возвращает число переименований."""
    try:
        with open(sessions.path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    titles = session_titles_map()
    done = changed = 0
    for chat, ent in data.items():
        if not isinstance(ent, dict) or not ent.get("sid") or ent.get("name"):
            continue
        sid, want = ent["sid"], session_title(chat)
        if sid not in titles:
            continue  # сессии нет в db — не переименовывать вслепую
        if titles[sid] == want:
            ent["name"] = want
            changed = True
            continue
        if _cli_sync("sessions", "rename", sid, want) == 0:
            ent["name"] = want
            done += 1
    if done or changed:
        tmp = f"{sessions.path}.tmp{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, sessions.path)
        except OSError:
            pass
    if done:
        log(f"SK-29 миграция: названы текущие сессии {done} чат(ов)")
    return done


class RpcWatch:
    """SK-29: незакрытые rpc-вердикты и rpc-активность по задачам (idle-надзор).

    Строка rpc_in {id, method, params:{task}} — потомок ждёт вердикт; rpc_out {id}
    — вердикт выдан. Невыданные id = человек отвечает на гейт (perm.check ждёт до
    90 с, handoff — минуты) — idle-пауза в это время убила бы ожидание человека.
    rpc с params.task (SK-25) обновляет активность задачи — «10 мин без rpc»
    буквально. При ротации файла (size < offset, SK-33) pending очищается:
    пережевание старых строк не должно навсегда глушить надзор, а свежий висячий
    rpc остаётся в текущем файле и будет добавлен снова."""

    def __init__(self, rpc_in: str = RPC_IN_F, rpc_out: str = RPC_OUT_F):
        self.in_path, self.out_path = rpc_in, rpc_out
        self.off_in = self.off_out = 0
        self.pending: set = set()

    @staticmethod
    def _tail(path: str, off: int):
        try:
            reset = os.path.getsize(path) < off
        except FileNotFoundError:
            reset = False
        lines, off2 = tail_lines(path, off)
        return lines, off2, reset

    def poll(self) -> dict:
        """Сдвинуть курсоры → {task_id: ts} для rpc-строк с params.task."""
        touched: dict = {}
        lines, self.off_in, reset = self._tail(self.in_path, self.off_in)
        if reset:
            self.pending.clear()
        for ln in lines:
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            rid = rec.get("id")
            if rid is None:
                continue
            self.pending.add(str(rid))
            p = rec.get("params")
            if isinstance(p, dict) and p.get("task"):
                touched[str(p["task"])] = time.time()
        lines, self.off_out, reset = self._tail(self.out_path, self.off_out)
        if reset:
            self.pending.clear()
        for ln in lines:
            try:
                rid = json.loads(ln).get("id")
            except json.JSONDecodeError:
                continue
            if rid is not None:
                self.pending.discard(str(rid))
        return touched


# ---------------------------------------------------------------- outbox ----

def out_frame(**frame) -> None:
    """Аддитивная запись строки v1-контракта в outbox (мост ретранслирует как есть)."""
    line = json.dumps({"v": 1, **frame}, ensure_ascii=False)
    fd = os.open(OUTBOX, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


# --------------------------------------------------------------- humanize ---
# SK-36: шаг в ленте = человекочитаемая русская фраза, сырая команда — под катом
# «подробнее» (поле raw кадра step, сайдбар прячет его по умолчанию). Приоритет
# подписи: caption агента (параметр `caption` в rpc-вызове — sk срезает его на
# клиенте, в провод не летит) > шаблон метода моста > малая CPU-модель (асинхронно,
# бюджет SIDEKICK_CAPTION_BUDGET_MS) > шаблон-заглушка. Никакого облака.

_RPC_METHOD = re.compile(r'"method"\s*:\s*"([A-Za-z0-9._]+)"')
# SK-33 обвязка: `sk rpc <method> '<json>'` (путь к sk любой: bin/sk, ~/.../sk).
_SK_RPC = re.compile(r"(?:^|[\s;&|(`])(?:[\w.~/-]*/)?sk\s+rpc\s+([A-Za-z][A-Za-z0-9._]*)\b")


def _first_json_obj(s: str, start: int = 0):
    """Первый сбалансированный {...} (со строками и экранированием) из позиции start.
    (dict|None, подстрока). Фигурные скобки внутри строк не путаем."""
    i = s.find("{", start)
    if i < 0:
        return None, ""
    depth, in_str, esc = 0, False, False
    for j in range(i, len(s)):
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                frag = s[i:j + 1]
                try:
                    d = json.loads(frag)
                    return (d if isinstance(d, dict) else None), frag
                except (json.JSONDecodeError, ValueError):
                    return None, frag
    return None, ""


def _rpc_from_cmd(cmd: str):
    """(method, params, raw) | (None, None, "") — rpc-вызов в терминальной команде:
    `sk rpc <method> '<json>'` (SK-33) или рукописная rpc_in-строка (legacy:
    {"method":...,"params":{...}} + >> rpc_in.jsonl)."""
    m = _SK_RPC.search(cmd)
    if m:
        params, _ = _first_json_obj(cmd, m.end())
        return m.group(1), (params if isinstance(params, dict) else {}), cmd.strip()
    mm = _RPC_METHOD.search(cmd)
    if mm and "rpc_in" in cmd:
        frame, _ = _first_json_obj(cmd)
        p = frame.get("params") if isinstance(frame, dict) else None
        return mm.group(1), (p if isinstance(p, dict) else {}), cmd.strip()
    return None, None, ""


def _domain(url) -> str:
    """Хост по url (или голому домену) — для фразы «открываю {домен}»."""
    u = str(url or "").strip()
    m = re.search(r"https?://([^/\s\"']+)", u)
    host = m.group(1) if m else (u.split("/")[0] if u and "." in u and " " not in u else "")
    host = host.split("@")[-1].split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


def _bn(p) -> str:
    return os.path.basename(str(p or "").rstrip("/"))


def _target(p: dict) -> str:
    """Имя цели action.do для фразы: name > mark > sid; значения полей НЕ показываем."""
    name = str(p.get("name") or "").strip()
    if name:
        return clip(name, 48)
    if p.get("mark") is not None:
        return f"метка {p['mark']}"
    if p.get("sid"):
        return "элемент со страницы"
    return ""


def _phrase_action_do(p: dict, domain: str):
    """Русская фраза глагола action.do; None — неизвестный глагол (weak → модель).
    Значения value/text в подпись НЕ попадают (секреты/PII): только цель."""
    act = str(p.get("action") or "click")
    tgt = _target(p)
    if act == "input":
        return f"печатаю в «{tgt}»" if tgt else "печатаю на странице"
    if act == "submit":
        return f"отправляю форму «{tgt}»" if tgt else "отправляю форму"
    if act == "key":
        return f"жму клавишу {clip(str(p.get('key') or '?'), 12)}"
    if act == "scroll":
        to = str(p.get("to") or "")
        if to == "top":
            return "прокручиваю страницу к началу"
        if to == "bottom":
            return "прокручиваю страницу до конца"
        if p.get("sid") or p.get("mark") is not None:
            return "прокручиваю к нужному элементу"
        return "прокручиваю страницу"
    if act in ("click", "dblclick", "rightclick", "hover", "move"):
        dom = f" на {domain}" if domain and tgt and act in ("click", "dblclick", "rightclick") else ""
        no_tgt = {"click": "кликаю на странице", "dblclick": "дважды кликаю на странице",
                  "rightclick": "кликаю правой кнопкой по странице",
                  "hover": "навожу курсор на страницу", "move": "веду курсор по странице"}
        if not tgt:
            return no_tgt[act]
        with_tgt = {"click": f"кликаю «{tgt}»{dom}", "dblclick": f"дважды кликаю «{tgt}»{dom}",
                    "rightclick": f"кликаю правой кнопкой по «{tgt}»{dom}",
                    "hover": f"навожу курсор на «{tgt}»", "move": f"веду курсор к «{tgt}»"}
        return with_tgt[act]
    return None


_PERM_RU = {"sensitive_input": "поле с секретом", "payment": "платёж",
            "oauth": "вход через OAuth", "2fa": "второй фактор (2FA)",
            "send_message": "отправить сообщение", "send_email": "отправить письмо",
            # некритичные типы из контракта (always_human:false) — тоже покрыты,
            # чтобы не дёргать модель на штатном пермисе
            "action": "действие на странице", "click": "обычный клик",
            "close_tabs": "закрытие вкладок", "input": "ввод в обычное поле",
            "navigate": "переход по странице", "submit": "отправка обычной формы"}


def _phrase_bridge(method: str, p: dict, domain: str):
    """(фраза | None, weak). None → неизвестный метод/параметры: показываем
    шаблон-заглушку и зовём модель уточнить. Покрыты ВСЕ методы контракта v1
    (sk capabilities: bridge_local + forwarded + gated) — п.11 AGENTS.md."""
    if method == "action.do":
        ph = _phrase_action_do(p, domain)
        return ph, ph is None
    if method == "action.preview":
        return "показываю действие на подтверждение", False
    if method == "page.screenshot":
        return ("размечаю кнопки на экране" if p.get("marks") else "смотрю на страницу"), False
    if method == "page.text":
        return "читаю текст страницы", False
    if method == "page.dom":
        return "читаю страницу", False
    if method == "page.media":
        return "проверяю видео на странице", False
    if method == "page.active":
        return "проверяю, что открыто передо мной", False
    if method == "tabs.open_grouped":
        d = _domain(p.get("url"))
        return (f"открываю {d}" if d else "открываю новую вкладку"), False
    if method == "tabs.close":
        return "закрываю вкладки", False
    if method == "tabs.list":
        return "проверяю открытые вкладки", False
    if method == "tabs.activate":
        return "переключаюсь на свою вкладку", False
    if method == "tabs.use_tab":
        return "привязываю вкладку к задаче", False
    if method == "tabs.highlight_group":
        return "подсвечиваю свою группу вкладок", False
    if method == "tabs.park_group":
        return "паркую свою группу вкладок", False
    if method == "perm.check":
        what = _PERM_RU.get(str(p.get("perm") or ""))
        if not what:
            return None, True  # неизвестный тип пермиса → weak
        dom = _domain(p.get("url")) or str(p.get("domain") or "").strip()
        return f"спрашиваю разрешение: {what}" + (f" на {dom}" if dom else ""), False
    if method == "handoff.request":
        what = clip(str(p.get("what") or ""), 48)
        return ("зову тебя к экрану: " + what) if what else "зову тебя к экрану", False
    if method == "handoff.status":
        return "справляюсь, нужна ли помощь", False
    if method == "capabilities":
        return "сверяюсь с контрактом моста", False
    if method == "audit.tail":
        return "листаю аудит моста", False
    if method == "chats.get":
        return "читаю нашу переписку", False
    if method == "chats.list":
        return "проверяю список чатов", False
    if method == "grants.get":
        return "проверяю выданные разрешения", False
    if method == "grants.set":
        return "обновляю разрешения моста", False
    return None, True


# Фразы нетуловых шагов — тем же словарём (голые команды терминала в подпись не
# попадают: только «выполняю команду», сырая строка — под «подробнее»).
_TOOL_PHRASE = {
    "read_file": lambda a: "📄 читаю файл " + (clip(_bn(a.get("path")), 40) or "на диске"),
    "write_file": lambda a: "✍️ пишу файл " + (clip(_bn(a.get("path")), 40) or "на диске"),
    "patch": lambda a: "✏️ правлю файл " + (clip(_bn(a.get("path")), 40) or "на диске"),
    "search_files": lambda a: "🔎 ищу по файлам"
                              + (": " + clip(str(a.get("pattern") or a.get("query") or ""), 36)
                                 if (a.get("pattern") or a.get("query")) else ""),
    "execute_code": lambda a: "🐍 считаю в Python",
    "web_search": lambda a: "🌐 ищу в интернете: " + clip(str(a.get("query") or ""), 40),
    "web_extract": lambda a: "🌐 читаю страницу " + (_domain(a.get("url")) or "из интернета"),
    "vision_analyze": lambda a: "👁 рассматриваю изображение",
    "browser_navigate": lambda a: "🧭 открываю " + (_domain(a.get("url")) or "страницу"),
    "knet_search": lambda a: "🗑 вспоминаю: " + clip(str(a.get("query") or ""), 40)
                             if a.get("query") else "🗑 вспоминаю из памяти",
    "knet_fact": lambda a: ("🗑 запоминаю: " + clip(str(a.get("entity") or ""), 40))
                           if str(a.get("action") or "") == "put"
                           else ("🗑 сверяюсь с памятью: " + clip(str(a.get("entity") or ""), 40))
                           if a.get("entity") else "🗑 сверяюсь с памятью",
    "knet_stats": lambda a: "🗑 проверяю память",
}


def step_parts(name: str, args, domain: str = "") -> dict:
    """Части шага ленты (SK-36): text — русская фраза; raw — сырая команда под кат;
    caption — собственная подпись агента (приоритет над шаблоном и моделью);
    weak — шаблон слаб (неизвестный метод/глагол/пермис) → уточнит модель;
    method/params/domain — контекст для модели и трекинга домена задачи."""
    args = args if isinstance(args, dict) else {}
    if name == "terminal":
        cmd = str(args.get("command", ""))
        method, params, raw = _rpc_from_cmd(cmd)
        if method:
            p = dict(params or {})
            cap = str(p.pop("caption", "") or "").strip()
            cap = clip(cap, 120) if cap else ""
            phrase, weak = _phrase_bridge(method, p, domain)
            text = phrase or "📡 работаю через мост"
            return {"text": clip(text, STEP_TEXT_MAX), "raw": clip(raw, 600), "caption": cap,
                    "weak": bool(weak) and not cap, "method": method, "params": p,
                    "domain": _domain(p.get("url")) or domain}
        if cmd.strip():
            return {"text": "🖥 выполняю команду в терминале", "raw": clip(cmd.strip(), 600)}
        return {"text": "🖥 выполняю команду в терминале"}
    fn = _TOOL_PHRASE.get(name)
    if fn:
        try:
            text = fn(args)
        except Exception:
            text = f"🔧 {name}"
        raw = json.dumps(args, ensure_ascii=False, separators=(",", ":"))[:600] if args else ""
        return {"text": clip(text, STEP_TEXT_MAX), "raw": raw}
    return {"text": f"🔧 {name}",
            "raw": json.dumps(args, ensure_ascii=False, separators=(",", ":"))[:600] if args else ""}


def humanize_tool(name: str, args) -> str:
    """Одна строка шага из tool_use-события (обёртка step_parts — только подпись)."""
    return step_parts(name, args)["text"]


# ---- SK-36: малая CPU-модель для слабых шаблонов (асинхронно, ≤500 мс) ----------
# Выбор бэкенда (исследование 23.10, задокументировано): на машине нет готового
# малогабаритного chat-сервера на CPU — :8086 llama-server держит bge-m3 (только
# эмбеддинги), :8080 Strata — основная модель агентов на GPU (подписывать шаги той
# же моделью, что исполняет задачу = конкуренция за очередь и нарушение требования
# «малюсенькая моделька на CPU»), `hermes -z` — запуск процесса 1–3 с (бюджет 500 мс
# не влезает даже без генерации). Отдельный сервер: llama-server + GGUF, порт 8087,
# unit agentd/sidekick-caption.service (CPU, loopback, Nice=10).
# Выбор модели (замерено 23.10 на живом железе, i5-13600KF, load ~11):
#   Qwen2.5-0.5B-Instruct Q4_K_M — тёплый ответ 170–380 мс (бюджет влезает), НО для
#     свободной русской фразы модель слабее минимума: пародирует строку примера
#     («размечаю кнопки» на drag-запрос), повторяет прайминг («я») или сыплет
#     английским — подписи-пародии хуже подписей-шаблонов, путь забракован;
#   Qwen2.5-1.5B-Instruct Q4_K_M — русские фразы по делу («тыкаю по кнопкам»,
#     «проверяю доступ к перемещению»); замер тёплого пути на живом железе при
#     load ~11: 291–943 мс (median ~600) — в 500-мс бюджет успевает не всегда,
#     асинхронный дизайн это и лечит: не успела = остаётся шаблон (честный
#     fallback по спеке), кэш одинаковых шагов делает повторные шаги бесплатными;
#   Qwen2.5-Omni-3B — base-поведение (эхо промпта) и ~1.3 с — не годится.
# Не успела/лежит — остаётся шаблон-заглушка. Никакого облака.
CAPTION_URL = os.environ.get("SIDEKICK_CAPTION_URL",
                             "http://127.0.0.1:8087/v1/chat/completions")
CAPTION_BUDGET_MS = float(os.environ.get("SIDEKICK_CAPTION_BUDGET_MS", "500"))
CAPTION_MODEL = os.environ.get("SIDEKICK_CAPTION_MODEL", "sidekick-caption")
CAPTION_OFF = os.environ.get("SIDEKICK_CAPTION_DISABLE", "") == "1"
CAPTION_CACHE_MAX = 256
_CAPTION_SYS = (
    "Подпиши шаг браузерного агента ОДНОЙ русской фразой из 3-5 слов: настоящее "
    "время, от первого лица, без кавычек, точки в конце и пояснений. Только "
    "русские слова, названия из параметров не переводить.\n"
    'page.screenshot {"marks":true} → размечаю кнопки\n'
    'tabs.close {"tabIds":[3,4]} → закрываю вкладки\n'
    'action.do {"action":"drag","x":10,"y":20} → тяну мышью')
_CAPTION_CACHE: "collections.OrderedDict[str, str]" = collections.OrderedDict()


def caption_refine(method: str, params, domain: str = "") -> str | None:
    """Фраза для слабого шаблона от локальной модели. Бюджет — реальный: ответ
    позже SIDEKICK_CAPTION_BUDGET_MS отбрасываем (шаг остаётся с заглушкой).
    Только loopback: приватность пользователя — никакого облака.
    Домен в промпт НЕ кладём: 1.5B на нём сбивается на английский («link to
    example.com»), а шаблон домен уже показывает."""
    if CAPTION_OFF or CAPTION_BUDGET_MS <= 0 or not CAPTION_URL:
        return None
    if "://" in CAPTION_URL and not re.match(r"https?://(127\.0\.0\.1|localhost|::1)[:/]",
                                              CAPTION_URL):
        return None  # не loopback — приватность важнее красоты
    params = params if isinstance(params, dict) else {}
    key = method + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False)[:300]
    hit = _CAPTION_CACHE.get(key)
    if hit is not None:
        _CAPTION_CACHE.move_to_end(key)
        return hit
    body = {"model": CAPTION_MODEL, "temperature": 0, "max_tokens": 14,
            # стоп-маркеры: 1.5B болтает до max_tokens, если не обрывать; точка/
            # восклицание в подписи запрещены промптом — обрыв по ним безопасен
            "stop": ["\n", ".", "!", "…", ";"],
            "messages": [{"role": "system", "content": _CAPTION_SYS},
                         {"role": "user", "content":
                             f"{method} "
                             f"{json.dumps(params, ensure_ascii=False, separators=(',', ':'))[:240]}"
                             "\nФраза:"}]}
    req = urllib.request.Request(
        CAPTION_URL, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=CAPTION_BUDGET_MS / 1000.0) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        spent_ms = (time.monotonic() - t0) * 1000.0
        if spent_ms > CAPTION_BUDGET_MS:
            return None  # не успела — честно считаем отказом (шаблон остаётся)
        text = str(d["choices"][0]["message"]["content"] or "").strip()
        text = text.strip("«»\"'` ").rstrip(".。! ;…").strip()
        # подпись обязана быть русской: латиница/кавычки/многострочие = модель
        # срезалась стопом на середине цитаты («с ID "el4») — в ленту не идёт
        if (not text or len(text) > STEP_TEXT_MAX or "\n" in text
                or re.search(r"[a-zA-Z\"'`«»\\]", text)):
            return None
        _CAPTION_CACHE[key] = text
        while len(_CAPTION_CACHE) > CAPTION_CACHE_MAX:
            _CAPTION_CACHE.popitem(last=False)
        return text
    except Exception:
        return None  # недоступна/таймаут — тихий fallback на шаблон

# SK-31: тулы, чей непустой успешный результат = ответ собран «из памяти» (не из
# этого чата). knet_stats — служебная статистика, не память; knet_fact put — запись,
# а не вспоминание (вердикт — в memory_hit по форме результата).
_MEM_TOOLS = ("knet_search", "knet_fact")


def memory_hit(name: str, output: str) -> bool:
    """Нашёл ли knet-тул реально что-то: knet_search → непустой {"results":[...]},
    knet_fact lookup/history → непустой {"facts":[...]}; put/ошибка/пусто/не-JSON — нет.
    Отвечает по выводу тула (stream-json tool_result.output), не по наитию агента."""
    try:
        d = json.loads(output or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(d, dict) or d.get("error"):
        return False
    if name == "knet_search":
        return bool(d.get("results"))
    return bool(d.get("facts"))


def build_child_message(tid: str, text: str) -> str:
    """Конверт прогона: потомок не пишет в outbox, RPC-контракт в силе."""
    return (
        f"[sidekick-agentd task={tid}] Правила этого прогона: в outbox.jsonl не пиши "
        "ничего (ни task/step/chat) — ленту шагов, thinking и финальный ответ человеку "
        "отдаёт агентд из твоего stdout-потока. RPC-контракт в силе: rpc_in.jsonl → "
        "rpc_out.jsonl (perm.check, action.do, page.*, tabs.*, handoff.request), hub/stop "
        "проверяй между шагами. SK-25: во ВСЕ браузерные rpc (page.*, action.preview/"
        "action.do, page.screenshot, tabs.use_tab, tabs.open_grouped) клади в params "
        f'\"task\":\"{tid}\" — мост привяжет задачу к её вкладке и будет бить строго по '
        "ней, даже когда человек переключится в другое окно; tab_gone/tab_moved — стоп, "
        "переспроси человека в чате. SK-28: мост отвечает no_extension — НЕ долби rpc_in "
        "вслепую: sk status назовёт причину (расширение offline → скажи человеку «открой "
        "Firefox» и остановись, агентд сам поставит задачу на паузу и продолжит при "
        "переподключении). Финальный текст этого запроса — и есть ответ человеку "
        "(по-русски, без упоминания агентда и служебных деталей).\n---\n" + text)


# -------------------------------------------------------------- TaskFeed ----

class TaskFeed:
    """Чистый автомат кадров ленты одной задачи (без I/O — тестируется)."""

    def __init__(self, tid: str, title: str):
        self.tid = tid
        self.title = title
        self.n = 0
        self.cur = None       # номер открытого шага или None
        self.cur_text = ""
        self.cur_raw = ""     # SK-36: сырая команда открытого шага (под кат «подробнее»)
        self.cur_caption = ""  # SK-36: подпись агента открытого шага (приоритетная)
        self.closed = False
        # SK-36: домен задачи — из последней tabs.open_grouped/перехода, чтобы фраза
        # «кликаю «…», на каком сайте» не требовала rpc-ответов (агентд их не читает).
        self.domain = ""
        self.pending_weak = None  # {n, method, params, domain} — слабый шаблон, ждёт модель
        # SK-31: «из памяти» — хотя бы один knet-тул отдал непустой успешный результат
        # (вердикт по tool_result, не по факту вызова); _mem_pending — имя тула,
        # чей результат ещё не пришёл.
        self.used_memory = False
        self._mem_pending = None

    def task(self, state: str) -> dict:
        return {"type": "task", "id": self.tid, "title": self.title, "state": state}

    def step(self, n: int, text: str, state: str, raw: str = "", caption: str = "") -> dict:
        f = {"type": "step", "task": self.tid, "n": n, "text": text, "state": state}
        # SK-36: аддитивные поля контракта v1 — старые читатели их игнорируют.
        if raw:
            f["raw"] = raw
        if caption:
            f["caption"] = caption
        return f

    def open_step(self, text: str, raw: str = "", caption: str = "") -> list[dict]:
        frames = self.close_step("done")
        self.n += 1
        self.cur, self.cur_text = self.n, text
        self.cur_raw, self.cur_caption = raw, caption
        return frames + [self.step(self.n, text, "active", raw, caption)]

    def close_step(self, state: str = "done") -> list[dict]:
        if self.cur is None:
            return []
        n, text, raw, cap = self.cur, self.cur_text, self.cur_raw, self.cur_caption
        self.cur, self.cur_text, self.cur_raw, self.cur_caption = None, "", "", ""
        return [self.step(n, text, state, raw, cap)]

    def refine_step(self, n: int, text: str) -> list[dict]:
        """SK-36: модель уточнила слабый шаг — тот же n, состояние НЕ трогаем
        (мост при отсутствии state сохраняет прежнее). Подпись агента (caption)
        моделью не перетирается: caption > шаблон > модель."""
        if self.closed or not text:
            return []
        if self.cur == n and self.cur_caption:
            return []
        if self.cur == n:
            self.cur_text = text
        return [{"type": "step", "task": self.tid, "n": n, "text": clip(text, STEP_TEXT_MAX)}]

    def start(self) -> list[dict]:
        return [self.task("running")] + self.open_step("⚡ сообщение получено · запускаю Hermes")

    def on_init(self, session_id: str) -> list[dict]:
        return self.close_step("done")

    def on_session_reset(self) -> list[dict]:
        return self.open_step("⚠ сессия не принята — стартую новую")

    def on_tool_use(self, name: str, args) -> list[dict]:
        # SK-31: knet-вызов — только кандидат; флаг ставит on_tool_result по выводу.
        self._mem_pending = name if name in _MEM_TOOLS else None
        p = step_parts(name, args, domain=self.domain)
        if p.get("domain"):
            self.domain = p["domain"]  # «открыл ozon.ru» → клики подписываются им
        frames = self.open_step(p["text"], raw=p.get("raw", ""), caption=p.get("caption", ""))
        self.pending_weak = ({"n": self.n, "method": p.get("method", ""),
                              "params": p.get("params", {}), "domain": self.domain}
                             if p.get("weak") else None)
        return frames

    def on_tool_result(self, is_error: bool, name: str = "", output: str = "") -> list[dict]:
        if self._mem_pending is not None:
            if not is_error and memory_hit(self._mem_pending, output):
                self.used_memory = True
            self._mem_pending = None
        return self.close_step("failed" if is_error else "done")

    def on_think(self, chunk: str) -> list[dict]:
        return [{"type": "think", "task": self.tid, "text": chunk}]

    def finish(self, *, rc: int, text: str, stopped: bool, crashed: bool = False) -> list[dict]:
        """Финал: закрыть шаг, выдать chat и терминальный task-кадр."""
        if self.closed:
            return []
        self.closed = True
        failed = bool(rc) or stopped or crashed
        frames = self.close_step("failed" if failed else "done")
        if stopped:
            chat, state = text or "⏹ Остановлено по твоему ■ Stop.", "failed"
        elif crashed:
            chat = text or f"⚠ Hermes упал (rc={rc}). Сообщение не потеряно — напиши ещё раз."
            state = "failed"
        elif rc == 0:
            chat, state = text or "…", "done"
        else:
            chat = text or f"⚠ Агент завершился с ошибкой (rc={rc})."
            state = "failed"
        frames.append({"type": "chat", "role": "agent", "task": self.tid,
                       "text": str(chat)[:CHAT_MAX],
                       # SK-31: ответ собран из knet (не из этого чата) — аддитивный
                       # флаг; сайдбар рисует глиф «🗑 из памяти». Мост ретранслирует as-is.
                       **({"memory": True} if self.used_memory else {})})
        frames.append(self.task(state))
        return frames

    def pause(self, why: str) -> list[dict]:
        """SK-28: пауза браузерного канала — закрыть шаг, задача paused, человеку —
        подсказка в chat-кадре. NOT closed: после восстановления канала задача
        продолжится (resume), финал ещё впереди. Закрытая (завершённая) задача не
        паузится — финальный кадр не перетирается."""
        if self.closed:
            return []
        frames = self.close_step("failed")
        frames.append({"type": "chat", "role": "agent", "task": self.tid,
                       "text": str(why)[:CHAT_MAX]})
        frames.append(self.task("paused"))
        return frames

    def resume(self) -> list[dict]:
        """SK-28: канал ожил — задача снова running, шаг с пометкой продолжения."""
        return [self.task("running")] + self.open_step("▶ канал восстановлен — продолжаю")

    def abandon(self, note: str) -> list:
        """SK-29: paused-задача просрочила окно диалога — терминальный кадр
        (задача обязана закрываться, SOUL/аддендум 21.10: не висять «paused» вечно).
        NOT reuse finish(): финальный chat — служебная заметка, а не ответ агента."""
        if self.closed:
            return []
        self.closed = True
        frames = self.close_step("failed")
        frames.append({"type": "chat", "role": "agent", "task": self.tid,
                       "text": str(note)[:CHAT_MAX]})
        frames.append(self.task("failed"))
        return frames


# --------------------------------------------------------------- sessions ---

class SessionsMap:
    """hub/sessions.json — карта chat → {sid, updated}. Атомарная запись."""

    def __init__(self, path: str):
        self.path = path

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def get(self, chat: str):
        ent = self._load().get(chat)
        return ent.get("sid") if isinstance(ent, dict) and ent.get("sid") else None

    def entry(self, chat: str) -> dict:
        """Полная запись чата {sid, updated, name} или {} — SK-29 окну нужна
        отметка последней активности, одного sid мало."""
        ent = self._load().get(chat)
        return ent if isinstance(ent, dict) and ent.get("sid") else {}

    def set(self, chat: str, sid, name: str = "") -> None:
        data = self._load()
        if sid:
            ent = {"sid": sid, "updated": round(time.time(), 1)}
            if name:
                ent["name"] = name
            data[chat] = ent
        else:
            data.pop(chat, None)
        tmp = f"{self.path}.tmp{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


# ---------------------------------------------------------------- cursor ----

class Cursor:
    """cursor.json — смещения чтения inbox/events.

    inbox-смещение сохраняется только после закрытия задачи (см. main):
    падение демона не «съедает» сообщения. read_inbox — внутреннее смещение
    чтения (может опережать сохранённое: очередь ещё не обработана).
    """

    def __init__(self, path: str):
        self.path = path
        self.inbox = 0        # сохранённое (коммитится после закрытия задачи)
        self.read_inbox = 0   # фактическое место чтения в этом прогоне
        self.events = 0

    def load(self) -> bool:
        """Загрузить курсор. False — файла не было (первая установка: вызывающий
        ставит чтения в конец файлов, чтобы не пережевывать историю)."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            self.inbox = max(0, int(d.get("inbox", 0)))
            self.events = max(0, int(d.get("events", 0)))
        except FileNotFoundError:
            return False
        except (ValueError, TypeError, json.JSONDecodeError, OSError):
            self.inbox = self.events = 0
        self.read_inbox = self.inbox
        return True

    def save(self) -> None:
        tmp = f"{self.path}.tmp{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"inbox": self.inbox, "events": self.events}, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass


def tail_lines(path: str, offset: int):
    """Дочитать ПОЛНЫЕ строки с offset → (строки, новый offset).

    Файл короче offset (усечение/ротация) — читаем с нуля. Неполный хвост без
    \\n остаётся до следующего тика.
    """
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return [], offset
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    with open(path, encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        data = fh.read()
    nl = data.rfind("\n")
    if nl < 0:
        return [], offset
    chunk = data[: nl + 1]
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    return lines, offset + len(chunk.encode())


class ThinkTail:
    """Чтение think-файла плагина sidekick-think (SIDEKICK_THINK_FILE) дельтами."""

    def __init__(self, path: str):
        self.path = path
        # старт с конца существующего: при ретрае без resume в тот же файл
        # дописывает новый потомок — старые дельты повторно гнать в ленту нельзя
        try:
            self.off = os.path.getsize(path)
        except OSError:
            self.off = 0

    def read(self) -> list[str]:
        lines, self.off = tail_lines(self.path, self.off)
        texts = []
        for ln in lines:
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "delta" and rec.get("delta"):
                texts.append(str(rec["delta"]))
        return texts


# ----------------------------------------------------------------- Active ---

class Active:
    """Одна активная задача: потомок, его stdout-очередь, think-хвост.
    SK-25 п.3: задач может быть несколько одновременно (не больше одной на чат),
    поэтому всё пер-задачное живёт здесь (включая прошлый потомок после ретрая)."""

    def __init__(self, chat: str, text: str, src_end: int, tid: str, resume_sid,
                 new_task: bool = False, prev_sid=None):
        self.chat = chat
        self.text = text
        self.src_end = src_end  # смещение конца строки inbox — будущий курсор
        self.tid = tid
        first = text.splitlines()[0] if text.strip() else "Hermes"
        self.feed = TaskFeed(tid, clip(first, TITLE_MAX))
        self.proc: subprocess.Popen | None = None
        self.old_proc: subprocess.Popen | None = None  # ретраенный потомок, добить по grace
        self.old_since = 0.0
        self.events: queue.Queue = queue.Queue()
        self.think: ThinkTail | None = None
        self.think_acc: list[str] = []
        self.think_last = 0.0
        self.stop_sent = False
        self.stop_at = 0.0
        self.got_result = False
        self.finished = False
        self.started = time.time()
        self.resume_sid = resume_sid
        self.retried = False  # уже ретраили без resume
        self.pause_sent = False  # SK-28: канал был мёртв, задача приостановлена (живёт в paused)
        # SK-29:
        self.new_task = new_task    # старт без resume → после init именуем сессию
        # что уходит в историю: для новой задачи — прошлая сессия чата (её в
        # resume уже не было — передаётся отдельно), для ретрая битого resume —
        # сам битый sid (выставляется на ретрае)
        self.prev_sid = prev_sid if new_task else None
        self.prev_updated = 0.0     # когда жила прошлая сессия (для «· дата»)
        self.last_event = time.time()  # idle-надзор: stdout/rpc/чат активность
        self.idle_paused = False    # пауза по idle (не по каналу): resume — реплика человека
        # SK-45: watchdog зависания — часы отдельно от idle last_event: реплика
        # человека НЕ сбрасывает их (спека: «если оба молчат»), каналы жизни —
        # только вывод потомка (stdout-стрим/think) и rpc этой задачи.
        self.last_out = self.last_event   # последняя строка stdout или think-дельта
        self.last_rpc = 0.0               # последний rpc задачи (0 — ещё не было)
        self.last_seen = "ожидание первого вывода потомка"  # «вот что было последним»
        self.hang_kill_at = 0.0     # эскалация SIGKILL зависшему (0 — не назначена)
        self.hang_killed = False    # эскалация уже залогирована (не спамить)


def emit(a: "Active", frames) -> None:
    """SK-23: every frame of a task carries its chat (additive — old readers ignore
    the field). This is how the bridge mirrors the agent side into the right chat in
    chats.jsonl and how the sidebar filters the feed to the chat being viewed."""
    for f in frames:
        out_frame(chat=a.chat, **f)


def child_env(think_path: str) -> dict:
    env = dict(os.environ)
    # SK-29: kanban-окружение worker'а, из которого вручную запустили агентда,
    # не должно протекать в потомков: Hermes помечает сессию source=kanban —
    # её скрывает `sessions list`, а resume по имени guard вообще отказывает.
    # Агентд — не диспетчер: чиним source явно (потомок = обычный CLI-чат).
    for k in [k for k in env if k.startswith("HERMES_KANBAN")
              or k in ("HERMES_TENANT", "HERMES_DISPATCHER_TASK")]:
        env.pop(k, None)
    env["HERMES_SESSION_SOURCE"] = "cli"
    node_dirs = [p for pat in ("~/.hermes/tools/node-*/bin", "~/.local/bin", "/usr/local/bin")
                 for p in globmod.glob(os.path.expanduser(pat)) if os.path.isdir(p)]
    env["PATH"] = ":".join([env.get("PATH", "")] + node_dirs).strip(":")
    env["SIDEKICK_THINK_FILE"] = think_path
    return env


def spawn_child(a: Active, sid) -> None:
    """Запуск потомка + читатель stdout; каждая генерация — своя очередь."""
    # SK-45: зависший пережиток (SIGSTOP/игнорит SIGTERM) не переживает перезапуск:
    # добить до того, как a.proc будет перезаписан и процесс останется сиротой.
    if a.proc is not None and a.hang_kill_at and a.proc.poll() is None:
        kill_child(a.proc, signal.SIGKILL)
    a.hang_kill_at = 0.0
    a.hang_killed = False
    think_path = os.path.join(RUN, f"think-{a.tid}.jsonl")
    a.events = queue.Queue()  # старая очередь (со старым сентинелем) просто выбрасывается
    a.think = ThinkTail(think_path)
    a.last_event = time.time()  # SK-29: старт — активность
    a.last_out = a.last_event   # SK-45: часы watchdogа отсчитываются от спавна
    a.last_seen = "спавн потомка, жду hermes init"
    cmd = child_cmd(sid)
    with open(os.path.join(RUN, f"child-{a.tid}.err"), "ab") as err_fh:
        a.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_fh,
            cwd=RUN, env=child_env(think_path), text=True, encoding="utf-8",
            errors="replace", start_new_session=True)
    proc, eq = a.proc, a.events
    assert proc is not None  # Popen только что создан
    try:
        assert proc.stdin is not None
        proc.stdin.write(build_child_message(a.tid, a.text))
        proc.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        pass  # ребёнок умер на старте — EOF-сентинель обработает финал

    def _reader():
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                eq.put(line)
        except (OSError, ValueError):
            pass
        eq.put(None)  # EOF-сентинель

    threading.Thread(target=_reader, daemon=True, name=f"agentd-out-{a.tid}").start()


def kill_child(proc, sig=signal.SIGTERM) -> None:
    try:
        if proc and proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def reap_run_dir() -> None:
    """Мусор прошлого рантайма: think-файлы долой, stderr — хвост RUN_KEEP."""
    try:
        names = os.listdir(RUN)
    except OSError:
        return
    for f in names:
        if f.startswith("think-"):
            try:
                os.remove(os.path.join(RUN, f))
            except OSError:
                pass
    errs = sorted((f for f in names if f.startswith("child-")),
                  key=lambda f: os.path.getmtime(os.path.join(RUN, f)))
    for f in errs[:-RUN_KEEP]:
        try:
            os.remove(os.path.join(RUN, f))
        except OSError:
            pass


def acquire_lock():
    fh = open(LOCK_F, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("другой экземпляр агентда держит lock — выхожу")
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


# ------------------------------------------------------------------- main ---

def main() -> None:
    os.makedirs(RUN, exist_ok=True)
    lock_fh = acquire_lock()  # noqa: F841 — держать открытым весь рантайм
    reap_run_dir()
    cursor = Cursor(CURSOR_F)
    had_cursor = cursor.load()
    try:
        ev_size = os.path.getsize(EVENTS)
    except OSError:
        ev_size = 0
    if not had_cursor:
        # первая установка: историю inbox/events не пережёвываем — уже отработана
        # cron-слушателем; с этого байта начинаем жить.
        try:
            cursor.inbox = cursor.read_inbox = os.path.getsize(INBOX)
        except OSError:
            cursor.inbox = cursor.read_inbox = 0
        cursor.events = ev_size
        cursor.save()
        log(f"курсор создан: пропуск истории inbox={cursor.inbox} events={cursor.events}")
    # события — только живые: исторические stop/resume не должны влиять на старт
    cursor.events = ev_size
    sessions = SessionsMap(SESSIONS)
    rpcw = RpcWatch()  # SK-29: висячие вердикты гейтов и rpc-активность задач (idle-надзор)
    migrate_legacy_names(sessions)  # SK-29: legacy-сессии получают канонические имена
    q: collections.deque = collections.deque()
    actives: dict[str, Active] = {}  # SK-25 п.3: параллельные задачи, не более одной на чат
    # SK-28: приостановленные мёртвым браузерным каналом. src_end этих задач НЕ
    # коммитится в курсор (см. commit_cursor) — падение агентда = перечитывание.
    paused: dict[str, Active] = {}
    chan = "ext_online"       # последнее измеренное состояние канала
    chan_bad_since = 0.0      # с какого момента канал плохой (0 = хороший)
    health_next = 0.0         # когда следующий тик чтения health
    # SK-46: мост умер нечисто (событие bridge_unclean_exit) — алерт в чат при
    # СЛЕДУЮЩЕМ сообщении человека (не в пустоту: у алерта нет задачи-контейнера).
    # Персистентность не нужна: факт уже лежит в events.jsonl, рестарт агентда
    # теряет только напоминание, не событие.
    pending_bridge_alert: dict | None = None
    st0 = channel_state(read_health(), time.time())
    log(f"стартовый браузерный канал: {st0}")
    chan = st0
    if st0 != "ext_online":
        chan_bad_since = time.time()
    log(f"старт: hub={HUB} профиль={PROFILE} hermes={hermes_bin()} курсор={cursor.inbox} "
        f"макс_задач={MAX_ACTIVE}")
    threading.Thread(target=_name_worker, daemon=True,
                     name="agentd-name").start()  # SK-29: единая очередь rename
    threading.Thread(target=rotate_sessions, daemon=True,
                     name="agentd-rotate0").start()  # SK-29: догоняем ротацию после простоя

    # ---- SK-36: уточнение слабого шаблона малой CPU-моделью (асинхронно) --------
    # Один запрос в полёта: caption_refine блокирует СВОЙ поток на бюджет ≤500 мс,
    # main-цикл не ждёт никогда — шаг выходит с шаблоном-заглушкой, потом (если
    # модель успела и шаблон был слаб) тот же n перерисовывается refine-кадром.
    cap_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                      thread_name_prefix="caption")
    cap_fut: list = [None]

    def _caption_job(pw: dict, a: "Active"):
        return caption_refine(pw["method"], pw["params"], pw["domain"]), pw["n"], a

    def pump_caption() -> None:
        fut = cap_fut[0]
        if fut is not None and fut.done():
            cap_fut[0] = None
            text, n, a = None, 0, None
            try:
                text, n, a = fut.result()
            except Exception:
                text = None
            if text and a is not None and actives.get(a.tid) is a:  # задача жива — тот же n
                emit(a, a.feed.refine_step(n, text))
        if cap_fut[0] is None:
            for a in actives.values():
                pw = a.feed.pending_weak
                if pw:
                    a.feed.pending_weak = None
                    cap_fut[0] = cap_pool.submit(_caption_job, pw, a)
                    break

    def commit_cursor() -> None:
        # SK-25: курсор коммитится только до САМОГО раннего незакрытого сообщения
        # (очередь + активные) — при параллельных задачах нельзя «съесть» сообщение,
        # чья задача ещё не закрылась; упав, агентд перечитает хвост заново.
        # SK-28: приостановленные — тоже незакрытые (их задача ждёт авто-resume).
        bound = None
        for a in list(actives.values()) + list(paused.values()):
            if bound is None or a.src_end < bound:
                bound = a.src_end
        if q:
            first_end = q[0]["end"]
            if bound is None or first_end < bound:
                bound = first_end
        if bound is None:
            bound = cursor.read_inbox
        if bound > cursor.inbox:
            cursor.inbox = bound
            cursor.save()

    def finish(a: Active, frames) -> None:
        if a.think_acc:  # остаток thinking — последним кадром перед финалом
            emit(a, a.feed.on_think("".join(a.think_acc)[:THINK_MAX_CHUNK]))
            a.think_acc = []
        emit(a, frames)
        if a.stop_sent:
            try:
                os.remove(STOP)  # hub/stop потреблён агентдом
            except OSError:
                pass
        actives.pop(a.tid, None)
        a.finished = True
        commit_cursor()

    def pause_task(a: Active, why: str) -> None:
        """SK-28: браузерный канал мёртв — потомку SIGTERM (его rpc всё равно не
        пройдут: шторм no_extension ×354 за 10 ч, аудит 21.10), задача в paused с
        подсказкой человеку. Авто-resume при восстановлении канала; sessions.json
        даёт контекст — человек ничего не пересылает."""
        a.pause_sent = True
        kill_child(a.proc)
        actives.pop(a.tid, None)
        paused[a.tid] = a
        emit(a, a.feed.pause(why))
        log(f"задача {a.tid} приостановлена: {clip(why, 80)} (приостановленных {len(paused)})")
        commit_cursor()

    while True:
        pump_caption()  # SK-36: отдать/забрать подпись слабого шаблона (неблокирующе)
        # 1. inbox — новые сообщения человека в очередь (курсор-чтение опережает
        #    сохранённый: он коммитится только до закрытия задачи)
        lines, cursor.read_inbox = tail_lines(INBOX, cursor.read_inbox)
        for ln in lines:
            try:
                msg = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "chat" and msg.get("role") == "user" \
                    and str(msg.get("text", "")).strip():
                mchat = str(msg.get("chat") or "default")
                if pending_bridge_alert:
                    # SK-46: «мост убит/упал» — человек узнаёт об этом своим
                    # сообщением: отдельный agent-кадр в этот чат (мост зеркалит
                    # его в chats.jsonl, сайдбар рисует как реплику Hermes).
                    t_ = time.strftime("%d.%m %H:%M",
                                       time.localtime(pending_bridge_alert.get("ts") or time.time()))
                    out_frame(chat=mchat, type="chat", role="agent",
                              text=f"⚠ Мост перезапустился нечисто: прошлый ран оборвался без "
                                   f"clean-exit метки (~{t_}) — SIGKILL/крах, systemd поднял. "
                                   f"Канал в это время мог прерываться; детали: events.jsonl "
                                   f"bridge_unclean_exit.")
                    log(f"SK-46: алерт о нечистом выходе моста отдан в чат {mchat}")
                    pending_bridge_alert = None
                q.append({"text": str(msg["text"]),
                          "chat": mchat,
                          "end": cursor.read_inbox})
                # SK-29: «10 мин без rpc/чата» — сообщение человека в чат тоже
                # активность: человек рядом, его задача-неудержавка живёт дальше.
                for a in list(actives.values()) + list(paused.values()):
                    if a.chat == mchat:
                        a.last_event = time.time()
        # 2. events — наблюдаем ленту событий (стоп-флаг hub/stop первичен)
        elines, cursor.events = tail_lines(EVENTS, cursor.events)
        for ln in elines:
            try:
                ev = json.loads(ln)
                ev_type = ev.get("type")
            except json.JSONDecodeError:
                continue
            if ev_type in ("stop", "resume"):
                log(f"событие: {ev_type}")
            elif ev_type == "bridge_unclean_exit":
                # SK-46: мост стартовал без метки чистого выхода прошлого рана
                # (SIGKILL/крах/reboot). Алерт — не в пустоту, а первому же
                # следующему сообщению человека (см. inbox). Событие при этом
                # уже в events.jsonl — sk events --since его покажет всегда.
                pending_bridge_alert = {"ts": ev.get("ts") or int(time.time())}
                log("SK-46: событие bridge_unclean_exit — алерт в чат поставлен "
                    "в очередь (отдад при следующем сообщении)")
            elif ev_type == "perm_timeout":
                # SK-34 п.3: два молчаливых таймаута одного пермиса — задача на
                # паузу с ЯВНЫМ вопросом в чат (не восьмой карточный вопрос).
                # idle_paused=True: пауза «человеческая» — следующая реплика в чат
                # возобновляет сессию (путь idle-resume), а не авто-канал (SK-28).
                tid_ = perm_timeout_target(ev, actives)
                if tid_:
                    a = actives[tid_]
                    if not a.finished and not a.pause_sent:
                        a.idle_paused = True
                        pause_task(a, perm_pause_text(ev))
                        log(f"задача {tid_} на паузе: perm-молчание "
                            f"{ev.get('domain')}/{ev.get('action')} streak={ev.get('streak')}")
        commit_cursor()
        # 2.2 SK-29: rpc-надзор — незакрытые вердикты (гейт ждёт человека — idle не
        #     трогает) и rpc задачи по params.task (SK-25) = активность задачи.
        for tid_, ts_ in rpcw.poll().items():
            if tid_ in actives:
                actives[tid_].last_event = ts_
                actives[tid_].last_rpc = ts_  # SK-45: rpc задачи — канал жизни watchdogа
        # 2.5 SK-28: тик браузерного канала (health.json, не чаще HEALTH_READ_S).
        #     Ожил — снимаем приостановленные задачи с паузы (приоритет над очередью:
        #     у resumed уже есть контекст сессии и ждущий человек).
        now = time.time()
        if now >= health_next:
            health_next = now + HEALTH_READ_S
            st = channel_state(read_health(), now)
            if st != chan:
                log(f"SK-28: браузерный канал: {chan} -> {st}")
            chan = st
            if st == "ext_online":
                chan_bad_since = 0.0
                for tid in list(paused):
                    if paused[tid].idle_paused:
                        continue  # SK-29: idle-паузу снимает реплика человека, не канал
                    if len(actives) >= MAX_ACTIVE:
                        break  # слотов нет — пауза переживёт ещё один тик, это не потеря
                    if any(a.chat == paused[tid].chat for a in actives.values()):
                        continue  # в чате уже живёт задача — не плодим вторую на чат (SK-25)
                    a = paused.pop(tid)
                    a.pause_sent = False
                    emit(a, a.feed.resume())
                    actives[tid] = a
                    sid = sessions.get(a.chat) or a.resume_sid
                    spawn_child(a, sid)
                    log(f"задача {tid} возобновлена (resume={sid or 'нет'})")
            elif chan_bad_since == 0.0:
                chan_bad_since = now
        # 3. pump каждой активной задачи: события потомка, think-дельты, hub/stop.
        #    hub/stop — общий ■ человека: SIGTERM уходит КАЖДОМУ живому потомку.
        for a in list(actives.values()):
            if a.old_proc is not None:  # ретраенный потомок — добить по grace
                if a.old_proc.poll() is not None:
                    a.old_proc = None
                elif time.time() - a.old_since > OLD_CHILD_GRACE_S:
                    kill_child(a.old_proc)
                    a.old_proc = None
            drained = 0
            while drained < 200 and not a.finished:
                drained += 1
                try:
                    line = a.events.get_nowait()
                except queue.Empty:
                    break
                if line is None:  # EOF потока этого потомка
                    if not a.got_result:
                        rc = a.proc.wait() if a.proc else -1
                        finish(a, a.feed.finish(rc=rc if rc is not None else -1, text="",
                                                stopped=a.stop_sent, crashed=True))
                    break
                a.last_event = a.last_out = time.time()  # SK-29/SK-45: любая строка потомка = он жив
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "system" and ev.get("subtype") == "init":
                    sid = str(ev.get("session_id") or "")
                    a.last_seen = f"старт сессии {clip(sid, 40)}" if sid else "старт сессии"
                    if sid:
                        sessions.set(a.chat, sid, session_title(a.chat))
                        if a.new_task or a.retried:
                            # SK-29: новая задача / новая сессия после битого resume —
                            # старая в историю, новая получает голое имя sidekick/<chat>
                            name_new_session(a.chat, sid, a.prev_sid, a.prev_updated)
                    emit(a, a.feed.on_init(sid))
                elif t == "tool_use":
                    a.last_seen = humanize_tool(str(ev.get("name", "")), ev.get("input"))
                    emit(a, a.feed.on_tool_use(str(ev.get("name", "")), ev.get("input")))
                elif t == "tool_result":
                    # SK-31: name/output нужны вердикту «из памяти» (memory_hit по выводу).
                    emit(a, a.feed.on_tool_result(bool(ev.get("is_error")),
                                                  str(ev.get("name") or ""),
                                                  str(ev.get("output") or "")))
                elif t == "result":
                    rc = int(ev.get("exit_code", 0))
                    text = str(ev.get("text") or "")
                    if a.stop_sent:
                        # наш ■ Stop: потомок штатно дописывает result(error=Interrupted) —
                        # это НЕ битый resume: финалим stopped, без перезапуска задачи
                        a.got_result = True
                        finish(a, a.feed.finish(rc=rc if rc in (130, 143) else 143,
                                                text="", stopped=True))
                        break
                    if ev.get("error") and not text and a.resume_sid and not a.retried:
                        # битый resume: сброс сессии и один перевызов без неё
                        a.retried = True
                        a.prev_sid = a.resume_sid  # SK-29: битую — в историю, новую — имевать
                        sessions.set(a.chat, None)
                        log(f"resume {a.resume_sid} не принят ({ev.get('error')}) — старт без сессии")
                        emit(a, a.feed.on_session_reset())
                        if a.proc is not None and a.proc.poll() is None:
                            a.old_proc, a.old_since = a.proc, time.time()
                        spawn_child(a, None)
                        break
                    a.got_result = True
                    sid = str(ev.get("session_id") or "")
                    if sid:
                        sessions.set(a.chat, sid, session_title(a.chat))
                    finish(a, a.feed.finish(rc=rc, text=text, stopped=a.stop_sent))
            if not a.finished and a.think is not None:
                texts = a.think.read()
                if texts:
                    # SK-45: think-дельта — вывод потомка: модель жива, просто
                    # долго думает без инструментов. Это вывод, а не зависание.
                    a.last_out = time.time()
                    a.last_seen = "thinking: " + clip("".join(texts), 80)
                a.think_acc.extend(texts)
                joined = "".join(a.think_acc)
                now = time.time()
                if joined and (len(joined) >= THINK_MAX_CHUNK
                               or (now - a.think_last) >= THINK_FLUSH_S):
                    a.think_acc, a.think_last = [], now
                    emit(a, a.feed.on_think(joined[:THINK_MAX_CHUNK]))
            if not a.finished and not a.stop_sent and os.path.exists(STOP):
                a.stop_sent = True
                a.stop_at = time.time()
                log(f"hub/stop → SIGTERM потомку (задача {a.tid})")
                kill_child(a.proc)
            if a.stop_sent and not a.finished and not a.got_result \
                    and time.time() - a.stop_at > STOP_KILL_GRACE_S:
                kill_child(a.proc, signal.SIGKILL)
                finish(a, a.feed.finish(rc=143, text="", stopped=True))
            # SK-45 watchdog зависания (аудит 22.10 п.3): stdout/think потомка И
            # rpc задачи молчат ОБА дольше WATCHDOG_S — это зависание, а не «долго
            # думает». SIGTERM группе; потомок, которого SIGTERM не берёт (залип
            # в SIGSTOP — приёмка, или игнорирует), добивается эскалацией ниже.
            # Ожидание человека — не зависание (та же экзепция, что у idle).
            blocked = os.path.exists(PAUSED_F) or bool(rpcw.pending)
            if not a.finished and not a.stop_sent and \
                    hang_expired(a.last_out, a.last_rpc, time.time(), blocked):
                a.idle_paused = True  # пауза «человеческая»: реплика возобновит (SK-29)
                pause_task(a, hang_text(a.last_seen))
                a.hang_kill_at = time.time() + STOP_KILL_GRACE_S
                log(f"SK-45: задача {a.tid} зависла (тишина с "
                    f"{time.strftime('%H:%M:%S', time.localtime(max(a.last_out, a.last_rpc)))}, "
                    f"последнее: {clip(a.last_seen, 60)}) — SIGTERM, пауза")
            # SK-29 idle-close: повисшая задача (аудит 21.10: sk-ozon-cart 69447 с)
            # не должна жить вечно — без stdout/rpc/чата дольше IDLE_S пауза с
            # подсказкой. Ожидание человека (handoff hub/paused или незакрытый
            # вердикт гейта) — НЕ зависание: убивать нельзя.
            if not a.pause_sent and not a.finished and not a.stop_sent and idle_expired(
                    a.last_event, time.time(), blocked):
                a.idle_paused = True
                pause_task(a, f"⏸ Без активности {max(1, int(IDLE_S // 60))} мин — "
                              "задача на паузе. Напиши сообщение — продолжу сессию "
                              "(или начну новую задачу)")
        # 3.4 SK-45: эскалация зависшего SIGTERM — SIGSTOP-нутый/упрямый потомок
        #     от SIGTERM не умирает (сигнал висит pending), а задача уже в paused:
        #     процесс не должен переживать свою паузу — SIGKILL после grace и
        #     reap через poll(). Протыкает и редкий случай «человек ответил
        #     раньше эскалации»: спавн нового потомка сам добивает пережиток.
        for a in list(actives.values()) + list(paused.values()):
            if a.hang_kill_at and time.time() >= a.hang_kill_at:
                if a.proc is None or a.proc.poll() is not None:
                    a.hang_kill_at = 0.0  # умер от SIGTERM сам — эскалация не нужна
                else:
                    kill_child(a.proc, signal.SIGKILL)
                    if not a.hang_killed:
                        a.hang_killed = True
                        log(f"SK-45: потомок {a.tid} не умер от SIGTERM — SIGKILL")
                    a.hang_kill_at = time.time() + STOP_KILL_GRACE_S
        # 3.5 SK-28: канал подтверждённо мёртв дольше EXT_PAUSE_AFTER_S — ставим живые
        #     задачи на паузу (ребёнок без канала только долбит rpc; человек предупреждён).
        #     Подтверждение нужно, чтобы не дёргать задачи на блипах и рестартах моста.
        if chan != "ext_online" and chan_bad_since \
                and now - chan_bad_since >= EXT_PAUSE_AFTER_S:
            for a in list(actives.values()):
                # потомок уже сдох (результат на прочтении в pump) — не паузим
                # дозавершающуюся задачу, иначе переживём finish как «паузу»
                if a.proc is not None and a.proc.poll() is not None:
                    continue
                pause_task(a, CHAN_HINT.get(chan, "⏸ Браузерный канал недоступен"))
        # 4. старт следующих задач из очереди: есть слот, и в чате нет живой задачи
        #    (сообщение чата с активной задачей «направляет» её — дождётся закрытия);
        #    hub/stop стоит — но новое сообщение человека и есть «продолжай»
        #    (AGENTS.md п.4): снимаем флаг и стартуем.
        #    SK-28: при подтверждённо мёртвом канале не стартуем — сообщение дождётся
        #    переподключения в очереди (оно уже в ленте человека, курсор не коммитится).
        chan_down = (chan != "ext_online" and chan_bad_since
                     and now - chan_bad_since >= EXT_PAUSE_AFTER_S)
        started_any = True
        while started_any and q and len(actives) < MAX_ACTIVE and not chan_down:
            started_any = False
            for msg in list(q):
                # SK-29: idle-приостановленная задача чата — новое сообщение либо
                # возобновляет её (окно диалога жив: «следующая реплика возобновляет»),
                # либо abandonит (окно просрочено: «...или открывает новую»).
                # Задача обязана закрываться — abandon даёт терминальный кадр.
                idle_a = next((p for p in paused.values()
                               if p.idle_paused and p.chat == msg["chat"]), None)
                if idle_a is not None:
                    ent = sessions.entry(msg["chat"])
                    if not new_session_needed(
                            {"sid": ent.get("sid") or idle_a.resume_sid,
                             "updated": ent.get("updated")}, time.time()):
                        q.remove(msg)
                        paused.pop(idle_a.tid, None)
                        idle_a.text = msg["text"]  # реплика человека и есть «продолжай»
                        idle_a.src_end = msg["end"]
                        idle_a.pause_sent = False
                        idle_a.idle_paused = False
                        emit(idle_a, idle_a.feed.resume())
                        actives[idle_a.tid] = idle_a
                        sid = ent.get("sid") or idle_a.resume_sid
                        log(f"задача {idle_a.tid} возобновлена после idle "
                            f"(resume={sid or 'нет'})")
                        spawn_child(idle_a, sid)
                        started_any = True
                        break
                    emit(idle_a, idle_a.feed.abandon(
                        "⏱ Пауза задачи просрочила окно диалога — закрываю её; "
                        "продолжаю новой задачей в новой сессии"))
                    paused.pop(idle_a.tid, None)
                    commit_cursor()
                    continue  # сообщение остаётся в очереди — стартует новой задачей
                # SK-28: приостановленная задача чата — тоже «живая» (одна на чат, SK-25):
                # сообщение дождётся resume, а не запустит второго ребёнка в том же чате
                if any(a.chat == msg["chat"]
                       for a in list(actives.values()) + list(paused.values())):
                    continue
                q.remove(msg)
                tid = f"sk{int(time.time()) % 100000:05d}-{uuid.uuid4().hex[:4]}"
                if os.path.exists(STOP):
                    log("пауза hub/stop: новое сообщение человека снимает паузу")
                try:
                    os.remove(STOP)
                except OSError:
                    pass
                # SK-29 граница гигиены: просроченное окно / нет сессии → новая
                # ЗАДАЧА → новая сессия (старт без resume, имя после init); внутри
                # окна — продолжение диалога (resume той же сессии).
                ent = sessions.entry(msg["chat"])
                fresh = new_session_needed(ent, time.time())
                a = Active(msg["chat"], msg["text"], msg["end"], tid,
                           None if fresh else ent["sid"], new_task=fresh,
                           prev_sid=(ent.get("sid") if fresh else None))
                if fresh:
                    try:
                        a.prev_updated = float(ent.get("updated") or 0)
                    except (TypeError, ValueError):
                        a.prev_updated = 0.0
                actives[tid] = a
                log(f"задача {tid} chat={msg['chat']} "
                    f"{'новая сессия' if fresh else 'resume=' + str(a.resume_sid or 'нет')}: "
                    f"{clip(msg['text'], 60)} (активных {len(actives)})")
                emit(a, a.feed.start())
                spawn_child(a, a.resume_sid)
                started_any = True
                break
        time.sleep(POLL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
