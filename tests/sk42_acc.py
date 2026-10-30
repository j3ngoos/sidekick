#!/usr/bin/env python3
"""SK-42 живая приёмка: примитивы моста — ввод в contenteditable, scroll,
координатный ввод, хинты ошибок (карточка t_5f6f7edc, аудит 22.10 инциденты A/B).

Стенд: изолированный мост :8797 + тест-копия расширения (FALLBACK_SERVER->:8797)
в firefox-158 (Marionette 2929). Прод :8791 не трогаем; в конце — переустановка
prod-аддона. Прецеденты: sk38_acc.py, sk30_acc.py.

Фазы:
  S0 стенд: мост, XPI, расширение на :8797, http://127.0.0.1:8899/primitives.html
  A  ввод: contenteditable-хост/span, фокус, координаты, алиас text, controlled
     (слушатель переписывает), native-регресс, sid, plaintext-only, FALSE-остров,
     Enter->отправка (полный цикл композера), замена а не доклейка
  B  scroll: dy (page.dom viewport.sy>0), to bottom/top, sid внутр. фида,
     без превью-кадров (audit ev:preview delta=0)
  C  хинты: bad_request+пример, not_editable гейт (selector/точка), stale sid,
     value+text вместе
  D  регресс: SK-24 координатный клик+hit, SK-27 sid-клик
  E  реальные сайты: web.telegram.org (нативный инпут телефона + координаты +
     scroll/note), wikipedia (реальный скролл); композер Telegram — SKIP без
     сессии в тест-профиле (полный цикл доказан на реплике A1/A11)
  Z  уборка: вкладки, prod-аддон, :8791, тест-Firefox погашен по PID (SK-49)
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

HOME = pathlib.Path.home()
REPO = HOME / "sidekick"
S = pathlib.Path(os.environ.get("SK42_SCRATCH", str(HOME / ".hermes/cache/scratch/sk42")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
SHOTS = S / "shots"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
TEST_PORT = 8899
PAGE_URL = f"http://127.0.0.1:{TEST_PORT}/primitives.html"
results = {}

sys.path.insert(0, str(HOME / ".hermes/skills/hermes-browser-provider-setup/scripts"))
from mario import Marionette  # noqa: E402


def note(name, ok, detail=""):
    ok_v = None if ok == "skip" else bool(ok)
    results[name] = {"ok": ok_v, "detail": str(detail)[:400]}
    print(("PASS " if ok_v else ("SKIP " if ok_v is None else "FAIL ")) + name +
          ("  " + str(detail)[:180] if detail else ""), flush=True)


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


# ---- RPC через файловую шину моста (тот же контракт, что видит агент) --------

_rpc_off = 0


def rpc(method, params, timeout=30):
    global _rpc_off
    rid = str(uuid.uuid4())
    with open(HUB / "rpc_in.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"v": 1, "id": rid, "method": method, "params": params},
                           ensure_ascii=False) + "\n")
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            size = os.path.getsize(HUB / "rpc_out.jsonl")
            if size < _rpc_off:  # ротация
                _rpc_off = 0
            with open(HUB / "rpc_out.jsonl", encoding="utf-8") as f:
                f.seek(_rpc_off)
                data = f.read()
                _rpc_off = f.tell()
        except FileNotFoundError:
            time.sleep(0.2)
            continue
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("id") == rid:
                return d
        time.sleep(0.12)
    return {"ok": False, "err": "harness_timeout", "id": rid}


def state(timeout=30):
    """документированное состояние страницы: title = sk42:{json} (page.text)."""
    r = rpc("page.text", {"maxChars": 10}, timeout=timeout)
    t = str((r.get("result") or {}).get("title") or "")
    if not t.startswith("sk42:"):
        return {"_err": t[:120]}
    try:
        return json.loads(t[5:])
    except ValueError:
        return {"_err": "bad-title:" + t[:120]}


def dom(timeout=30):
    return rpc("page.dom", {}, timeout=timeout).get("result") or {}


def entry_by(dom_res, needle):
    for e in (dom_res.get("elements") or []) + ((dom_res.get("more") or {}).get("elements") or []):
        if needle in str(e.get("selector") or "") or needle in str(e.get("name") or ""):
            return e
    return None


def center(e):
    r = e["rect"]
    return int(r["x"] + r["w"] / 2), int(r["y"] + r["h"] / 2)


# ---- Marionette-блоки (стенд: вкладки, установка XPI) -------------------------

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


def install(m, xpi):
    r = m.ascript(ADDON_INSTALL_JS, args=[str(pathlib.Path(xpi).resolve())])
    if not (isinstance(r, dict) and r.get("ok")):
        raise RuntimeError(f"installTemporaryAddon: {r}")
    return r


def http_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/primitives.html", timeout=2)
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
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(20):
                if http_up():
                    break
                time.sleep(0.4)
        note("S0b тестовые страницы на :8899", http_up())
        t1 = time.time()
        while time.time() - t1 < 40 and not listener_pid(MAR_PORT):
            time.sleep(1.0)
        m = Marionette("127.0.0.1", MAR_PORT)
        m.script(SPARE_TAB_JS)
        r0 = install(m, EXT_XPI)
        note("S0c тест-XPI установлен", bool(r0.get("ok")), r0.get("url", ""))
        m.script(OPEN_TAB_JS, [PAGE_URL])
        token = (HUB / "token").read_text().strip()
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        if ss_count(PORT) == 0:
            # запасной круг: открыть options.html (токен в тест-хабе = копия прода,
            # storage профиля уже содержит его — переустановка XPI его сохраняет)
            url = (r0.get("url") or "").rsplit("sidebar.html", 1)[0]
            if url:
                m.script(OPEN_TAB_JS, [url + "options.html"])
                time.sleep(1.5)
                t1 = time.time()
                while time.time() - t1 < 25 and ss_count(PORT) == 0:
                    time.sleep(0.5)
                m.script(CLOSE_TAB_JS, args=["moz-extension"])
        note("S0e расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод: было {prod_before})")
        st0 = state(timeout=25)
        note("S0f primitives.html загружена (state-канал жив)", "_err" not in st0, st0)
        shot("s0-page")

        # ---------- A: ввод ----------
        r = rpc("action.do", {"action": "input", "selector": "#composer", "value": "test"})
        st = state()
        note("A1 input в contenteditable-хост (селектор хоста)",
             r.get("ok") and (r.get("result") or {}).get("executed") and st.get("comp") == "test"
             and st.get("mir") == "test", f"{r.get('result') or r.get('err')} comp={st.get('comp')}")
        note("A1b beforeinput прошёл (inputType вставки)", str(st.get("bi", "")).startswith("insert"),
             f"bi={st.get('bi')}")
        shot("a1-composer")

        r = rpc("action.do", {"action": "input", "selector": "#inner2", "value": "span-in"})
        st = state()
        note("A2 input из span внутри хоста (резолвер вверх, замена)",
             st.get("ce2") == "span-in", f"ce2={st.get('ce2')} {r.get('result') or r.get('err')}")

        # фокус даёт предыдущий input по селектору (sk42DoInput вызывает el.focus();
        # синтетический клик фокус не ставит — documented synthetic-граница)
        rpc("action.do", {"action": "input", "selector": "#composer", "value": "x"})
        r = rpc("action.do", {"action": "input", "value": "focus-mode"})
        st = state()
        note("A3 input без цели — в фокус (как key)",
             st.get("comp") == "focus-mode", f"comp={st.get('comp')} {r.get('result') or r.get('err')}")

        d = dom()
        e = entry_by(d, "#composer")
        if e:
            x, y = center(e)
            r = rpc("action.do", {"action": "input", "x": x, "y": y, "value": "coord-in"})
            st = state()
            note("A4 координатный ввод в contenteditable (x,y,value)",
                 st.get("comp") == "coord-in", f"comp={st.get('comp')} {r.get('result') or r.get('err')}")
        else:
            note("A4 координатный ввод в contenteditable (x,y,value)", False, "нет #composer в page.dom")

        r = rpc("action.do", {"action": "input", "selector": "#composer", "text": "alias-ok"})
        st = state()
        note("A5 алиас text вместо value", st.get("comp") == "alias-ok",
             f"comp={st.get('comp')} {r.get('result') or r.get('err')}")

        r = rpc("action.do", {"action": "input", "selector": "#controlled", "value": "test"})
        st = state()
        note("A6 controlled-слушатель (framework-переписывание) — ввёл test, стало TEST",
             st.get("ctl") == "TEST" and int(st.get("ctlRewrites") or 0) >= 1,
             f"ctl={st.get('ctl')} rewrites={st.get('ctlRewrites')}")

        r = rpc("action.do", {"action": "input", "selector": "#native", "value": "test"})
        st = state()
        note("A7 регресс: нативный input", st.get("nat") == "test",
             f"nat={st.get('nat')} {r.get('result') or r.get('err')}")

        d = dom()
        e = entry_by(d, "#composer")
        if e and e.get("sid"):
            r = rpc("action.do", {"action": "input", "sid": e["sid"], "value": "sid-in"})
            st = state()
            note("A8 input по sid из page.dom", st.get("comp") == "sid-in",
                 f"comp={st.get('comp')} {r.get('result') or r.get('err')}")
        else:
            note("A8 input по sid из page.dom", False, "sid композера не найден")

        r = rpc("action.do", {"action": "input", "selector": "#ce-plaintext", "value": "pt"})
        st = state()
        note("A9 plaintext-only принимает ввод", st.get("cePlaintext") == "pt",
             f"pt={st.get('cePlaintext')} {r.get('result') or r.get('err')}")

        r = rpc("action.do", {"action": "input", "selector": "#ce-false", "value": "x"})
        res = r.get("result") or {}
        st = state()
        note("A10 FALSE-остров внутри CE — отказ not_editable, ввод не прошёл",
             res.get("reason") == "not_editable" and res.get("executed") is False
             and int(st.get("ceFalseTyped") or 0) == 0, f"{res}")

        rpc("action.do", {"action": "input", "selector": "#composer", "value": "test"})
        r = rpc("action.do", {"action": "key", "key": "Enter"})
        st = state()
        note("A11 Enter в композере = отправка (полный цикл Telegram)",
             st.get("sent") == "test", f"sent={st.get('sent')} {r.get('result') or r.get('err')}")
        shot("a11-sent")

        rpc("action.do", {"action": "input", "selector": "#composer", "value": "first"})
        rpc("action.do", {"action": "input", "selector": "#composer", "value": "second"})
        st = state()
        note("A12 повторный ввод — ЗАМЕНА, не доклейка", st.get("comp") == "second",
             f"comp={st.get('comp')}")

        # ---------- B: scroll ----------
        rpc("action.do", {"action": "scroll", "to": "top"})
        pv_before = 0
        if (HUB / "audit.jsonl").exists():
            for ln in (HUB / "audit.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(ln).get("ev") == "preview":
                        pv_before += 1
                except ValueError:
                    pass
        r = rpc("action.do", {"action": "scroll", "dy": 800})
        res = r.get("result") or {}
        st = state()
        d = dom()
        sy = (d.get("viewport") or {}).get("sy")
        note("B1 scroll dy:800 — page.dom viewport.sy>0 (приёмка карточки)",
             r.get("ok") and int(sy or 0) > 0 and int(st.get("winY") or 0) > 0,
             f"sy={sy} winY={st.get('winY')} scrolled={res.get('scrolled')}")
        shot("b1-scrolled")

        r = rpc("action.do", {"action": "scroll", "to": "bottom"})
        st = state()
        note("B2a scroll to:bottom — низ страницы", int(st.get("winY") or 0) > 3000,
             f"winY={st.get('winY')} {res.get('scrolled') or r.get('err')}")
        shot("b2-bottom")
        r = rpc("action.do", {"action": "scroll", "to": "top"})
        st = state()
        note("B2b scroll to:top — ноль", int(st.get("winY") or 0) == 0, f"winY={st.get('winY')}")

        d = dom()
        e = entry_by(d, "элемент ленты 60")
        if e and e.get("sid"):
            r = rpc("action.do", {"action": "scroll", "sid": e["sid"]})
            st = state()
            note("B3 scroll по sid — прокручен внутренний контейнер (фид)",
                 int(st.get("feedTop") or 0) > 0,
                 f"feedTop={st.get('feedTop')} winY={st.get('winY')} (окно тоже — семантика "
                 f"scrollIntoView) {r.get('result') or r.get('err')}")
        else:
            note("B3 scroll по sid — прокручен внутренний контейнер (фид)", False,
                 "sid #deep не найден в page.dom")

        for _ in range(3):
            rpc("action.do", {"action": "scroll", "dy": 100})
        pv_after = 0
        if (HUB / "audit.jsonl").exists():
            for ln in (HUB / "audit.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(ln).get("ev") == "preview":
                        pv_after += 1
                except ValueError:
                    pass
        blog = (HUB / "bridge.log").read_text(encoding="utf-8", errors="replace")
        note("B4 scroll — единственный глагол без превью-кадров (audit delta=0)",
             pv_after == pv_before and "gate-free, no preview" in blog,
             f"preview-кадров: {pv_before}->{pv_after}")

        # ---------- C: хинты ----------
        r = rpc("action.do", {"action": "scroll"})
        note("C1 bad_request со ссылкой на пример (scroll без режима)",
             (not r.get("ok")) and r.get("err") == "bad_request" and "dy" in str(r.get("hint"))
             and "пример" in str(r.get("hint")), f"hint={str(r.get('hint'))[:120]}")

        r = rpc("action.do", {"action": "input", "selector": "#banner", "value": "x"})
        res = r.get("result") or {}
        note("C2 not_editable гейт (selector) — отказ ДО кадра + hint",
             res.get("reason") == "not_editable" and res.get("executed") is False
             and "page.dom" in str(res.get("hint")), f"{str(res)[:200]}")

        d = dom()
        e = entry_by(d, "#btn")
        if e:
            x, y = center(e)
            r = rpc("action.do", {"action": "input", "x": x, "y": y, "value": "x"})
            res = r.get("result") or {}
            note("C3 not_editable гейт (точка) — отказ + hint",
                 res.get("reason") == "not_editable" and bool(res.get("hint")), f"{str(res)[:200]}")
        else:
            note("C3 not_editable гейт (точка) — отказ + hint", False, "нет #btn в page.dom")

        r = rpc("action.do", {"action": "input", "selector": "#composer"})
        note("C4 input без value — bad_request + пример формы",
             (not r.get("ok")) and "пример" in str(r.get("hint")), f"hint={str(r.get('hint'))[:120]}")

        r = rpc("action.do", {"action": "input", "sid": "deadbeef", "value": "x"})
        res = r.get("result") or {}
        hint = str(res.get("hint") or r.get("hint") or "")
        note("C5 мёртвый sid — подсказка «пересними page.dom»",
             "page.dom" in hint and (res.get("reason") == "sid_stale" or r.get("err") == "sid_stale"),
             f"err={r.get('err')} reason={res.get('reason')} hint={hint[:120]}")

        r = rpc("action.do", {"action": "input", "selector": "#composer", "value": "a", "text": "b"})
        note("C6 value+text вместе — bad_request",
             (not r.get("ok")) and r.get("err") == "bad_request", f"{r.get('err')}")

        # ---------- D: регресс SK-24 / SK-27 ----------
        rpc("action.do", {"action": "scroll", "to": "top"})  # rect — вьюпортные координаты
        d = dom()
        e = entry_by(d, "#btn")
        if e:
            x, y = center(e)
            n0 = int(state().get("clkN") or 0)
            r = rpc("action.do", {"action": "click", "x": x, "y": y})
            st = state()
            note("D1 регресс SK-24: координатный клик + hit",
                 int(st.get("clkN") or 0) == n0 + 1 and "btn" in str(st.get("clkHit")),
                 f"clkHit={st.get('clkHit')} {r.get('result') or r.get('err')}")
        else:
            note("D1 регресс SK-24: координатный клик + hit", False, "нет #btn")

        d = dom()
        e = entry_by(d, "#btn")
        if e and e.get("sid"):
            n0 = int(state().get("clkN") or 0)
            r = rpc("action.do", {"action": "click", "sid": e["sid"]})
            st = state()
            note("D2 регресс SK-27: sid-клик", int(st.get("clkN") or 0) == n0 + 1,
                 f"clkN={st.get('clkN')} {r.get('result') or r.get('err')}")
        else:
            note("D2 регресс SK-27: sid-клик", False, "sid #btn не найден")

        # ---------- E: реальные сайты ----------
        m.script(OPEN_TAB_JS, ["https://web.telegram.org/k/"])
        t1 = time.time()
        phone = None
        d = {}
        while time.time() - t1 < 30:
            d = dom(timeout=20)
            if "telegram" in str(d.get("url")):
                els = d.get("elements") or []
                phone = next((e for e in els if e.get("role") == "textbox"), None)
                if phone:
                    break
                # страница QR — жмём переключатель входа по телефону, поле появится
                tog = next((e for e in els if e.get("role") == "button" and
                            re.search(r"phone|телефон", str(e.get("name")), re.I)), None)
                if tog:
                    rpc("action.do", {"action": "click", "selector": tog["selector"]})
                    time.sleep(2.0)
            time.sleep(1.2)
        note("E1a web.telegram.org открылся (реальный сайт)", bool(d.get("url")),
             str(d.get("url"))[:100])
        if phone:
            r = rpc("action.do", {"action": "input", "selector": phone["selector"],
                                  "value": "79990000000"})
            res = r.get("result") or {}
            note("E1b ввод в нативное поле телефона Telegram Web (без отправки)",
                 res.get("executed") is True, f"{str(res)[:160]}")
            shot("e1-telegram-phone")
            d2 = dom()  # Telegram перерисовывает вёрстку после ввода — свежий rect
            ph2 = next((e2 for e2 in d2.get("elements") or [] if e2.get("role") == "textbox"), None)
            if ph2:
                x, y = center(ph2)
                r = rpc("action.do", {"action": "input", "x": x, "y": y, "value": "79990001111"})
                res = r.get("result") or {}
                note("E1c координатный ввод на реальном сайте", res.get("executed") is True,
                     f"{str(res)[:160]}")
            else:
                note("E1c координатный ввод на реальном сайте", "skip", "после рендера поля нет")
        else:
            note("E1b ввод в нативное поле телефона Telegram Web (без отправки)", False,
                 "поле телефона не найдено на странице логина")
            note("E1c координатный ввод на реальном сайте", "skip", "нет цели")
        r = rpc("action.do", {"action": "scroll", "dy": 800})
        res = r.get("result") or {}
        note("E1d scroll на короткой странице — note вместо ложного успеха",
             r.get("ok") and (res.get("note") or (res.get("scrolled") or {}).get("dy")),
             f"{str(res)[:160]}")
        note("E3 композер Telegram Web (реальная отправка) — сессии в тест-профиле нет",
             "skip", "полный цикл композера доказан на реплике (A1/A2/A11); для живой "
             "отправки нужен ОДИН логин в telegram на тест-профиле стенда")

        m.script(OPEN_TAB_JS, ["https://ru.wikipedia.org/wiki/%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F"])
        t1 = time.time()
        ok_wiki = False
        while time.time() - t1 < 25:
            d = dom(timeout=20)
            if "wikipedia" in str(d.get("url")):
                ok_wiki = True
                break
            time.sleep(1.2)
        if ok_wiki:
            rpc("action.do", {"action": "scroll", "to": "top"})
            r = rpc("action.do", {"action": "scroll", "dy": 1500})
            d = dom()
            sy = (d.get("viewport") or {}).get("sy")
            note("E2 реальный скролл (wikipedia) — sy>0", int(sy or 0) > 0,
                 f"sy={sy} {str(r.get('result'))[:120]}")
            shot("e2-wiki-scroll")
        else:
            note("E2 реальный скролл (wikipedia) — sy>0", False, "wikipedia не загрузилась")

    finally:
        # ---------- Z: уборка ----------
        try:
            if m:
                m.script(CLOSE_TAB_JS, args=["primitives"])
                m.script(CLOSE_TAB_JS, args=["telegram"])
                m.script(CLOSE_TAB_JS, args=["wikipedia"])
                m.script(SPARE_TAB_JS)
        except Exception as e:
            print("WARN tabs:", e, flush=True)
        try:
            if m:
                rp = install(m, PROD_XPI)
                t2 = time.time()
                while time.time() - t2 < 20 and ss_count(8791) == 0:
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
        while time.time() - t1 < 30 and ss_count(8791) < 1:
            time.sleep(1.0)
        note("Z2 прод-аддон переустановлен, :8791 восстановлен",
             ss_count(8791) >= 1,
             f"@8791={ss_count(8791)} (было {prod_before})")
        # SK-49: тест-Firefox не должен оставаться вторым коннектом к проду :8791 —
        # мост теперь вытесняет первым пришедшим hello (close 4009), но браузеру
        # пользователя лучше вернуть маршрут сразу, без ожидания пинг-понга.
        # Убиваем ПО PID: слушатель порта Marionette — и есть процесс firefox;
        # pkill-паттерн («firefox-158/firefox --marionette») матчит и собственный
        # shell харнеса — SIGTERM себе.
        fpid = listener_pid(MAR_PORT)
        if fpid:
            subprocess.run(["kill", str(fpid)], capture_output=True)
            t1 = time.time()
            while time.time() - t1 < 15 and listener_pid(MAR_PORT):
                time.sleep(0.5)
            if listener_pid(MAR_PORT):
                subprocess.run(["kill", "-9", str(listener_pid(MAR_PORT))],
                               capture_output=True)
                time.sleep(1.0)
        note("Z3 тест-Firefox погашен (стенд не висит вторым коннектом)",
             not listener_pid(MAR_PORT), f"pid={listener_pid(MAR_PORT) or '—'}")
        skipped = [k for k, v in results.items() if v["ok"] is None]
        fails = [k for k, v in results.items() if v["ok"] is False]
        passed = [k for k, v in results.items() if v["ok"] is True]
        print(f"\nИТОГ: {len(passed)} PASS / {len(fails)} FAIL / {len(skipped)} SKIP "
              f"из {len(results)}", flush=True)
        for k in fails:
            print("FAIL:", k, "—", results[k]["detail"][:200], flush=True)
        for k in skipped:
            print("SKIP:", k, "—", results[k]["detail"][:200], flush=True)
        (S / "sk42_acc_results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
