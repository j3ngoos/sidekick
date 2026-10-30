"""SK-44 контракты: ложный tab_moved на SPA — pushState-свидетель и динамический grace.

Чистая функция свидетеля witnessNav под node: окно агента, цепочка позже базовых 4 с
внутри динамического окна (инцидент B), потолок NAV_GRACE_MAX_MS, ре-арм новым exec,
tab_returned, идемпотентный двойной выстрел (onUpdated + webNavigation на одной
навигации), уход человека вне окон. Статика: слушатели webNavigation
(onHistoryStateUpdated + onLocationChange) с фильтром главного кадра, пермиссия
webNavigation в манифесте, делегат tabs.onUpdated в общий диспетчер, navOpenedAt в
seedTrack/noteNavGrace. tabGate и мост не изменены (tab_moved формируется на стороне
расширения; мост ретранслирует как есть).
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BG = (ROOT / "extension/background.js").read_text(encoding="utf-8")
MAN = json.loads((ROOT / "extension/manifest.json").read_text(encoding="utf-8"))

# ------------------------------------------------ witnessNav: чистая машина под node ---

NODE_WITNESS = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[1], "utf8"));
const G = 4000, M = 15000, t0 = 1000000;
const out = {};
const tr = (o) => Object.assign({ url: "http://a/", moved: false, graceUntil: 0, navOpenedAt: 0 }, o);

// нет трека / пустой url — no-op (тот же объект)
out.no_track = witnessNav(null, "http://a/x", t0, G, M) === null;
const keep = tr({ graceUntil: t0 + G, navOpenedAt: t0 });
out.empty_url = witnessNav(keep, "", t0, G, M) === keep;

// человек ушёл ВНЕ окон: moved, эталон не перетёрт (S3b)
const human = witnessNav(tr({}), "http://evil/", t0 + 1000, G, M);
out.human_moved = human.moved === true && human.url === "http://a/";

// агент внутри базового grace: эталон следует, окно продлено от навигации
const g1 = witnessNav(tr({ graceUntil: t0 + G, navOpenedAt: t0 }), "http://a/shorts", t0 + 500, G, M);
out.agent_grace = g1.url === "http://a/shorts" && g1.moved === false && g1.graceUntil === t0 + 500 + G;

// ИНЦИДЕНТ B: вторая навигация SPA-цепочки через 6 с — базовый grace истёк, но
// exec:True был (navOpenedAt=t0) и потолок 15 с не достигнут -> агент, не человек
const g2 = witnessNav(g1, "http://a/shorts/V1", t0 + 6000, G, M);
out.chain_after_grace = g2.url === "http://a/shorts/V1" && g2.moved === false && g2.graceUntil === t0 + 6000 + G;

// задержка полной навигации (YouTube-класс: onUpdated/onLocationChange пришёл на 5-й
// секунде) — тоже окно агента
const g3 = witnessNav(tr({ graceUntil: t0 + G, navOpenedAt: t0 }), "http://a/late", t0 + 5000, G, M);
out.late_full_nav = g3.url === "http://a/late" && g3.moved === false;

// потолок: цепочка старше NAV_GRACE_MAX_MS от последнего exec-глагола -> честность
const g4 = witnessNav(tr({ graceUntil: t0 + G, navOpenedAt: t0 }), "http://a/beyond", t0 + 16000, G, M);
out.cap_honest = g4.moved === true && g4.url === "http://a/";

// новый exec-глагол (noteNavGrace) ре-армит окно: та же точка времени, но navOpenedAt свежий
const g5 = witnessNav(tr({ graceUntil: t0 + G, navOpenedAt: t0 + 15500 }), "http://a/fresh", t0 + 16000, G, M);
out.rearm = g5.url === "http://a/fresh" && g5.moved === false;

// tab_returned: возврат на эталон снимает moved, эталон цел
const back = witnessNav(tr({ moved: true }), "http://a/", t0 + 9000, G, M);
out.tab_returned = back.moved === false && back.url === "http://a/";

// двойной выстрел (webNavigation + tabs.onUpdated на одной навигации) идемпотентен:
// второй выстрел тем же URL вне окон не трогает ни moved, ни эталон
const d1 = witnessNav(tr({}), "http://evil/", t0 + 1000, G, M);
const d2 = witnessNav(d1, "http://evil/", t0 + 1200, G, M);
out.double_fire = d1.moved === true && d2.moved === true && d2.url === "http://a/";

// no-change: url === эталон без moved -> тот же объект (не плодим записи)
const same = tr({});
out.same_identity = witnessNav(same, "http://a/", t0, G, M) === same;

console.log(JSON.stringify(out));
"""


def test_witness_nav_state_machine_node(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    m = re.search(r"(function witnessNav\(tr, url, now, graceMs, maxMs\) \{.*?\n\})", BG, re.S)
    assert m, "чистая функция witnessNav исчезла из background.js"
    block = tmp_path / "sk44_witness.js"
    block.write_text(m.group(1), encoding="utf-8")
    # чистота: ни TAB_TRACK, ни Date.now — вся время-зависимость входит параметром now
    assert "TAB_TRACK" not in m.group(1) and "Date.now" not in m.group(1)
    r = subprocess.run([node, "-e", NODE_WITNESS, str(block)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node: {r.stderr}"
    got = json.loads(r.stdout)
    assert got == {k: True for k in got}, f"провалившиеся сценарии: {[k for k, v in got.items() if not v]}"


# ------------------------------------------------------------------ статика ---

def test_web_navigation_witness_wired():
    # спека SK-44 п.1: оба события webNavigation кормят свидетеля...
    assert "browser.webNavigation.onHistoryStateUpdated.addListener" in BG
    assert "browser.webNavigation.onLocationChange.addListener" in BG
    # ...только главный кадр (iframe-роутер не двигает эталон вкладки) и http(s)
    assert "d.frameId === 0" in BG and "d.tabId >= 0" in BG
    assert re.search(r'/\^https\?:/.test\(d\.url', BG)
    # делегат tabs.onUpdated в общий диспетчер — одна логика на оба глаза
    assert re.search(r"if \(info\.url\) \{ witnessTabUrl\(id, info\.url\); refreshTabTitle\(id\); \}", BG)
    # пермиссия (пин набора — в test_sidebar_open_sk19)
    assert "webNavigation" in MAN["permissions"]


def test_dynamic_grace_state():
    # спека SK-44 п.2: grace динамический — окно открывается exec-глаголом (navOpenedAt)
    # и живёт до потолка NAV_GRACE_MAX_MS, а не фиксированные 4 с
    assert "NAV_GRACE_MAX_MS" in BG
    assert re.search(r"tr\.navOpenedAt = now", BG)          # noteNavGrace: каждый exec:True ре-армит
    assert "navOpenedAt: now" in BG                          # seedTrack: use_tab — свидетель из спеки
    # ответ с exec:True несёт свежий url — noteUrl якорит эталон (спека: «свежее свидетельство»)
    assert re.search(r"if \(res && res\.done\) \{\s*noteUrl\(tab\.id, res\.url\);\s*if \(NAV_ACTIONS\.includes\(params\.action\)\) noteNavGrace\(tab\.id\);", BG)


def test_tab_gate_untouched():
    # SK-25 регресс-страховка: gate-машину (fresh/grace/ok/moved) SK-44 не трогал
    assert re.search(r'function tabGate\(track, now\) \{\s*if \(!track\) return "fresh";\s*if \(track\.moved\) return "moved";', BG)


def test_agents_md_spa_clause():
    # контракт агента: SPA-навигации собственного клика — агентские, tab_moved = только человек
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "SK-44" in agents and "pushState" in agents
