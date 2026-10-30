"""SK-10 contract tests — the pure skills logic (extension/skills.js) under node.

The «/»-menu logic (trigger parsing, ranking, upsert caps, export/import merge) is the
whole feature's contract; the sidebar is thin DOM over it. Live UI acceptance
(mouse + keyboard, persistence across an extension restart) is the sk10 bench on the
firefox-158 stand, not pytest.
"""
import json
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILLS_JS = ROOT / "extension" / "skills.js"

RUNNER = """
const S = require(process.argv[2]);
const input = JSON.parse(process.argv[3]);
let out;
try { out = S[input.fn](...input.args); }
catch (e) { out = { __err: String(e && e.message || e) }; }
process.stdout.write(JSON.stringify(out === undefined ? null : out));
"""


def js(fn, *args):
    payload = json.dumps({"fn": fn, "args": list(args)})
    r = subprocess.run(["node", "-e", RUNNER, "runner", str(SKILLS_JS), payload],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ---- slash trigger -------------------------------------------------------------

@pytest.mark.parametrize("value,want", [
    ("/", ""),
    ("/са", "са"),
    ("/a b c", "a b c"),
    ("", None),
    ("привет", None),
    ("  /cmd", None),   # trigger is ONLY a leading slash
    ("2/3", None),
    (None, None),
])
def test_slash_query(value, want):
    got = js("slashQuery", value)
    assert got == want, (value, got)


# ---- filtering / ranking --------------------------------------------------------

LIST = [
    {"id": "a", "name": "Саммари", "text": "Сделай саммари этой страницы"},
    {"id": "b", "name": "Перевод", "text": "Переведи на русский"},
    {"id": "c", "name": "Проверка", "text": "Проверь ошибки"},
    {"id": "d", "name": "Найти в тексте", "text": "саммари не про это"},
]

def test_filter_empty_query_all_sorted_by_name():
    names = [s["name"] for s in js("filter", LIST, "")]
    assert sorted(names) == sorted(s["name"] for s in LIST)

def test_filter_name_prefix_beats_infix_beats_text():
    got = [s["name"] for s in js("filter", LIST, "са")]
    assert got[0] == "Саммари"          # name prefix
    assert "Найти в тексте" in got      # text contains "саммари"
    assert "Проверка" not in got
    assert [s["name"] for s in js("filter", LIST, "аммари")] == ["Саммари", "Найти в тексте"]

def test_filter_case_insensitive():
    got = [s["name"] for s in js("filter", LIST, "ПЕРЕВ")]
    assert got == ["Перевод"]

def test_filter_no_match_and_caps():
    assert js("filter", LIST, "zzz") == []
    big = [{"id": str(i), "name": f"скилл {i:03d}", "text": "x"} for i in range(50)]
    assert len(js("filter", big, "скилл")) == 12  # SHOWN_MAX


# ---- upsert ---------------------------------------------------------------------

def test_upsert_add_then_update_by_name_case_insensitive():
    r1 = js("upsert", [], "Саммари", "текст один", 1700000000000)
    assert r1["ok"] and r1["action"] == "added" and len(r1["list"]) == 1
    assert r1["list"][0]["created"].startswith("20")
    r2 = js("upsert", r1["list"], "саммари", "текст два", 1700000000001)
    assert r2["action"] == "updated" and len(r2["list"]) == 1
    assert r2["list"][0]["text"] == "текст два"
    assert r2["list"][0]["id"] == r1["list"][0]["id"]  # same entry, not a new one

def test_upsert_default_name_from_text_and_newlines_collapsed():
    r = js("upsert", [], "", "первая\nстрока   и\rвторая", 1700000000000)
    assert r["ok"] and r["name"] == "первая строка и вторая"
    assert r["list"][0]["text"] == "первая строка и вторая"

def test_upsert_empty_rejected_and_caps():
    assert js("upsert", [], "имя", "   ")["ok"] is False
    assert js("upsert", [], "имя", None)["ok"] is False
    r = js("upsert", [], "  " + "и" * 100 + "  ", "т" * 9000, 1700000000000)
    assert len(r["name"]) == 60 and len(r["list"][0]["text"]) == 4000
    pre = [{"id": str(i), "name": f"n{i}", "text": "т"} for i in range(199)]
    r2 = js("upsert", pre, "new", "т")
    assert len(r2["list"]) == 200 and r2["list"][-1]["name"] == "new"
    r3 = js("upsert", r2["list"], "over", "т" * 5000)
    assert len(r3["list"]) == 200 and r3["list"][0]["name"] == "n1"  # oldest evicted


# ---- export / import --------------------------------------------------------------

def test_export_shape_and_roundtrip():
    r = js("upsert", [], "Альфа", "дело А", 1700000000000)
    lst = js("upsert", r["list"], "Бета", "дело Б", 1700000000001)["list"]
    txt = js("exportJson", lst, "2026-10-17T00:00:00.000Z")
    data = json.loads(txt)
    assert data["app"] == "hermes-sidekick" and data["kind"] == "skills" and data["version"] == 1
    assert {s["name"] for s in data["skills"]} == {"Альфа", "Бета"}
    imp = js("importJson", txt, [])
    assert imp["ok"] and imp["added"] == 2 and {s["name"] for s in imp["list"]} == {"Альфа", "Бета"}

def test_import_bare_array_and_merge_wins():
    base = [{"id": "x", "name": "Один", "text": "старый"}, {"id": "y", "name": "Два", "text": "два"}]
    imp = js("importJson", json.dumps([{"name": "один", "text": "новый"},
                                       {"name": "Три", "text": "три"},
                                       {"name": "", "text": "мусор"},
                                       {"nope": 1}]), base)
    assert imp["ok"] and imp["added"] == 1 and imp["updated"] == 1 and imp["skipped"] == 2
    by = {s["name"].lower(): s["text"] for s in imp["list"]}  # imported name wins (case too)
    assert by["один"] == "новый" and by["два"] == "два" and by["три"] == "три"
    assert len(imp["list"]) == 3

@pytest.mark.parametrize("bad,err", [("не json", "bad_json"), ('{"a":1}', "no_skills"), ("[]", None)])
def test_import_bad_input(bad, err):
    r = js("importJson", bad, [])
    if err is None:
        assert r["ok"] and r["added"] == 0
    else:
        assert r["ok"] is False and r["err"] == err


# ---- static wiring contract ---------------------------------------------------------

def test_sidebar_wires_skills_module():
    html = (ROOT / "extension" / "sidebar.html").read_text()
    js_src = (ROOT / "extension" / "sidebar.js").read_text()
    # order contract: skills.js loads before sidebar.js. SK-11 added audit.js in
    # between (its own adjacency to sidebar.js is pinned in test_audit_js.py).
    assert html.index('<script src="skills.js">') < html.index('src="sidebar.js"')  # SK-71: sidebar.js is type="module"
    for needle in ("sk10Skills", "Skills.slashQuery", "Skills.filter", "Skills.upsert",
                   "Skills.exportJson", "Skills.importJson", "skmenu", "sksave"):
        assert needle in js_src, needle
    # the old unconditional Enter-submits listener must be gone (menu owns Enter)
    assert 'addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); })' not in js_src

def test_no_new_permissions_for_storage_local():
    man = json.loads((ROOT / "extension" / "manifest.json").read_text())
    assert "storage" in man["permissions"]
    assert "clipboardWrite" not in man["permissions"]  # export is a blob download, not clipboard
