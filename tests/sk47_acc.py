#!/usr/bin/env python3
"""SK-47 живая приёмка: vision-скорость — даунскейл/кроп кадра, кэш, scale, SoM после даунскейла.

Аудит №2 22.10, проблема 7: 13 вызовов vision_analyze по 33–120 с (один timeout) на PNG
2578×1898 DPR2 — 10–13 мин wall-clock за смену.

Стенд: изолированный мост :8797 + тест-копия расширения (FALLBACK_SERVER->:8797) в
firefox-158 (Marionette 2929). Прод :8791 не трогаем; в конце — переустановка prod-аддона.
Прецеденты: sk44_acc.py, sk46_acc.py.

Фазы:
  S0  стенд: мост :8797, XPI, vision.html на :8899, вкладка задачи sk47-a
  V1  даунскейл: дефолтный скрин = JPEG ≤1280 + scale≈outW/CSS-ширины + capture; max_width:0
      = чистый PNG; max_width:800 = кадр ровно 800; rect карты marks == rect page.dom (CSS px)
  V2  SoM-матрица ПОСЛЕ даунскейла (max_width=1280): клик по КАЖДОМУ номеру — попадание
      (executed, hit=button, страница записала ожидаемый id) — «матрица зелёная»
  V3  clip: кроп диалога (rect из page.dom) — PNG-кроп размера clip×DPR, пиксели только
      диалога (ни одного красного фона), JPEG-кроп ≤1280 с честным scale; скрин-артефакт
  V4  кэш: повтор ≤2 с — hit:cache и тот же dataUrl; action.do scroll — сброс; refill; TTL
  V5  DPR2 живьём (devPixelsPerPx=2): capture.w/vp.w ≈ 2, scale ≈ outW/vp.w, mark-клики
      после двойного DPR попадают; артефакт; pref восстановлен
  V6  замер vision_analyze (тот же эндпоинт/модель/лимит, что у инструмента): 5 прогонов
      «до» (PNG 2578×1898, эквивалент кадра инцидента) vs 5 «после» (JPEG 1280 из V-фаз):
      медиана обеих серий < 20 с, 0 таймаутов (критерий карточки); + тайминг самого rpc page.screenshot
  Z   уборка: вкладки, pref, prod-аддон, :8797 по PID, httpd только если поднимали

Запуск:  python3 tests/sk47_acc.py
"""
import base64
import json
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

import numpy

# ffmpeg лежит не в PATH (~/.hermes/tools/ffmpeg-*/bin) — подставляем в PATH для всех вызовов
for _d in (str(next(iter(pathlib.Path.home().glob(".hermes/tools/ffmpeg-*/bin")), "")),):
    if _d and os.path.isdir(_d) and _d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
S = pathlib.Path(os.environ.get("SK47_SCRATCH", str(HOME / ".hermes/cache/scratch/sk47")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
SHOTS = S / "shots"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
TEST_PORT = 8899
PAGE_URL = f"http://127.0.0.1:{TEST_PORT}/vision.html"
VISION_URL = os.environ.get("SK47_VISION_URL", "http://127.0.0.1:8080/v1/chat/completions")
VISION_MODEL = "qwen3.8-flash-next-strata-ud-iq4-xs"
VISION_PROMPT = "Кратко опиши, что на скриншоте. Перечисли все видимые кнопки и текст."
VISION_TIMEOUT = 120  # потолок таймаута прогона — ровно аудитовский (timeout 120 с)
results = {}

sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
from mario import Marionette  # noqa: E402


def note(name, ok, detail=""):
    ok_v = None if ok == "skip" else bool(ok)
    results[name] = {"ok": ok_v, "detail": str(detail)[:400]}
    print(("PASS " if ok_v else ("SKIP " if ok_v is None else "FAIL ")) + name +
          ("  " + str(detail)[:200] if detail else ""), flush=True)


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)


def listener_pid(port):
    r = subprocess.run(["ss", "-tlnp", f"( sport = :{port} )"], capture_output=True, text=True)
    m2 = re.search(r"pid=(\d+)", r.stdout)
    return int(m2.group(1)) if m2 else None


def ss_count(port):
    r = subprocess.run(["ss", "-tn", "state", "established", f"( sport = :{port} )"],
                       capture_output=True, text=True)
    return max(0, len(r.stdout.strip().splitlines()) - 1)


def shot_screen(name):
    p = SHOTS / f"{name}.png"
    subprocess.run(["scrot", "-o", str(p)], env=ENV, capture_output=True)
    return p.exists()


# ---- RPC через файловую шину моста (тот же контракт, что видит агент) ----------------

def rpc(method, params, timeout=60):
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
        time.sleep(0.1)
    return {"ok": False, "err": "harness_timeout", "id": rid}


def state(task="sk47-a", timeout=40):
    r = rpc("page.text", {"maxChars": 10, "task": task}, timeout=timeout)
    if not r.get("ok"):
        return {"_err": str(r.get("err"))[:160]}
    t = str((r.get("result") or {}).get("title") or "")
    if not t.startswith("sk47:"):
        return {"_err": t[:120]}
    try:
        return json.loads(t[5:])
    except ValueError:
        return {"_err": "bad-title:" + t[:120]}


def state_ready(task="sk47-a", tries=24):
    st = state(task)
    for _ in range(tries):
        if "_err" not in st or "internal_page" in str(st.get("_err", "")):
            pass
        if "_err" not in st:
            return st
        time.sleep(0.5)
        st = state(task)
    return st


def dom(task="sk47-a"):
    r = rpc("page.dom", {"task": task}, timeout=40)
    return r.get("result") or {}


def entry_by(dom_res, sel):
    for e in (dom_res.get("elements") or []) + ((dom_res.get("more") or {}).get("elements") or []):
        if e.get("selector") == sel:
            return e
    return None


# ---- картинки: размеры/пиксели без PIL ------------------------------------------------

def dataurl_bytes(durl):
    return base64.b64decode(durl.split(",", 1)[1])


def img_dims(raw):
    """(w, h, fmt) для PNG/JPEG по заголовкам."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", raw[16:24])
        return w, h, "png"
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            m = raw[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                return w, h, "jpeg"
            if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7 or m == 0x01:
                i += 2
                continue
            seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
            i += 2 + seg
        raise ValueError("JPEG SOF не найден")
    raise ValueError("не PNG/JPEG")


def img_rgb(raw, path):
    """Декоднуть в numpy RGB через ffmpeg (тот же путь, что sk44)."""
    p = pathlib.Path(path)
    p.write_bytes(raw)
    w, h, _ = img_dims(raw)
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p),
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, env=ENV).stdout
    a = numpy.frombuffer(out, numpy.uint8)
    if a.size != w * h * 3:
        raise RuntimeError(f"ffmpeg decode mismatch {a.size} != {w * h * 3}")
    return a.reshape(h, w, 3).astype(int), (w, h)


# ---- vision_analyze: тот же эндпоинт/модель/лимит, что у инструмента ------------------

def vision_once(data_url):
    body = json.dumps({"model": VISION_MODEL, "max_tokens": 800, "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}}]}]}).encode()
    req = urllib.request.Request(VISION_URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ["HERMES_CUSTOM_HOME1_API_KEY"]})
    t0 = time.time()
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=VISION_TIMEOUT).read())
        return time.time() - t0, (r["choices"][0]["message"]["content"] or "")[:200], None
    except Exception as e:  # noqa: BLE001 — засчитываем таймаут/ошибку как прогон
        return time.time() - t0, "", str(e)[:160]


def vision_series(label, data_url, runs=5):
    ts, errs = [], []
    for i in range(runs):
        dt, txt, err = vision_once(data_url)
        ts.append(dt)
        if err:
            errs.append((i, err))
        print(f"    [{label} {i + 1}/{runs}] {dt:.1f}s" + (f" ERR {err}" if err else ""),
              flush=True)
    return ts, errs


# ---- Marionette: вкладки, XPI, pref DPR -------------------------------------------------

SPARE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "win.gBrowser.addTrustedTab('about:blank'); return 'spare';})();")
CLOSE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "for (let i = win.gBrowser.tabs.length - 1; i >= 0; i--) {"
                " const u = win.gBrowser.tabs[i].linkedBrowser.currentURI.spec;"
                " if (u.includes(arguments[0]) && win.gBrowser.tabs.length > 1)"
                " win.gBrowser.removeTab(win.gBrowser.tabs[i]);}"
                " if (win.gBrowser.tabs.length === 0) win.gBrowser.addTrustedTab('about:blank');"
                "return 'closed';})();")
RELOAD_URL_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                 "for (const t of win.gBrowser.tabs)"
                 " if (t.linkedBrowser.currentURI.spec.includes(arguments[0]))"
                 " { win.gBrowser.reloadTab(t); return 'reloaded'; } return 'not-found';})();")
DPR_JS = ("return (() => { Services.prefs.setCharPref('layout.css.devPixelsPerPx',"
          " arguments[0]); return Services.prefs.getCharPref('layout.css.devPixelsPerPx'); })();")

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


def install(m, xpi):
    r = m.ascript(ADDON_INSTALL_JS, args=[str(pathlib.Path(xpi).resolve())])
    if not (isinstance(r, dict) and r.get("ok")):
        raise RuntimeError(f"installTemporaryAddon: {r}")
    return r


def http_up():
    try:
        urllib.request.urlopen(f"{PAGE_URL}", timeout=2)
        return True
    except Exception:
        return False


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    if HUB.exists():
        shutil.rmtree(HUB)
    HUB.mkdir(parents=True)
    shutil.copy(HOME / "sidekick/bridge/hub/token", HUB / "token")
    prod_before = ss_count(8791)
    br, m, httpd = None, None, None
    dpr_raised = False
    try:
        # ---------- S0: стенд ----------
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
        # Стендовая находка SK-47: у временного аддона storage.local ВРЕМЕННАЯ — токен,
        # введённый через options, умирает вместе с браузером (после рестенда профиля
        # стенд молча глодал на no_token). Fission не даёт достать contentDocument из
        # системы, CDP-лист пуст — токен инжектируется в тест-сборку прямо отсюда
        # (то же содержимое, что в HUB/token; прод-XPI не патчится и остаётся без токена
        # = молчит, канал Mac-расширения не вытесняется).
        tok = (HUB / "token").read_text(encoding="utf-8").strip()
        old_get = ('  const { bridgeToken } = await browser.storage.local.get("bridgeToken");\n'
                   '  return (bridgeToken || "").trim();')
        assert old_get in src, "getToken не узнал — патч токена не применён"
        src = src.replace(old_get,
                          '  const { bridgeToken } = await browser.storage.local.get("bridgeToken");\n'
                          f'  return (bridgeToken || "{tok}").trim(); // SK-47 стенд: fallback-токен')
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
                              env={**os.environ, "SIDEKICK_HOME": str(HUB),
                                   "SIDEKICK_PORT": str(PORT)},
                              stdout=open(HUB / "bridge.log", "w", encoding="utf-8"),
                              stderr=subprocess.STDOUT)
        time.sleep(1.0)
        note("S0a тест-мост слушает :8797", listener_pid(PORT) == br.pid,
             f"pid={listener_pid(PORT)}")
        if not http_up():
            httpd = subprocess.Popen(["python3", str(REPO / "tests" / "serve.py")],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(20):
                if http_up():
                    break
                time.sleep(0.4)
        note("S0b vision.html на :8899", http_up())
        if not listener_pid(MAR_PORT):
            subprocess.Popen(
                [str(HOME / ".local/firefox-158/firefox"), "--marionette",
                 "--remote-debugging-port=9393", "--remote-allow-system-access",
                 "--profile", str(HOME / ".firefox-test-profile"), "--no-remote"],
                env={**ENV, "MOZ_DISABLE_CONTENT_SANDBOX": "1"},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t1 = time.time()
        while time.time() - t1 < 40 and not listener_pid(MAR_PORT):
            time.sleep(1.0)
        m = Marionette("127.0.0.1", MAR_PORT)
        m.script(SPARE_TAB_JS)
        # самодиагностика стенда: JS-ошибы background ловятся в консоль браузера молча —
        # вешаем слушателя ДО установки и печатаем в детали S0e при промахе коннекта
        m.script("window.__sk47errs = []; const l = {observe: (msg) => { try {"
                 " window.__sk47errs.push(String(msg.message).slice(0,200)"
                 " + ' @' + String(msg.filename || '').slice(-60) + ':' + msg.lineNumber); } catch (e) {} }};"
                 " Services.console.registerListener(l); window.__sk47l = l; return 'listener';")
        r0 = install(m, EXT_XPI)
        note("S0c тест-XPI (SK-47) установлен", bool(r0.get("ok")), str(r0)[:120])
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        jserr = m.script("return (window.__sk47errs || []).filter(s => s.includes('Error')).slice(0,3);") or []
        note("S0e расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод: было {prod_before})"
             + (f" jserr={jserr}" if jserr else ""))
        og = rpc("tabs.open_grouped", {"url": PAGE_URL + "?r=A", "task": "sk47-a", "title": "SK47"})
        st0 = state_ready()
        note("S0f vision.html открыта и привязана (state-канал жив)",
             og.get("ok") and "_err" not in st0, f"st={st0}")

        # ---------- V1: даунскейл, scale, capture, чистый путь ----------
        d = dom()
        vp = d.get("viewport") or {}
        vp_w = vp.get("w") or 0
        if not vp_w:
            # page.dom не отдал viewport (стенд ещё не прогрет) — деление защищено:
            # честный FAIL случится в проверках scale ниже, а не в ZeroDivisionError
            note("V1.0 viewport из page.dom", False, f"vp={vp} dom_keys={list(d)[:8]}")
            vp_w = 1
        sd = rpc("page.screenshot", {"task": "sk47-a"})  # дефолт = vision-путь 1280
        r1 = sd.get("result") or {}
        ok1 = sd.get("ok") and str(r1.get("dataUrl", "")).startswith("data:image/jpeg")
        w1, h1, f1 = (0, 0, "")
        if ok1:
            w1, h1, f1 = img_dims(dataurl_bytes(r1["dataUrl"]))
        cap = r1.get("capture") or {}
        note("V1a дефолтный скрин = JPEG ≤1280 (vision-дефолт)",
             ok1 and f1 == "jpeg" and w1 <= 1280, f"{w1}x{h1} {f1}")
        note("V1b scale ≈ outW/CSS-ширины и capture = исходный захват",
             abs(r1.get("scale", 0) - w1 / vp_w) < 0.02 * max(1, w1 / vp_w)
             and cap.get("w", 0) >= w1, f"scale={r1.get('scale')} outW={w1} vpW={vp_w} cap={cap}")
        sc = rpc("page.screenshot", {"task": "sk47-a", "max_width": 0})
        rc = sc.get("result") or {}
        w0, h0, f0 = img_dims(dataurl_bytes(rc.get("dataUrl", ""))) if sc.get("ok") else (0, 0, "?")
        note("V1c max_width:0 = чистый PNG без ресайза (capture == out)",
             sc.get("ok") and f0 == "png" and w0 == cap.get("w"), f"{w0}x{h0} {f0} cap={cap}")
        s8 = rpc("page.screenshot", {"task": "sk47-a", "max_width": 800})
        r8 = s8.get("result") or {}
        w8, h8, f8 = img_dims(dataurl_bytes(r8.get("dataUrl", ""))) if s8.get("ok") else (0, 0, "?")
        note("V1d max_width:800 — кадр ровно 800, scale пересчитан",
             w8 == 800 and abs(r8.get("scale", 0) - 800 / vp_w) < 0.03, f"{w8}x{h8} scale={r8.get('scale')}")
        # rect карты marks == rect page.dom (CSS px, не масштабированы)
        # (форматы разные: marks — [x,y,w,h], page.dom — {x,y,w,h}; нормализуем)
        def rect4(r):
            if isinstance(r, dict):
                return (r.get("x"), r.get("y"), r.get("w"), r.get("h"))
            return tuple(r) if isinstance(r, (list, tuple)) else None
        sm = rpc("page.screenshot", {"task": "sk47-a", "marks": True, "max_width": 1280})
        rm = sm.get("result") or {}
        marks = rm.get("marks") or []
        cmp_bad = []
        for mk in marks[:4]:
            e = entry_by(d, mk.get("selector"))
            r_a, r_b = rect4(mk.get("rect", [])), rect4((e or {}).get("rect"))
            if r_b is None:
                cmp_bad.append((mk.get("n"), "no-dom"))
                continue
            if any(abs(a - b) > 3 for a, b in zip(r_a, r_b)):
                cmp_bad.append((mk.get("n"), r_a, r_b))
        note("V1e rect marks остаются CSS-пикселями == page.dom (не пересчитаны в кадр)",
             sm.get("ok") and len(marks) >= 12 and not cmp_bad, f"marks={len(marks)} bad={cmp_bad[:3]}")
        (SHOTS / "v1-default.jpg").write_bytes(dataurl_bytes(r1["dataUrl"])) if ok1 else None

        # ---------- V2: SoM-матрица после даунскейла ----------
        exp = {mk["n"]: str(mk.get("selector", "")).lstrip("#") for mk in marks}
        bad2, hits2 = [], 0
        for mk in marks:
            n = mk["n"]
            r = rpc("action.do", {"mark": n, "action": "click", "task": "sk47-a", "ms": 200})
            res = r.get("result") or {}
            stt = state()
            got = (stt.get("clicks") or ["?"])[-1] if "_err" not in stt else "_state_err"
            hit_ok = (r.get("ok") and res.get("executed") and (res.get("hit") or {}).get("tag") == "button"
                      and got == exp[n])
            hits2 += 1 if hit_ok else 0
            if not hit_ok:
                bad2.append((n, exp[n], got, str(res.get("hit"))[:40], str(r.get("err"))[:30]))
        note(f"V2 SoM-матрица с max_width=1280: {hits2}/{len(marks)} mark-кликов попали",
             len(marks) >= 12 and hits2 == len(marks), bad2[:6])
        shot_screen("v2-after-marks")

        # ---------- V3: clip диалога ----------
        # rect #dlg берём из state-канала страницы (dlgRect): обычный div не попадает
        # в page.dom как интерактив, а fixed-геометия стабильна при скролле
        rect = rect4(state().get("dlgRect"))
        if not rect:
            note("V3 clip: dlgRect не найден в state", False, state())
        else:
            clip = {"x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3]}
            sc3 = rpc("page.screenshot", {"task": "sk47-a", "clip": clip, "max_width": 0})
            r3 = sc3.get("result") or {}
            raw3 = dataurl_bytes(r3.get("dataUrl", "")) if sc3.get("ok") else b""
            try:
                a3, (w3, h3) = img_rgb(raw3, SHOTS / "v3-clip.png") if raw3 else (None, (0, 0))
            except Exception as ex:  # noqa: BLE001
                a3, w3, h3 = None, 0, 0
                note("V3a decode clip-кропа", False, ex)
                a3 = None
            if a3 is not None:
                dpr3 = cap.get("w", 1) / vp_w if vp_w else 1
                red = ((a3[:, :, 0] > 200) & (a3[:, :, 1] < 80) & (a3[:, :, 2] < 80)).sum()
                green = ((a3[:, :, 1] > 120) & (a3[:, :, 0] < 120)).sum()
                cx3, cy3 = w3 // 2, h3 // 2
                center = a3[cy3, cx3]
                note("V3a clip PNG = размер региона × DPR, echo clip в ответе",
                     abs(w3 - rect[2] * dpr3) <= 3 and abs(h3 - rect[3] * dpr3) <= 3
                     and r3.get("clip") == clip, f"{w3}x{h3} clip={r3.get('clip')}")
                note("V3b в кропе только диалог: ни одного пикселя красного фона, центр зелёный",
                     red == 0 and green > 0.3 * w3 * h3 and center[1] > 120 and center[0] < 120,
                     f"red={red} green={green} center={list(center)}")
                (SHOTS / "v3-clip.png").write_bytes(raw3)
            sc3j = rpc("page.screenshot", {"task": "sk47-a", "clip": clip, "max_width": 1280})
            r3j = sc3j.get("result") or {}
            w3j = h3j = 0
            f3j = ""
            try:
                w3j, h3j, f3j = img_dims(dataurl_bytes(r3j.get("dataUrl", "")))
            except Exception:  # noqa: BLE001
                pass
            note("V3c clip JPEG ≤1280 с честным scale (outW ≈ clip.w×scale)",
                 f3j == "jpeg" and w3j <= 1280 and abs(w3j - clip["w"] * r3j.get("scale", 0)) <= 3,
                 f"{w3j}x{h3j} scale={r3j.get('scale')}")
            # плохой clip отсекается мостом локально
            sbad = rpc("page.screenshot", {"task": "sk47-a", "clip": {"x": 0, "y": 0, "w": 0, "h": 5}})
            note("V3d мост: clip с w=0 -> bad_request локально",
                 sbad.get("ok") is False and sbad.get("err") == "bad_request", sbad.get("err"))

        # ---------- V4: кэш кадра ----------
        c1 = rpc("page.screenshot", {"task": "sk47-a", "max_width": 1280})
        c2 = rpc("page.screenshot", {"task": "sk47-a", "max_width": 1280})
        r_c1, r_c2 = c1.get("result") or {}, c2.get("result") or {}
        note("V4a повтор ≤2 с — hit:cache и тот же кадр",
             "hit" not in r_c1 and r_c2.get("hit") == "cache"
             and r_c2.get("dataUrl") == r_c1.get("dataUrl"),
             f"hit1={r_c1.get('hit')} hit2={r_c2.get('hit')}")
        rpc("action.do", {"action": "scroll", "dy": 300, "task": "sk47-a"})
        c3 = rpc("page.screenshot", {"task": "sk47-a", "max_width": 1280})
        r_c3 = c3.get("result") or {}
        c4 = rpc("page.screenshot", {"task": "sk47-a", "max_width": 1280})
        r_c4 = c4.get("result") or {}
        note("V4b scroll сбрасывает кэш (свежий кадр), затем refill-хит",
             "hit" not in r_c3 and r_c4.get("hit") == "cache",
             f"hit3={r_c3.get('hit')} hit4={r_c4.get('hit')}")
        rpc("action.do", {"action": "scroll", "dy": -300, "task": "sk47-a"})
        time.sleep(2.3)
        c5 = rpc("page.screenshot", {"task": "sk47-a", "max_width": 1280})
        note("V4c TTL 2 с: спустя 2.3 с — снова свежий кадр (не хит)",
             "hit" not in (c5.get("result") or {}), (c5.get("result") or {}).get("hit"))
        # разные параметры — разные записи кэша (не подмена кадра)
        d1_ = rpc("page.screenshot", {"task": "sk47-a", "max_width": 1280})
        d2_ = rpc("page.screenshot", {"task": "sk47-a", "max_width": 640})
        note("V4d max_width:640 сразу после 1280 — не кэшированный хит (разный ключ)",
             "hit" not in (d2_.get("result") or {}), (d2_.get("result") or {}).get("hit"))

        # ---------- V5: DPR2 живьём ----------
        m.script(DPR_JS, ["2"])
        dpr_raised = True
        m.script(RELOAD_URL_JS, ["vision.html"])
        time.sleep(3.0)
        d5 = dom()
        vp5 = (d5.get("viewport") or {}).get("w") or 0
        s5 = rpc("page.screenshot", {"task": "sk47-a", "marks": True, "max_width": 1280})
        r5 = s5.get("result") or {}
        cap5 = r5.get("capture") or {}
        dpr_meas = (cap5.get("w", 0) / vp5) if vp5 else 0
        scale_ok = abs(r5.get("scale", 0) - (1280 / vp5 if cap5.get("w", 0) > 1280 else dpr_meas)) < 0.06
        note("V5a DPR2: capture.w / vp.w ≈ 2, scale ≈ outW/vp.w",
             s5.get("ok") and 1.8 <= dpr_meas <= 2.2 and scale_ok,
             f"dpr={dpr_meas:.2f} cap={cap5} vp={vp5} scale={r5.get('scale')}")
        marks5 = r5.get("marks") or []
        bad5, hits5 = [], 0
        for mk in marks5[:6]:
            n = mk["n"]
            want = str(mk.get("selector", "")).lstrip("#")
            r = rpc("action.do", {"mark": n, "action": "click", "task": "sk47-a", "ms": 200})
            res = r.get("result") or {}
            stt = state()
            got = (stt.get("clicks") or ["?"])[-1] if "_err" not in stt else "_state_err"
            if r.get("ok") and res.get("executed") and got == want:
                hits5 += 1
            else:
                bad5.append((n, want, got, str(r.get("err"))[:30]))
        note(f"V5b DPR2: mark-клики попадают ({hits5}/{min(6, len(marks5))})",
             len(marks5) >= 6 and hits5 == min(6, len(marks5)), bad5[:4])
        (SHOTS / "v5-dpr2.jpg").write_bytes(dataurl_bytes(r5["dataUrl"])) if r5.get("dataUrl") else None
        shot_screen("v5-screen-dpr2")
        m.script(DPR_JS, ["-1"])
        dpr_raised = False
        m.script(RELOAD_URL_JS, ["vision.html"])
        time.sleep(3.0)
        note("V5c pref DPR восстановлён (-1), страница перезагружена",
             state().get("ready") == 1 or "ready" in json.dumps(state()))

        # ---------- V6: vision_analyze до/после ----------
        # «до» = кадр инцидента: PNG 2578×1898 (DPR2-эквивалент: стендовый полный кадр,
        # разтянутый lanczos до пиксель-в-пиксель аудитовского размера)
        sraw = rpc("page.screenshot", {"task": "sk47-a", "max_width": 0})
        raw_png = dataurl_bytes((sraw.get("result") or {}).get("dataUrl", ""))
        (SHOTS / "v6-raw.png").write_bytes(raw_png)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(SHOTS / "v6-raw.png"),
                        "-vf", "scale=2578:1898:flags=lanczos", str(SHOTS / "v6-before.png")],
                       check=True, capture_output=True, env=ENV)
        before_bytes = (SHOTS / "v6-before.png").read_bytes()
        before_url = "data:image/png;base64," + base64.b64encode(before_bytes).decode()
        safter = rpc("page.screenshot", {"task": "sk47-a"})  # дефолт 1280 — путь агента
        after_bytes = dataurl_bytes((safter.get("result") or {}).get("dataUrl", ""))
        (SHOTS / "v6-after.jpg").write_bytes(after_bytes)
        after_url = "data:image/jpeg;base64," + base64.b64encode(after_bytes).decode()
        wb, wa = len(before_bytes), len(after_bytes)
        print(f"    кадр ДО: {wb / 1024:.0f} KB PNG 2578×1898; ПОСЛЕ: {wa / 1024:.0f} KB JPEG "
              f"{img_dims(after_bytes)[0]}×{img_dims(after_bytes)[1]}", flush=True)
        tb, eb = vision_series("до", before_url)
        ta, ea = vision_series("после", after_url)
        mb, ma = sorted(tb)[2], sorted(ta)[2]
        # Критерий карточки: медиана <20 с (5 прогонов до/после) и 0 таймаутов.
        # На пустом сервере узкое место — фиксированный оверход инференса (энкодер
        # 1024 image-токена + генерация), поэтому медианы близки; выигрыш SK-47 —
        # payload (в ~5 раз меньше) и поведение под конкуренцией с агентным циклом
        # (аудит: 33–120 с). Релативная разница — в деталях и timing.json.
        note(f"V6a vision_analyze: медиана ДО {mb:.1f} с / ПОСЛЕ {ma:.1f} с, обе <20 с "
             f"(5 прогонов; payload {wb / 1024:.0f} KB → {wa / 1024:.0f} KB)",
             ma < 20 and mb < 20 and not ea and not eb,
             f"до={[round(x, 1) for x in tb]} после={[round(x, 1) for x in ta]} errs_до={eb} errs_после={ea}")
        note("V6b 0 таймаутов в серии «после» (аудит: был timeout 120 с)", not ea, ea)
        # тайминг самого rpc (3+3 медианы) — даунскейл не должен дорого стоить
        def rpc_ms(mwv):
            t0 = time.time()
            rpc("page.screenshot", {"task": "sk47-a", "max_width": mwv})
            return (time.time() - t0) * 1000
        r_raw = sorted(rpc_ms(0) for _ in range(3))[1]
        r_dw = sorted(rpc_ms(1280) for _ in range(3))[1]
        note("V6c rpc page.screenshot: даунскейл не дороже чистого кадра +1.5 с",
             r_dw <= r_raw + 1500, f"raw={r_raw:.0f}ms down={r_dw:.0f}ms")
        (S / "timing.json").write_text(json.dumps({
            "before_png_bytes": wb, "after_jpeg_bytes": wa,
            "before_s": tb, "after_s": ta, "median_before": mb, "median_after": ma,
            "errs_before": eb, "errs_after": ea,
            "rpc_raw_ms": r_raw, "rpc_down_ms": r_dw}, ensure_ascii=False, indent=1))
        shot_screen("z-final")
    except Exception as ex:  # noqa: BLE001
        note("EXCEPTION", False, f"{type(ex).__name__}: {ex}")
        raise
    finally:
        # ---------- Z: уборка ----------
        try:
            if m:
                if dpr_raised:
                    m.script(DPR_JS, ["-1"])
                m.script(CLOSE_TAB_JS, args=["vision.html"])
                install(m, PROD_XPI)  # прод-аддон обратно (стенд остаётся живым на проде-коде)
        except Exception as e:  # noqa: BLE001
            print("Z-предупреждение:", e, file=sys.stderr)
        if br:
            br.terminate()
        if httpd:
            httpd.terminate()
        fails = [k for k, v in results.items() if v["ok"] is False]
        print(f"\nИТОГ: {sum(1 for v in results.values() if v['ok'])} PASS / {len(fails)} FAIL "
              f"из {len(results)}", flush=True)
        (S / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
        sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
