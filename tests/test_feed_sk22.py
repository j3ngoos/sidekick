"""SK-22 контрактные тесты — сворачиваемая панель «работа» + think-блок.

Рендер-логика живёт в extension/feed.js (паттерн skills.js/audit.js: чистый файл,
DOM передаётся параметром `doc`). Здесь две ноги:

1. node-контракты рендера: DOM-заглушка + сценарии карточки (одна карточка,
   свёрнута по умолчанию, заголовок = активный шаг, клик разворот/сворот,
   авто-разворот при failed с ✗, отметки времени, спиннер/✓/✗) и think-блока
   («💭 Думает…» → «💭 Подумал (N сек)», свёрнут по умолчанию, автоскролл к
   хвосту только развёрнутым, хвост с ограничением).
2. статические контракты: полоса #tl удалена из html/css, feed.js подключён,
   sidebar.js рендерит карточку через Feed и не создаёт её без шагов (п.5),
   без inline-стилей, transition ≤150 мс, sidebar.css без hex-цветов (токены SK-13).

Живое поведение (реальный сайдбар, обе темы, replay-харнесс 5+ шагов) — стенд
firefox-158, не pytest (прецедент SK-15/16/18/19/21).
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
FEED_JS = EXT / "feed.js"
SB = (EXT / "sidebar.js").read_text()
HTML = (EXT / "sidebar.html").read_text()
CSS = (EXT / "sidebar.css").read_text()
NODE = shutil.which("node") or ""

needs_node = pytest.mark.skipif(not NODE, reason="node не в PATH")


# ---- node-харнесс: DOM-заглушка + сценарии ----------------------------------------

HARNESS = r"""
const F = require(process.argv[2]);
const fails = [];
function check(name, cond, extra) {
  if (!cond) fails.push(name + (extra !== undefined ? " | " + JSON.stringify(extra).slice(0, 200) : ""));
}
function makeEl(tag) {
  const e = {
    tagName: String(tag).toUpperCase(), children: [], className: "", textContent: "",
    dataset: {}, hidden: false, title: "", alt: "", src: "", onclick: null,
    scrollTop: 0, scrollHeight: 100, parent: null,
  };
  e.append = (...kids) => { for (const k of kids) { k.parent = e; e.children.push(k); } };
  e.appendChild = (k) => { e.append(k); return k; };
  e.insertBefore = (k, ref) => {
    const i = e.children.indexOf(ref);
    if (i < 0) e.children.push(k); else e.children.splice(i, 0, k);
    k.parent = e; return k;
  };
  e.replaceChildren = (...kids) => { e.children = []; e.append(...kids); };
  e.remove = () => { if (e.parent) e.parent.children = e.parent.children.filter((c) => c !== e); };
  e.scrollIntoView = () => {};
  const cls = () => new Set(e.className.split(/\s+/).filter(Boolean));
  const put = (s) => { e.className = [...s].join(" "); };
  e.classList = {
    toggle: (c, on) => { const s = cls(); const has = s.has(c);
      const want = on === undefined ? !has : !!on; if (want) s.add(c); else s.delete(c); put(s); },
    add: (c) => { const s = cls(); s.add(c); put(s); },
    remove: (c) => { const s = cls(); s.delete(c); put(s); },
    contains: (c) => cls().has(c),
  };
  return e;
}
const doc = { createElement: makeEl };
const txt = (e) => (e.textContent || "") + e.children.map(txt).join("");

// ---- сценарий 1: многашаговая задача — ОДНА карточка, свёрнута, заголовок-активник
const log = makeEl("div");
const times = {};
let now = 1779000000000;
const c = F.buildCard(doc, "t1");
log.appendChild(c.card);
const steps5 = [
  { n: 1, text: "открыть вкладку", state: "done" },
  { n: 2, text: "найти форму", state: "done" },
  { n: 3, text: "заполнить поле", state: "active" },
  { n: 4, text: "отправить", state: "queued" },
  { n: 5, text: "сверить результат", state: "queued" },
];
let tl = { task: "t1", title: "SK22 задача", state: "running", steps: steps5 };
c.live = true;
F.paintCard(doc, c, tl, times, now);
check("card: одна карточка в ленте", log.children.length === 1);
check("card: свёрнута по умолчанию", !c.card.className.split(" ").includes("open"), c.card.className);
check("card: заголовок = активный шаг", c.tx.textContent === "⚙ заполнить поле", c.tx.textContent);
check("card: спиннер running", c.ic.className.includes("spin") && c.ic.textContent === "", c.ic.className);
check("card: 5 строк шагов", c.body.children.length === 5, c.body.children.length);
check("card: ряд active помечен", c.body.children[2].className.includes("active"));
check("card: ряд queued помечен", c.body.children[4].className.includes("queued"));
check("card: время HH:MM:SS у ряда", /^\d{2}:\d{2}:\d{2}$/.test(c.body.children[0].children[2].textContent),
      c.body.children[0].children[2].textContent);
check("card: ✓ done-шага в ряду", txt(c.body.children[0]).includes("✓"));
c.hd.onclick();
check("card: клик развернул", c.card.className.split(" ").includes("open"), c.card.className);
c.hd.onclick();
check("card: повторный клик свернул", !c.card.className.split(" ").includes("open"));
// done: ✓ в заголовке, спиннера нет
tl = { task: "t1", title: "SK22 задача", state: "done",
       steps: steps5.map((s) => ({ ...s, state: "done" })) };
c.live = true;
F.paintCard(doc, c, tl, times, now += 5000);
check("card: done → ✓ без спиннера", c.ic.textContent === "✓" && !c.ic.className.includes("spin"), c.ic.textContent);
check("card: done-класс", c.card.className.includes("done"));
// заголовок без активного шага = последний по n
F.paintCard(doc, c, { task: "t1", state: "running",
  steps: [{ n: 2, text: "второй", state: "done" }, { n: 1, text: "первый", state: "done" }] }, times, now);
check("card: без active — последний шаг", c.tx.textContent === "⚙ второй", c.tx.textContent);

// ---- сценарий 2: failed-шаг → авто-разворот, ✗ виден, человек может свернуть
const c2 = F.buildCard(doc, "t2");
log.appendChild(c2.card);
const fsteps = [{ n: 1, text: "а", state: "done" }, { n: 2, text: "б", state: "failed" }];
c2.live = true;
F.paintCard(doc, c2, { task: "t2", state: "running", steps: fsteps }, times, now);
check("failed: карточка сама развернулась", c2.card.className.split(" ").includes("open"), c2.card.className);
check("failed: ✗ видна в ряду шага", txt(c2.body.children[1]).includes("✗"));
c2.hd.onclick(); // человек свернул — обратно не раскрываем (один авто-разворот)
c2.live = true;
F.paintCard(doc, c2, { task: "t2", state: "running", steps: fsteps }, times, now);
check("failed: после ручного сворота остаётся свёрнутой", !c2.card.className.split(" ").includes("open"));
c2.live = true;
F.paintCard(doc, c2, { task: "t2", state: "failed", steps: fsteps }, times, now);
check("failed: задача failed → ✗ в заголовке", c2.ic.textContent === "✗", c2.ic.textContent);

// ---- сценарий 3: stale-карточка (задача пропала без терминального кадра)
const c3 = F.buildCard(doc, "t3");
F.paintCard(doc, c3, { task: "t3", state: "running", steps: [{ n: 1, text: "x", state: "active" }] }, times, now);
F.markStale(c3);
check("stale: спиннер погашен", !c3.ic.className.includes("spin") && c3.ic.textContent === "○");
check("stale: класс stale", c3.card.className.includes("stale"));
c3.hd.onclick(); // человек кликнул по stale-карточке — repaint не возвращает спиннер
check("stale: repaint не оживляет спиннер", !c3.ic.className.includes("spin") && c3.ic.textContent === "○",
      c3.ic.className + " " + c3.ic.textContent);
// done/failed — НЕ stale: ✓/✗ после repaint
const cd = F.buildCard(doc, "td");
F.paintCard(doc, cd, { task: "td", state: "done", steps: [{ n: 1, text: "x", state: "done" }] }, times, now);
check("done: ✓ не затирается stale-логикой", cd.ic.textContent === "✓" && !cd.card.className.includes("stale"),
      cd.ic.textContent + " " + cd.card.className);

// ---- сценарий 4: шаг со shot — клик по ряду раскрывает картинку (SK-06 внутри карточки)
const c4 = F.buildCard(doc, "t4");
const shot = "data:image/png;base64,AAAA";
c4.live = true;
F.paintCard(doc, c4, { task: "t4", state: "running", steps: [{ n: 1, text: "снимок", state: "done", shot }] }, times, now);
const row4 = c4.body.children[0];
check("shot: подсказка 📷 в свёрнутом ряду", txt(row4).includes("📷"));
row4.onclick();
check("shot: img после клика", c4.body.children[0].children.some((e) => e.tagName === "IMG" && e.src === shot));

// ---- сценарий 5: think-блок — свёрнут по умолчанию, заголовки, автоскролл только открытым
const b = F.buildThink(doc, "t1");
check("think: заголовок «💭 Думает…»", b.hd.textContent === "💭 Thinking…", b.hd.textContent);
check("think: свёрнут по умолчанию", !b.box.className.split(" ").includes("open"));
F.thinkDelta(b, "раз ", 1000);
F.thinkDelta(b, "два", 2000);
check("think: дельты складываются в хвост", b.tx.textContent === "раз два", b.tx.textContent);
check("think: в свёрнутом автоскролла нет", b.tx.scrollTop === 0, b.tx.scrollTop);
b.hd.onclick();
check("think: клик развернул", b.box.className.split(" ").includes("open"));
check("think: при открытии показан хвост", b.tx.scrollTop === b.tx.scrollHeight);
F.thinkDelta(b, " три", 3000);
check("think: в развёрнутом автоскролл к хвосту", b.tx.scrollTop === b.tx.scrollHeight);
F.thinkFinish(b, 12500); // t0=1000 → 11.5 с → «12 сек»
check("think: финал «💭 Подумал (12 сек)»", b.hd.textContent === "💭 Thought (12 sec)", b.hd.textContent);
check("think: done-класс", b.box.className.split(" ").includes("done"));
F.thinkDelta(b, " ещё", 13000); // поздняя дельта — заголовок честно снова «думает»
check("think: поздняя дельта вернула «Думает…»", b.hd.textContent === "💭 Thinking…");
F.thinkFinish(b, 13001);
F.thinkFinish(b, 99000); // повторный финал не переписывает время
check("think: повторный finish идемпотентен", b.hd.textContent === "💭 Thought (12 sec)", b.hd.textContent);
const big = "х".repeat(F.THINK_TAIL + 5000);
const b2 = F.buildThink(doc, "t9");
F.thinkDelta(b2, big, 1000);
check("think: хвост ограничен THINK_TAIL", b2.tx.textContent.length === F.THINK_TAIL);

// ---- сценарий 6: чистые хелперы
check("fmtDur: минуты", F.fmtDur(125) === "2 min 5 sec", F.fmtDur(125));
check("fmtDur: 0", F.fmtDur(0) === "0 sec");
const tm = {};
F.noteStep(tm, "k", "queued", 100); F.noteStep(tm, "k", "queued", 500);
check("noteStep: та же фаза время не двигает", tm.k.last === 100 && tm.k.first === 100);
F.noteStep(tm, "k", "done", 700);
check("noteStep: смена фазы двигает last", tm.k.last === 700 && tm.k.first === 100);
check("headerText: без шагов — титул", F.headerText({ title: "Титл", steps: [] }) === "⚙ Титл");
check("hasFailed: шаг failed", F.hasFailed({ steps: [{ state: "failed" }] }));
check("hasFailed: пусто — false", !F.hasFailed({ steps: [{ state: "done" }], state: "running" }));

process.stdout.write(JSON.stringify({ fails }));
"""


@pytest.fixture(scope="session")
def harness():
    """Один прогон node-харнесса на всю сессию: сценарии дорогие, проверок много."""
    r = subprocess.run([NODE, "-e", HARNESS, "runner", str(FEED_JS)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    return json.loads(r.stdout)


@needs_node
@pytest.mark.parametrize("scenario", [
    "card: одна карточка в ленте",
    "card: свёрнута по умолчанию",
    "card: заголовок = активный шаг",
    "card: спиннер running",
    "card: 5 строк шагов",
    "card: ряд active помечен",
    "card: ряд queued помечен",
    "card: время HH:MM:SS у ряда",
    "card: ✓ done-шага в ряду",
    "card: клик развернул",
    "card: повторный клик свернул",
    "card: done → ✓ без спиннера",
    "card: done-класс",
    "card: без active — последний шаг",
    "failed: карточка сама развернулась",
    "failed: ✗ видна в ряду шага",
    "failed: после ручного сворота остаётся свёрнутой",
    "failed: задача failed → ✗ в заголовке",
    "stale: спиннер погашен",
    "stale: класс stale",
    "stale: repaint не оживляет спиннер",
    "done: ✓ не затирается stale-логикой",
    "shot: подсказка 📷 в свёрнутом ряду",
    "shot: img после клика",
    "think: заголовок «💭 Думает…»",
    "think: свёрнут по умолчанию",
    "think: дельты складываются в хвост",
    "think: в свёрнутом автоскролла нет",
    "think: клик развернул",
    "think: при открытии показан хвост",
    "think: в развёрнутом автоскролл к хвосту",
    "think: финал «💭 Подумал (12 сек)»",
    "think: done-класс",
    "think: поздняя дельта вернула «Думает…»",
    "think: повторный finish идемпотентен",
    "think: хвост ограничен THINK_TAIL",
    "fmtDur: минуты",
    "fmtDur: 0",
    "noteStep: та же фаза время не двигает",
    "noteStep: смена фазы двигает last",
    "headerText: без шагов — титул",
    "hasFailed: шаг failed",
    "hasFailed: пусто — false",
])
def test_render_contract(scenario, harness):
    hit = [f for f in harness["fails"]
           if f == scenario or f.startswith(scenario + " |")]
    assert not hit, f"проверка «{scenario}» провалилась: {hit}"


# ---- статические контракты ----------------------------------------------------------

def test_permanent_band_is_gone():
    assert 'id="tl"' not in HTML, "полоса #tl должна быть удалена — шаги живут в карточке"
    assert not re.search(r"#tl\s*\{", CSS), "CSS-правило полосы #tl должно уйти"
    assert "tlBox" not in SB


def test_feed_js_loaded_before_sidebar():
    assert 'src="feed.js"' in HTML
    assert HTML.index('src="feed.js"') < HTML.index('src="sidebar.js"'), \
        "feed.js обязан грузиться раньше sidebar.js"


def test_sidebar_renders_card_via_feed():
    assert "TASK_CARDS" in SB, "одна карточка на задачу — реестр карточек обязателен"
    assert "Feed.buildCard" in SB and "Feed.paintCard" in SB and "Feed.markStale" in SB
    assert "Feed.buildThink" in SB and "Feed.thinkDelta" in SB and "Feed.thinkFinish" in SB
    # п.5 backward compat: карточка создаётся только когда шаги есть
    # (SK-23 аддитивно добавил к условию третий член — фильтр по чату; SK-25 перелицевал
    # в per-task цикл — интент «без шагов карточки нет» и фильтр чата сохранены в
    # renderCardFor/цикле рендера; прецедент синхронной правки контракта: SK-22/test_audit_js)
    assert re.search(r"if \(!t \|\| !t\.task \|\| !\(t\.steps && t\.steps\.length\)\) continue", SB), \
        "без шагов (старый мост) карточка не создаётся — лента работает как раньше"
    # карточка вставляется ПЕРЕД think-блоком той же задачи
    assert "log.insertBefore(c.card, th.box)" in SB


def test_no_inline_styles():
    for p in (FEED_JS, EXT / "sidebar.js"):
        assert ".style." not in p.read_text(), f"{p.name}: инлайн-стили запрещены (SK-22 п.4)"


def test_transitions_short_and_tokens_only():
    for ms in re.findall(r"transition:[^;]*?(\d+)\s*ms", CSS):
        assert int(ms) <= 150, f"transition {ms} ms > 150 мс (SK-22 п.4)"
    for ms in re.findall(r"transition:[^;]*?(\d+(?:\.\d+)?)s(?!\w)", CSS):
        assert float(ms) * 1000 <= 150, f"transition {ms}s > 150 мс"
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", CSS), \
        "sidebar.css красит только токенами SK-13 — hex-литералов быть не должно"


def test_card_css_hooks_present():
    for hook in (".m.task", ".tkhd", ".tksteps", ".tkic.spin", ".tkcaret", ".stime",
                 ".m.task.open .tksteps", ".m.think.open .tx2"):
        assert hook in CSS, f"CSS-хук {hook} пропал"


@pytest.mark.parametrize("f", ["feed.js", "sidebar.js"])
@needs_node
def test_js_syntax(f):
    r = subprocess.run([NODE, "--check", str(EXT / f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
