#!/usr/bin/env python3
"""SK-39 живая приёмка: выразительное движение — пружина, ripple, позы, reduced-motion.

Стенд: изолированный мост :8798 + тест-копия расширения (FALLBACK_SERVER->:8798,
пробников нет) в firefox-158 (Marionette 2929, DISPLAY :10 Xephyr). Прод :8791 не
трогаем; в конце — переустановка prod-аддона, если Firefox был общий. Прецеденты:
sk36_acc.py (стенд/актор), sk35_acc.py (RPC-клик через мост).

Фазы:
  S0  стенд: мост, XPI, токен, страница sk39.html, оверлей жив
  A1  скриншот-серия клика: до / превью (курсор+спиннер+кольцо+пузырёк) / ripple
      (кольца WAAPI, акцент из темы страницы) / после (чисто). Клик реально исполнен,
      ripple появился ПОСЛЕ клика (не гейтит). Кадры различаются побайтово.
  A2  fps: rAF-счётчик во время цепочки кликов (глайды+риппели+спиннеры) — не ниже
      55 (или >=90% базовой линии, если Xephyр сам ограничен)
  A3  prefers-reduced-motion (pref ui.prefersReducedMotion=1): transition 0s,
      у ripple НЕТ WAAPI-анимации (мгновенное состояние), кольца исчезают за ~200 мс
  A4  latency before/after: медиана round-trip action.do (ms=0) HEAD vs рабочее
      дерево — деградация <= 20 мс (визуал не имеет права тормозить исполнение)
  A5  поза ввода: во время hold у курсора I-beam (s-text), значение легло
  A6  hover: мягкая подсветка (.glw) после наведения
  R1  прод :8791 не получил тестовых коннектов
  Z   уборка: вкладки, мост, serve.py, Firefox (или prod-аддон, если Firefox общий)
"""
import asyncio
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import statistics
import sys
import threading
import time
import uuid
import zipfile

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
S = pathlib.Path(os.environ.get("SK39_SCRATCH", str(HOME / ".hermes/cache/scratch/sk39")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, BEFORE_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "before.xpi", S / "prod.xpi"
SHOTS = S / "shots"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8798
PAGE_PORT = 8899
PAGE_URL = f"http://127.0.0.1:{PAGE_PORT}/sk39.html"
results = {}
TOKEN = ""

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


def shot(name):
    p = SHOTS / f"{name}.png"
    subprocess.run(["scrot", "-o", str(p)], env=ENV, capture_output=True)
    if not p.exists():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


# ---- агентский RPC через мост -----------------------------------------------------
# Шина агента — файлы: rpc_in.jsonl -> WS "rpc" расширение -> rpc_out.jsonl.
# WS-хелло от агента недопустим: SK-49 — один сокет, второй hello вытесняет расширение.

def rpc_sync(method, params, timeout=25):
    for attempt in range(2):
        rid = str(uuid.uuid4())
        with open(HUB / "rpc_in.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"v": 1, "id": rid, "method": method, "params": params},
                               ensure_ascii=False) + "\n")
        t0 = time.time()
        while time.time() - t0 < timeout:
            p = HUB / "rpc_out.jsonl"
            if p.exists():
                for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        obj = json.loads(ln)
                    except ValueError:
                        continue
                    if obj.get("id") == rid:
                        if obj.get("err") == "no_extension" and attempt == 0:
                            time.sleep(1.0)  # реконнект расширения (refire, прецедент conftest)
                            break
                        return obj, time.time() - t0
            time.sleep(0.05)
        return {"err": "harness_timeout"}, None
    return {"err": "harness_exhausted"}, None


FIRED = {}


def fire_async(key, method, params):
    def _w():
        try:
            FIRED[key] = rpc_sync(method, params)
        except Exception as e:  # noqa: BLE001
            FIRED[key] = ({"err": str(e)[:150]}, None)
    th = threading.Thread(target=_w, daemon=True)
    th.start()
    return th


# ---- Marionette-блоки (прецедент sk36_acc.py) -------------------------------------

SPARE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "win.gBrowser.addTrustedTab('about:blank'); return 'spare';})();")
OPEN_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
               "const b = win.gBrowser.addTrustedTab(arguments[0]); win.gBrowser.selectedTab = b;"
               "return 'tab'; })();")
RELOAD_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
             "win.gBrowser.selectedBrowser.reload(); return 'reloaded';})();")
CLOSE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "for (let i = win.gBrowser.tabs.length - 1; i >= 0; i--) {"
                " const u = win.gBrowser.tabs[i].linkedBrowser.currentURI.spec;"
                " if ((u.includes('8899') || u.includes('options.html')) && win.gBrowser.tabs.length > 1)"
                " win.gBrowser.removeTab(win.gBrowser.tabs[i]);}"
                " if (win.gBrowser.tabs.length === 0) win.gBrowser.addTrustedTab('about:blank');"
                "return 'closed';})();")
RM_PREF_JS_ON = "return (() => { Services.prefs.setIntPref('ui.prefersReducedMotion', 1); return 'rm-on';})();"
RM_PREF_JS_OFF = "return (() => { Services.prefs.setIntPref('ui.prefersReducedMotion', 0); return 'rm-off';})();"
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
    let url = uri ? uri.spec : "";
    if (!url) {
      try {
        const { ExtensionManager } =
          ChromeUtils.importESModule("resource://gre/modules/ExtensionParent.sys.mjs");
        const e = ExtensionManager.get("hermes-sidekick@hermes.local");
        if (e && e.baseURI) url = e.baseURI.spec;
      } catch (e) {}
    }
    resolve({ok: true, id: ext && ext.id, url});
  } catch (e) { resolve({ok: false, err: String(e && e.message || e).slice(0, 200)}); }
})();
"""
SETUP_JS = r"""
const resolve = arguments[arguments.length - 1];
(async () => {
  try {
  const ph = Services.io.getProtocolHandler('resource').QueryInterface(Ci.nsIResProtocolHandler);
  ph.setSubstitution('sk39', await Services.io.newURI('file://%s'))
    ;
    try { ChromeUtils.unregisterWindowActor('Sk39Probe'); } catch (e) {}
    // FF158: remoteTypes матчится префиксом StringifyKind (glob '*' не работает) —
    // пустой список = все типы; для webIsolated-процесса обязателен флаг
    // safeForUntrustedWebProcess (гейт dom.jsipc.check_safeForUntrustedWebProcess).
    ChromeUtils.registerWindowActor('Sk39Probe', {
      child: { esModuleURI: 'resource://sk39/sk39_actor.sys.mjs' },
      parent: { esModuleURI: 'resource://sk39/sk39_actor.sys.mjs',
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
    let gw = null;
    if (payload.match === 'options') {
      for (const t of win.gBrowser.tabs)
        if (t.linkedBrowser.currentURI.spec.includes('options.html')) {
          gw = t.linkedBrowser.browsingContext.currentWindowGlobal; break; }
    } else {
      gw = win.gBrowser.selectedBrowser.browsingContext.currentWindowGlobal;
    }
    if (!gw) return resolve({ok: false, err: 'no-gw:' + payload.match});
    const val = await gw.getActor('Sk39Probe').sendQuery('sk39:op', payload);
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
    except Exception as e:  # noqa: BLE001
        return {"_err": str(e)[:120]}
    if not (isinstance(r, dict) and r.get("ok")):
        return {"_err": r.get("err", "?") if isinstance(r, dict) else str(r)[:120]}
    v = r.get("val")
    try:
        j = json.loads(v) if isinstance(v, str) else {}
    except (json.JSONDecodeError, TypeError):
        return {"_err": "bad-json:" + str(v)[:120]}
    return j if "err" in j else j.get("v")


def scan(m):
    s = op(m, "page", "scan")
    return s if isinstance(s, dict) else {"host": False, "_err": str(s)[:120]}


def page_state(m):
    s = op(m, "page", "page")
    return s if isinstance(s, dict) else {"_err": str(s)[:120]}


def build_xpi(src_dir, xpi):
    with zipfile.ZipFile(xpi, "w", zipfile.ZIP_DEFLATED) as z:
        for p in pathlib.Path(src_dir).rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(src_dir))


def prep_ext(dst, content_js=None):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(REPO / "extension", dst, ignore=shutil.ignore_patterns(".chrome"))
    if content_js is not None:
        (dst / "content.js").write_text(content_js, encoding="utf-8")
    bj = dst / "background.js"
    src = bj.read_text(encoding="utf-8")
    assert 'const FALLBACK_SERVER = "ws://127.0.0.1:8791";' in src
    src = src.replace('const FALLBACK_SERVER = "ws://127.0.0.1:8791";',
                      f'const FALLBACK_SERVER = "ws://127.0.0.1:{PORT}";')
    assert "for (const u of [saved, FALLBACK_SERVER])" in src
    src = src.replace("for (const u of [saved, FALLBACK_SERVER])",
                      "for (const u of [FALLBACK_SERVER])")
    # SK-39: токен зашит в тест-сборку (посев через options.html не нужен: base URI
    # тестового XPI из chrome-слоя в FF158 не достаётся, а токен — статический файл)
    tok = (HUB / "token").read_text().strip()
    gt_old = ('async function getToken() {\n'
              '  const { bridgeToken } = await browser.storage.local.get("bridgeToken");\n'
              '  return (bridgeToken || "").trim();\n}')
    assert gt_old in src, "getToken не найден — тест-инъекция токена не применена"
    gt_new = "async function getToken() {\n  return '" + tok.replace("'", "") + "'; // sk39-test-token\n}"
    src = src.replace(gt_old, gt_new)
    bj.write_text(src, encoding="utf-8")


def wait_ext_ready(m, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if op(m, "page", "ready") is True:
            return True
        time.sleep(0.5)
    return False


def median_latency(m, n=6):  # noqa: ARG001
    lat = []
    for _ in range(n):
        res, dt = rpc_sync("action.do", {"action": "click", "selector": "#btn", "ms": 0})
        if not (isinstance(res, dict) and res.get("ok") and (res.get("result") or {}).get("executed", True)
                and "err" not in (res.get("result") or {})):
            lat.append(float("inf"))
        else:
            lat.append(dt * 1000)
        time.sleep(0.35)
    return statistics.median([x for x in lat if x != float("inf")] or [float("inf")])


def main():
    global TOKEN
    SHOTS.mkdir(parents=True, exist_ok=True)
    if HUB.exists():
        shutil.rmtree(HUB)
    HUB.mkdir(parents=True)
    shutil.copy(HOME / "sidekick/bridge/hub/token", HUB / "token")
    TOKEN = (HUB / "token").read_text().strip()
    prod_before = ss_count(8791)
    br, m, srv, ff = None, None, None, None
    ff_owned = False
    try:
        # ---------- S0: стенд ----------
        prep_ext(EXT_SRC)
        build_xpi(EXT_SRC, EXT_XPI)
        head_js = subprocess.run(["git", "-C", str(REPO), "show", "HEAD:extension/content.js"],
                                 capture_output=True, text=True)
        assert head_js.returncode == 0 and len(head_js.stdout) > 10000, "HEAD content.js не получен"
        prep_ext(S / "ext_before", content_js=head_js.stdout)
        build_xpi(S / "ext_before", BEFORE_XPI)
        prod_dir = S / "ext_prod"
        if prod_dir.exists():
            shutil.rmtree(prod_dir)
        shutil.copytree(REPO / "extension", prod_dir, ignore=shutil.ignore_patterns(".chrome"))
        build_xpi(prod_dir, PROD_XPI)
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
        note("S0a тест-мост слушает :8798", listener_pid(PORT) == br.pid, f"pid={listener_pid(PORT)}")
        if not listener_pid(PAGE_PORT):
            srv = subprocess.Popen(["python3", str(REPO / "tests" / "serve.py")],
                                   env={**os.environ, "SIDEKICK_TEST_PORT": str(PAGE_PORT)},
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.8)
        note("S0b страницы тестов на :8899", bool(listener_pid(PAGE_PORT)))
        if not listener_pid(MAR_PORT):
            ff = subprocess.Popen(
                [str(HOME / ".local/firefox-158/firefox"), "--marionette",
                 "--remote-debugging-port=9393", "--remote-allow-system-access",
                 "--profile", str(HOME / ".firefox-test-profile"), "--no-remote"],
                env={**ENV, "MOZ_DISABLE_CONTENT_SANDBOX": "1"},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ff_owned = True
        t1 = time.time()
        while time.time() - t1 < 40 and not listener_pid(MAR_PORT):
            time.sleep(1.0)
        m = Marionette("127.0.0.1", MAR_PORT)
        m.script(SPARE_TAB_JS)
        reg = m.ascript(SETUP_JS % str(REPO / "tests"))
        note("S0c window-актор зарегистрирован", isinstance(reg, dict) and reg.get("ok"), reg)
        r0 = m.ascript(ADDON_INSTALL_JS, args=[str(EXT_XPI.resolve())])
        note("S0d тест-XPI (рабочее дерево, токен зашит) установлен", isinstance(r0, dict) and r0.get("ok"),
             r0.get("err", "") if isinstance(r0, dict) else r0)
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        note("S0e расширение подключено к :8798", ss_count(PORT) > 0, f"conns={ss_count(PORT)}")
        m.script(OPEN_TAB_JS, [PAGE_URL])
        t1 = time.time()
        while time.time() - t1 < 15 and "sk39" not in str(op(m, "page", "href")):
            time.sleep(0.5)
        ok_ready = wait_ext_ready(m)
        note("S0g страница sk39.html + оверлей жив", ok_ready, op(m, "page", "href"))
        if not ok_ready:
            raise RuntimeError("стенд слепой: оверлей не появился")

        # ---------- A1: скриншот-серия клика ----------
        h_before = shot("a1_before")
        st0 = page_state(m)
        fire_async("a1", "action.do", {"action": "click", "selector": "#btn", "ms": 1400})
        time.sleep(0.5)
        h_prev = shot("a2_preview")
        s2 = scan(m)
        cur2, ring2, pv2 = s2.get("cur") or {}, s2.get("ring") or {}, s2.get("pv") or {}
        ok_prev = (cur2.get("on") and cur2.get("wait") and s2.get("spin", {}).get("disp") == "block"
                   and "on" in str(ring2.get("cls", "")) and "on" in str(pv2.get("cls", ""))
                   and s2.get("curiAnim") == "sk39breath")
        note("A1a превью: курсор+спиннер+кольцо+пузырёк и дыхание", ok_prev,
             f"cur={cur2} spin={s2.get('spin')} ring={ring2.get('cls')} pv={pv2.get('cls')}")
        time.sleep(1.15)
        h_rip = shot("a3_ripple")
        s3 = scan(m)
        st_mid = page_state(m)
        rp0 = s3.get("rp0") or {}
        ok_rip = (s3.get("rp", 0) >= 3 and rp0.get("anims", 0) >= 1
                  and rp0.get("bc") == "rgb(255, 102, 0)"
                  and st_mid.get("clicks", 0) >= 1 and 0 <= st_mid.get("rpSeen", -1) < 400)
        note("A1b ripple после клика: 3+ кольца WAAPI, акцент темы (оранж.), rpSeen<400мс",
             ok_rip, f"rp={s3.get('rp')} rp0={rp0} page={st_mid} btnColor={st_mid.get('btnColor')}")
        time.sleep(1.1)
        s4 = scan(m)
        h_after = shot("a4_after")
        ok_clean = s4.get("rp") == 0 and s4.get("fxKids") == 0
        note("A1c после: ripple убран, слой чист", ok_clean, f"rp={s4.get('rp')} fx={s4.get('fxKids')}")
        res1, _ = FIRED.get("a1", ({"err": "нет ответа"}, None))
        ok_exec = isinstance(res1, dict) and res1.get("ok") and \
            (res1.get("result") or {}).get("executed", True) and st_mid.get("clicks", 0) == st0.get("clicks", 0) + 1
        frames_distinct = len({h_before, h_prev, h_rip, h_after}) == 4 and all(
            [h_before, h_prev, h_rip, h_after])
        note("A1d клик реально исполнен (счётчик +1), RPC ok", ok_exec, res1 if not ok_exec else "")
        note("A1e кадры серии различаются побайтово (до/превью/ripple/после)", frames_distinct,
             f"{h_before} {h_prev} {h_rip} {h_after}")

        # ---------- A2: fps под нагрузкой ----------
        op(m, "page", "fpsStart")
        time.sleep(1.2)
        base = op(m, "page", "fpsRead") or {}
        op(m, "page", "fpsStart")
        for _ in range(3):
            rpc_sync("action.do", {"action": "click", "selector": "#btn", "ms": 250})
        busy = op(m, "page", "fpsRead") or {}
        bf, uf = base.get("fps", 0), busy.get("fps", 0)
        ok_fps = uf >= 55 or (0 < bf < 55 and uf >= 0.9 * bf)
        note("A2 fps во время анимаций >= 55 (или >=90% базы Xephyr)", ok_fps,
             f"base={bf} busy={uf} frames={busy.get('frames')}/{busy.get('ms')}мс")

        # ---------- A3: prefers-reduced-motion ----------
        m.script(RM_PREF_JS_ON)
        time.sleep(0.5)  # медиа-состояние доезжает до контента не мгновенно
        rm_on = op(m, "page", "rmq")
        # слить старые ripple от A2, иначе снимем чужую анимацию вместо своей
        t_drain = time.time()
        while time.time() - t_drain < 4:
            if scan(m).get("rp", 0) == 0:
                break
            time.sleep(0.2)
        fire_async("a3", "action.do", {"action": "click", "selector": "#btn", "ms": 300})
        snap_rm, t_end = None, time.time() + 1.0
        while time.time() < t_end:
            s = scan(m)
            if s.get("rp", 0) >= 1:
                snap_rm = s
                shot("a5_reduced_motion")
                break
            time.sleep(0.05)
        time.sleep(0.6)
        s_after_rm = scan(m)
        m.script(RM_PREF_JS_OFF)
        rm_off = op(m, "page", "rmq")
        ok_rm = (rm_on is True and snap_rm is not None
                 and (snap_rm.get("rp0") or {}).get("anims", 9) == 0
                 and abs(float((snap_rm.get("rp0") or {}).get("op", 0)) - 0.55) < 0.2
                 and (snap_rm.get("cur") or {}).get("td") == "0s"
                 and (snap_rm.get("spin") or {}).get("anim") == "none"
                 and s_after_rm.get("rp") == 0 and rm_off is False)
        note("A3 reduced-motion: 0s переходы, ripple без WAAPI, исчезает за ~200мс", ok_rm,
             f"rmq={rm_on}->{rm_off} snap={snap_rm and {'rp': snap_rm.get('rp'), 'rp0': snap_rm.get('rp0'), 'td': (snap_rm.get('cur') or {}).get('td'), 'spin': (snap_rm.get('spin') or {}).get('anim')}} after={s_after_rm.get('rp')}")

        # ---------- A4: latency before/after ----------
        r1i = m.ascript(ADDON_INSTALL_JS, args=[str(BEFORE_XPI.resolve())])
        time.sleep(1.2)
        m.script(RELOAD_JS)
        ok_b = wait_ext_ready(m) and ss_count(PORT) > 0
        lat_before = median_latency(m) if ok_b else float("inf")
        r2i = m.ascript(ADDON_INSTALL_JS, args=[str(EXT_XPI.resolve())])
        time.sleep(1.2)
        m.script(RELOAD_JS)
        ok_a = wait_ext_ready(m) and ss_count(PORT) > 0
        lat_after = median_latency(m) if ok_a else float("inf")
        ok_lat = ok_b and ok_a and lat_after != float("inf") and \
            (lat_after - lat_before) <= 20
        note("A4 latency action.do: after(раб.дерево) - before(HEAD) <= 20мс", ok_lat,
             f"before={lat_before:.0f}мс after={lat_after:.0f}мс Δ={lat_after - lat_before:+.0f}мс "
             f"( reinstalls: {bool(r1i.get('ok') if isinstance(r1i, dict) else 0)}/"
             f"{bool(r2i.get('ok') if isinstance(r2i, dict) else 0)} )")

        # ---------- A5: поза ввода (I-beam) ----------
        fire_async("a5", "action.do", {"action": "input", "selector": "#inp",
                                       "value": "привет мир", "ms": 1200})
        time.sleep(0.5)
        s5 = scan(m)
        shot("a6_input_ibeam")
        time.sleep(0.95)
        st5 = page_state(m)
        ok_inp = (s5.get("cur") or {}).get("itext") is True and st5.get("inp") == "привет мир"
        note("A5 ввод: во время hold I-beam (s-text), значение легло", ok_inp,
             f"cur={s5.get('cur')} inp={st5.get('inp')!r}")

        # ---------- A6: hover-подсветка ----------
        # hover — координатный verb (ACTION_DO_COORDS моста); центр #btn берём
        # из A1a-скана: курсор превью был наведён ровно на центр кнопки
        mb = re.search(r"translate3d\((\d+)px, (\d+)px", (cur2.get("tr") or ""))
        bx, by = (int(mb.group(1)), int(mb.group(2))) if mb else (109, 111)
        fire_async("a6", "action.do", {"action": "hover", "x": bx, "y": by, "ms": 300})
        t_a6 = time.time()
        while time.time() - t_a6 < 6 and "a6" not in FIRED:
            time.sleep(0.05)  # ждём исполнения, glow живёт 900мс после него
        s6 = scan(m)
        ok_hover = s6.get("glw", 0) >= 1
        note("A6 hover: мягкая подсветка цели (.glw) после наведения", ok_hover,
             f"glw={s6.get('glw')} fx={s6.get('fxKids')} rpc={str(FIRED.get('a6', ('нет',))[0])[:120]}")

        # ---------- R1: прод не затронут ----------
        prod_after = ss_count(8791)
        note("R1 прод :8791 не получил новых коннектов", prod_after <= prod_before,
             f"before={prod_before} after={prod_after}")
    except Exception as e:  # noqa: BLE001
        import traceback
        note("FATAL", False, traceback.format_exc()[-400:])
    finally:
        # ---------- Z: уборка ----------
        try:
            if m:
                m.script(CLOSE_TAB_JS)
        except Exception:  # noqa: BLE001
            pass
        try:
            if ff and ff_owned:
                ff.terminate()
            elif m:
                m.ascript(ADDON_INSTALL_JS, args=[str(PROD_XPI.resolve())])
        except Exception:  # noqa: BLE001
            pass
        try:
            if srv:
                srv.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            if br:
                br.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            if m:
                m.close()
        except Exception:  # noqa: BLE001
            pass

    bad = [k for k, v in results.items() if v["ok"] is False]
    print(f"\n=== SK-39: {len(results) - len(bad)}/{len(results)} PASS"
          f"{' | FAIL: ' + ', '.join(bad) if bad else ''} ===")
    print("SHOTS:", SHOTS)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
