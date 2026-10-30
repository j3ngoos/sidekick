#!/usr/bin/env python3
"""SK-71 живая приёмка: i18n — весь UI из словаря en/ru, live-switch без reload.

Карточка t_63263c28 (запрос пользователя 30.10): интерфейс сайдбара, options и
превью-оверлей контент-скрипта говорят на языке из словаря; переключение в
options.html применяется МГНОВЕННО (сайдбар, options, content — без перезагрузки
страницы/окна).

Стенд: изолированный мост :8797 + тест-копия расширения (FALLBACK_SERVER->:8797,
в background.js вживлён storage-пробник оверлея — только тестовый XPI) в
firefox-158 (Marionette 2929, CDP :9393). Прод :8791 не трогаем; в конце —
переустановка prod-аддона. Прецеденты: sk37_acc.py (стенд/шина/оверлей),
sk38_acc.py (сайдбар-дом).

Фазы:
  S0  стенд: мост :8797, XPI, firefox+sidebar, опции, youtube.html, коннект
  I1  EN по умолчанию: заголовок/подпись/плейсхолдер/тултипы из en.json,
      lang="en", скрин en
  I2  тема light: клик темы в сайдбаре, label из словаря, скрин en-light
  I3  RU через options <select> (юзер-путь): сайдбар ПЕРЕРАХИВАЕТСЯ ЖИВОМ
      (win.__sk71ts не меняется = без reload), значения посимвольно == ru.json;
      options.html сам RU; скрин ru-light (live-switch)
  I4  тема dark при RU: скрин ru-dark (итого 4 скрина EN/RU x 2 темы)
  I5  content-world live-switch: превью action.do #q — подпись EN (латиница),
      переключение -> та же подпись RU без перезагрузки вкладки, скрин pt-ru
  Z   уборка: вкладки, prod-аддон, мост, тест-Firefox погашен по PID
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
S = pathlib.Path(os.environ.get("SK71_SCRATCH", str(HOME / ".hermes/cache/scratch/sk71/acc")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
SHOTS = S / "shots"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
# :8899 занял dev-мок kompege-pip (чужой процесс) — стенд SK-71 живёт на своём порту
TEST_PORT = int(os.environ.get("SK71_TEST_PORT", "8877"))
PAGE_URL = f"http://127.0.0.1:{TEST_PORT}/youtube.html?v=sk71r1"
TASK = "sk71-acc"
results = {}

EN = json.loads((REPO / "extension" / "locales" / "en.json").read_text(encoding="utf-8"))
RU = json.loads((REPO / "extension" / "locales" / "ru.json").read_text(encoding="utf-8"))

sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
from mario import Marionette  # noqa: E402

CYR = re.compile(r"[\u0400-\u04FF]")


def note(name, ok, detail=""):
    ok_v = None if ok == "skip" else bool(ok)
    results[name] = {"ok": ok_v, "detail": str(detail)[:400]}
    tag = "PASS " if ok_v else ("SKIP " if ok_v is None else "FAIL ")
    print(tag + name + ("  " + str(detail)[:180] if detail else ""), flush=True)


def ss_count(port):
    r = subprocess.run(["ss", "-tn", "state", "established", f"( sport = :{port} )"],
                       capture_output=True, text=True)
    return max(0, len(r.stdout.strip().splitlines()) - 1)


def listener_pid(port):
    r = subprocess.run(["ss", "-tlnp", f"( sport = :{port} )"], capture_output=True, text=True)
    m2 = re.search(r"pid=(\d+)", r.stdout)
    return int(m2.group(1)) if m2 else None


def shot(name):
    p = SHOTS / f"{name}.png"
    subprocess.run(["scrot", "-o", str(p)], env=ENV, capture_output=True)
    return p.exists()


# ---- RPC через файловую шину моста (прецедент sk37) ---------------------------

def rpc(method, params, timeout=30):
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
    return {"ok": False, "err": "harness_timeout", "id": rid}


def rpc_async(method, params, box):
    th = threading.Thread(target=lambda: box.update(r=rpc(method, params, timeout=60)))
    th.start()
    return th


# ---- Marionette-блоки (прецедент sk37) ----------------------------------------

SPARE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "win.gBrowser.addTrustedTab('about:blank'); return 'spare';})();")
OPEN_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
               "const b = win.gBrowser.addTrustedTab(arguments[0]); win.gBrowser.selectedTab = b;"
               "return 'tab:' + b.linkedBrowser.browsingContext.currentURI.spec; })();")
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
  ph.setSubstitution('sk71', await Services.io.newURI('file://%s'));
    try { ChromeUtils.unregisterWindowActor('Sk71Probe'); } catch (e) {}
    ChromeUtils.registerWindowActor('Sk71Probe', {
      child: { esModuleURI: 'resource://sk71/sk71_actor.sys.mjs' },
      parent: { esModuleURI: 'resource://sk71/sk71_actor.sys.mjs',
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
    } else if (payload.match === 'options') {
      for (const t of win.gBrowser.tabs) {
        const u = t.linkedBrowser.currentURI.spec;
        // только moz-extension-вкладка: в профиле стенда живут file://-табы
        // (kompege-моки), у их window-global актор расширения не заведётся
        if (u.startsWith('moz-extension://') && u.includes('options.html')) {
          gw = t.linkedBrowser.browsingContext.currentWindowGlobal; break; } }
    } else if (payload.match === 'seed') {
      for (const t of win.gBrowser.tabs)
        if (t.linkedBrowser.currentURI.spec.includes('seed_probe')) {
          gw = t.linkedBrowser.browsingContext.currentWindowGlobal; break; }
    } else { // page: активная вкладка
      gw = win.gBrowser.selectedBrowser.browsingContext.currentWindowGlobal;
    }
    if (!gw) return resolve({ok: false, err: 'no-gw:' + payload.match});
    const val = await gw.getActor('Sk71Probe').sendQuery('sk71:op', payload);
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


def ui_state(m):
    r = op(m, "sidebar", "uiState")
    return r if isinstance(r, dict) else {"_err": str(r)[:120]}


def opt_state(m):
    r = op(m, "options", "optState")
    return r if isinstance(r, dict) else {"_err": str(r)[:120]}


def overlay_probe(m):
    """Превью-оверлей: фон читает shadow-DOM изолированного мира через
    scripting.executeScript (зонд вживлён в тестовый bg), актор пинает и читает storage."""
    t0 = time.time()
    op(m, "options", "overlayKick")
    ov = {}
    while time.time() - t0 < 10:
        g = op(m, "options", "storageGet", arg="__sk71_overlay")
        if isinstance(g, dict) and g.get("ts", 0) >= (t0 - 2) * 1000:
            ov = g.get("v") if g.get("v") is not None else g
            break
        time.sleep(0.4)
    return ov


def sidebar_alive(m):
    d = ui_state(m)
    return "sidebar.html" in str(d.get("href", ""))


def ensure_sidebar_open(m):
    for attempt in range(5):
        if sidebar_alive(m):
            return True
        if attempt == 3:
            try:
                m.script("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                         "if (win.SidebarController.isOpen) win.SidebarController.toggle(); return 'reset';})();")
                time.sleep(1.5)
            except Exception:
                pass
        try:
            m.script(SC_SHOW_JS)
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < 6:
            if sidebar_alive(m):
                return True
            time.sleep(0.4)
    return False


def http_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/youtube.html", timeout=2)
        return True
    except Exception:
        return False


UNINSTALL_JS = """
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
    const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
    const a = await AddonManager.getAddonByID("hermes-sidekick@hermes.local");
    if (!a) return resolve({ok: true, note: "not-installed"});
    await AddonManager.uninstallTemporaryAddon(a);
    resolve({ok: true, note: "uninstalled"});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""


def uninstall(m):
    try:
        r = m.ascript(UNINSTALL_JS)
    except Exception as e:
        return {"ok": False, "err": str(e)[:120]}
    # worker-скоп умирает не мгновенно: ждём снятия регистрации bg-скриптов
    time.sleep(2.0)
    return r if isinstance(r, dict) else {"ok": False, "err": str(r)[:120]}


def install(m, xpi):
    r = m.ascript(ADDON_INSTALL_JS, args=[str(pathlib.Path(xpi).resolve())])
    if not (isinstance(r, dict) and r.get("ok")):
        raise RuntimeError(f"installTemporaryAddon: {r}")
    return r


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    if HUB.exists():
        shutil.rmtree(HUB)
    HUB.mkdir(parents=True)
    shutil.copy(HOME / "sidekick/bridge/hub/token", HUB / "token")
    prod_before = ss_count(8791)
    br, m, httpd = None, None, None
    try:
        # ---------- S0: стенд ----------
        if EXT_SRC.exists():
            shutil.rmtree(EXT_SRC)
        shutil.copytree(REPO / "extension", EXT_SRC, ignore=shutil.ignore_patterns(".chrome"))
        bj = EXT_SRC / "background.js"
        src = bj.read_text(encoding="utf-8")
        # SK-71: фоновый зонд подписи превью (только тестовый XPI) — isolated world
        # shadow root оверлея CLOSED и живёт в world content.js — зонд идём
        # tabs.sendMessage в контент (его собственный closed-корень виден только ему)
        probe_block = """
// SK-71 ACC PROBE (test-only): фон гонит зонд в content world активной вкладки, результат в storage
browser.runtime.onMessage.addListener((mm) => {
  if (!mm || mm.type !== "sk71_overlay_probe") return false;
  (async () => {
    const tabs = await browser.tabs.query({ active: true, lastFocusedWindow: true });
    const t = tabs[0];
    if (!t) { await browser.storage.local.set({ __sk71_overlay: { v: { err: "no-active-tab" }, ts: Date.now() } }); return; }
    const v = await browser.tabs.sendMessage(t.id, { type: "sk71_overlay_probe_tab" })
      .catch((e) => ({ err: "tab-probe:" + String((e && e.message) || e).slice(0, 120) }));
    await browser.storage.local.set({ __sk71_overlay: { v, ts: Date.now() } });
  })();
  return false;
});
"""
        # content-зонд (тестовый XPI): closed shadow своего world + i18n-подпись .pv
        cj = EXT_SRC / "content.js"
        csrc = cj.read_text(encoding="utf-8")
        csrc = """
// SK-71 ACC PROBE (test-only): ответ на фоновый зонд из мира самого content.js
browser.runtime.onMessage.addListener((mm, _sndr, sendResponse) => {
  if (!mm || mm.type !== "sk71_overlay_probe_tab") return false;
  const host = document.getElementById("hermes-sidekick-host");
  const sr = host && host.shadowRoot;
  const pv = sr && sr.querySelector(".pv");
  sendResponse({
    host: !!host, sr: !!sr,
    pvVisible: !!(pv && getComputedStyle(pv).display !== "none"),
    label: pv ? String(pv.textContent || "").trim().slice(0, 80) : "",
    href: location.href.slice(0, 60),
  });
  return true;
});
""" + csrc
        # closed-тень в тестовой копии открываем: closed root не виден НИОДНОМУ
        # внешнему миру (ни executeScript, ни стороннему listener) — label читаем
        # из открытого корня; путь рендера подписи (L() в content world) тот же
        assert csrc.count('mode: "closed"') == 1, "якорь closed-тени не найден"
        csrc = csrc.replace('mode: "closed"', 'mode: "open"')
        cj.write_text(csrc, encoding="utf-8")
        src = probe_block + src
        assert 'const FALLBACK_SERVER = "ws://127.0.0.1:8791";' in src
        src = src.replace('const FALLBACK_SERVER = "ws://127.0.0.1:8791";',
                          f'const FALLBACK_SERVER = "ws://127.0.0.1:{PORT}";')
        assert "for (const u of [saved, FALLBACK_SERVER])" in src
        src = src.replace("for (const u of [saved, FALLBACK_SERVER])",
                          "for (const u of [FALLBACK_SERVER])")
        bj.write_text(src, encoding="utf-8")
        # seed-страница тестового XPI: детерминированные uiLang/theme ПЕРЕД любым
        # открытием страниц (storage temp-аддона живёт между установками одного id)
        (EXT_SRC / "seed_probe.html").write_text(
            "<!doctype html><meta charset=utf-8><title>SEEDING</title>"
            "<script src=\"seed_probe.js\"></script>", encoding="utf-8")
        (EXT_SRC / "seed_probe.js").write_text(
            "browser.storage.local.set({ uiLang: \"en\", sk30Theme: \"system\" })"
            ".then(() => { document.title = \"SEEDOK\"; });", encoding="utf-8")
        # SK-71: диагностика bg-ws boot в ТЕСТОВОМ XPI (connect/wsAttempt/no_token +
        # fetchPack-ошибки i18n.js) — только тестовая сборка, prod не трогаем
        ij = EXT_SRC / "i18n.js"
        isrc = ij.read_text(encoding="utf-8")
        isrc = isrc.replace(
            '      .then(function (r) { return r.ok ? r.json() : {}; })\n      .catch(function () { return {}; });',
            '      .then(function (r) { return r.ok ? r.json()\n'
            '        : (browser.storage.local.set({ __sk71_bgfetch: "status:" + r.status + " " + r.url }), {}); })\n'
            '      .catch(function (e) { try { browser.storage.local.set({ __sk71_bgfetch: String((e && e.message) || e).slice(0, 160) }); } catch (x) {} return {}; });')
        ij.write_text(isrc, encoding="utf-8")
        src = bj.read_text(encoding="utf-8")
        a1 = "function connect() {\n  if (trying || (ws && ws.readyState <= 1)) return;"
        assert a1 in src, "якорь connect() не найден"
        src = src.replace(a1,
            'function connect() {\n'
            '  try { browser.storage.local.set({ __sk71_bgconnect: { t: Date.now(), trying: trying,\n'
            '    ws: ws ? ws.readyState : -1 } }); } catch (e) {}\n'
            '  if (trying || (ws && ws.readyState <= 1)) return;')
        a2 = "      trying = false;\n      status(\"no_token\");"
        assert a2 in src, "якорь no_token не найден"
        src = src.replace(a2,
            '      trying = false;\n      try { browser.storage.local.set({ __sk71_bgnotoken: Date.now() }); } catch (e) {}\n'
            '      status("no_token");')
        a3 = "function wsAttempt(url, token) {"
        assert a3 in src, "якорь wsAttempt не найден"
        src = src.replace(a3, a3 + "\n  try { browser.storage.local.set({ __sk71_bgdial: { url: url, t: Date.now() } }); } catch (e) {}")
        bj.write_text(src, encoding="utf-8")
        with zipfile.ZipFile(EXT_XPI, "w", zipfile.ZIP_DEFLATED) as z:
            for p in EXT_SRC.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(EXT_SRC))
        with zipfile.ZipFile(PROD_XPI, "w", zipfile.ZIP_DEFLATED) as z:
            for p in (REPO / "extension").rglob("*"):
                if p.is_file() and ".chrome" not in str(p):
                    z.write(p, p.relative_to(REPO / "extension"))
        for _ in range(10):
            zpid = listener_pid(PORT)
            if not zpid:
                break
            subprocess.run(["kill", "-9", str(zpid)])
            time.sleep(0.4)
        br = subprocess.Popen(["python3", str(REPO / "bridge" / "sidekick_bridge.py")],
                              env={**os.environ, "SIDEKICK_HOME": str(HUB), "SIDEKICK_PORT": str(PORT)},
                              stdout=open(HUB / "bridge.log", "w", encoding="utf-8"),
                              stderr=subprocess.STDOUT)
        time.sleep(1.0)
        note("S0a тест-мост слушает :8797", listener_pid(PORT) == br.pid, f"pid={listener_pid(PORT)}")
        if not http_up():
            httpd = subprocess.Popen(["python3", str(REPO / "tests" / "serve.py")],
                                     env={**os.environ, "SIDEKICK_TEST_PORT": str(TEST_PORT)},
                                     stdout=open(S / "serve.log", "w", encoding="utf-8"),
                                     stderr=subprocess.STDOUT)
            for _ in range(30):
                if http_up():
                    break
                time.sleep(0.4)
        note("S0b тестовые страницы на :8899", http_up())
        if not listener_pid(MAR_PORT):
            flog = open(S / "firefox.log", "w", encoding="utf-8")
            subprocess.Popen(
                [str(HOME / ".local/firefox-158/firefox"), "--marionette",
                 "--remote-debugging-port=9393", "--remote-allow-system-access",
                 "--profile", str(HOME / ".firefox-test-profile"), "--no-remote"],
                env={**ENV, "MOZ_DISABLE_CONTENT_SANDBOX": "1"},
                stdout=flog, stderr=subprocess.STDOUT)
        t1 = time.time()
        while time.time() - t1 < 40 and not listener_pid(MAR_PORT):
            time.sleep(1.0)
        m = Marionette("127.0.0.1", MAR_PORT)
        m.script(SPARE_TAB_JS)
        reg = m.ascript(SETUP_JS % str(REPO / "tests"))
        note("S0c0 window-актор зарегистрирован", isinstance(reg, dict) and reg.get("ok"), reg)
        # FF кэширует manifest-цепочку background.scripts в регистрации аддона:
        # temp-установка поверх живого id НЕ обновляет список bg-скриптов —
        # сначала честный uninstall (иначе bg стартует со старым списком и
        # падает на ReferenceError: I18N)
        u = uninstall(m)
        r0 = install(m, EXT_XPI)
        note("S0c тест-XPI установлен (i18n-сборка)", bool(r0.get("ok")),
             f"{r0.get('url','')} un={u}")
        # storage временного аддона переживает переустановку (тот же id) — трёмstate
        # стенда, чтобы тема/язык стартовали детерминированно (system/en)

        token = (HUB / "token").read_text().strip()
        # storage временного аддона переживает переустановку (тот же id) — трём state
        # ДО подъёма сайдбара: тема/язык стартуют system/en, цикл темы детерминирован
        m.script(OPEN_TAB_JS, [(r0.get("url") or "").rstrip("/") + "/options.html"])
        time.sleep(1.2)
        # temp storage одного id переживает переустановку в профиле — пишем
        # детерминированные значения СТРАНИЦЕЙ самого аддона (не через options UI,
        # который при загрузке может вернуть старый select-язык в storage)
        t1 = time.time()
        ext_url, href = "", ""
        while time.time() - t1 < 20 and not ext_url:
            d0 = ui_state(m)
            href = d0.get("href", "") or d0.get("_err", "")
            if "sidebar.html" in href:
                ext_url = href.split("sidebar.html")[0]
            else:
                m.script(SC_SHOW_JS)
                time.sleep(0.8)
        note("S0d сайдбар тест-XPI поднят", bool(ext_url), href[:80])
        # temp storage одного id переживает переустановку в профиле — пишем
        # детерминированные значения СТРАНИЦЕЙ самого аддона (url известна из
        # поднятого сайдбара), затем пересоздаём сайдбар: новый boot = en/system
        assert ext_url, "нет url аддона для seed"
        seed_open = m.ascript(r"""
const resolve = arguments[arguments.length - 1];
(() => {
  try {
    const win = Services.wm.getMostRecentWindow('navigator:browser');
    win.gBrowser.addTrustedTab(arguments[0] + "seed_probe.html");
    resolve("seed-tab-opened");
  } catch (e) { resolve("err:" + String(e && e.message || e).slice(0, 140)); }
})();
""", args=[ext_url])
        seed_ok = ""
        t1 = time.time()
        while time.time() - t1 < 12:
            seed_ok = op(m, "seed", "seedCheck")
            if seed_ok == "SEEDOK":
                break
            time.sleep(0.5)
        m.script(CLOSE_TAB_JS, args=["seed_probe"])
        note("S0d1 storage засеян страницей аддона (en/system)", seed_ok == "SEEDOK",
             f"{seed_open} {seed_ok}")
        m.script(SC_HIDE_JS)  #hide destroys the sidebar browser => fresh boot on SHOW
        time.sleep(1.2)
        m.script(SC_SHOW_JS)
        seed_boot_ok = False
        t1 = time.time()
        while time.time() - t1 < 15:
            d0 = ui_state(m)
            if d0.get("langAttr") == "en" and d0.get("theme") == "system":
                seed_boot_ok = True
                break
            time.sleep(0.5)
        note("S0d1b сайдбар перезапущен на en/system", seed_boot_ok, d0)
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
        note("S0d2 токен посеян (options.html)", seeded == "seeded", seeded)
        m.script(OPEN_TAB_JS, [PAGE_URL])
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        ping = op(m, "options", "bgPing")
        wsc = op(m, "options", "wsCheck", timeout=15)
        diag = {}
        for key in ("__sk71_bgconnect", "__sk71_bgnotoken", "__sk71_bgdial", "__sk71_bgfetch"):
            diag[key[2:]] = op(m, "options", "storageGet", arg=key)
        blog = ""
        try:
            blog = (HUB / "bridge.log").read_text(encoding="utf-8", errors="replace")[-200:]
        except Exception:
            pass
        note("S0e расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} bgPing={ping} ws={wsc} "
             f"diag={json.dumps(diag, ensure_ascii=False)[:280]} bridge.log tail: {blog!r}")
        if ext_url:
            m.script(OPEN_TAB_JS, [ext_url + "options.html"])
            time.sleep(1.0)
            m.script(OPEN_TAB_JS, [PAGE_URL])
        spec = m.script("return Services.wm.getMostRecentWindow('navigator:browser')"
                        ".gBrowser.selectedBrowser.currentURI.spec")
        if "youtube" not in str(spec):
            spec = m.script(
                "return (() => { const g = Services.wm.getMostRecentWindow('navigator:browser');"
                "for (const t of g.gBrowser.tabs) {"
                "  if (t.linkedBrowser.currentURI.spec.includes('youtube.html')) {"
                "    g.gBrowser.selectedTab = t; return t.linkedBrowser.currentURI.spec; }}"
                "return 'no-youtube-tab'; })();")
        note("S0f youtube.html активна", "youtube" in str(spec), str(spec)[:120])

        # ---------- I1: EN по умолчанию, всё из словаря ----------
        # композер: SK-38 live-ротация фраз (overlay #phin из PH_KEYS) — проверяем
        # живую фразу по набору словаря, статичный placeholder может быть пуст
        PH_KEYS = ["sb.composer_ph", "sb.chip_tabs", "sb.chip_trash",
                   "sb.chip_youtube", "sb.chip_group", "sb.chip_explain"]
        EN_PH = {EN[k] for k in PH_KEYS}
        RU_PH = {RU[k] for k in PH_KEYS}

        def phrase(d):
            return d.get("phin") or d.get("ph") or ""

        ok_sb = ensure_sidebar_open(m)
        d1 = ui_state(m)
        note("I1a сайдбар открыт, интерфейс EN из en.json",
             ok_sb and d1.get("ttl") == EN["sbt.new_chat"]
             and d1.get("sub") == EN["sbt.tagline"]
             and phrase(d1) in EN_PH
             and d1.get("go") == EN["sbt.t_send"]
             and d1.get("langAttr") == "en", d1)
        note("I1b ни одной кириллической подписи в EN-сборке",
             not any(CYR.search(str(v)) for v in
                     [d1.get("ttl"), d1.get("sub"), d1.get("ph"), d1.get("phin"),
                      d1.get("go"), d1.get("themeLbl"), d1.get("settings")]), d1)
        note("I1c скрин EN", shot("i1-en-default"), "shots/i1-en-default.png")

        # ---------- I2: тема light (label из словаря) ----------
        th2 = op(m, "sidebar", "clickTheme")
        d2 = ui_state(m)
        note("I2a тема light кликом, label темы из словаря (EN)",
             th2 == "light" and d2.get("theme") == "light"
             and d2.get("themeLbl") == EN["sb.theme_light"], f"{th2} {d2.get('themeLbl')}")
        note("I2b скрин EN light", shot("i2-en-light"), "shots/i2-en-light.png")

        # ---------- I3: RU через options — ЖИВОЙ пересбор без reload ----------
        ts_en = d2.get("ts")
        ch = op(m, "options", "langSet", arg="ru")
        d3, t3 = {}, time.time()
        while time.time() - t3 < 10:
            d3 = ui_state(m)
            if d3.get("ttl") == RU["sbt.new_chat"]:
                break
            time.sleep(0.5)
        o3 = opt_state(m)
        note("I3a RU через select: сайдбар перераснулся ЖИВОМ",
             str(ch).startswith("changed:ru") and d3.get("ttl") == RU["sbt.new_chat"]
             and d3.get("sub") == RU["sbt.tagline"]
             and phrase(d3) in RU_PH and CYR.search(phrase(d3))
             and d3.get("go") == RU["sbt.t_send"]
             and d3.get("langAttr") == "ru", f"{ch} {d3}")
        note("I3b БЕЗ перезагрузки окна: win ts не менялся, feed-домен жив",
             ts_en and d3.get("ts") == ts_en and d3.get("href") == d2.get("href"),
             f"ts_en={ts_en} ts_ru={d3.get('ts')}")
        note("I3c options.html сам RU (select=ru, заголовки из ru.json)",
             o3.get("langVal") == "ru" and o3.get("tokH") == RU["opt.h_token"]
             and o3.get("save") == RU["opt.save"] and RU["opt.title"] == o3.get("title"), o3)
        note("I3d скрин RU light (live-switch виден на скрине)", shot("i3-ru-light"),
             "shots/i3-ru-light.png")

        # ---------- I4: тема dark при RU (вторая тема) ----------
        th4 = op(m, "sidebar", "clickTheme")
        d4 = ui_state(m)
        note("I4a тема dark, label RU из словаря",
             th4 == "dark" and d4.get("theme") == "dark"
             and d4.get("themeLbl") == RU["sb.theme_dark"]
             and d4.get("ttl") == RU["sbt.new_chat"], f"{th4} {d4.get('themeLbl')}")
        note("I4b скрин RU dark (4/4: EN/RU x 2 темы)", shot("i4-ru-dark"), "shots/i4-ru-dark.png")

        # ---------- I5: content-world live-switch (подпись превью) ----------
        # I5a снимает EN-превью, I5b — RU; обе фазы обязаны стартовать из
        # известного языка (после I3/I4 стенд стоит в RU) — пируем select и
        # ждём, пока фон подтвердит словарь
        def set_lang(lang):
            op(m, "options", "langSet", arg=lang)
            t1 = time.time()
            while time.time() - t1 < 8:
                p = op(m, "options", "bgPing")
                if isinstance(p, dict) and p.get("lang") == lang:
                    return True
                time.sleep(0.4)
            return False

        set_lang("en")
        time.sleep(1.2)  # дать content-вкладке переварить i18n_changed
        box = {}
        th = rpc_async("action.do", {"selector": "#q", "action": "click",
                                     "ms": 2200, "task": TASK}, box)
        time.sleep(0.9)
        ov_en = overlay_probe(m)
        shot("i5-preview-ru-later")  # превью-кадр EN-фазы (переименование не нужно: baseline)
        th.join(timeout=60)
        r_en = box.get("r", {})
        lab_en = str(ov_en.get("label", ""))
        ftail = ""
        try:
            ftail = "|".join(re.findall(r"JavaScript error:[^\n]*(?:content|background)[^\n]*",
                                        (S / "firefox.log").read_text(encoding="utf-8", errors="replace")))[-150:]
        except Exception:
            pass
        # кириллица в имени ЦЕЛИ («Поиск» с самой страницы) — данные, не UI;
        # UI-часть подписи — глагол из словаря: проверяем префикс-глагол
        note("I5a превью EN: подпись из en.json, ноль кириллицы",
             r_en.get("ok") and ov_en.get("pvVisible")
             and lab_en.lower().startswith(EN["prev.click"]),
             f"label={lab_en!r} ov={json.dumps(ov_en, ensure_ascii=False)[:140]} r={str(r_en)[:100]} fx={ftail}")
        assert set_lang("ru"), "content-фаза: фон не подтвердил ru"
        time.sleep(1.5)
        box2 = {}
        th2b = rpc_async("action.do", {"selector": "#q", "action": "click",
                                       "ms": 2200, "task": TASK}, box2)
        time.sleep(0.9)
        ok_shot5 = shot("i5-preview-ru")
        ov_ru = overlay_probe(m)
        th2b.join(timeout=60)
        r_ru = box2.get("r", {})
        lab_ru = str(ov_ru.get("label", ""))
        note("I5b content live-switch: та же подпись RU без перезагрузки вкладки",
             r_ru.get("ok") and ov_ru.get("pvVisible")
             and lab_ru.startswith(RU["prev.click"]) and CYR.search(lab_ru),
             f"label={lab_ru!r} ov={json.dumps(ov_ru, ensure_ascii=False)[:140]} r={str(r_ru)[:100]}")
        note("I5c скрин RU-превью", ok_shot5, "shots/i5-preview-ru.png")
        # вкладки/язык: стенд cleanup, язык не трогаем (временный аддон уйдёт вместе со storage)

    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — traceback в note при падении
        import traceback
        note("BODY крах харнесса", False, traceback.format_exc()[-400:])
    finally:
        # ---------- Z: уборка (прецедент sk37) ----------
        try:
            if m:
                m.script(CLOSE_TAB_JS, args=["youtube"])
                m.script(CLOSE_TAB_JS, args=["moz-extension"])
                m.script(SPARE_TAB_JS)
        except Exception as e:
            print("WARN tabs:", e, flush=True)
        try:
            if m:
                uninstall(m)  # та же причина: prod-цепочка [bell,background] обязана
                install(m, PROD_XPI)  # вытеснить i18n-цепочку из регистрации начисто
                t2 = time.time()
                while time.time() - t2 < 20 and ss_count(8791) == 0 and prod_before > 0:
                    time.sleep(0.5)
        except Exception as e:
            print("WARN prod reinstall:", e, flush=True)
        try:
            if m:
                m.close()
        except Exception:
            pass
        if br:
            br.terminate()
            try:
                br.wait(timeout=5)
            except subprocess.TimeoutExpired:
                br.kill()
        if httpd:
            httpd.terminate()
        t1 = time.time()
        while prod_before > 0 and time.time() - t1 < 30 and ss_count(8791) < prod_before:
            time.sleep(1.0)
        note("Z2 прод-аддон переустановлен, :8791 не хуже исходного",
             ss_count(8791) >= prod_before,
             f"@8791={ss_count(8791)} (было {prod_before})")
        fpid = listener_pid(MAR_PORT)
        if fpid:
            subprocess.run(["kill", str(fpid)], capture_output=True)
            t1 = time.time()
            while time.time() - t1 < 15 and listener_pid(MAR_PORT):
                time.sleep(0.5)
            if listener_pid(MAR_PORT):
                subprocess.run(["kill", "-9", str(listener_pid(MAR_PORT))], capture_output=True)
                time.sleep(1.0)
        note("Z3 тест-Firefox погашен", not listener_pid(MAR_PORT),
             f"pid={listener_pid(MAR_PORT) or '—'}")
        skipped = [k for k, v in results.items() if v["ok"] is None]
        fails = [k for k, v in results.items() if v["ok"] is False]
        passed = [k for k, v in results.items() if v["ok"] is True]
        print(f"\nИТОГ: {len(passed)} PASS / {len(fails)} FAIL / {len(skipped)} SKIP "
              f"из {len(results)}", flush=True)
        for k in fails:
            print("FAIL:", k, "—", results[k]["detail"][:200], flush=True)
        (S / "sk71_acc_results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
