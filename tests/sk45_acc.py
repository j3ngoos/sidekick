#!/usr/bin/env python3
"""SK-45 живая приёмка: watchdog зависания (3 мин) + честный retry переименования.

Аудит №2 22.10: проблема 3 (sk79745 завис в thinking ровно на 10 мин, спас только
ручной Stop) и проблема 9 (rc=1 «No session» на каждой новой сессии — rename
успевал до коммита строки в state.db; замер харнессом 23.10: init→строка 1.41 с).

Стенд: изолированные хабы :8797 (мост + agentd#1 с НАСТОЯЩИМ hermes, watchdog
по умолчанию 180 с) и hub2 без моста (agentd#2 с ФЕЙКОВЫМ hermes, watchdog 20 с —
детерминированные сценарии blocked/stop). Тест-копия расширения
(FALLBACK_SERVER->:8797) в firefox-158 (Marionette 2929). WS-клиента харнесс НЕ
поднимает: SK-49 политика одного соединения вытеснила бы расширение (close 4009) —
сообщения уходят ЧЕРЕЗ САЙДБАР (реальный путь пользователя: композер #in + Enter),
а кадры читаются из ленты #log системными привилегиями Marionette. Прод :8791 не
трогаем: в конце — удаление temp-аддона. state.db общий с продом (rename читает
его по-настоящему), поэтому чаты генерятся сайдбаром, KEEP=10000 (ротация
прод-сессий глушится), а sk45-сессии после приёмки архивируются.

Фазы:
  S0  стенд: мост :8797, XPI, firefox+sidebar, agentd#1 (real), agentd#2 (fake)
  R   rename race: 10 новых чатов через «+» сайдбара -> 10/10 titles в state.db,
      в agentd.log ни rc=1, ни «No session», ни «не появилась»
  W   watchdog: реальная задача думает и молчит (SIGSTOP потомка) -> пауза
      «зависла (3 мин тишины)» ≤235 с, SIGSTOP-нутый переживает SIGTERM ->
      эскалация SIGKILL, скрин сайдбара + таймстампы
  W2  resume репликой: ▶ + новый потомок; hub/stop -> «■ Stop» (ручной Stop цел)
  B   blocked = не зависание: висячий rpc-вердикт глушит watchdog на 3.5 порога;
      после вердикта — пауза за ≤35 с (agentd#2, wd=20)
  S   живые think-дельты watchdog не копят; hub/stop -> «■ Stop», агентд жив
  Z   уборка: агентды/мост/дети долой, temp-аддон удалён, sk45-*-сессии
      заархивированы, тест-Firefox остаётся живым стендом агента (:9393)
"""
import asyncio
import glob as globmod
import json
import os
import pathlib
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
S = pathlib.Path(os.environ.get("SK45_SCRATCH",
                                str(HOME / ".hermes/cache/scratch/sk45/acc")))
HUB1, HUB2 = S / "hub1", S / "hub2"
ST1, ST2 = S / "state1", S / "state2"
SHOTS = S / "shots"
EXT_SRC, EXT_XPI = S / "ext", S / "ext.xpi"
FAKE = S / "fake_hermes.py"
STATE_DB = os.environ.get("SIDEKICK_STATE_DB") or str(
    HOME / ".hermes/profiles/sidekick/state.db")
HERMES_PY = str(HOME / ".hermes/tools/python-3.14.7+20260901-linux-x64/bin/python3")
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
PORT, MAR_PORT = 8797, 2929
results = {}

sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
from mario import Marionette  # noqa: E402


def note(name, ok, detail=""):
    ok_v = None if ok == "skip" else bool(ok)
    results[name] = {"ok": ok_v, "detail": str(detail)[:400]}
    print(("PASS " if ok_v else ("SKIP " if ok_v is None else "FAIL ")) + name +
          ("  " + str(detail)[:200] if detail else ""), flush=True)


def ss_count(port):
    r = subprocess.run(["ss", "-tn", "state", "established", f"( sport = :{port} )"],
                       capture_output=True, text=True)
    return max(0, len(r.stdout.strip().splitlines()) - 1)


def listener_pid(port):
    r = subprocess.run(["ss", "-tlnp", f"( sport = :{port} )"],
                       capture_output=True, text=True)
    m2 = re.search(r"pid=(\d+)", r.stdout)
    return int(m2.group(1)) if m2 else None


def shot(name):
    p = SHOTS / f"{name}.png"
    subprocess.run(["scrot", "-o", str(p)], env=ENV, capture_output=True)
    return p.exists()


# ------------------------------------------------------------- firefox стенд --
# firefox-158 на :10 в этот день умерся сам по себе несколько раз (замеров
# причины нет: ни OOM, ни coredump в journal). Харнесс это переживает:
# ResilientM при обрыве переподключается, при надобности поднимает firefox
# заново и перепрогоняет UI-бутстрап (temp-XPI + сайдбар-вкладка).

def ff_pids():
    r = subprocess.run(["pgrep", "-f", "/firefox-158/firefox-bin"],
                       capture_output=True, text=True)
    return {int(x) for x in r.stdout.split()}


def ff_watch(stop, logp):
    seen = set()
    with open(logp, "a", encoding="utf-8") as lg:
        while not stop.is_set():
            cur = ff_pids()
            for p in cur - seen:
                lg.write(f"{time.strftime('%H:%M:%S')} BIRTH {p}\n"); lg.flush()
            for p in seen - cur:
                lg.write(f"{time.strftime('%H:%M:%S')} DEATH {p}\n"); lg.flush()
            seen = cur
            stop.wait(5.0)


def ensure_ff():
    if not listener_pid(MAR_PORT):
        subprocess.Popen(
            [str(HOME / ".local/firefox-158/firefox"), "--marionette",
             "--remote-debugging-port=9393", "--remote-allow-system-access",
             "--profile", str(HOME / ".firefox-test-profile"), "--no-remote"],
            env={**ENV, "MOZ_DISABLE_CONTENT_SANDBOX": "1"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        t1 = time.time()
        while time.time() - t1 < 45 and not listener_pid(MAR_PORT):
            time.sleep(1.0)
    t1 = time.time()
    while time.time() - t1 < 40:
        try:
            mm = Marionette("127.0.0.1", MAR_PORT)
            if mm.script("return 1+1;", timeout=10) == 2:
                return mm
            mm.close()
        except Exception:
            time.sleep(2.0)
    raise RuntimeError("Marionette не отвечает здраво (:2929)")


class ResilientM:
    """Прокси Marionette: обрыв/смерть стенда -> reconnect -> UI-бутстрап -> retry."""

    def __init__(self, bootstrap):
        self._bootstrap, self._m = bootstrap, None

    def warm(self):
        self._ensure()
        return getattr(self, "_url", "")

    def _ensure(self):
        if self._m is None:
            self._m = ensure_ff()
            self._url = self._bootstrap(self._m)

    def _call(self, meth, js, args, timeout):
        last = None
        for _ in range(4):
            try:
                self._ensure()
                return getattr(self._m, meth)(js, args=args, timeout=timeout)
            except Exception as e:
                last = e
                try:
                    if self._m:
                        self._m.close()
                except Exception:
                    pass
                self._m = None
                time.sleep(3.0)
        raise last if last else RuntimeError("UI-вызов не выполнен")

    def ascript(self, js, args=None, timeout=25):
        return self._call("ascript", js, args, timeout)

    def script(self, js, args=None, timeout=15):
        return self._call("script", js, args, timeout)

    def sb(self, op, arg=None, timeout=25):
        """DOM-операция над сайдбаром с теми же retry, что и _call."""
        last = None
        for _ in range(4):
            try:
                self._ensure()
                return sb_raw(self._m, op, arg)
            except Exception as e:
                last = e
                try:
                    if self._m:
                        self._m.close()
                except Exception:
                    pass
                self._m = None
                time.sleep(3.0)
        raise last if last else RuntimeError("SB-вызов не выполнен")

    def close(self):
        try:
            if self._m:
                self._m.close()
        except Exception:
            pass
        self._m = None


def find_child(hub, timeout=15.0):
    """PID потомка agentd'а этого хаба: cmdline `chat --format stream-json` +
    environ SIDEKICK_HOME=<hub> (не путать с детьми прод-агентда)."""
    hub = str(hub)
    t0 = time.time()
    while time.time() - t0 < timeout:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                cl = open(f"/proc/{pid}/cmdline", "rb").read() \
                    .replace(b"\0", b" ").decode(errors="replace")
                if "chat --format stream-json" not in cl:
                    continue
                envb = open(f"/proc/{pid}/environ", "rb").read() \
                    .replace(b"\0", b"\n").decode(errors="replace")
                if f"SIDEKICK_HOME={hub}\n" in envb + "\n":
                    return int(pid)
            except OSError:
                continue
        time.sleep(0.4)
    return None


def db_rows(sql, args=()):
    try:
        db = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
        rows = db.execute(sql, args).fetchall()
        db.close()
        return rows
    except sqlite3.Error:
        return []


def state_title(sid):
    rows = db_rows("SELECT title FROM sessions WHERE id=?", (sid,))
    return rows[0][0] if rows else None


def session_ended(sid):
    rows = db_rows("SELECT ended_at FROM sessions WHERE id=?", (sid,))
    return bool(rows and rows[0][0])


def sessions_map(hub):
    try:
        return json.loads((pathlib.Path(hub) / "sessions.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def log_text(hub):
    try:
        return (pathlib.Path(hub) / "agentd.log").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""


def outbox_frames(hub):
    out = []
    try:
        for ln in (pathlib.Path(hub) / "outbox.jsonl").read_text(
                encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def wait_outbox(hub, pred, timeout=60, step=0.3):
    """Файловый outbox (hub2 без моста): ждём кадр-предикат -> (ts, frame)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for f in outbox_frames(hub):
            if pred(f):
                return time.time(), f
        time.sleep(step)
    return None, None


def fresh_think(run_dir, since):
    """Свежий непустой think-файл (плагин sidekick-think пишет дельты) —
    доказательство, что модель живой мыслью думает, а не зависла на старте."""
    for p in globmod.glob(str(pathlib.Path(run_dir) / "think-*.jsonl")):
        try:
            st = os.stat(p)
            if st.st_mtime > since and st.st_size > 0:
                return p
        except OSError:
            pass
    return None


# ------------------------------------------------------------- fake hermes ---

FAKE_SRC = '''#!/usr/bin/env python3
"""Фейковый hermes для SK-45 приёмки: детерминированные потомки без модели."""
import json, os, re, signal, sys, time

argv = sys.argv[1:]
rest = argv[2:] if argv[:1] == ["-p"] else argv
if rest and rest[0] == "sessions":
    sys.exit(0)  # rename: фейк без session_id его не зовёт; на всякий — успех
if not rest or rest[0] != "chat":
    sys.exit(2)
q = sys.stdin.read()
m = re.search(r"task=(\\S+)\\]", q)
tid = m.group(1) if m else ""
hub = os.environ.get("SIDEKICK_HOME", "")
think = os.environ.get("SIDEKICK_THINK_FILE", "")


def emit(o):
    print(json.dumps(o, ensure_ascii=False), flush=True)


emit({"type": "system", "subtype": "init", "session_id": ""})
if "SK45BLOCK" in q:
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "terminal", "input": {"command": "жду вердикт"}}]}})
    if hub:
        with open(os.path.join(hub, "rpc_in.jsonl"), "a") as f:
            f.write(json.dumps({"v": 1, "id": "sk45rpc-" + tid, "method": "perm.check",
                                "params": {"domain": "sk45.test", "action": "input",
                                           "task": tid}}) + "\\n")
    while True:
        time.sleep(1)
if "SK45STOP" in q:
    def bye(sig, fr):
        emit({"type": "result", "exit_code": 130, "is_error": True})
        sys.exit(130)
    signal.signal(signal.SIGTERM, bye)
    i = 0
    while True:
        i += 1
        if think:
            try:
                with open(think, "a") as f:
                    f.write(json.dumps({"kind": "delta", "delta": "шаг %d" % i}) + "\\n")
            except OSError:
                pass
        time.sleep(2)
emit({"type": "result", "exit_code": 0, "is_error": False})
'''

# ------------------------------------------------------------- browser JS ----

ADDON_INSTALL_JS = """
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
    const nsFile = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
    nsFile.initWithPath(arguments[0]);
    const a = await AddonManager.installTemporaryAddon(nsFile);
    const byId = await AddonManager.getAddonByID("hermes-sidekick@hermes.local");
    const ext = a || byId;
    // FF158: resourceURI/baseURI у temp-аддона не экспортируются (baseURI даёт
    // jar:-URI). Единственный живой источник — WebExtensionPolicy:
    // mozExtensionHostname даёт хост moz-extension://<uuid>/ (с дефисами)
    let url = "";
    try {
      const p = WebExtensionPolicy.getByID("hermes-sidekick@hermes.local");
      if (p && p.mozExtensionHostname)
        url = "moz-extension://" + p.mozExtensionHostname + "/";
    } catch (e) {}
    resolve({ok: true, id: ext && ext.id, url});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""

ADDON_UNINSTALL_JS = """
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
    const a = await AddonManager.getAddonByID("hermes-sidekick@hermes.local");
    if (!a) return resolve("none");
    await a.uninstall();  // FF158: AddonManager.uninstallAddon нет в ES-экспорте, метод на аддоне
    resolve("uninstalled");
  } catch (e) { resolve({err: String(e && e.message || e).slice(0, 160)}); }
})();
"""

OPEN_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
               "const t = win.gBrowser.addTrustedTab(arguments[0]); win.gBrowser.selectedTab = t;"
               "return 'tab'; })();")
CLOSE_SIDEBAR_TAB_JS = (
    "return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
    "for (let i = win.gBrowser.tabs.length - 1; i >= 0; i--) {"
    " const b = win.gBrowser.tabs[i].linkedBrowser;"
    " if (String(b.currentURI && b.currentURI.spec).includes('sidebar.html'))"
    " win.gBrowser.removeTab(win.gBrowser.tabs[i]);}"
    " return 'closed';})();")

# sidebar.html открыт ВКЛАДКОЙ (в этом билде firefox новая sidebar-tabs UI, где
# панель расширения не инстанцируется — vertical-tabs стенд 23.10): тот же DOM,
# тот же композер и порт. FF158/Fission: chrome-скрипт больше не видит
# contentWindow remote-вкладки (browsingContext.window === null), поэтому DOM
# сайдбера трогается из content-контекста Marionette (SwitchToWindow на вкладку).

# чистый page-side DOM (без Services/chrome-API — исполняется в content-контексте)
SB_JS = r"""
const resolve = arguments[arguments.length - 1];
(async () => {
 try {
 const op = arguments[0], arg = arguments[1] || "";
 const d = document;
 if (!d.getElementById('log')) return resolve('SIDEBAR-NOT-LOADED-YET');
 if (op === 'href') return resolve('OK log=' + d.querySelectorAll('#log > *').length);
 if (op === 'new') { d.getElementById('newBtn').click(); await new Promise(r => setTimeout(r, 700)); return resolve('new'); }
 if (op === 'send') {
   const inp = d.getElementById('in');
   inp.value = arg;
   inp.dispatchEvent(new Event('input', {bubbles: true}));
   inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
   await new Promise(r => setTimeout(r, 250));
   return resolve('sent:' + (inp.value === "" ? "ok" : "FIELD-NOT-CLEARED")); }
 if (op === 'feed-has') return resolve(d.getElementById('log').textContent.includes(arg) ? 'HAS' : 'MISSING');
 if (op === 'set-token') {
   // страница расширения = browser.* доступен; путь ровно тот, что у options.js
   await browser.storage.local.set({ bridgeToken: arg });
   try { await browser.runtime.sendMessage({ type: "token-updated" }); } catch (e) {}
   return resolve('token-set'); }
 resolve('bad-op');
 } catch (e) { resolve('SB-ERR:' + String(e && e.message || e).slice(0, 140)); }
})();
"""


def sb_raw(mm, op, arg=None):
    """DOM-операция над сайдбаром: content ctx -> GetWindowHandles ->
    SwitchToWindow(вкладка sidebar.html) -> page JS. Канонический Marionette
    мульти-таб флоу: в FF158 chrome не видит contentWindow remote-вкладки, а
    кэш выбранного браузера на сессию не обновляется при смене вкладки."""
    mm.cmd("Marionette:SetContext", {"value": "content"})
    try:
        handles = mm.cmd("WebDriver:GetWindowHandles") or []
        target = None
        for h in handles:
            hh = h.get("handle", h) if isinstance(h, dict) else h
            mm.cmd("WebDriver:SwitchToWindow", {"handle": hh})
            try:
                u = mm.script("return document.location.href;", timeout=10)
            except Exception:
                continue
            if "sidebar.html" in str(u):
                target = hh
                break
        if not target:
            raise RuntimeError("NO-SIDEBAR-TAB")
        mm.cmd("WebDriver:SwitchToWindow", {"handle": target})
        return mm.ascript(SB_JS, args=[op, arg or ""], timeout=25)
    finally:
        mm.cmd("Marionette:SetContext", {"value": "chrome"})


def build_xpi():
    """Тест-копия расширения: FALLBACK_SERVER -> тест-мост :8797 и никаких
    saved-URL — чтобы ни один тестовый чат не ушёл в прод :8791."""
    if EXT_SRC.exists():
        shutil.rmtree(EXT_SRC)
    shutil.copytree(REPO / "extension", EXT_SRC,
                    ignore=shutil.ignore_patterns(".chrome"))
    bj = EXT_SRC / "background.js"
    src = bj.read_text(encoding="utf-8")
    assert 'const FALLBACK_SERVER = "ws://127.0.0.1:8791";' in src
    src = src.replace('const FALLBACK_SERVER = "ws://127.0.0.1:8791";',
                      f'const FALLBACK_SERVER = "ws://127.0.0.1:{PORT}";')
    assert "for (const u of [saved, FALLBACK_SERVER])" in src
    src = src.replace("for (const u of [saved, FALLBACK_SERVER])",
                      "for (const u of [FALLBACK_SERVER])")
    bj.write_text(src, encoding="utf-8")
    with zipfile.ZipFile(EXT_XPI, "w", zipfile.ZIP_DEFLATED) as z:
        for p in EXT_SRC.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(EXT_SRC))


# ------------------------------------------------------------------ helpers --

def sb(m, op, arg=None):
    if isinstance(m, ResilientM):
        return m.sb(op, arg)
    return sb_raw(m, op, arg)


def ui_bootstrap(mm):
    """UI-состояние харнесса: temp-XPI + сайдбар-вкладка. Идемпотентен,
    вызывается ResilientM при каждом (пере)подключении."""
    mm.ascript(ADDON_UNINSTALL_JS, timeout=25)  # сносим остаток temp-аддона
    r = mm.ascript(ADDON_INSTALL_JS, args=[str(EXT_XPI.resolve())], timeout=40)
    url = (r or {}).get("url", "") if isinstance(r, dict) else ""
    if not url.startswith("moz-extension://"):
        raise RuntimeError(f"XPI не встал/без URL: {r}")
    # снос temp-аддона превращает его старые вкладки в дохлые доки — убираем,
    # чтобы content-контекст не натыкался на «no such window»
    try:
        mm.script(CLOSE_SIDEBAR_TAB_JS, timeout=15)
    except Exception:
        pass
    mm.script(OPEN_TAB_JS, args=[url.rstrip("/") + "/sidebar.html"], timeout=15)
    t1, side = time.time(), ""
    while time.time() - t1 < 30:
        try:
            side = str(sb_raw(mm, "href"))
        except Exception as e:
            side = repr(e)[:80]
        if side.startswith("OK"):
            break
        time.sleep(1.0)
    if not side.startswith("OK"):
        raise RuntimeError(f"сайдбар не ожил: {side}")
    # токен моста: temp-аддон стартует с пустым storage на чистом профиле —
    # заводим bridgeToken тем же путём, что и options.js (иначе no_token и
    # ни одного коннекта, S0c мёртвый стенд)
    tokp = HUB1 / "token"
    if tokp.exists():
        r = str(sb_raw(mm, "set-token", tokp.read_text(encoding="utf-8").strip()))
        if not r.startswith("token-set"):
            raise RuntimeError(f"токен не заведён: {r}")
    return url


def poll_feed(m, needle, timeout, step=0.5):
    """Момент, когда лента сайдбара показала подстроку (настоящая отрисовка)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if "HAS" in str(sb(m, "feed-has", needle)):
            return time.time()
        time.sleep(step)
    return None


def f_task(tid=None, marker=None, state=None):
    def p(f):
        if f.get("type") != "task":
            return False
        if tid and f.get("id") != tid:
            return False
        if marker and marker not in str(f.get("title", "")):
            return False
        if state and f.get("state") != state:
            return False
        return True
    return p


def f_chat(tid, needle):
    return lambda f: (f.get("type") == "chat" and f.get("task") == tid
                      and needle in str(f.get("text", "")))


def f_think(tid):
    return lambda f: f.get("type") == "think" and f.get("task") == tid and f.get("text")


# ------------------------------------------------------------------- phases --

async def phase_r(ctx):
    m = ctx["m"]
    before = set(sessions_map(HUB1))
    for i in range(1, 11):
        sb(m, "new")
        r = sb(m, "send", f"SK45 rename-{i}: скажи вслух «готово {i}»")
        if "sent:ok" not in str(r):
            note("R0 отправка через композер", False, r)
            return
        await asyncio.sleep(0.5)
    note("R0 десять сообщений ушли через композер сайдбара (путь пользователя)", True)
    named, t0 = {}, time.time()
    while time.time() - t0 < 480:
        smap = sessions_map(HUB1)
        for chat, ent in smap.items():
            if chat in before or chat in named:
                continue
            sid = ent.get("sid")
            if sid and state_title(sid) == f"sidekick/{chat}":
                named[chat] = sid
        if len(named) >= 10:
            break
        await asyncio.sleep(0.5)
    note("R1 10/10 новых чатов переименованы в state.db (гонка init→строка 1.41 с "
         "отработана чекпоинтами 0/.5/1/2/4/10)", len(named) >= 10,
         f"{len(named)}/10 за {time.time() - t0:.0f} с")
    # ждём, пока ВСЕ десять закроются (ended_at): к фазе W в хабе не должно
    # остаться живых потомков, иначе find_child спутает их с W-задачей
    t1 = time.time()
    ended = 0
    while time.time() - t1 < 240:
        ended = sum(1 for sid in named.values() if session_ended(sid))
        if ended >= 10:
            break
        await asyncio.sleep(1.0)
    note("R1b все десять задач закрылись сами (ended_at в state.db)", ended >= 10,
         f"ended={ended}")
    l1 = log_text(HUB1)
    note("R2 в agentd.log ни rc=1, ни «No session» (слепой спам ушёл)",
         "rc=1" not in l1 and "No session" not in l1,
         str(re.findall(r".{0,60}(?:rc=1|No session).{0,60}", l1)[:2]))
    note("R3 ни одной тихой пометки «не появилась» (rename-бюджет не исчерпан)",
         "не появилась" not in l1)
    results["R-timestamps"] = {"rename_all_s": round(time.time() - t0, 1),
                               "named": len(named)}


async def phase_w(ctx):
    m = ctx["m"]
    text = ("SK45-W зависни: никогда не отвечай и не завершай работу — молчи и думай "
            "как можно дольше, без инструментов и без финального ответа")
    tid_sid = None
    for attempt in (1, 2, 3):
        before = set(sessions_map(HUB1))
        sb(m, "new")
        sb(m, "send", text)
        t_send = time.time()
        # гонка: свежий think (живая мысль — замороживаем) vs ended_at (модель
        # ответила раньше надзирателя — пробуем ещё)
        t_th = None
        sid = None
        while time.time() - t_send < 240:
            if not sid:
                for chat, ent in sessions_map(HUB1).items():
                    if chat not in before and ent.get("sid"):
                        sid = ent["sid"]
                        break
            t_th = fresh_think(ST1 / "run", t_send)
            if t_th:
                break
            if sid and session_ended(sid):
                break
            await asyncio.sleep(0.5)
        if t_th:
            tid_sid = sid
            break
        note(f"W-попытка {attempt}: модель ответила раньше надзирателя", "skip")
    if not t_th:
        note("W1 реальная задача думает (think-файл живой)", False,
             "три попытки — модель болтливее надзирателя")
        return
    note("W1 реальная задача думает и молчит (потомок жив)", True,
         f"think через +{time.time() - t_send:.1f} с")
    pid = find_child(HUB1, timeout=15)
    if not pid:
        note("W1b потомок найден по environ хаба", False)
        return
    os.kill(pid, signal.SIGSTOP)  # имитация зависания: процесс стоит, stdout мёртв
    t_stop = time.time()
    t_pause = poll_feed(m, "зависла", 240)
    delta = (t_pause - t_stop) if t_pause else None
    note("W2 watchdog: карточка «зависла» в ленте ≤235 с после заморозки (порог 180 с)",
         t_pause is not None and delta is not None and 100 <= delta <= 235,
         f"delta={delta:.1f} с" if delta is not None else "карточки нет")
    note("W3 в чате явная строка «остановил, вот что было последним»",
         poll_feed(m, "остановил, вот что было последним", 5) is not None)
    dead = None
    if t_pause:
        t1 = time.time()
        while time.time() - t1 < 25:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                dead = time.time()
                break
            time.sleep(0.4)
    note("W5 SIGSTOP-нутый пережил SIGTERM и добит эскалацией SIGKILL",
         bool(dead) and t_pause and (dead - t_pause) <= 20,
         f"+{(dead - t_pause):.1f} с после паузы" if dead and t_pause else "жив")
    l1 = log_text(HUB1)
    note("W6 в agentd.log строки надзирателя и эскалации",
         "SK-45: задача" in l1 and "SIGKILL" in l1)
    p = shot("sk45-w-pause")
    note("W7 скрин сайдбара: карточка зависания в ленте (настоящая отрисовка)",
         bool(p) and poll_feed(m, "зависла", 3) is not None, str(p))
    results["W-timestamps"] = {
        "think_seen_after_s": round(time.time() - t_send, 1),
        "sigstop": round(t_stop, 1),
        "pause": round(t_pause, 1) if t_pause else None,
        "stop_to_pause_s": round(delta, 1) if delta is not None else None,
        "pause_to_child_death_s": round(dead - t_pause, 1) if dead and t_pause else None}
    ctx["w_sid"] = tid_sid


async def phase_w2(ctx):
    m = ctx["m"]
    sb(m, "send", "продолжай (SK45 resume-проверка)")
    t_res = poll_feed(m, "▶", 90)
    note("W2a реплика человека возобновила зависшую задачу (▶ в ленте)",
         t_res is not None)
    pid = find_child(HUB1, timeout=25)
    note("W2b новый потомок возродился (сирота-пережиток не мешал)", bool(pid))
    (HUB1 / "stop").touch()
    t_fin = poll_feed(m, "■ Stop", 60)
    note("W2c hub/stop -> «■ Stop» (ручной Stop не сломан)", t_fin is not None)
    try:
        (HUB1 / "stop").unlink()
    except OSError:
        pass
    await asyncio.sleep(2)
    note("W2d агентд#1 жив после всех казней", ctx["a1"].poll() is None)


async def phase_b(ctx):
    loop = asyncio.get_event_loop()
    with open(HUB2 / "inbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"v": 1, "type": "chat", "role": "user",
                            "text": "SK45BLOCK: дождись вердикта гейта",
                            "chat": "sk45-b1"}, ensure_ascii=False) + "\n")
    t_task, tf = await loop.run_in_executor(
        None, lambda: wait_outbox(HUB2, f_task(marker="SK45BLOCK"), 40))
    if not t_task:
        note("B0 задача фейка завелась", False)
        return
    tid = tf["id"]
    rid = "sk45rpc-" + tid
    t_rpc = None
    t0 = time.time()
    while time.time() - t0 < 30:
        try:
            if rid in (HUB2 / "rpc_in.jsonl").read_text(encoding="utf-8"):
                t_rpc = time.time()
                break
        except OSError:
            pass
        await asyncio.sleep(0.3)
    note("B0 задача фейка + висячий perm.check (гейт ждёт человека)", bool(t_rpc))
    await asyncio.sleep(70)  # 3.5 порога watchdog'а (20 с) полной тишины
    l2 = log_text(HUB2)
    note("B1 ожидание вердикта — НЕ зависание: 70 с тишины, паузы нет",
         f"SK-45: задача {tid} зависла" not in l2
         and not any(f.get("type") == "chat" and f.get("task") == tid
                     and "зависла" in str(f.get("text", ""))
                     for f in outbox_frames(HUB2)))
    with open(HUB2 / "rpc_out.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"v": 1, "id": rid, "ok": True,
                            "result": {"decision": "allow"}}) + "\n")
    t_pause, pf = await loop.run_in_executor(
        None, lambda: wait_outbox(HUB2, f_chat(tid, "зависла"), 35))
    note("B2 после вердикта watchdog сработал за ≤35 с (порог 20 с)", bool(t_pause),
         str(pf.get("text", ""))[:100] if pf else "")
    note("B3 текст паузы считает тишину порогом фейка («20 с тишины»)",
         bool(pf) and "20 с тишины" in str(pf.get("text", "")))


async def phase_s(ctx):
    loop = asyncio.get_event_loop()
    with open(HUB2 / "inbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"v": 1, "type": "chat", "role": "user",
                            "text": "SK45STOP: думай долго, потом я тебя остановлю",
                            "chat": "sk45-s"}, ensure_ascii=False) + "\n")
    t_task, tf = await loop.run_in_executor(
        None, lambda: wait_outbox(HUB2, f_task(marker="SK45STOP"), 40))
    if not t_task:
        note("S0 задача stop-сценария завелась", False)
        return
    tid = tf["id"]
    t_th, _ = await loop.run_in_executor(
        None, lambda: wait_outbox(HUB2, f_think(tid), 30))
    note("S0 think-дельты пошли (потомок жив)", bool(t_th))
    await asyncio.sleep(14)  # дельты каждые 2 с — тише порога 20 с не опускаемся
    l2 = log_text(HUB2)
    note("S1 живые think-дельты watchdog не копят (14 с, паузы нет)",
         f"SK-45: задача {tid} зависла" not in l2)
    (HUB2 / "stop").touch()
    t_fin, ff = await loop.run_in_executor(
        None, lambda: wait_outbox(HUB2, f_chat(tid, "■ Stop"), 25))
    note("S2 hub/stop -> «■ Stop» failed-кадр", bool(t_fin))
    try:
        (HUB2 / "stop").unlink()
    except OSError:
        pass
    await asyncio.sleep(2)
    note("S3 потомок умер, агентд#2 жив",
         find_child(HUB2, timeout=3) is None and ctx["a2"].poll() is None)


# --------------------------------------------------------------------- main --

def main():
    if S.exists():
        shutil.rmtree(S)
    for d in (S, SHOTS, HUB1, HUB2, ST1, ST2):
        d.mkdir(parents=True)
    shutil.copy(HOME / "sidekick/bridge/hub/token", HUB1 / "token")
    FAKE.write_text(FAKE_SRC, encoding="utf-8")
    os.chmod(FAKE, 0o755)
    prod_before = ss_count(8791)
    ctx, br, m, a1, a2 = {}, None, None, None, None
    health_stop = threading.Event()
    ff_stop = threading.Event()

    def health2():
        while not health_stop.is_set():
            (HUB2 / "health.json").write_text(json.dumps(
                {"bridge": "active", "extension": "connected",
                 "ts": int(time.time()), "ws_count": 1}), encoding="utf-8")
            health_stop.wait(8)

    try:
        # ---------- S0 ----------
        build_xpi()
        for _ in range(10):
            zpid = listener_pid(PORT)
            if not zpid:
                break
            subprocess.run(["kill", "-9", str(zpid)])
            time.sleep(0.4)
        br = subprocess.Popen(["python3", str(REPO / "bridge" / "sidekick_bridge.py")],
                              env={**os.environ, "SIDEKICK_HOME": str(HUB1),
                                   "SIDEKICK_PORT": str(PORT)},
                              stdout=open(HUB1 / "bridge.log", "w", encoding="utf-8"),
                              stderr=subprocess.STDOUT)
        time.sleep(1.2)
        note("S0a тест-мост слушает :8797", listener_pid(PORT) == br.pid)
        m = ResilientM(ui_bootstrap)
        threading.Thread(target=ff_watch, args=(ff_stop, S / "ff-watch.log"),
                         daemon=True).start()
        ext_url, side = "", ""
        try:
            ext_url = m.warm()
            ok_ui = ext_url.startswith("moz-extension://")
        except Exception as e:
            ok_ui = False
            ext_url = repr(e)[:200]
        note("S0b temp-XPI установлен, сайдбар-вкладка открыта (resilient)",
             ok_ui, ext_url)
        t1 = time.time()
        while time.time() - t1 < 60 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0c расширение подключено к :8797 (единственный сокет — SK-49)",
             ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод было {prod_before})")
        # sidebar.html — вкладкой (см. комментарий SB_JS): панель расширения в
        # этом билде не инстанцируется, а DOM/порт/композер те же
        side = str(sb(m, "href")) if ok_ui else "UI-DEAD"
        note("S0d сайдбар живой (композер и лента доступны)",
             side.startswith("OK"), side)
        if not (ok_ui and side.startswith("OK")):
            raise RuntimeError(f"UI-стенд не поднялся: {ext_url} / {side}")
        a1_env = {**os.environ, "SIDEKICK_HOME": str(HUB1),
                  "SIDEKICK_AGENTD_STATE": str(ST1), "SIDEKICK_AGENTD_KEEP": "10000",
                  "PYTHONUNBUFFERED": "1"}
        a1 = subprocess.Popen([HERMES_PY, str(REPO / "agentd" / "sidekick_agentd.py")],
                              env=a1_env, start_new_session=True,
                              stdout=open(HUB1 / "agentd.log", "w", encoding="utf-8"),
                              stderr=subprocess.STDOUT)
        a2_env = {**a1_env, "SIDEKICK_HOME": str(HUB2),
                  "SIDEKICK_AGENTD_STATE": str(ST2),
                  "SIDEKICK_AGENTD_WATCHDOG_S": "20",
                  "SIDEKICK_HERMES_BIN": str(FAKE)}
        a2 = subprocess.Popen([HERMES_PY, str(REPO / "agentd" / "sidekick_agentd.py")],
                              env=a2_env, start_new_session=True,
                              stdout=open(HUB2 / "agentd.log", "w", encoding="utf-8"),
                              stderr=subprocess.STDOUT)
        threading.Thread(target=health2, daemon=True).start()
        time.sleep(2.0)
        note("S0e агентды живы (real wd=180 / fake wd=20)",
             a1.poll() is None and a2.poll() is None)
        ctx.update(m=m, a1=a1, a2=a2, run_before=set(sessions_map(HUB1)))

        async def run():
            await phase_r(ctx)
            await phase_w(ctx)
            await phase_w2(ctx)
            await phase_b(ctx)
            await phase_s(ctx)

        asyncio.run(run())
    except Exception as e:
        import traceback
        traceback.print_exc()
        note("CRASH", False, repr(e)[:300])
    finally:
        # ---------- Z ----------
        health_stop.set()
        ff_stop.set()
        # детей агентдов (у них свой session) killpg агентда не забирает —
        # добираем по environ хаба
        for hub in (HUB1, HUB2):
            for _ in range(12):
                p = find_child(hub, timeout=1)
                if not p:
                    break
                try:
                    os.kill(p, signal.SIGKILL)
                except OSError:
                    break
        for a in (a1, a2):
            if a and a.poll() is None:
                try:
                    os.killpg(os.getpgid(a.pid), signal.SIGTERM)
                except OSError:
                    a.terminate()
        time.sleep(1.5)
        for a in (a1, a2):
            if a and a.poll() is None:
                try:
                    os.killpg(os.getpgid(a.pid), signal.SIGKILL)
                except OSError:
                    a.kill()
        if br:
            br.terminate()
            try:
                br.wait(timeout=5)
            except subprocess.TimeoutExpired:
                br.kill()
        # композер сам генерит chat-id (cmvkq...), а колонка chat_id в state.db
        # пуста — единственный точный селектор: заголовок sidekick/<chat>.
        # W-сессии остаются «открытыми» (потомка убил надзиратель, ended_at не
        # проставлен), а archive открытые строки пропускает — дозакрываем сами.
        new_chats = [c for c in sessions_map(HUB1)
                     if c not in ctx.get("run_before", set())]
        zbad = []
        for chat in new_chats:
            rr = subprocess.run([str(HOME / ".local/bin/hermes"), "-p", "sidekick",
                                 "sessions", "archive", "--title", f"sidekick/{chat}",
                                 "--yes"],
                                capture_output=True, text=True, timeout=60)
            if rr.returncode != 0:
                zbad.append(f"{chat}:rc{rr.returncode} " + (rr.stderr or "").strip()[:40])
                continue
            if "No sessions match" in (rr.stdout or ""):
                con = sqlite3.connect(STATE_DB, timeout=30)
                con.execute("""UPDATE sessions SET ended_at=?, archived=1
                               WHERE title=? AND archived=0""",
                            (time.time(), f"sidekick/{chat}"))
                con.commit()
                if not con.execute("SELECT archived FROM sessions WHERE title=?",
                                   (f"sidekick/{chat}",)).fetchone()[0]:
                    zbad.append(f"{chat}:не закрылась")
                con.close()
        note("Z0 сессии прототипа заархивированы (прод-ленту не мусорим)",
             not zbad, f"{len(new_chats)} чатов" + (f"; осечки: {zbad[:3]}" if zbad else ""))
        # temp-аддон живёт только пока жив firefox: сносить его перезапуском
        # стенда смысла нет — если firefox помер сам, аддон умер вместе с ним
        if listener_pid(MAR_PORT):
            try:
                m.script(CLOSE_SIDEBAR_TAB_JS)
            except Exception:
                pass
            try:
                r = m.ascript(ADDON_UNINSTALL_JS, timeout=25)
                time.sleep(2)
                note("Z1 temp-аддон удалён (на прод-канал тест-профиль не претендует)",
                     r in ("uninstalled", "none"), f"{r} @8797={ss_count(PORT)}")
            except Exception as e:
                note("Z1 temp-аддон удалён", False, repr(e)[:160])
        else:
            note("Z1 temp-аддон удалён (вместе с тест-Firefox)", True,
                 "стенд :2929 не слушается — temp-аддон не переживает смерть "
                 "процесса, персистентного состояния нет")
        try:
            m.close()
        except Exception:
            pass
        note("Z2 прод :8791 не тронут", ss_count(8791) >= 1,
             f"@8791={ss_count(8791)} (было {prod_before}); тест-Firefox оставлен "
             "живым стендом агента (:9393)")
        (S / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        scored = {k: v for k, v in results.items() if isinstance(v, dict) and "ok" in v}
        bad = [k for k, v in scored.items() if v["ok"] is False]
        print(f"\nSK45-ACC {'OK' if not bad else 'FAIL: ' + ', '.join(bad)} "
              f"({sum(1 for v in scored.values() if v['ok'])} pass, "
              f"{len(bad)} fail) report: {S / 'report.json'}", flush=True)
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
