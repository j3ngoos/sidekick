"""SK-43 contract: пермис-семантика, capabilities, русские подписи превью.

Аудит №2 22.10 (инцидент B, проблемы 6/8): агент вычитал ALWAYS_HUMAN-таблицу из
исходников моста и «для надёжности» пометил обычный клик по аватару YouTube как
sensitive_input — человек получил карточку «действие с полем пароля/карты» на
youtube.com; подписи превью были ASCII-мусором («Account menu», «empty area»).
Пины здесь:

  * perm.check с аддитивным `perm`: action — глагол действия, perm — тип пермиса;
    click/rightclick/dblclick/hover/move/key/scroll × sensitive_input/send_message/
    send_email -> err perm_action_mismatch + hint, карточка человеку НЕ уходит
    (ни perm_ask-рамки, ни ev:perm-строки аудита);
  * input/submit + чувствительный perm работают как раньше (карточка по типу);
    click + payment/oauth — легитимны (клик по «Оплатить» — настоящий платёж);
  * легаси-форма {action:"<тип>"} без perm не тронута (матрица SK-34 зелёная);
  * capabilities {} — полный контракт из констант моста: методы, глаголы action.do,
    типы пермисов с границами; отвечает и в hand-off паузе; drift-гарды к коду;
  * sk capabilities — тот же контракт через CLI (SK-33-паттерн);
  * ev:preview несёт label_src ("agent"|"dom") на обоих легах (action.do и
    standalone); node-юнит sk43EnrichLabel: ASCII-only подпись + русская цель ->
    подпись из DOM (aria-label → visible text), key/кириллица/латиница-DOM/BODY ->
    остаётся агентская.
"""
import asyncio
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

from conftest import kill_bridge, start_bridge
from test_action_do import EXEC_OK, CLEAN, mock_chain, run
from test_perm import MockExt, answered, read_jsonl, rpc_ok  # noqa: F401
from test_preview import mock_preview
from test_rpc_e2e import read_rpc_out, rpc_call

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE_SRC = (ROOT / "bridge" / "sidekick_bridge.py").read_text(encoding="utf-8")
BG_SRC = (ROOT / "extension" / "background.js").read_text(encoding="utf-8")
JS = (ROOT / "extension" / "content.js").read_text(encoding="utf-8")
SK = str(ROOT / "bin" / "sk")

CYR = re.compile(r"[\u0400-\u04FF]")


@pytest.fixture
def home(tmp_path):
    p = start_bridge(tmp_path)
    yield tmp_path
    kill_bridge(p)


# ---- п.1: семантика perm.check -------------------------------------------------------

MISMATCH_PAIRS = [("click", "sensitive_input"), ("click", "send_message"),
                  ("click", "send_email"), ("rightclick", "sensitive_input"),
                  ("dblclick", "send_message"), ("hover", "send_email"),
                  ("move", "sensitive_input"), ("key", "send_message"),
                  ("scroll", "sensitive_input")]


@pytest.mark.parametrize("op,perm", MISMATCH_PAIRS)
def test_mismatch_rejected_no_card(home, op, perm):  # noqa: F811
    """Инцидент B в тесте: обычный глагол + чувствительный perm — отказ моста с
    подсказкой, человек НЕ видит карточку (ни perm_ask-рамки, ни ev:perm-аудита)."""
    async def scenario():
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check",
                           {"domain": "youtube.com", "action": op, "perm": perm,
                            "task": "sk43-mismatch"})
            out = answered(home, rid)
            await asyncio.sleep(0.3)
            return ext, out

    ext, out = run(scenario())
    assert out["ok"] is False and out["err"] == "perm_action_mismatch", out
    hint = out.get("hint", "")
    assert "sk capabilities" in hint and len(hint) <= 600, hint
    assert not ext.asks(), f"карточка человеку на бессмыслице {op}+{perm}"
    audit = read_jsonl(pathlib.Path(home) / "audit.jsonl")
    assert not [a for a in audit if a.get("ev") == "perm" and a.get("action") == perm], audit
    line = [a for a in audit if a.get("method") == "perm.check" and not a.get("ok")]
    assert line and line[-1]["err"] == "perm_action_mismatch", audit


def test_mismatch_hint_names_right_action(home):
    """Подсказка обязана вести к правильному action (или «просто click»)."""
    rid, out = rpc_ok(home, "perm.check",
                      {"domain": "youtube.com", "action": "click", "perm": "sensitive_input"})
    assert out["ok"] is False and out["err"] == "perm_action_mismatch"
    assert "input" in out["hint"] and '"click"' in out["hint"], out["hint"]


def test_perm_form_input_sensitive_input_asks(home):
    """Новая форма для чувствительного ввода жива: ask, карточка ключуется ТИПОМ."""
    async def scenario():
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check",
                           {"domain": "shop.example", "action": "input",
                            "perm": "sensitive_input", "task": "sk43-a"})
            ask = await ext.wait_ask()
            await ext.answer(ask["id"], "once")
            return ask, answered(home, rid)

    ask, out = run(scenario())
    assert ask["action"] == "sensitive_input"  # не «input»: карточка — по типу пермиса
    assert out["ok"] and out["result"]["decision"] == "allow"
    assert out["result"]["via"] == "human_once"


def test_perm_form_submit_send_message_asks(home):
    async def scenario():
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check",
                           {"domain": "mail.example", "action": "submit",
                            "perm": "send_message", "task": "sk43-b"})
            ask = await ext.wait_ask()
            return ask, rid

    ask, rid = run(scenario())
    assert ask["action"] == "send_message"  # текст наружу несут input/submit — легально


def test_perm_form_click_payment_legit(home):
    """Клик по «Оплатить» — настоящий платёж: click+payment НЕ mismatch (п.1 карточки
    запрещает только sensitive_input/send_message/send_email на обычных глаголах)."""
    async def scenario():
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check",
                           {"domain": "shop.example", "action": "click",
                            "perm": "payment", "task": "sk43-c"})
            ask = await ext.wait_ask()
            return ask

    ask = run(scenario())
    assert ask["action"] == "payment"


def test_perm_form_nonverb_action_mismatch(home):
    """{action:"sensitive_input", perm:...} — action обязан быть глаголом."""
    rid, out = rpc_ok(home, "perm.check",
                      {"domain": "x.example", "action": "sensitive_input",
                       "perm": "sensitive_input"})
    assert out["ok"] is False and out["err"] == "perm_action_mismatch"
    assert "глагол" in out["hint"], out["hint"]


def test_legacy_form_unchanged(home):
    """Матрица SK-34: легаси-форма {action:"<тип>"} без perm ведёт себя по-старому."""
    async def scenario():
        async with MockExt(home) as ext:
            rid = rpc_call(home, "perm.check",
                           {"domain": "youtube.com", "action": "sensitive_input",
                            "task": "sk43-legacy"})
            ask = await ext.wait_ask()
            await ext.answer(ask["id"], "deny")
            return ask, answered(home, rid)

    ask, out = run(scenario())
    assert ask["action"] == "sensitive_input"
    assert out["ok"] and out["result"]["decision"] == "block"
    assert out["result"]["via"] == "human_deny"


def test_new_form_allow_grant_silent(home):
    """Не-чувствительный perm при Allow-гранте — мостотает без расширения."""
    rpc_call(home, "grants.set", {"domain": "shop.example", "level": "allow"})
    rid, out = rpc_ok(home, "perm.check",
                      {"domain": "shop.example", "action": "click", "perm": "click"})
    assert out["ok"] and out["result"] == {"decision": "allow", "via": "grant_hit"}


# ---- п.2: capabilities -----------------------------------------------------------------

def capabilities_of(home):
    rid, out = rpc_ok(home, "capabilities", {})
    assert out["ok"], out
    return out["result"]


def test_capabilities_full_contract(home):
    cap = capabilities_of(home)
    assert cap["v"] == 1
    for m in ("perm.check", "grants.set", "grants.get", "handoff.request",
              "handoff.status", "audit.tail", "chats.list", "chats.get", "capabilities"):
        assert m in cap["methods"]["bridge_local"], m
    for m in ("page.dom", "page.text", "page.screenshot", "action.preview",
              "tabs.open_grouped", "tabs.use_tab", "tabs.list", "page.active"):
        assert m in cap["methods"]["forwarded"], m
    assert set(cap["action_do"]["actions"]) >= {"click", "input", "submit", "key",
                                                "scroll", "rightclick", "hover",
                                                "dblclick", "move"}
    assert cap["perms"]["mismatch_err"] == "perm_action_mismatch"
    assert set(cap["perms"]["ops"]) == set(cap["action_do"]["actions"])
    types = {t["perm"]: t for t in cap["perms"]["types"]}
    for p in ("sensitive_input", "payment", "oauth", "2fa", "send_message", "send_email"):
        assert types[p]["always_human"] is True and types[p]["always_button"] is False, p
    assert types["sensitive_input"]["ops"] == ["input", "submit"]
    assert types["send_message"]["ops"] == ["input", "submit"]
    assert types["click"]["always_human"] is False
    assert "perm_action_mismatch" in cap["perms"]["rule"]
    assert "DOM" in cap["preview"]["label"] and "label_src" in cap["preview"]["label"]


def test_capabilities_drift_guard_constants(home):
    """Документ не может уехать от кода: глаголы и always-human типы сверяем с
    литералами моста, форвард-методы — со словарём расширения."""
    cap = capabilities_of(home)
    m = re.search(r'ACTION_DO_ACTIONS = \{([^}]+)\}', BRIDGE_SRC, re.S)
    assert m, "ACTION_DO_ACTIONS исчез из моста"
    verbs = set(re.findall(r'"([a-z]+)"', m.group(1)))
    assert set(cap["action_do"]["actions"]) == verbs
    m = re.search(r'ALWAYS_HUMAN_ACTIONS = \{([^}]+)\}', BRIDGE_SRC, re.S)
    assert m, "ALWAYS_HUMAN_ACTIONS исчез из моста"
    always = set(re.findall(r'"([a-z_0-9]+)"', m.group(1)))
    in_doc = {t["perm"] for t in cap["perms"]["types"]
              if t["always_human"] and t["perm"] != "close_tabs"}
    assert in_doc == always
    ext_methods = set(re.findall(r'"((?:page|tabs|action)\.[a-z_]+)"', BG_SRC))
    assert set(cap["methods"]["forwarded"]) | set(cap["methods"]["gated"]) == ext_methods
    dispatched = set(re.findall(r'method == "([a-z_.]+)"', BRIDGE_SRC))
    local_like = {x for x in dispatched
                  if x.split(".")[0] in ("handoff", "grants", "perm", "audit", "chats")
                  or x == "capabilities"}
    assert local_like == set(cap["methods"]["bridge_local"])


def test_capabilities_answers_during_handoff_pause(home):
    """Статика без данных браузера: capabilities отвечает и когда человек держит
    браузер (perm.check в это время paused_for_human — приватность не затронута)."""
    pathlib.Path(home / "paused").touch()
    cap = capabilities_of(home)
    assert cap["methods"]["bridge_local"]
    rid, out = rpc_ok(home, "perm.check", {"domain": "x.example", "action": "click"})
    assert out["ok"] is False and out["err"] == "paused_for_human"


def test_sk_cli_capabilities(home):
    """sk capabilities — CLI-путь агента (SK-33-паттерн)."""
    env = dict(os.environ, SIDEKICK_HOME=str(home))
    r = subprocess.run([sys.executable, SK, "capabilities"],
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    obj = json.loads(r.stdout.strip().splitlines()[-1])
    assert obj["ok"] is True
    res = obj["result"]
    assert "perm.check" in res["methods"]["bridge_local"]
    assert "click" in res["action_do"]["actions"]
    assert any(t["perm"] == "sensitive_input" for t in res["perms"]["types"])


# ---- п.4: label_src в ev:preview --------------------------------------------------------

def test_preview_audit_label_src_standalone(home):
    verdict = {"shown": True, "cancelled": False, "held_ms": 450,
               "label": "кликну «Меню аккаунта»", "label_src": "dom"}
    rid = rpc_call(home, "action.preview", {"selector": "#a", "action": "click",
                                            "name": "Account menu"})
    mock_preview(home, verdict)
    out = read_rpc_out(home, rid)
    assert out["ok"] and out["result"]["label_src"] == "dom"
    audit = [a for a in read_jsonl(pathlib.Path(home) / "audit.jsonl")
             if a.get("ev") == "preview"]
    assert audit and audit[-1]["via"] == "standalone"
    assert audit[-1]["label_src"] == "dom" and CYR.search(audit[-1]["label"])


def test_preview_audit_label_src_action_do(home):
    verdict = dict(CLEAN, label="кликну «Меню аккаунта»", label_src="dom")
    rid = rpc_call(home, "action.do", {"selector": "#avatar-btn", "action": "click",
                                       "name": "Account menu"})
    seen = mock_chain(home, verdict, exec_reply=EXEC_OK)
    out = read_rpc_out(home, rid)
    assert out["ok"] and out["result"]["executed"] is True
    assert [f["method"] for f in seen] == ["action.preview", "action.do"]
    audit = [a for a in read_jsonl(pathlib.Path(home) / "audit.jsonl")
             if a.get("ev") == "preview"]
    assert len(audit) == 1 and audit[0]["via"] == "action.do"
    assert audit[0]["label_src"] == "dom", audit


def test_preview_audit_label_src_absent_stays_clean(home):
    """Старый вердикт без label_src — аудит без поля (контракт v1 аддитивен)."""
    rid = rpc_call(home, "action.preview", {"selector": "#a", "action": "click"})
    mock_preview(home, CLEAN)
    read_rpc_out(home, rid)
    audit = [a for a in read_jsonl(pathlib.Path(home) / "audit.jsonl")
             if a.get("ev") == "preview"]
    assert audit and "label_src" not in audit[-1]


# ---- node-юнит sk43EnrichLabel -----------------------------------------------------------

NODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
eval(src); // sloppy-mode eval: sk43DomLabel/sk43EnrichLabel поднимаются в скоуп

function el(tag, attrs, text) {
  return { tagName: tag, attrs: attrs || {}, textContent: text === undefined ? "" : text,
           innerText: text === undefined ? "" : text,
           getAttribute(a) { return a in this.attrs ? this.attrs[a] : null; } };
}
const out = {};
// инцидент B: аватар с русской aria-label, агент тащит «Account menu»
out.aria = sk43EnrichLabel("кликну «Account menu»", "кликну",
                           el("BUTTON", { "aria-label": "Меню аккаунта" }, ""),
                           { name: "Account menu" });
// «empty area» на видео-карточке: aria нет — видимый текст
out.vis = sk43EnrichLabel("кликну «empty area»", "кликну",
                          el("DIV", {}, "Как собрать стенд Sidekick за вечер · 12 тыс. просмотров"),
                          { name: "empty area" });
// кириллица в имени — не трогаем
out.cyr = sk43EnrichLabel("кликну «Удалить»", "кликну",
                          el("BUTTON", { "aria-label": "Меню аккаунта" }, ""),
                          { name: "Удалить" });
// key: подпись — сама клавиша, не ярлык
out.key = sk43EnrichLabel("нажму «Enter»", "нажму",
                          el("INPUT", { "aria-label": "Сообщение" }, ""),
                          { skip: true, name: "Enter" });
// DOM-кандидат латиницей не лучше агентского — оставляем
out.latin = sk43EnrichLabel("кликну «Subscribe»", "кликну",
                            el("BUTTON", { "aria-label": "Subscribe" }, ""),
                            { name: "Subscribe" });
// фон не обогащаем (весь документ — не подпись)
out.body = sk43EnrichLabel("кликну «bg»", "кликну", el("BODY", {}, "весь текст страницы"),
                           { name: "bg" });
// mark-режим: номер метки доживает до подписи
out.mark = sk43EnrichLabel("кликну «Message [26]»", "кликну",
                           el("BUTTON", { "aria-label": "Отправить сообщение" }, ""),
                           { name: "Message [26]", markN: 26 });
// пусто в DOM — остаётся агентская
out.none = sk43EnrichLabel("кликну «icon»", "кликну", el("SPAN", {}, "   "), { name: "icon" });
// длинный видимый текст режется, подпись ≤120
out.long = sk43EnrichLabel("кликну «card»", "кликну", el("DIV", {}, "А".repeat(200)),
                           { name: "card" });
// нет цели (point-мимо, hit-test не нашёл) — не падает
out.noel = sk43EnrichLabel("кликну «area»", "кликну", null, { name: "area" });
// имя не передано — проба по всей подписи (обратная совместимость)
out.noprobe = sk43EnrichLabel("click Account menu", "кликну",
                              el("BUTTON", { "aria-label": "Меню аккаунта" }, ""), {});
console.log(JSON.stringify(out));
"""


def test_sk43_label_node_unit(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    m = re.search(r"// SK-43-BEGIN preview-label[^\n]*\n(.*?)// SK-43-END preview-label",
                  JS, re.S)
    assert m, "блок SK-43 preview-label исчез из content.js"
    block = tmp_path / "sk43_label.js"
    block.write_text(m.group(1), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(NODE_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(harness), str(block)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr}"
    out = json.loads(r.stdout)

    assert out["aria"] == {"label": "кликну «Меню аккаунта»", "label_src": "dom"}
    assert out["vis"]["label_src"] == "dom" and "Как собрать стенд" in out["vis"]["label"]
    assert out["cyr"] == {"label": "кликну «Удалить»", "label_src": "agent"}
    assert out["key"] == {"label": "нажму «Enter»", "label_src": "agent"}
    assert out["latin"] == {"label": "кликну «Subscribe»", "label_src": "agent"}
    assert out["body"] == {"label": "кликну «bg»", "label_src": "agent"}
    assert out["mark"]["label_src"] == "dom" and out["mark"]["label"].endswith("[26]»")
    assert "Отправить сообщение" in out["mark"]["label"]
    assert out["none"]["label_src"] == "agent"
    assert out["long"]["label_src"] == "dom"
    assert len(out["long"]["label"]) <= 120 and "…" in out["long"]["label"]
    assert out["noel"]["label_src"] == "agent"
    # без opts.name проба идёт по всей подписи: ASCII-подпись обогащается
    assert out["noprobe"] == {"label": "кликну «Меню аккаунта»", "label_src": "dom"}


# ---- wiring: preview() действительно зовёт обогащение -----------------------------------

def test_preview_wires_enrichment():
    """Точка вживления: label строится через sk43EnrichLabel, вердикт несёт label_src."""
    body = JS[JS.index("async function preview(m)"):]
    assert "sk43EnrichLabel(label0" in body, "preview() не обогащает подпись"
    assert body.count("label_src") >= 3  # verdict-лег(2 finish) + const
    assert "held_ms: wait, label, label_src" in body


def test_sidebar_action_ru_keeps_sensitive_wording():
    """ALWAYS_HUMAN-формулировка живёт в ACTION_RU и показывается только по типу
    пермиса (мост её больше не выдаёт для обычных глаголов — см. mismatch-тесты)."""
    src = (ROOT / "extension" / "sidebar.js").read_text(encoding="utf-8")
    # SK-71: the wording moved to the dictionary; the source wires the key
    assert 'sensitive_input: "perm.sensitive_input"' in src
    assert "ACTION_RU[m.action]" in src  # заголовок карточки — строго по типу
    _ru = json.loads((ROOT / "extension" / "locales" / "ru.json").read_text(encoding="utf-8"))
    assert _ru["perm.sensitive_input"] == "действие с полем пароля/карты"
