#!/usr/bin/env python3
"""SK-44 живая приёмка: ложный tab_moved на SPA — pushState-свидетель и динамический grace
(карточка t_2ef65913, аудит №2 22.10 инцидент B / проблема 2).

Стенд: изолированный мост :8797 + тест-копия расширения (FALLBACK_SERVER->:8797)
в firefox-158 (Marionette 2929). Прод :8791 не трогаем; в конце — переустановка
prod-аддона. Прецеденты: sk43_acc.py, sk42_acc.py.

Фазы:
  S0 стенд: мост, XPI, расширение на :8797, http://127.0.0.1:8899/spa-tabs.html
  A SPA-стенд (YouTube-подобный роутер, pushState):
    A1 серия из 5 SPA-кликов с page.text после каждого -> 0 tab_moved
    A2 цепочка инцидента: клик «История» -> второй pushState роутера через 4.5 с ->
      page.text на 5.5-й секунде ok (старый фиксированный grace 4 с к тому мёртв)
    A4 iframe-роутер пушит каждые 1.5 с вечно, окно закрыто -> page.text ok (фильтр frameId)
    A3 класс инцидента с полной навигацией: клик «Поздняя-загрузка» -> location.href
      через 4.5 с -> page.text на 5.5-й секунде ok + sk44-late
  B реальный youtube.com: клик Shorts -> 5.5 с -> page.text ok (буква инцидента);
    вторая поверхность -> снова ok; за фазу ноль tab_moved
  C честность (S3a-c матрица, стенд, вне окна агента):
    человек (реальный клик XTEST по кнопке «Помощь», isTrusted — для свидетеля
    неотличим от живого пользователя) уводит URL -> tab_moved; «Корневая» возвращает
    ровно на эталон -> tab_returned без перепривязки; снова увод -> tab_moved ->
    tabs.use_tab перепривязка -> ok
  D регресс: tab_gone после закрытия; tabs.list; аудит без unknown_method
  Z уборка: вкладки, prod-аддон, :8791, тест-Firefox погашен по PID (SK-49)
"""
import json
import numpy
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
S = pathlib.Path(os.environ.get("SK44_SCRATCH", str(HOME / ".hermes/cache/scratch/sk44")))
HUB = S / "hub"
EXT_SRC, EXT_XPI, PROD_XPI = S / "ext", S / "ext.xpi", S / "prod.xpi"
SHOTS = S / "shots"
ENV = {**os.environ, "DISPLAY": ":10", "XAUTHORITY": str(HOME / ".Xauthority-game")}
MAR_PORT = 2929
PORT = 8797
TEST_PORT = 8899
SPA_URL = f"http://127.0.0.1:{TEST_PORT}/spa-tabs.html"
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

def rpc(method, params, timeout=40):
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


def is_moved(r):
    return str(r.get("err") or "").startswith("tab_moved")


def audit_all():
    p = HUB / "audit.jsonl"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def state(task, timeout=40):
    """документированное состояние SPA-стенда: title = sk44:{json} (page.text задачи)."""
    r = rpc("page.text", {"maxChars": 10, "task": task}, timeout=timeout)
    if not r.get("ok"):
        return {"_err": str(r.get("err"))[:160]}
    t = str((r.get("result") or {}).get("title") or "")
    if t.startswith("sk44-late"):
        return {"late": True, "title": t}
    if not t.startswith("sk44:"):
        return {"_err": t[:120]}
    try:
        return json.loads(t[5:])
    except ValueError:
        return {"_err": "bad-title:" + t[:120]}


def state_ready(task, tries=24):
    """state() с ожиданием коммита http-документа: сразу после open_grouped вкладка
    ещё мгновение живёт на about:blank, куда content.js не инджектится (internal_page)."""
    st = state(task)
    for _ in range(tries):
        if "_err" not in st or "internal_page" not in st["_err"]:
            return st
        time.sleep(0.5)
        st = state(task)
    return st


def dom(task):
    r = rpc("page.dom", {"task": task}, timeout=40)
    return r.get("result") or {}


def entry_by(dom_res, pred):
    for e in (dom_res.get("elements") or []) + ((dom_res.get("more") or {}).get("elements") or []):
        if pred(e):
            return e
    return None


# ---- Marionette-блоки (стенд: вкладки, установка XPI, «человек») --------------

SPARE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "win.gBrowser.addTrustedTab('about:blank'); return 'spare';})();")
CLOSE_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                "for (let i = win.gBrowser.tabs.length - 1; i >= 0; i--) {"
                " const u = win.gBrowser.tabs[i].linkedBrowser.currentURI.spec;"
                " if (u.includes(arguments[0]) && win.gBrowser.tabs.length > 1)"
                " win.gBrowser.removeTab(win.gBrowser.tabs[i]);}"
                " if (win.gBrowser.tabs.length === 0) win.gBrowser.addTrustedTab('about:blank');"
                "return 'closed';})();")
SELECT_TAB_JS = ("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                 "for (const t of win.gBrowser.tabs) {"
                 " if (t.linkedBrowser.currentURI.spec.includes(arguments[0])) {"
                 "  win.gBrowser.selectedTab = t; return 'selected:' + t.linkedBrowser.currentURI.spec; }}"
                 "return 'not-found';})();")
# id вкладки WebExtension != browserId элемента <browser> (замерено: tabId=125,
# browserId другой) — выделение/закрытие стендовых вкладок только по URL-подстроке;
# к фазе C на :8899 остаётся одна вкладка C (вкладку A харнесс закрывает после фазы A)
# «человек вручную меняет URL» — НАСТОЯЩИЙ клик (XTEST, isTrusted): по кнопке «Помощь»,
# роутер страницы уводит URL pushState-ом. Свидетель видит ровно то же, что при живом
# пользователе: webNavigation главного кадра без предшествующего агентского exec:True.
# rect кэшируются из page.dom ДО угона (после moved gate честно не пустит page.dom).
# Экранный origin вьюпорта — по магента-маркеру 8×8, влитому в саму страницу (0,0):
# mozInnerScreen* на этом билде врёт (innerH==outerH при видимом тулбаре), а промах
# клика = табы в полоску вкладок. Marionette-контент-контекст не годится: его frame
# selector не следует за gBrowser.selectedTab (видит about:blank своего сессинга).
def viewport_origin(shot_path):
    subprocess.run(["scrot", "-o", shot_path], env=ENV, capture_output=True)
    import struct
    head = pathlib.Path(shot_path).read_bytes()[:24]
    w, h = struct.unpack(">II", head[16:24])
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", shot_path,
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True).stdout
    a = numpy.frombuffer(raw, numpy.uint8)
    if a.size != w * h * 3:
        raise RuntimeError(f"decode mismatch {a.size} != {w * h * 3}")
    a = a.reshape(h, w, 3).astype(int)
    ys, xs = numpy.nonzero((a[:, :, 0] > 230) & (a[:, :, 1] < 60) & (a[:, :, 2] > 230))
    if len(xs) == 0:
        raise RuntimeError("магента-маркер страницы не виден на скрине")
    return int(xs.min()), int(ys.min())


def human_click(rect, origin):
    x = origin[0] + int(rect["x"] + rect["w"] / 2)
    y = origin[1] + int(rect["y"] + rect["h"] / 2)
    wid = subprocess.run(["xdotool", "search", "--class", "firefox"],
                         capture_output=True, text=True, env=ENV).stdout.split()
    if not wid:
        raise RuntimeError("окно firefox не найдено")
    subprocess.run(["xdotool", "windowactivate", "--sync", wid[0]], env=ENV, capture_output=True)
    subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y), "click", "1"],
                   env=ENV, capture_output=True)
    time.sleep(0.7)
    return f"({x},{y})"


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
    for pg in ("spa-tabs.html", "spa-late.html", "spa-frame.html"):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{TEST_PORT}/{pg}", timeout=2)
        except Exception:
            return False
    return True


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    if HUB.exists():
        shutil.rmtree(HUB)
    HUB.mkdir(parents=True)
    shutil.copy(HOME / "sidekick/bridge/hub/token", HUB / "token")
    prod_before = ss_count(8791)
    br, m, httpd = None, None, None
    crash = None
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
        note("S0b тестовые страницы на :8899 (spa-tabs/spa-late/spa-frame)", http_up())
        if not listener_pid(MAR_PORT):  # стенд погашен (напр. убит после приёмки, чтобы
            # тест-Firefox не сидел вторым коннектом на проде) — поднимаем firefox-158 сами
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
        r0 = install(m, EXT_XPI)
        note("S0c тест-XPI установлен", bool(r0.get("ok")), r0.get("url", ""))
        t1 = time.time()
        while time.time() - t1 < 25 and ss_count(PORT) == 0:
            time.sleep(0.5)
        if ss_count(PORT) == 0:
            url = (r0.get("url") or "").rsplit("sidebar.html", 1)[0]
            if url:
                m.script("return (() => { const win = Services.wm.getMostRecentWindow('navigator:browser');"
                         "win.gBrowser.addTrustedTab(arguments[0]); return 'opt'; })();", [url + "options.html"])
                time.sleep(1.5)
                t1 = time.time()
                while time.time() - t1 < 25 and ss_count(PORT) == 0:
                    time.sleep(0.5)
                m.script(CLOSE_TAB_JS, args=["moz-extension"])
        note("S0e расширение подключено к :8797", ss_count(PORT) >= 1,
             f"@8797={ss_count(PORT)} @8791={ss_count(8791)} (прод: было {prod_before})")

        # ---------- A: SPA-стенд ----------
        og = rpc("tabs.open_grouped", {"url": SPA_URL + "?r=A", "task": "sk44-a", "title": "SK44-A"})
        tab_a = (og.get("result") or {}).get("tabId")
        st0 = state_ready("sk44-a")
        note("S0f spa-tabs открыта и привязана (state-канал жив)",
             og.get("ok") and tab_a and "_err" not in st0, f"tab={tab_a} st={st0}")
        moved_a = 0

        # A1: серия из 5 SPA-кликов, после каждого — page.text; 0 tab_moved
        # (без «Истории» — её цепочка +4.5 с создала бы гонку в финальном стейте; A2)
        series = ["#tab-shorts", "#tab-home", "#tab-subs", "#tab-shorts", "#tab-home"]
        bad = []
        for sel in series:
            r = rpc("action.do", {"selector": sel, "action": "click", "task": "sk44-a", "ms": 250})
            if is_moved(r):
                moved_a += 1
            t = state("sk44-a")
            if is_moved(t):
                moved_a += 1
            if not (r.get("ok") and (r.get("result") or {}).get("executed") and "_err" not in t):
                bad.append((sel, str(r.get("err"))[:60], str(t)[:60]))
        stA1 = state("sk44-a")
        note("A1 серия 5 SPA-кликов: каждый исполнен, page.text ok", not bad, bad)
        note("A1a 0 tab_moved на серии из 5 SPA-кликов", moved_a == 0, f"moved={moved_a}")
        note("A1b роутер дошёл до / (эталон следует за pushState, клики видны)",
             stA1.get("path") == "/" and "/shorts" in (stA1.get("clicks") or ["", "", "", ""]),
             stA1)
        shot("s-a1-series")

        # A2: цепочка инцидента — второй pushState роутера через 4.5 с после клика,
        # page.text на 5.5-й секунде (старый фиксированный grace 4 с к тому мёртв)
        r = rpc("action.do", {"selector": "#tab-history", "action": "click", "task": "sk44-a", "ms": 250})
        time.sleep(5.5)
        st2 = state("sk44-a")
        note("A2 SPA-цепочка +4.5 с: page.text на 5.5 с ok, deep-маршрут виден",
             r.get("ok") and not is_moved(r) and "_err" not in st2
             and st2.get("deep") is True and st2.get("path") == "/history/watch",
             f"exec={(r.get('result') or {}).get('executed')} st={st2}")

        # A4: iframe-роутер пушит каждые 1.5 с; выдержать дольше окна (15 с) + grace.
        # Если фильтр frameId сломан — пуши утащат эталон на /frame-route-N и вне окна
        # дадут ложный tab_moved уже здесь.
        time.sleep(17)
        st4 = state("sk44-a")
        note("A4 iframe-роутер вне окна агента: эталон не двинут, page.text ok (фильтр frameId)",
             "_err" not in st4 and st4.get("path") == "/history/watch", st4)

        # A3: класс инцидента с ПОЛНОЙ навигацией позже старой четырёхсекундки
        r = rpc("action.do", {"selector": "#late-full", "action": "click", "task": "sk44-a", "ms": 250})
        time.sleep(5.5)
        st3 = state("sk44-a")
        note("A3 полная навигация +4.5 с: page.text на 5.5 с ok (был бы tab_moved), sk44-late",
             r.get("ok") and st3.get("late") is True, f"err={r.get('err')} st={st3}")
        shot("s-a3-late")
        # фаза A кончена: крышка вкладке A, чтобы на :8899 в фазах C/D жила только C
        # (выделение/закрытие стенда — по URL-подстроке, id расширения != browserId)
        m.script(CLOSE_TAB_JS, args=["127.0.0.1:8899"])

        # ---------- B: реальный youtube.com ----------
        oy = rpc("tabs.open_grouped", {"url": "https://www.youtube.com/", "task": "sk44-b",
                                       "title": "SK44-B", "focus": True})
        tab_b = (oy.get("result") or {}).get("tabId")
        time.sleep(6)
        moved_b = 0
        pt = rpc("page.text", {"maxChars": 200, "task": "sk44-b"})
        if is_moved(pt):
            moved_b += 1
        # consent-диалог (если встанет) — best-effort, не ассерт
        d = dom("sk44-b")
        consent = entry_by(d, lambda e: any(s in str(e.get("name") or "")
                                            for s in ("Принять все", "Reject all", "Accept all")))
        if consent:
            rpc("action.do", {"selector": consent["selector"], "action": "click",
                              "task": "sk44-b", "ms": 250})
            time.sleep(2.5)
            d = dom("sk44-b")
        e_shorts = entry_by(d, lambda e: "Shorts" in str(e.get("name") or "")
                            and e.get("selector"))
        note("B0 youtube.com открыт, page.text ok, вкладка Shorts видна",
             oy.get("ok") and tab_b and pt.get("ok") and bool(e_shorts),
             f"tab={tab_b} pt_ok={pt.get('ok')} shorts={'да' if e_shorts else 'нет'}")
        if e_shorts:
            r = rpc("action.do", {"selector": e_shorts["selector"], "action": "click",
                                  "task": "sk44-b", "ms": 300})
            if is_moved(r):
                moved_b += 1
            time.sleep(5.5)  # старый фиксированный grace к этому моменту мёртв
            pt2 = rpc("page.text", {"maxChars": 200, "task": "sk44-b"})
            if is_moved(pt2):
                moved_b += 1
            url2 = str((pt2.get("result") or {}).get("url") or "")
            note("B1-B2 клик Shorts -> 5.5 с -> page.text ok, url /shorts (инцидент 15:25:56)",
                 r.get("ok") and (r.get("result") or {}).get("executed")
                 and pt2.get("ok") and "/shorts" in url2,
                 f"exec={(r.get('result') or {}).get('executed')} pt2_ok={pt2.get('ok')} "
                 f"err={pt2.get('err')} url={url2[:80]}")
            shot("s-b2-shorts")
            # вторая поверхность: лента/подписки/главная — любая следующая SPA-навигация
            d2 = dom("sk44-b")
            e2 = entry_by(d2, lambda e: any(s in str(e.get("name") or "")
                                            for s in ("Подписки", "Explore", "Главная",
                                                      "Home", "For You"))
                          and e.get("selector"))
            if e2:
                r2 = rpc("action.do", {"selector": e2["selector"], "action": "click",
                                       "task": "sk44-b", "ms": 300})
                if is_moved(r2):
                    moved_b += 1
                time.sleep(5.5)
                pt3 = rpc("page.text", {"maxChars": 200, "task": "sk44-b"})
                if is_moved(pt3):
                    moved_b += 1
                note("B3 вторая поверхность («%s») -> 5.5 с -> page.text ok" %
                     str(e2.get("name"))[:20],
                     r2.get("ok") and pt3.get("ok"),
                     f"err={pt3.get('err')}")
            else:
                note("B3 вторая поверхность", "skip", "кандидат не найден в page.dom")
        else:
            note("B1-B2 клик Shorts", "skip", "Shorts не найден в page.dom")
        note("B4 за фазу B ни одного tab_moved", moved_b == 0, f"moved={moved_b}")

        # ---------- C: честность S3a-c (стенд, реальный клик человека XTEST) ----------
        oc = rpc("tabs.open_grouped", {"url": SPA_URL + "?r=C", "task": "sk44-c", "title": "SK44-C"})
        tab_c = (oc.get("result") or {}).get("tabId")
        sel = m.script(SELECT_TAB_JS, args=["127.0.0.1:8899"])  # на :8899 теперь только C
        stC = state_ready("sk44-c")
        note("C0 вторая вкладка стенда привязана", oc.get("ok") and tab_c and "_err" not in stC,
             f"tab={tab_c} sel={sel} st={stC}")
        time.sleep(16)  # динамическое окно после exec/seed закрывается (15 с + grace)
        base = state("sk44-c")
        ok_base = "_err" not in base
        def select_c():
            m.script(SELECT_TAB_JS, args=["127.0.0.1:8899"])  # URL уходит (help/эталон) — порт тот же

        select_c()
        time.sleep(0.5)  # кадр: выделение и отрисовка C до скрина калибровки
        origin = viewport_origin(str(SHOTS / "s-c-origin.png"))
        note("C2 origin вьюпорта откалибрирован маркером", origin[1] > 60, f"origin={origin}")
        # rect кэшируются ДО угона: после moved page.dom задачи честно отбивается
        # tab_moved (это и есть тестируемый gate), «человек» бьёт по снапшоту до перемещения.
        def button_rect(name):
            e = entry_by(dom("sk44-c"), lambda x: str(x.get("name") or "").strip() == name)
            if not e:
                raise RuntimeError(f"кнопка «{name}» не найдена в page.dom")
            return e["rect"]
        rect_help, rect_root = button_rect("Помощь"), button_rect("Корневая")

        # S3a: человек кликает «Помощь» (XTEST, isTrusted) — SPA-уход от эталона вне окна
        select_c()
        where = human_click(rect_help, origin)
        time.sleep(0.8)
        pt_moved = rpc("page.text", {"maxChars": 10, "task": "sk44-c"})
        note("C3 S3a: человек кликает «Помощь» (XTEST) -> tab_moved",
             ok_base and is_moved(pt_moved), f"at={where} err={pt_moved.get('err')}")
        shot("s-c3-hijack")
        # S3b: человек кликает «Корневая» — возврат ровно на эталонный URL -> tab_returned
        select_c()
        human_click(rect_root, origin)
        time.sleep(0.8)
        pt_back = rpc("page.text", {"maxChars": 10, "task": "sk44-c"})
        note("C4 S3b: возврат человека на эталон -> tab_returned, rpc ok без перепривязки",
             pt_back.get("ok"), f"err={pt_back.get('err')}")
        # S3c: снова увод -> tab_moved -> tabs.use_tab перепривязывает
        select_c()
        human_click(rect_help, origin)
        time.sleep(0.8)
        pt_m2 = rpc("page.text", {"maxChars": 10, "task": "sk44-c"})
        reb = rpc("tabs.use_tab", {"tabId": tab_c, "task": "sk44-c"})
        pt_reb = rpc("page.text", {"maxChars": 10, "task": "sk44-c"})
        note("C5 S3c: увод -> tab_moved; tabs.use_tab перепривязал -> rpc ok",
             is_moved(pt_m2) and reb.get("ok") and pt_reb.get("ok"),
             f"moved2={is_moved(pt_m2)} reb_ok={reb.get('ok')} after={pt_reb.get('err') or 'ok'}")
        # после перепривязки агент снова хозяин: свой SPA-клик проходит
        r = rpc("action.do", {"selector": "#tab-shorts", "action": "click", "task": "sk44-c", "ms": 250})
        stC2 = state("sk44-c")
        note("C6 после перепривязки агентский SPA-клик + page.text ok",
             r.get("ok") and "_err" not in stC2 and stC2.get("path") == "/shorts",
             f"err={r.get('err')} st={stC2}")

        # ---------- D: регресс ----------
        closed = m.script(CLOSE_TAB_JS, args=["127.0.0.1:8899"])
        time.sleep(1.0)
        gone = rpc("page.text", {"maxChars": 10, "task": "sk44-c"})
        note("D1 закрытая вкладка задачи -> tab_gone (S4a-класс)",
             str(gone.get("err") or "").startswith("tab_gone"), f"{closed} {gone.get('err')}")
        tl = rpc("tabs.list", {})
        note("D2 tabs.list ok (SK-32 не сломан)",
             tl.get("ok") and isinstance((tl.get("result") or {}).get("tabs"), list),
             f"tabs={len((tl.get('result') or {}).get('tabs') or [])}")
        aud = audit_all()
        unk = [a for a in aud if a.get("err") == "unknown_method"]
        note("D3 аудит: ни одного unknown_method", not unk, unk[:2])

    except BaseException as e:  # вскрываем аварию фазы: иначе finally с sys.exit её проглотит
        import traceback
        crash = traceback.format_exc()
        note("EXC фаза", False, crash[-600:])

    finally:
        # ---------- Z: уборка ----------
        try:
            if m:
                m.script(CLOSE_TAB_JS, args=["8899"])
                m.script(CLOSE_TAB_JS, args=["youtube.com"])
                m.script(SPARE_TAB_JS)
        except Exception as e:
            print("WARN tabs:", e, flush=True)
        try:
            if m:
                install(m, PROD_XPI)
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
              f"{'— ' + str(fails) if fails else ''}", flush=True)
        if crash:
            print("CRASH:\n" + crash, flush=True)
        (S / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        sys.exit(1 if (fails or crash) else 0)


if __name__ == "__main__":
    main()
