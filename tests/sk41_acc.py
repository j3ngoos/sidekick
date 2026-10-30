#!/usr/bin/env python3
"""SK-41: антибот-детекторы — дамп метрик + скриншоты стенда.

Конфиги:
  clean  — firefox-158 БЕЗ расширения (базовая линия стенда)
  wt     — расширение из --ext (снимок рабочего дерева ДО починки)
  wtfix  — расширение из --ext (рабочее дерево ПОСЛЕ починки)

Во всех тестовых сборках токен нейтрализован (getToken -> ""): тестовое
расширение НИКОГДА не коннектится к проду :8791 (живой hello вытеснил бы
сокет расширения пользователя — SK-49). Прод :8791 проверяется до/после (R1).

Стенд: firefox-158, Marionette 2929, DISPLAY :10 (Xephyr), профиль
~/.firefox-test-profile. CDP-порт НЕ поднимаем (лишний пробуемый порт +
Runtime.enable-детект). User Firefox не трогаем.

Использование:
  python3 tests/sk41_acc.py --config wt --ext /path/to/ext \
      --shots DIR --dump DIR
"""
import argparse
import base64
import json
import os
import pathlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import zipfile

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PAGE_PORT = 8899
CAL_URL = f"http://127.0.0.1:{PAGE_PORT}/sk41.html"
PROFILE = str(HOME / ".firefox-test-profile")
PROD_PORT = 8791

# (имя, URL, секунды ожидания вердикта). Заметы таска: creepjs.net/pixelguard.net/
# scrapedetect.com не резолвятся (NXDOMAIN) — взяты живые эквиваленты: CreepJS-demo,
# pixelscan.net, browserscan.net.
SITES = [
    ("creepjs", "https://abrahamjuliot.github.io/creepjs/", 26),
    ("bl_canvas", "https://browserleaks.com/canvas", 7),
    ("bl_webgl", "https://browserleaks.com/webgl", 7),
    ("bl_webrtc", "https://browserleaks.com/webrtc", 7),
    ("bl_js", "https://browserleaks.com/javascript", 6),
    ("incolumitas", "https://bot.incolumitas.com/", 38),
    ("pixelscan", "https://pixelscan.net/", 22),
    ("iphey", "https://iphey.com/", 16),
    ("browserscan", "https://browserscan.net/", 22),
]

SPARE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "win.gBrowser.addTrustedTab('about:blank'); return 'spare';})();")
OPEN_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
               "const t = win.gBrowser.addTrustedTab(arguments[0]); win.gBrowser.selectedTab = t;"
               "return 'tab';})();")
CLOSE_OTHER_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                  "for (let i = win.gBrowser.tabs.length - 1; i >= 0; i--) {"
                  " const t = win.gBrowser.tabs[i];"
                  " if (t !== win.gBrowser.selectedTab) win.gBrowser.removeTab(t);}"
                  "return 'ok';})();")
PAGE_INFO_JS = None  # (удалено) страница читается через actor op("info") — см. sk41_actor.sys.mjs
CLOSE_EXTRA_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                  "while (win.gBrowser.tabs.length > 1) win.gBrowser.removeTab(win.gBrowser.tabs[0]);"
                  "if (win.gBrowser.tabs.length === 0) win.gBrowser.addTrustedTab('about:blank'); return 'ok';})();")
SETUP_JS = r"""
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
  const ph = Services.io.getProtocolHandler('resource').QueryInterface(Ci.nsIResProtocolHandler);
  ph.setSubstitution('sk41', await Services.io.newURI('file://%s'));
    try { ChromeUtils.unregisterWindowActor('Sk41Probe'); } catch (e) {}
    ChromeUtils.registerWindowActor('Sk41Probe', {
      child: { esModuleURI: 'resource://sk41/sk41_actor.sys.mjs' },
      parent: { esModuleURI: 'resource://sk41/sk41_actor.sys.mjs',
                matches: ['<all_urls>', 'moz-extension://*/*'] },
      safeForUntrustedWebProcess: true,
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
    const bc = win.gBrowser.selectedBrowser.browsingContext;
    const gw = bc && bc.currentWindowGlobal;
    if (!gw) return resolve({ok: false, err: 'no-gw'});
    const val = await gw.getActor('Sk41Probe').sendQuery('sk41:op', payload);
    resolve({ok: true, val});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 160)}); }
})();
"""
ADDON_INSTALL_JS = """
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
    const nsFile = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
    nsFile.initWithPath(arguments[0]);
    const a = await AddonManager.installTemporaryAddon(nsFile);
    resolve({ok: true, id: a && a.id});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""

# Скрипт для инъекции на https-страницу: видит ли СТРАНИЦА коннект к мосту.
WS_PAGE_PROBE = r"""
(() => { try {
  const o = { ctx: location.origin, open: 0, close: null, err: 0 };
  const t0 = performance.now();
  try {
    const ws = new WebSocket('ws://127.0.0.1:8791');
    ws.onopen = () => { o.open = 1; };
    ws.onerror = () => { o.err = 1; };
    ws.onclose = (e) => { o.close = [e.code, e.reason || '']; o.ms = Math.round(performance.now() - t0);
                          document.title = 'WSP:' + JSON.stringify(o); };
    setTimeout(() => { document.title = 'WSP:' + JSON.stringify(o); }, 6000);
  } catch (e) { document.title = 'WSP:throw:' + e.message; }
} catch (e) { document.title = 'WSP:outer:' + e.message; } })();
"""


def log(*a):
    print(*a, flush=True)


def ss_count(port):
    r = subprocess.run(["ss", "-tn", "state", "established", f"( sport = :{port} )"],
                       capture_output=True, text=True)
    return max(0, len(r.stdout.strip().splitlines()) - 1)


def listener_pid(port):
    r = subprocess.run(["ss", "-tlnp", f"( sport = :{port} )"], capture_output=True, text=True)
    m = re.search(r"pid=(\d+)", r.stdout)
    return int(m.group(1)) if m else None


def shot(shots, name):
    p = pathlib.Path(shots) / f"{name}.png"
    subprocess.run(["scrot", "-o", str(p)], env=ENV, capture_output=True)
    return p.exists()


def build_xpi(src_dir, xpi):
    with zipfile.ZipFile(xpi, "w", zipfile.ZIP_DEFLATED) as z:
        for p in pathlib.Path(src_dir).rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(src_dir))


def prep_ext_from(src, dst):
    """Копия расширения; getToken -> "" — тестовый коннект к проду :8791 невозможен."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".chrome"))
    bj = dst / "background.js"
    s = bj.read_text(encoding="utf-8")
    gt_old = ('async function getToken() {\n'
              '  const { bridgeToken } = await browser.storage.local.get("bridgeToken");\n'
              '  return (bridgeToken || "").trim();\n}')
    assert gt_old in s, "getToken не найден — нейтрализация токена не применена"
    s = s.replace(gt_old, 'async function getToken() {\n  return ""; // sk41-test: прод не трогаем\n}')
    bj.write_text(s, encoding="utf-8")
    return dst


# ---- сырой ws-щуп к мосту (поведение сервера без браузера) -------------------------

def _ws_connect(port=PROD_PORT):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (port, key)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(4096)
        if not c:
            return s, None, buf
        buf += c
    return s, buf.split(b"\r\n")[0].decode(), buf


def _ws_send_text(s, data):
    payload = data.encode()
    mask = os.urandom(4)
    frame = bytearray([0x81])
    n = len(payload)
    if n < 126:
        frame.append(0x80 | n)
    else:
        frame.append(0x80 | 126)
        frame += struct.pack(">H", n)
    frame += mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    s.sendall(bytes(frame))


def _ws_read(s, timeout):
    s.settimeout(timeout)
    try:
        hdr = s.recv(2)
        if len(hdr) < 2:
            return "eof", b""
        op = hdr[0] & 0x0F
        ln = hdr[1] & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", s.recv(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", s.recv(8))[0]
        data = b""
        while len(data) < ln:
            c = s.recv(ln - len(data))
            if not c:
                break
            data += c
        return op, data
    except socket.timeout:
        return "timeout", b""
    except ConnectionError as e:
        return "reset:" + str(e)[:40], b""


def ws_probe(mode, wait=8.0):
    """mode: silent | garbage | bad_token — что шлём после handshake, что ждём в ответ."""
    out = {"mode": mode}
    try:
        s, status, _ = _ws_connect()
    except Exception as e:  # noqa: BLE000
        out["handshake"] = "connect-fail:" + str(e)[:80]
        return out
    out["handshake"] = status or "no-response"
    t0 = time.time()
    if status and "101" in status:
        if mode == "garbage":
            s.sendall(b"this-is-not-a-ws-frame\r\n")
        elif mode == "bad_token":
            _ws_send_text(s, json.dumps({"v": 1, "type": "hello", "token": "00" * 32}))
        frames = []
        while time.time() - t0 < wait:
            op, data = _ws_read(s, max(0.2, wait - (time.time() - t0)))
            if op == "timeout":
                frames.append("timeout")
                break
            if op == "eof" or str(op).startswith("reset"):
                frames.append(str(op))
                break
            if op == 8:  # close
                code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else None
                frames.append({"close": [code, data[2:].decode("utf-8", "replace")[:60]]})
                break
            if op == 1:
                frames.append({"text": data.decode("utf-8", "replace")[:120]})
                continue
            frames.append(f"op{op}")
        out["frames"] = frames[:6]
        out["t_s"] = round(time.time() - t0, 2)
        s.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, choices=["clean", "wt", "wtfix"])
    ap.add_argument("--ext", default="", help="каталог расширения для сборки XPI")
    ap.add_argument("--shots", required=True)
    ap.add_argument("--dump", required=True)
    a = ap.parse_args()
    shots, dump = pathlib.Path(a.shots), pathlib.Path(a.dump)
    shots.mkdir(parents=True, exist_ok=True)
    dump.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
    from mario import Marionette  # noqa: E408

    meta = {"config": a.config, "ts": time.strftime("%F %T")}
    prod_before = ss_count(PROD_PORT)
    meta["prod_conns_before"] = prod_before

    # сервер страниц
    srv = None
    if not listener_pid(PAGE_PORT):
        srv = subprocess.Popen(["python3", str(REPO / "tests" / "serve.py")],
                               env={**os.environ, "SIDEKICK_TEST_PORT": str(PAGE_PORT)},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)

    # firefox — ВСЕГДА свежий: висящий тест-Firefox от прошлого (упавшего) прогона
    # привязывает Marionette к мёртвым фреймам (CPOW-эпоха, проверено 24.10).
    for _ in range(10):
        zpid = listener_pid(MAR_PORT)
        if not zpid:
            break
        subprocess.run(["kill", "-9", str(zpid)])
        time.sleep(0.5)
    subprocess.run(["pkill", "-9", "-f", "firefox-158/[f]irefox"])
    time.sleep(1.0)
    # профиль — ВСЕГДА свежая копия чистого шаблона: восстановленная сессия даёт
    # контент-процессы, стартовавшие ДО регистрации actor-замены res:// (таблица
    # подмен отдаётся процессу при старте — actor не грузится), а из прошлых
    # прогонов в профиле остаются расширения/кэш, чем трахается чистота замера.
    prof = shots.parent / f"profile-{a.config}"
    if prof.exists():
        shutil.rmtree(prof)
    shutil.copytree(HOME / ".firefox-clean-template", prof)
    ff = subprocess.Popen(
        [str(HOME / ".local/firefox-158/firefox"), "--marionette", "--remote-allow-system-access",
         "--profile", str(prof), "--no-remote"],
        env={**ENV, "MOZ_DISABLE_CONTENT_SANDBOX": "1"},
        # window-актор грузится в контент-процессе чтением файла подмены res:// —
        # seccomp-фильтр песочницы это режет (проверено 24.10: без флага
        # «Failed to load resource://sk41/...», с флагом — работает; sk39 делал так же)
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    while time.time() - t0 < 45 and not listener_pid(MAR_PORT):
        time.sleep(1.0)
    m = Marionette("127.0.0.1", MAR_PORT)
    m.script(SPARE_TAB_JS)
    m.script(CLOSE_EXTRA_JS)
    meta["firefox_up_s"] = round(time.time() - t0, 1)

    reg = m.ascript(SETUP_JS % str(REPO / "tests"))
    if not (isinstance(reg, dict) and reg.get("ok")):
        raise RuntimeError(f"actor не зарегистрирован: {reg}")

    def op(name, arg=None, timeout=20):
        """sendQuery окну ВЫБРАННОЙ вкладки — actor всегда в нужном фрейме
        (Marionette content-контекст за сменой вкладки из chrome не следует)."""
        payload = {"op": name}
        if arg is not None:
            payload["arg"] = arg
        try:
            r = m.ascript(OP_JS, args=[json.dumps(payload)], timeout=timeout)
        except Exception as e:  # noqa: BLE001
            return {"_err": str(e)[:120]}
        if not (isinstance(r, dict) and r.get("ok")):
            return {"_err": str(r)[:120]}
        v = r.get("val")
        try:
            j = json.loads(v) if isinstance(v, str) else {}
        except (json.JSONDecodeError, TypeError):
            return {"_err": "bad-json:" + str(v)[:120]}
        return {"_err": j["err"]} if "err" in j else j.get("v")

    if a.config != "clean":
        assert a.ext, "--ext обязателен для конфигурации с расширением"
        build_dir = prep_ext_from(pathlib.Path(a.ext), shots.parent / f"extbuild_{a.config}")
        xpi = shots.parent / f"sk41_{a.config}.xpi"
        build_xpi(build_dir, xpi)
        r = m.ascript(ADDON_INSTALL_JS, args=[str(xpi.resolve())])
        meta["addon_install"] = r
        log("addon:", r)
        time.sleep(2.5)

    def nav(url):
        m.script(OPEN_TAB_JS, [url])
        m.script(CLOSE_OTHER_JS)
        # ждём readyState complete
        t1 = time.time()
        while time.time() - t1 < 25:
            if op("ready") == "complete":
                break
            time.sleep(0.5)

    def page_info():
        info = op("info")
        return info if isinstance(info, dict) and "title" in info else {"title": "", "href": "?",
                                                                        "txt": "OP-ERR:" + str(info)[:150]}

    # 1) калибровка (page-world истина)
    nav(CAL_URL)
    t1 = time.time()
    cal = None
    while time.time() - t1 < 40:
        info = page_info()
        if str(info.get("title", "")).startswith("SK41:"):
            try:
                cal = json.loads(base64.b64decode(info["title"][5:]).decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                cal = {"decode_err": str(e)[:120], "title": info["title"][:200]}
            break
        time.sleep(1.0)
    (dump / "calibration.json").write_text(json.dumps(cal, ensure_ascii=False, indent=1))
    shot(shots, "calibration")
    log("calibration:", "ok" if cal and "decode_err" not in cal and "nav" in cal else "FAIL")

    # 2) ws-щуп из https-контекста (example.com, CSP нет)
    nav("https://example.com/")
    time.sleep(1.5)
    inj = op("inject", WS_PAGE_PROBE)
    t1 = time.time()
    wsp = None
    while time.time() - t1 < 12:
        info = page_info()
        if str(info.get("title", "")).startswith("WSP:"):
            try:
                wsp = json.loads(info["title"][4:])
            except Exception:  # noqa: BLE001
                wsp = {"raw": info["title"][:200]}
            break
        time.sleep(0.7)
    meta["ws_page_probe"] = {"inject": str(inj), "result": wsp}
    log("ws_page_probe:", wsp)

    # 3) сырой ws-щуп к проду (поведение сервера)
    meta["ws_raw"] = {mode: ws_probe(mode) for mode in ("silent", "garbage", "bad_token")}
    log("ws_raw:", json.dumps(meta["ws_raw"], ensure_ascii=False)[:400])

    # 4) детекторы
    for name, url, wait in SITES:
        nav(url)
        time.sleep(wait)
        ok_shot = shot(shots, name)
        info = page_info()
        (dump / f"{name}.txt").write_text(
            f"URL: {info.get('href')}\nTITLE: {info.get('title')}\n\n{info.get('txt', '')}")
        log(f"site {name}: title={str(info.get('title'))[:70]!r} shot={ok_shot} "
            f"txt={len(info.get('txt', ''))}")

    meta["prod_conns_after"] = ss_count(PROD_PORT)
    meta["R1_prod_untouched"] = meta["prod_conns_after"] >= prod_before
    log("R1 prod conns:", prod_before, "->", meta["prod_conns_after"])

    (dump / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    m.script(CLOSE_EXTRA_JS)
    m.close()
    ff.terminate()
    try:
        ff.wait(timeout=10)
    except Exception:  # noqa: BLE001
        ff.kill()
    if srv:
        srv.terminate()
    log("done", a.config)


if __name__ == "__main__":
    main()
