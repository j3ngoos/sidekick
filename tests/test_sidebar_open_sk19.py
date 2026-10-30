"""SK-19 контрактные тесты — повторное открытие сайдбара (иконка + клавиатура).

Манифестный контракт: команда _execute_action есть (у action нет popup — только
так Firefox ведёт её в action.onClicked), open_at_install сохранён, НОВЫХ пермиссий
нет. Фоновая нога: onClicked слушатель есть, зовёт sidebarAction.open() и только
его (toggle запрещён — «клик закрывает» ровно ломает UX). Живое поведение
(реальный клик по иконке, Alt+Shift+S, идемпотентность) — харнесс стенды
sk19_worker.py, не pytest.
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


# ---- манифест: команда + идемпотентный вход -----------------------------------------

def test_execute_action_command_declared():
    cmd = MAN["commands"]["_execute_action"]
    assert cmd["suggested_key"]["default"] == "Alt+Shift+S"
    assert re.search(r"Hermes Sidekick", cmd["description"]), "описание команды обязано называть расширение"


def test_action_has_no_popup():
    # с popup'ом _execute_action открыл бы popup, а onClicked не сработал бы вовсе
    assert "popup" not in MAN["action"], "popup у action запрещён: он глушит onClicked"
    assert "default_popup" not in MAN.get("browser_action", {}), "MV3: popup-легаси запрещён"


def test_open_at_install_kept():
    assert MAN["sidebar_action"]["open_at_install"] is True, "п.3 спеки: автооткрытие при установке сохраняем"


def test_no_new_permissions():
    # команды — манифестная декларация без пермиссий; набор заморожен со времён SK-18,
    # плановые пополнения: webNavigation (SK-44: pushState-свидетель, без него
    # tabs.onUpdated не видит SPA-навигации и ложный tab_moved на SPA) и
    # notifications (SK-37: bell — нативное уведомление о пермиссе/задаче, когда
    # сайдбар свёрнут; ТЗ пользователя 22.10)
    assert set(MAN["permissions"]) == {"tabs", "storage", "activeTab", "alarms", "scripting", "tabGroups", "webNavigation", "notifications"}
    assert MAN["host_permissions"] == ["<all_urls>"]


# ---- фоновая нога: onClicked -> open() -----------------------------------------------

def _onclicked_block():
    m = re.search(r"browser\.action\.onClicked\.addListener\(\(\)\s*=>\s*\{(.*?)\}\);", BG, re.S)
    assert m, "слушатель browser.action.onClicked пропал из background.js"
    return m.group(1)


def test_onclicked_opens_sidebar():
    block = _onclicked_block()
    assert "browser.sidebarAction.open()" in block, "onClicked обязан звать sidebarAction.open()"


def test_no_toggle_anywhere():
    assert "sidebarAction.toggle" not in BG, "toggle запрещён: повторный клик закрывал бы панель"
    assert "sidebarAction.close" not in BG, "close из onClicked — тот же баг «клик закрывает»"


def test_open_failure_is_swallowed():
    assert ".catch(" in _onclicked_block(), "отказ open() не должен становиться unhandled rejection"


def test_manifest_is_valid_json_and_js_parses():
    json.loads((ROOT / "extension" / "manifest.json").read_text())
    if not NODE:
        pytest.skip("node нет — синтаксис проверим на стенде")
    r = subprocess.run([NODE, "--check", str(ROOT / "extension" / "background.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
