"""SK-11 contract tests — the pure feed logic (extension/audit.js) under node.

parseLine turns one audit.jsonl line into the render row model; mask is the
sidebar's defensive second masking pass; filterEntries is the rendered-list
semantics. The bridge legs (rpc audit.tail / WS audit_get) are test_audit.py;
the live UI (button, filter box, raw toggle, cross-check against the file) is the
sk11 bench on the firefox-158 stand, not pytest.
"""
import json
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
AUDIT_JS = ROOT / "extension" / "audit.js"

RUNNER = """
const A = require(process.argv[2]);
const input = JSON.parse(process.argv[3]);
let out;
try { out = A[input.fn](...input.args); }
catch (e) { out = { __err: String(e && e.message || e) }; }
process.stdout.write(JSON.stringify(out === undefined ? null : out));
"""


def js(fn, *args):
    payload = json.dumps({"fn": fn, "args": list(args)})
    r = subprocess.run(["node", "-e", RUNNER, "runner", str(AUDIT_JS), payload],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def parse(obj_or_line):
    raw = obj_or_line if isinstance(obj_or_line, str) else json.dumps(obj_or_line, ensure_ascii=False)
    return js("parseLine", raw)


# ---- masking (the card: «секреты/пароли в ленте маскировать (поле value -> ***)») ----

@pytest.mark.parametrize("key", ["token", "password", "secret", "cvv", "cvc", "value"])
def test_mask_masks_secret_shaped_keys(key):
    line = json.dumps({"ev": "perm", key: "hunter2-секрет", "domain": "a.test"})
    out = js("mask", line)
    assert "hunter2-секрет" not in out
    assert json.loads(out)[key] == "***"
    assert json.loads(out)["domain"] == "a.test"   # the feed stays useful

def test_mask_leaves_clean_lines_and_case_variants():
    clean = json.dumps({"ts": 1, "method": "page.text", "ok": True, "domain": "a.test"})
    assert js("mask", clean) == clean
    assert "S3cr3t" not in js("mask", '{"Password":"S3cr3t","VALUE":"x"}')

def test_mask_value_with_spaces_and_unicode():
    out = js("mask", '{"value":"привет мир secret","ok":1}')
    assert json.loads(out)["value"] == "***" and json.loads(out)["ok"] == 1


# ---- timeOf ---------------------------------------------------------------------

def test_time_of_local_hhmmss():
    ts = int(__import__("time").mktime(__import__("time").strptime("2026-10-17 07:05:09", "%Y-%m-%d %H:%M:%S")))
    assert js("timeOf", ts) == "07:05:09"          # local time, zero-padded

@pytest.mark.parametrize("bad", [0, -1, None, "abc", 1.5e308])
def test_time_of_garbage_is_empty(bad):
    assert js("timeOf", bad) == ""


# ---- parseLine: the audit vocabulary ----------------------------------------------

def test_parse_perm_allow_and_block():
    a = parse({"ts": 1, "ev": "perm", "domain": "example.com", "action": "sensitive_input",
               "decision": "allow", "via": "human_once"})
    assert a["kind"] == "perm" and a["tone"] == "ok" and a["domain"] == "example.com"
    assert "password/card field" in a["title"] and "human: once" in a["sub"]  # SK-71: EN default pack
    b = parse({"ts": 1, "ev": "perm", "domain": "evil.test", "action": "payment",
               "decision": "block", "via": "grant_block"})
    assert b["tone"] == "bad" and "Blocked:" in b["title"]

def test_parse_preview_verdicts():
    assert parse({"ts": 1, "ev": "preview", "shown": True, "cancelled": False})["tone"] == "dim"
    c = parse({"ts": 1, "ev": "preview", "shown": True, "cancelled": True, "reason": "user_stop"})
    assert c["verdict"] == "cancelled" and c["tone"] == "bad"
    n = parse({"ts": 1, "ev": "preview", "shown": False, "cancelled": False, "reason": "no_element"})
    assert n["verdict"] == "not shown" and n["tone"] == "warn"

def test_parse_rpc_line_with_and_without_domain():
    ok = parse({"ts": 1, "method": "page.text", "ok": True, "domain": "site.example"})
    assert ok["kind"] == "rpc" and ok["tone"] == "ok" and ok["domain"] == "site.example"
    assert "read the page text" in ok["title"]
    err = parse({"ts": 1, "method": "page.screenshot", "ok": False, "err": "capture_failed"})
    assert err["tone"] == "bad" and "capture_failed" in err["sub"] and err["domain"] == ""
    unk = parse({"ts": 1, "method": "future.method", "ok": True})
    assert "future.method" in unk["title"]        # unknown methods render verbatim

def test_parse_lifecycle_events():
    assert parse({"ts": 1, "ev": "stop", "task": "t1"})["kind"] == "stop"
    assert parse({"ts": 1, "ev": "handoff", "decision": "pause", "what": "логин"})["tone"] == "warn"
    assert parse({"ts": 1, "ev": "resume", "task": "t1"})["verdict"] == "resumed"
    g = parse({"ts": 1, "ev": "grants.set", "domain": "a.test", "level": "allow"})
    assert g["kind"] == "grants" and g["domain"] == "a.test" and g["tone"] == "warn"
    assert parse({"ts": 1, "ev": "state", "state": "acting"})["tone"] == "dim"
    assert parse({"ts": 1, "ev": "handoff_block", "method": "page.text"})["tone"] == "warn"

def test_parse_garbage_is_raw_row_not_exception():
    for bad in ("не json", "[1,2]", "42", "", None):
        r = js("parseLine", bad)
        assert r["kind"] == "raw" and r["tone"] == "dim"
        assert r["title"] == ("" if bad in ("", None) else str(bad))  # empty in = empty row


# ---- filterEntries ------------------------------------------------------------------

ROWS = [parse(x) for x in [
    {"ts": 1, "ev": "perm", "domain": "Example.COM", "action": "click", "decision": "allow", "via": "grant"},
    {"ts": 2, "method": "page.text", "ok": True, "domain": "sub.example.com"},
    {"ts": 3, "ev": "state", "state": "idle"},
    {"ts": 4, "ev": "stop"},
]]

def test_filter_empty_query_keeps_everything():
    assert js("filterEntries", ROWS, "") == ROWS
    assert js("filterEntries", ROWS, None) == ROWS

def test_filter_domain_substring_case_insensitive():
    got = js("filterEntries", ROWS, "example")
    assert [r["domain"] for r in got] == ["Example.COM", "sub.example.com"]
    assert js("filterEntries", ROWS, "sub.ex")[0]["domain"] == "sub.example.com"
    assert js("filterEntries", ROWS, "nope") == []

def test_filter_hides_domainless_rows_while_active():
    # «what happened ON that site» — state/Stop rows are not on any site
    assert all(r["domain"] for r in js("filterEntries", ROWS, "e"))
    assert len(js("filterEntries", ROWS, "")) == 4


# ---- static wiring contract ------------------------------------------------------------

def test_sidebar_wires_audit_module_and_view():
    html = (ROOT / "extension" / "sidebar.html").read_text()
    src = (ROOT / "extension" / "sidebar.js").read_text()
    # order matters; SK-22 добавил feed.js между модулями — проверяем порядок, а не смежность
    assert html.index('src="audit.js"') < html.index('src="sidebar.js"')
    for needle in ("audBtn", "audf", "audrawBtn", "audlist", "audraw", "audn",
                   "audit_get", "audit_data", "Audit.parseLine", "Audit.mask", "AUDREQ"):
        assert needle in src, needle
    # the card's «кнопка открыть сырой файл» and the readable-row view both exist
    assert "raw file" in html and 'data-i18n="sbt.aud_raw"' in html and 'id="aud"' in html  # SK-71: EN markup + dictionary key

def test_background_relays_audit_pull():
    src = (ROOT / "extension" / "background.js").read_text()
    assert '"audit_get"' in src and '"audit_data"' in src and '"audit_err"' in src

def test_bridge_contract_strings():
    src = (ROOT / "bridge" / "sidekick_bridge.py").read_text()
    for needle in ('"audit.tail"', '"audit_get"', '"audit_data"', "audit_tail",
                   "AUDIT_TAIL_MAX", "audit_extra"):
        assert needle in src, needle
    assert "value" in src.split("SECRET_RE = re.compile(r'")[1].split("'")[0]  # in the regex


# ---- SK-49: строки жизни расширения (ev "ext") ----------------------------------

def test_ext_connect_row_shows_peer():
    r = parse({"ev": "ext", "decision": "connect", "peer": "192.0.2.10:53211"})
    assert r["kind"] == "ext" and r["tone"] == "ok"
    assert "connected" in r["title"] and r["sub"] == "192.0.2.10:53211"


def test_ext_replaced_row_shows_both_peers():
    r = parse({"ev": "ext", "decision": "replaced",
               "peer": "192.0.2.10:53211", "by": "127.0.0.1:41000"})
    assert r["verdict"] == "replaced" and r["tone"] == "warn"
    assert r["sub"] == "192.0.2.10:53211 → 127.0.0.1:41000"


def test_ext_disconnect_row_is_dim():
    r = parse({"ev": "ext", "decision": "disconnect", "peer": "127.0.0.1:41000"})
    assert r["verdict"] == "offline" and r["tone"] == "dim"
    assert r["sub"] == "127.0.0.1:41000"
