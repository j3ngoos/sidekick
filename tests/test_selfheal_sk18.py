"""SK-18 контрактные тесты — self-healing page RPC (extension/background.js).

Классификатор URL — чистая функция pageClass прямо в background.js: контракт
вытащивает её дословно и гоняет под node (тот же приём, что skills.js/audit.js —
манифест из-за тестов не тронут ни на байт). Сам флоу самоизлечения (инжект +
РОВНО ОДИН автоповтор, internal_page/inject_failed, подсказка Cmd+R только при
сбое инжекта) здесь — статические контракты; живая приёмка на стенде
firefox-158 — харнесс sk18_r1_worker.py, не pytest.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BG = (ROOT / "extension" / "background.js").read_text()
MAN = json.loads((ROOT / "extension" / "manifest.json").read_text())
NODE = shutil.which("node")

RUNNER = ("process.stdout.write(JSON.stringify("
          "require(process.argv[1]).pageClass(process.argv[2])))")


def _extract():
    """Тело pageClass дословно (function ... до закрывающей скобки на нулевом отступе)."""
    m = re.search(r"function pageClass\(url\) \{.*?\n\}", BG, re.S)
    assert m, "pageClass пропала из background.js (или изменила сигнатуру)"
    return m.group(0)


def _body(name):
    m = re.search(r"async function " + name + r"\([^)]*\) \{(.*?)\n\}", BG, re.S)
    assert m, name + " пропала из background.js"
    return m.group(1)


def _err(name):
    m = re.search(r"const " + name + r" = \"([^\"]+)\";", BG)
    assert m, name + " пропала"
    return m.group(1)


@pytest.fixture(scope="module")
def pc(tmp_path_factory):
    """pageClass под node: файл = дословно извлечённая функция + module.exports."""
    d = tmp_path_factory.mktemp("sk18")

    def run(url):
        if not NODE:
            pytest.skip("node нет — классификатор проверим на стенде")
        f = d / "pageclass.js"
        f.write_text(_extract() + "\nmodule.exports = { pageClass };\n", encoding="utf-8")
        r = subprocess.run([NODE, "-e", RUNNER, str(f), url],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    return run


# ---- классификатор URL ---------------------------------------------------------------

@pytest.mark.parametrize("url,want", [
    ("https://a.test", "injectable"),
    ("http://127.0.0.1:8897/page.html", "injectable"),
    ("HTTPS://A.TEST/X", "injectable"),
    ("file:///home/agent-host/x/y.html", "injectable"),
    ("about:config", "internal"),
    ("about:blank", "internal"),
    ("about:debugging#/runtime/this-firefox", "internal"),
    ("moz-extension://6f1d2c3a/sidebar.html", "internal"),
    ("chrome://browser/content/browser.xhtml", "internal"),
    ("devtools://devtools/bender/ha.html", "internal"),
    ("resource://gre/res/hidden.html", "internal"),
    ("blob:https://a.test/9a7b", "internal"),
    ("", "internal"),
])
def test_url_classifier(pc, url, want):
    assert pc(url) == want


def test_classifier_is_pure():
    src = _extract()
    assert "await" not in src and "browser." not in src and " log(" not in src, \
        "pageClass обязана оставаться чистой (её гоняет node без браузера)"


# ---- флоу самоизлечения (статика) ----------------------------------------------------

def test_rpc_to_tab_self_heals_on_failure():
    body = _body("rpcToTab")
    assert "browser.tabs.sendMessage" in body
    assert "selfHealRpc(id, tab, msg)" in body, "первый сбой больше не ведёт в самоизлечение"


def test_exactly_one_auto_retry():
    body = _body("selfHealRpc")
    assert body.count("browser.tabs.sendMessage") == 1, "автоповторов должен быть ровно один"
    assert "executeScript" in body, "инжект content.js пропал"
    assert not re.search(r"\b(for|while)\s*\(", body), "в самоизлечении появился цикл"
    assert "setTimeout" not in body and ".then(" not in body, "самоизлечение — только await"


def test_internal_check_precedes_inject():
    body = _body("selfHealRpc")
    assert body.index("ERR_INTERNAL_PAGE") < body.index("executeScript"), \
        "служебная страница должна опознаваться до попытки инжекта"


def test_action_do_goes_through_self_heal():
    body = _body("actionDoRpc")
    assert "selfHealRpc(id, tab, msg, true)" in body, "action.do обошёл самоизлечение"


def test_no_content_script_emission_gone():
    assert 'rpcResult(id, false, "no_content_script")' not in BG
    for line in BG.splitlines():
        assert "no_content_script" not in line, "no_content_script вернулся в код: " + line


def test_inject_is_content_js_in_isolated_world():
    body = _body("selfHealRpc")
    inj = re.findall(r"executeScript\(\{.*?\}\)(?=;)", body)
    assert len(inj) == 2, "ожидаем bootstrap-func + content.js files"
    boot = [x for x in inj if "func:" in x]
    assert len(boot) == 1
    assert "hermes-sidekick-host" in boot[0] and "__hermesSidekick = false" in boot[0], \
        "bootstrap обязан сбрасывать гард и хост"
    cont = [x for x in inj if "content.js" in x]
    assert len(cont) == 1 and 'files: ["content.js"]' in cont[0]
    assert "world" not in cont[0], "изолированный мир — дефолт, явный MAIN запрещён"
    assert not any("theme.css" in x for x in inj), \
        "theme.css оверлею не нужен (токены инлайнит content.js)"


# ---- форма ошибок (контракт v1 аддитивен) --------------------------------------------

def test_internal_page_error_shape():
    e = _err("ERR_INTERNAL_PAGE")
    assert e.startswith("internal_page"), "err обязан начиняться с машинного кода (аддитивно)"
    assert re.search(r"[а-я]{4,}", e), "в err нет человекочитаемого RU-сообщения"
    assert "Cmd+R" not in e, "SK-18: подсказка про перезагрузку — только при сбое инжекта"


def test_inject_failed_error_shape():
    e = _err("ERR_INJECT_FAILED")
    assert e.startswith("inject_failed")
    assert re.search(r"[а-я]{4,}", e)
    assert "Cmd+R" in e and "агрузи вкладку" in e, "подсказка «перезагрузи вкладку (Cmd+R)» обязательна"


def test_errors_are_err_strings_contract_v1():
    assert "rpcResult(id, false, ERR_INTERNAL_PAGE)" in BG
    assert "rpcResult(id, false, ERR_INJECT_FAILED)" in BG
    assert 'type: "rpc_error"' not in BG, "контракт v1: ошибки идут err-строкой rpc_result"


def test_log_anchors_for_live_harness():
    # живой харнесс атрибутирует исходы по этим подстрокам console-лога фоновой страницы
    for anchor in ("self-heal: healed", "internal page", "retry failed", "inject threw"):
        assert anchor in BG, "пропал якорь атрибуции харнесса: " + anchor


# ---- манифест и синтаксис --------------------------------------------------------------

def test_manifest_has_scripting_and_all_urls():
    assert "scripting" in MAN["permissions"]
    assert "<all_urls>" in MAN["host_permissions"]


def test_manifest_permission_set_frozen():
    # канонический пин набора — test_sidebar_open_sk19; здесь дубль-страховка self-heal
    # скоупа. Пополнение одно и то же: webNavigation (SK-44, pushState-свидетель),
    # notifications (SK-37, bell: позвать человека при свёрнутом сайдбаре).
    assert set(MAN["permissions"]) == {"tabs", "storage", "activeTab", "alarms", "scripting", "tabGroups", "webNavigation", "notifications"}


def test_background_js_parses():
    if not NODE:
        pytest.skip("node нет — синтаксис проверим на стенде")
    r = subprocess.run([NODE, "--check", str(ROOT / "extension" / "background.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
