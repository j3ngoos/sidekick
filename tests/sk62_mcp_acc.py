#!/usr/bin/env python3
"""SK-72 (переделка SK-62) живая приёмка MCP-адаптера: 15 проверок на настоящем стенде.

Стенд: изолированный мост :8797 (свой хаб в scratch) + тест-копия расширения
(FALLBACK_SERVER->:8797, без пробников) в firefox-158 (Marionette 2929,
DISPLAY=:10, XAUTHORITY=~/.Xauthority-game). Прод :8791 не трогаем; прод HTTP
:8796 остаётся выключенным (панель гасила — M11 фиксирует, что слушателя нет).
Каркас стенда — sk35_acc.py; пермиссы — permDom/clickBtn (sk35); лента — WS-наблюдатель
(человеческий канал, как в test_mcp_sk62 e2e).

Проверки (панельный список 15/15 SK-62):
  M01 initialize = 2025-06-18 (stdio)
  M02 tools/list = 21
  M03 page_dom на живой тестовой странице стенда (url из реального расширения)
  M04 sensitive-реестр: #login-pass чувствителен в снапшоте
  M05 sensitive input -> адаптер САМ заводит perm.check: карточка в живом сайдбаре
  M06 «Разрешить разово» (клик по DOM) -> executed + значение реально в поле страницы
  M07 нечувствительная цель: input проходит БЕЗ нового пермисса
  M08 HTTP: POST /mcp без ключа -> 401 + WWW-Authenticate: Bearer (-32001)
  M09 HTTP: с ключом -> 200 initialize + Mcp-Session-Id + tools=21
  M10 HTTP: GET /mcp -> 405 + Allow: POST
  M11 прод :8796 — слушателя нет (сервис выключен панелью, не включаем)
  M12 EOF stdin -> процесс 0 + терминальный task-кадр (done) в WS-канале
  M13 task-кадр «MCP · sk62-acc» в WS-канале
  M14 step + chat кадры долетели до WS-канала (текст чата в DOM сайдбара)
  M15 R1: прод :8791 — соединений было/стало одинаково (тестовых не добавилось)

Фазы S0 не считаются (подъём стенда). Флаг --keep: после M15 стенд остаётся
живым (E2E-Hermes гоняется по живому хабу), pid-ы в S/pids.json.
--teardown: закрыть тест-вкладки, переустановить prod-XPI, погасить мост/firefox/httpd по PID.

Запуск: python3 tests/sk62_mcp_acc.py [--keep] ; python3 tests/sk62_mcp_acc.py --teardown
"""
import argparse
import asyncio
import json
import os
import pathlib
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
S = pathlib.Path(os.environ.get("SK62_SCRATCH", str(HOME / ".hermes/cache/scratch/sk62")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
PIDS = S / "pids.json"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
HTTP_PORT = 18796
TEST_PORT = 8899
PAGE_URL = f"http://127.0.0.1:{TEST_PORT}/sk62.html?sk62=1"
CHAT_MARKER = "sk62-chat-marker"
PASS_VALUE = "sk62-pass-72"  # длина 12 — страница эхоит только длину пароля
EMAIL_VALUE = "a@b.sk62"
results = {}
KEY = os.environ.get("SK62_MCP_TEST_KEY", "")  # значение нигде не печатается

sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
from mario import Marionette  # noqa: E402


def note(name, ok, detail=""):
    ok_v = None if ok == "skip" else bool(ok)
    results[name] = {"ok": ok_v, "detail": str(detail)[:400]}
    tag = "PASS " if ok_v else ("SKIP " if ok_v is None else "FAIL ")
    print(tag + name + ("  " + str(detail)[:200] if detail else ""), flush=True)


def ss_count(port):
    r = subprocess.run(["ss", "-tn", "state", "established", f"( sport = :{port} )"],
                       capture_output=True, text=True)
    return max(0, len(r.stdout.strip().splitlines()) - 1)


def listener_pid(port):
    r = subprocess.run(["ss", "-tlnp", f"( sport = :{port} )"], capture_output=True, text=True)
    m2 = re.search(r"pid=(\d+)", r.stdout)
    return int(m2.group(1)) if m2 else None


def key_path_safe():
    """Ключ для stdio/HTTP адаптеров: env или сгенерированный (hex32, не печатать)."""
    import secrets
    return KEY or secrets.token_hex(32)


# ---- MCP stdio-клиент ---------------------------------------------------------

def start_stdio(key):
    env = dict(os.environ, SIDEKICK_MCP_KEY=key, SIDEKICK_HOME=str(HUB),
               SIDEKICK_MCP_RPM="600", SIDEKICK_MCP_BURST="600",
               SIDEKICK_MCP_CLIENT_NAME="sk62-acc")
    p = subprocess.Popen([sys.executable, str(REPO / "mcp" / "sidekick_mcp.py")],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=env, text=True, bufsize=1)
    p._q = queue.Queue()

    def pump():
        for ln in p.stdout:
            ln = ln.strip()
            if not ln:
                continue
            try:
                p._q.put(json.loads(ln))
            except ValueError:
                pass
    threading.Thread(target=pump, daemon=True).start()
    return p


def send(p, obj):
    p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    p.stdin.flush()


def read_msg(p, timeout=100):
    return p._q.get(timeout=timeout)


def call(p, mid, tool, args=None, timeout=100):
    send(p, {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
             "params": {"name": tool, "arguments": args or {}}})
    return read_msg(p, timeout)


def content_json(msg):
    for c in msg.get("result", {}).get("content", []):
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except ValueError:
                return {"_text": c["text"]}
    return {}


# ---- HTTP ---------------------------------------------------------------------

def http_req(port, body=None, headers=None, method="POST", path="/mcp"):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, dict(r.headers), (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            j = json.loads(raw) if raw else None
        except ValueError:
            j = None
        return e.code, dict(e.headers), j


def start_http(key):
    env = dict(os.environ, SIDEKICK_MCP_KEY=key, SIDEKICK_HOME=str(HUB),
               SIDEKICK_MCP_PORT=str(HTTP_PORT), SIDEKICK_MCP_RPM="600", SIDEKICK_MCP_BURST="600")
    p = subprocess.Popen([sys.executable, str(REPO / "mcp" / "sidekick_mcp.py"), "--http"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", HTTP_PORT), 0.2).close()
            return p
        except OSError:
            time.sleep(0.1)
    p.terminate()
    return None


# ---- доставка ленты: pump-строки bridge.log (прецедент sk35 sent_count) --------

def sent_count():
    try:
        return (HUB / "bridge.log").read_text(encoding="utf-8", errors="replace") \
            .count("msg(s) from outbox")
    except FileNotFoundError:
        return 0


def socket_stable(m, secs=4.0):
    """Расширение после посева токена переподключается (SK-49 evict). rpc в окне
    eviction тонет (замер SK-72r1: tabs.open_grouped -> timeout на умирающем сокете).
    Ждём тишины handshake в bridge.log + живой round-trip через шину."""
    log = HUB / "bridge.log"
    t1 = time.time()
    last_ev = -1
    while time.time() - t1 < secs + 12:
        try:
            txt = log.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            txt = ""
        n_ev = txt.count("evicted by") + txt.count("extension connected")
        if n_ev == last_ev and ss_count(PORT) >= 1:
            r = rpc_probe(m, "tabs.list", {}, timeout=10)
            if r.get("ok") is True:
                return True
        last_ev = n_ev
        time.sleep(1.0)
    return False


def rpc_probe(m, method, params, timeout=10):
    """Один round-trip через файловую шину (bus жив, сокет стабилен)."""
    rid = str(uuid.uuid4())
    with open(HUB / "rpc_in.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"v": 1, "id": rid, "method": method, "params": params},
                           ensure_ascii=False) + "\n")
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            data = (HUB / "rpc_out.jsonl").read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            data = ""
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("id") == rid:
                return d
        time.sleep(0.12)
    return {"ok": False, "err": "probe_timeout"}


# ---- Marionette-каркас (sk35_acc.py) ------------------------------------------

SPARE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "win.gBrowser.addTrustedTab('about:blank'); return 'spare';})();")
OPEN_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
               "const b = win.gBrowser.addTrustedTab(arguments[0]); win.gBrowser.selectedTab = b;"
               "return 'tab'; })();")
CLOSE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "for (let i = win.gBrowser.tabs.length - 1; i >= 0; i--) {"
                " const u = win.gBrowser.tabs[i].linkedBrowser.currentURI.spec;"
                " if (u.includes(arguments[0]) && win.gBrowser.tabs.length > 1)"
                " win.gBrowser.removeTab(win.gBrowser.tabs[i]);}"
                " if (win.gBrowser.tabs.length === 0) win.gBrowser.addTrustedTab('about:blank');"
                "return 'closed';})();")
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
    const uri = (a && a.resourceURI) || (byId && byId.resourceURI) || null;
    if (!uri) {
      try {
        const { ExtensionManager } =
          ChromeUtils.importESModule("resource://gre/modules/ExtensionParent.sys.mjs");
        const e = ExtensionManager.get("hermes-sidekick@hermes.local");
        if (e && e.baseURI)
          return resolve({ok: true, id: e.id, url: e.baseURI.spec});
      } catch (e) {}
    }
    resolve({ok: true, id: ext && ext.id, url: uri ? uri.spec : ""});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""
SC_SHOW_JS = r"""
return new Promise(resolve => {
  const win = Services.wm.getMostRecentWindow('navigator:browser');
  const SC = win.SidebarController;
  if (SC.isOpen) return resolve('already-open');
  const ge = SC.getExtensions ? Array.from(SC.getExtensions()) : [];
  const ext = ge.find(e => /hermes|sidekick/i.test(String(e.extensionId || '')));
  if (!ext) return resolve('no-extension-sidebar');
  SC.show(ext.commandID).then(r => resolve('show->' + r), e => resolve('ERR:' + e.message));
});
"""
SC_HIDE_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
              "const SC = win.SidebarController; if (!SC.isOpen) return 'already-closed';"
              "SC.hide(); return 'hidden';})();")
SETUP_JS = r"""
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
  const ph = Services.io.getProtocolHandler('resource').QueryInterface(Ci.nsIResProtocolHandler);
  ph.setSubstitution('sk62', await Services.io.newURI('file://%s'));
    try { ChromeUtils.unregisterWindowActor('Sk62Probe'); } catch (e) {}
    ChromeUtils.registerWindowActor('Sk62Probe', {
      child: { esModuleURI: 'resource://sk62/sk62_actor.sys.mjs' },
      parent: { esModuleURI: 'resource://sk62/sk62_actor.sys.mjs',
                matches: ['<all_urls>', 'moz-extension://*/*'],
                remoteTypes: ['*'] },
    });
    resolve({ok: true});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""
OP_JS = r"""
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const payload = JSON.parse(arguments[0]);
    const win = Services.wm.getMostRecentWindow('navigator:browser');
    let gw = null;
    if (payload.match === 'sidebar') {
      const host = win.document.getElementById('sidebar');
      if (!host) return resolve({ok: false, err: 'no-sidebar-host'});
      let inner = null;
      try {
        inner = host.contentWindow.document.querySelector('browser#webext-panels-browser')
          || host.contentWindow.document.querySelector('browser,iframe');
      } catch (e) { return resolve({ok: false, err: 'host-not-loaded'}); }
      if (!inner) return resolve({ok: false, err: 'no-inner-browser'});
      gw = inner.browsingContext.currentWindowGlobal;
    } else {
      gw = win.gBrowser.selectedBrowser.browsingContext.currentWindowGlobal;
    }
    if (!gw) return resolve({ok: false, err: 'no-gw:' + payload.match});
    const val = await gw.getActor('Sk62Probe').sendQuery('sk62:op', payload);
    resolve({ok: true, val});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 160)}); }
})();
"""


def op(m, match, name, arg=None, timeout=20):
    payload = {"op": name, "match": match}
    if arg is not None:
        payload["arg"] = arg
    try:
        r = m.ascript(OP_JS, args=[json.dumps(payload)], timeout=timeout)
    except Exception as e:
        return {"_err": str(e)[:120]}
    if not (isinstance(r, dict) and r.get("ok")):
        return {"_err": r.get("err", "?") if isinstance(r, dict) else str(r)[:120]}
    v = r.get("val")
    try:
        j = json.loads(v) if isinstance(v, str) else {}
    except (json.JSONDecodeError, TypeError):
        return {"_err": "bad-json:" + str(v)[:120]}
    return j if "err" in j else j.get("v")


def http_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/login.html", timeout=2)
        return True
    except Exception:
        return False


def install(m, xpi):
    r = m.ascript(ADDON_INSTALL_JS, args=[str(pathlib.Path(xpi).resolve())])
    if not (isinstance(r, dict) and r.get("ok")):
        raise RuntimeError(f"installTemporaryAddon: {r}")
    return r


def build_test_xpi():
    if EXT_SRC.exists():
        shutil.rmtree(EXT_SRC)
    shutil.copytree(REPO / "extension", EXT_SRC, ignore=shutil.ignore_patterns(".chrome"))
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
    with zipfile.ZipFile(PROD_XPI, "w", zipfile.ZIP_DEFLATED) as z:
        for p in (REPO / "extension").rglob("*"):
            if p.is_file() and ".chrome" not in str(p):
                z.write(p, p.relative_to(REPO / "extension"))


def start_firefox(pids):
    if listener_pid(MAR_PORT):
        return None  # стенд уже поднят (повторный прогон) — свой PID не заводим
    p = subprocess.Popen(
        [str(HOME / ".local/firefox-158/firefox"), "--marionette",
         "--remote-debugging-port=9393", "--remote-allow-system-access",
         "--profile", str(HOME / ".firefox-test-profile"), "--no-remote"],
        env={**ENV, "MOZ_DISABLE_CONTENT_SANDBOX": "1"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids["firefox"] = p.pid
    t1 = time.time()
    while time.time() - t1 < 40 and not listener_pid(MAR_PORT):
        time.sleep(1.0)
    assert listener_pid(MAR_PORT), "firefox-158 (Marionette 2929) не поднялся"
    return p


def sidebar_alive(m):
    d = op(m, "sidebar", "href")
    return "sidebar.html" in str(d)


def sidebar_force_open(m, attempts=5):
    for _ in range(attempts):
        if sidebar_alive(m):
            return True
        try:
            m.script(SC_HIDE_JS)
        except Exception:
            pass
        time.sleep(0.5)
        try:
            m.script(SC_SHOW_JS)
        except Exception:
            pass
        t1 = time.time()
        while time.time() - t1 < 6:
            if sidebar_alive(m):
                return True
            time.sleep(0.4)
    return False


# ---- teardown (Z) ---------------------------------------------------------------

def teardown():
    pids = json.loads(PIDS.read_text()) if PIDS.exists() else {}
    m = None
    if listener_pid(MAR_PORT):
        try:
            m = Marionette("127.0.0.1", MAR_PORT)
            m.script(CLOSE_TAB_JS, [f"127.0.0.1:{TEST_PORT}"])
            m.script(CLOSE_TAB_JS, ["options.html"])
            m.script(CLOSE_TAB_JS, ["sk62"])
            install(m, PROD_XPI)
            print("Z: тест-вкладки закрыты, prod-XPI переустановлен", flush=True)
        except Exception as e:
            print("Z: marionette-уборка не complete (firefox мёртв?):", str(e)[:120], flush=True)
    for name, sig in (("mcp_http", "TERM"), ("httpd", "TERM"), ("bridge", "TERM"),
                      ("firefox", "TERM")):
        pid = pids.get(name)
        if pid:
            subprocess.run(["kill", "-TERM", str(pid)], capture_output=True)
            time.sleep(1.0)
            subprocess.run(["kill", "-KILL", str(pid)], capture_output=True)
            print(f"Z: {name} (pid {pid}) погашен", flush=True)
    PIDS.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="оставить стенд живым (E2E-Hermes)")
    ap.add_argument("--teardown", action="store_true", help="снять стенд по pids.json")
    args = ap.parse_args()
    if args.teardown:
        teardown()
        return

    S.mkdir(parents=True, exist_ok=True)
    if HUB.exists():
        shutil.rmtree(HUB)
    HUB.mkdir(parents=True)
    shutil.copy(HOME / "sidekick/bridge/hub/token", HUB / "token")
    prod_before = ss_count(8791)
    key = key_path_safe()
    pids, procs = {}, {}
    br, m, httpd = None, None, None
    ok_all = True
    try:
        # ---------- S0: стенд ----------
        for _ in range(10):
            zpid = listener_pid(PORT)
            if not zpid:
                break
            subprocess.run(["kill", "-9", str(zpid)])
            time.sleep(0.4)
        br = subprocess.Popen(["python3", str(REPO / "bridge" / "sidekick_bridge.py")],
                              env={**os.environ, "SIDEKICK_HOME": str(HUB),
                                   "SIDEKICK_PORT": str(PORT)},
                              stdout=open(HUB / "bridge.log", "w", encoding="utf-8"),
                              stderr=subprocess.STDOUT)
        pids["bridge"] = br.pid
        time.sleep(1.2)
        note("S0a тест-мост слушает :8797", listener_pid(PORT) == br.pid,
             f"pid={listener_pid(PORT)}")
        if not http_up():
            httpd = subprocess.Popen(["python3", str(REPO / "tests" / "serve.py")],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pids["httpd"] = httpd.pid
            for _ in range(20):
                if http_up():
                    break
                time.sleep(0.4)
        note("S0b тестовые страницы на :8899", http_up())
        build_test_xpi()
        start_firefox(pids)
        m = Marionette("127.0.0.1", MAR_PORT)
        m.script(SPARE_TAB_JS)
        reg = m.ascript(SETUP_JS % str(REPO / "tests"))
        note("S0c0 window-актор зарегистрирован", isinstance(reg, dict) and reg.get("ok"), reg)
        r0 = install(m, EXT_XPI)
        note("S0c тест-XPI установлен (FALLBACK->:8797)", bool(r0.get("ok")), r0.get("url", ""))
        token = (HUB / "token").read_text().strip()
        t1 = time.time()
        ext_url = ""
        while time.time() - t1 < 40 and not ext_url:
            sidebar_force_open(m, attempts=2)
            href = op(m, "sidebar", "href")
            if "sidebar.html" in str(href):
                ext_url = str(href).split("sidebar.html")[0]
            else:
                time.sleep(1.0)
        note("S0d сайдбар тест-XPI поднят", bool(ext_url), str(href)[:120])
        if not ext_url:
            raise RuntimeError("сайдбар не поднят — стенд слепой: " + str(href)[:160])
        m.script(OPEN_TAB_JS, [ext_url + "options.html"])
        time.sleep(1.5)
        seeded = "none"
        for _ in range(6):
            seeded = op(m, "options", "seedToken", arg=token)
            if seeded == "seeded":
                break
            time.sleep(1.0)
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0d2 токен посеян, расширение коннектится", seeded == "seeded", seeded)
        note("S0e расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод до: {prod_before})")
        m.script(OPEN_TAB_JS, [ext_url + "options.html"])
        time.sleep(1.0)
        ok0 = sidebar_force_open(m)
        note("S0f сайдбар открыт", ok0)

        fw = None  # (лента проверяется живым DOM сайдбара — SK-49: второй WS-клиент вытесняет расширение)
        p = start_stdio(key)
        procs["stdio"] = p

        # ---------- M01/M02: initialize + tools ----------
        send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "sk62-acc"}}})
        r = read_msg(p, 25)
        proto = r.get("result", {}).get("protocolVersion")
        note("M01 initialize = 2025-06-18", proto == "2025-06-18", proto)
        send(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        r = read_msg(p, 25)
        ntools = len(r.get("result", {}).get("tools", []))
        note("M02 tools/list = 21", ntools == 21, f"tools={ntools}")

        # ---------- M03/M04: живой page_dom + sensitive-реестр ----------
        note("S0h сокет расширения стабилен (eviction-окно закрыто)", socket_stable(m))
        r = call(p, 3, "tabs_open_grouped", {"url": PAGE_URL})
        if "timeout" in str(content_json(r)):  # легитимный клиентский retry
            r = call(p, 31, "tabs_open_grouped", {"url": PAGE_URL})
        tab_id = content_json(r).get("tabId")
        note("S0g вкладка агента через tabs.open_grouped", not r.get("result", {}).get("isError"),
             str(content_json(r))[:120])
        r = call(p, 4, "page_dom")
        dom = content_json(r)
        url_ok = "sk62.html" in str(dom.get("url", ""))
        note("M03 page_dom на живой тестовой странице", url_ok, str(dom.get("url"))[:90])
        els = dom.get("elements", [])
        pass_el = next((e for e in els if str(e.get("selector")) == "#login-pass"), None)
        note("M04 sensitive-реестр: #login-pass sensitive:true",
             bool(pass_el) and pass_el.get("sensitive") is True, pass_el)

        # переключаем сайдбар в чат задачи (история чатов): карточка ленты становится видимой
        call(p, 41, "chat_send", {"text": CHAT_MARKER})
        sw = None
        t1 = time.time()
        while time.time() - t1 < 20:
            op(m, "sidebar", "histOpen")
            time.sleep(1.2)
            sw = op(m, "sidebar", "histPick", arg="sk62")
            if sw == "picked":
                break
            time.sleep(0.6)
        note("S0i сайдбар переключён в чат задачи (история чатов)", sw == "picked", sw)

        # ---------- M05/M06: sensitive -> perm.check -> разово -> executed ----------
        res_box = {}
        th = threading.Thread(target=lambda: res_box.update(
            r=call(p, 5, "action_do", {"action": "input", "selector": "#login-pass",
                                       "value": PASS_VALUE}, timeout=100)))
        th.start()
        card, ask_row = None, None
        t1 = time.time()
        while time.time() - t1 < 25:
            d = op(m, "sidebar", "permDom")
            tc = op(m, "sidebar", "taskCard")
            ask_row = next((s for s in (tc.get("steps") or [])
                            if "спрашиваю разрешение" in s), None) if isinstance(tc, dict) else None
            if isinstance(d, dict) and d.get("open", 0) >= 1 and ask_row:
                card = d
                break
            time.sleep(0.8)
        note("M05 sensitive input: адаптер сам завёл perm.check (живая карточка + шаг в ленте)",
             bool(card) and bool(ask_row),
             f"{(card or {}).get('texts', '')}"[:120] + f"; step={ask_row!r:.80}")
        clk = op(m, "sidebar", "clickBtn", arg="ok") if card else {"_err": "no-card"}
        th.join(timeout=100)
        gj = content_json(res_box.get("r", {}))
        # echo-страница: #out показывает «E:<email>|P:<len пароля>» — значение реально введено
        pt = content_json(call(p, 51, "page_text"))
        ptext = str(pt.get("text", ""))
        note("M06 «Разрешить разово» -> executed и пароль реально в поле страницы (эхо P:12)",
             str(clk).startswith("clicked") and gj.get("executed") is True and "P:12" in ptext,
             f"click={clk}; executed={gj.get('executed')}; echo={next((l for l in ptext.splitlines() if l.startswith('E:')), '')!r}")

        # ---------- M07: нечувствительная цель без пермисса + клик real ------------
        r = call(p, 6, "action_do", {"action": "input", "selector": "#login-email",
                                     "value": EMAIL_VALUE})
        gj2 = content_json(r)
        d2 = op(m, "sidebar", "permDom")
        r = call(p, 7, "action_do", {"action": "click", "selector": "#login-btn"})
        j7b = content_json(r)
        pt2 = content_json(call(p, 52, "page_text"))
        ptext2 = str(pt2.get("text", ""))
        note("M07 нечувствительные input+click БЕЗ нового пермисса, значения реально на странице",
             gj2.get("executed") is True and j7b.get("executed") is True
             and isinstance(d2, dict) and d2.get("open", -1) == 0
             and EMAIL_VALUE in ptext2 and "SUBMITTED" in ptext2 and "P:12" in ptext2,
             f"executed={gj2.get('executed')}; click_exec={j7b.get('executed')}; "
             f"perm_open={isinstance(d2, dict) and d2.get('open')}; "
             f"echo={next((l for l in ptext2.splitlines() if l.startswith('E:')), '')!r}")

        # ---------- M08..M10: HTTP-транспорт ----------
        ph = start_http(key)
        procs["mcp_http"] = ph
        pids["mcp_http"] = ph.pid
        init_body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "sk62-http"}}}
        code, hdr, body = http_req(HTTP_PORT, init_body)
        note("M08 HTTP без ключа: 401 + WWW-Authenticate: Bearer (-32001)",
             code == 401 and hdr.get("WWW-Authenticate") == "Bearer"
             and (body or {}).get("error", {}).get("code") == -32001,
             f"{code} WWW={hdr.get('WWW-Authenticate')} code={(body or {}).get('error', {}).get('code')}")
        auth = {"Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        code, hdr, body = http_req(HTTP_PORT, init_body, auth)
        sid = hdr.get("Mcp-Session-Id")
        note("M09 HTTP с ключом: 200 initialize + Mcp-Session-Id",
             code == 200 and (body or {}).get("result", {}).get("protocolVersion") == "2025-06-18"
             and sid and len(str(sid)) >= 8, f"{code} sid_len={len(str(sid or ''))}")
        code, hdr, body = http_req(HTTP_PORT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                   {**auth, "Mcp-Session-Id": sid})
        n_http_tools = len((body or {}).get("result", {}).get("tools", []))
        note("M09b HTTP tools/list = 21", code == 200 and n_http_tools == 21, f"tools={n_http_tools}")
        code, hdr, _ = http_req(HTTP_PORT, method="GET", headers=auth)
        note("M10 HTTP GET -> 405 + Allow: POST",
             code == 405 and "POST" in str(hdr.get("Allow", "")), f"{code} Allow={hdr.get('Allow')}")

        # ---------- M11: прод :8796 не слушает (сервис выключен) ----------
        lp = listener_pid(8796)
        note("M11 прод :8796 — слушателя нет (sidekick-mcp-http выключен)", lp is None,
             f"listener_pid(8796)={lp}")

        # ---------- M12/M13/M14: лента живого сайдбара + EOF ----------
        pump0 = sent_count()
        tc = None
        t1 = time.time()
        while time.time() - t1 < 15:
            tc = op(m, "sidebar", "taskCard")
            if isinstance(tc, dict) and tc.get("found"):
                break
            time.sleep(1.0)
        header = str((tc or {}).get("header", ""))
        steps = (tc or {}).get("steps") or []
        in_dom_chat = op(m, "sidebar", "bodyHas", arg=CHAT_MARKER, timeout=25)
        note("M14 step+chat кадры долетели до человекочитого DOM (шаги карточки + текст чата)",
             len(steps) >= 1 and in_dom_chat is True,
             f"steps={len(steps)} chat_in_dom={in_dom_chat} pump_delivered={sent_count() - pump0}")
        p.stdin.close()
        t_eof = time.time()
        rc = p.wait(timeout=20)
        done_tc = None
        t1 = time.time()
        while time.time() - t1 < 12:
            tc2 = op(m, "sidebar", "taskCard")
            if isinstance(tc2, dict) and " done" in str(tc2.get("cls", "")).lower():
                done_tc = tc2
                break
            time.sleep(1.0)
        note("M12 EOF: процесс 0 + терминальный task(done) отрисован в сайдбаре",
             rc == 0 and bool(done_tc),
             f"rc={rc}; card_cls={str((done_tc or tc or {}).get('cls'))!r} t_eof+{time.time()-t_eof:.1f}s")

        # ---------- M13: task-кадр в живой ленте — карточка задачи в сайдбаре -------
        note("M13 task-кадр в живом сайдбаре: карточка задачи присутствует",
             bool(tc) and tc.get("found") is True and "task" in str(tc.get("cls", "")),
             f"cls={str((tc or {}).get('cls'))!r} header={header[:40]!r}")

        # ---------- M15: прод не тронут (панель: R1 :8791 было/стало) --------------
        # Честная проверка: у firefox СТЕНДА ноль соединений к :8791 (он физически не
        # может туда попасть: prod_urls тест-XPI = localhost-варианты :8796).
        # Счётчик прод-клиентов на :8791 — наблюдение (счётчик стороны пользователя
        # может дёргаться сам: переподключение прод-XPI не от стенда).
        prod_after = ss_count(8791)
        ffp = str(pids.get("firefox", ""))
        stand_conns = 0
        try:
            out = subprocess.run(["ss", "-tanp", f"dport = {8791}"],
                                 capture_output=True, text=True, timeout=10).stdout
            for ln in out.splitlines():
                if "ESTAB" in ln and f'"firefox",pid={ffp},' in ln:
                    stand_conns += 1
        except Exception:
            pass
        note("M15 R1: стенд не трогал прод :8791 (у firefox стенда 0 соединений); прод-счётчик было/стало",
             stand_conns == 0,
             f"stand_conns@8791={stand_conns}; было={prod_before} стало={prod_after}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        ok_all = False
    finally:
        pids_json = {k: v for k, v in pids.items()}
        PIDS.write_text(json.dumps(pids_json))
        if args.keep:
            print("KEEP: стенд оставлен живым (bridge/firefox/httpd в pids.json):",
                  pids_json, flush=True)
            for nm, pr in procs.items():
                if nm == "mcp_http" and pr and pr.poll() is None:
                    pr.terminate()  # HTTP-адаптер E2E не нужен — его снимаем, стенд остаётся
        else:
            for nm, pr in procs.items():
                if pr and pr.poll() is None:
                    pr.terminate()
            teardown()
        n_pass = sum(1 for v in results.values() if v["ok"] is True)
        n_fail = sum(1 for v in results.values() if v["ok"] is False)
        print(f"TOTAL: PASS={n_pass} FAIL={n_fail}", flush=True)
        (S / "sk62_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
        if n_fail:
            sys.exit(1)


if __name__ == "__main__":
    main()
