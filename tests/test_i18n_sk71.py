#!/usr/bin/env python3
"""SK-71 i18n: словари en/ru, live-переключатель, ноль кириллицы в литералах UI.

Статика: паритет словарей, параметризованные плейсхолдеры, все ссылки на ключи
из кода/разметки резолвятся, литералы с кириллицей запрещены (кроме эндемонима
«Русский»), manifest/_locales целостны, EN-паки feed.js/audit.js зеркалят
en.json, порядок загрузки i18n.js.

Node-юнит: i18n.js — разрешение языка (en/ru/system), fallback на en,
{{param}}, apply() по data-i18n*, ключ key наружу не отдаёт.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"

EN = json.loads((EXT / "locales" / "en.json").read_text(encoding="utf-8"))
RU = json.loads((EXT / "locales" / "ru.json").read_text(encoding="utf-8"))
CYR = re.compile(r"[\u0400-\u04FF]")

# Эндемонимы/языковые подписи живут в разметке намеренно: в языке-селекторе
# «Русский» подписан самим собой (так делают все языковые переключатели).
ALLOWED_CYR = {"Русский", "opt.lang_ru"}


def _js_files():
    return [p for p in EXT.glob("*.js")]


def _js_literals(src):
    """Строковые литералы JS после вырезания комментариев (прецедент инвентаря sk71)."""
    s = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    s = re.sub(r"(?m)//[^\n]*", " ", s)
    for m in re.finditer(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\]|\\.)*`', s, re.S):
        yield m.group(0)


def test_dictionaries_parity():
    en_k, ru_k = set(EN), set(RU)
    assert not (en_k - ru_k), f"ключей нет в ru.json: {sorted(en_k - ru_k)[:10]}"
    assert not (ru_k - en_k), f"ключей нет в en.json: {sorted(ru_k - en_k)[:10]}"
    for k in en_k:
        assert EN[k].strip(), f"{k}: пустое EN-значение"
        assert RU[k].strip(), f"{k}: пустое RU-значение"
        ph_en = set(re.findall(r"\{\{(\w+)\}\}", EN[k]))
        ph_ru = set(re.findall(r"\{\{(\w+)\}\}", RU[k]))
        assert ph_en == ph_ru, f"{k}: набор параметров расходится: {ph_en} vs {ph_ru}"


def test_no_cyrillic_in_js_literals():
    """UI-строки живут ТОЛЬКО в locales/*.json: кириллица в js-литералах — баг."""
    bad = []
    for p in _js_files():
        for lit in _js_literals(p.read_text(encoding="utf-8")):
            inner = lit[1:-1]
            if CYR.search(inner) and inner not in ALLOWED_CYR:
                bad.append(f"{p.name}: {inner[:80]!r}")
    assert not bad, "кириллица в литералах: " + " | ".join(bad[:10])


def test_no_cyrillic_in_html_text_and_attrs():
    """Статический текст/тултипы html — через data-i18n (EN-разметка)."""
    bad = []
    for p in EXT.glob("*.html"):
        s = p.read_text(encoding="utf-8")
        s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
        for m in re.finditer(r">([^<>]+)<", s):
            txt = m.group(1).strip()
            if CYR.search(txt) and txt not in ALLOWED_CYR:
                bad.append(f"{p.name} text: {txt[:60]!r}")
        for m in re.finditer(r'(title|placeholder|aria-label|alt)="([^"]*)"', s):
            if CYR.search(m.group(2)) and m.group(2) not in ALLOWED_CYR:
                bad.append(f"{p.name} {m.group(1)}: {m.group(2)[:60]!r}")
    assert not bad, "кириллица в html: " + " | ".join(bad[:10])


def test_every_referenced_key_exists():
    """L("k")/I18N.t("k")/t("k")/data-i18n* ссылаются только на существующие ключи."""
    refs = set()
    for p in _js_files() + list(EXT.glob("*.html")):
        s = p.read_text(encoding="utf-8")
        if p.suffix == ".js":
            for m in re.finditer(r'\b(?:L|I18N\.t|t)\(\s*"([a-z][a-z0-9_]*\.[A-Za-z0-9_.]+)"', s):
                refs.add(m.group(1))
        else:
            for m in re.finditer(r'data-i18n(?:-[a-z]+)?="([a-z][a-z0-9_]*\.[A-Za-z0-9_.]+)"', s):
                refs.add(m.group(1))
    # мапы «структура -> ключ» (THEME_LBL, BELL_ACTION_RU, PREVIEW_VERBS): значения — ключи
    for p2 in _js_files():
        s2 = p2.read_text(encoding="utf-8")
        for m2 in re.finditer(r':\s*"([a-z][a-z0-9_]*\.[A-Za-z0-9_.]+)"', s2):
            refs.add(m2.group(1))
    missing = sorted(k for k in refs if k not in EN)
    assert not missing, f"несуществующие ключи словаря: {missing[:10]}"
    assert len(refs) > 150, f"подозрительно мало ссылок на словарь ({len(refs)}) — i18n выветрился?"


def test_feed_audit_builtin_packs_mirror_en():
    """Чистые модули feed.js/audit.js несут EN-пак для node-контрактов — зеркало en.json."""
    for fname, prefix in (("feed.js", "feed."), ("audit.js", "aud.")):
        src = (EXT / fname).read_text(encoding="utf-8")
        blk = re.search(r"EN_PACK = \{(.*?)\};", src, re.S)
        assert blk, f"{fname}: блок EN_PACK не найден"
        pairs = re.findall(r'"(' + re.escape(prefix) + r'[A-Za-z0-9_.]+)"\s*:\s*"((?:[^"\\]|\\.)*)"', blk.group(1))
        assert pairs, f"{fname}: собственный EN-пак исчез"
        for k, v in pairs:
            assert k in EN, f"{fname}: ключ {k} нет в en.json"
            want = EN[k].replace('"', '\\"')
            assert v == want, f"{fname}: {k} расходится с en.json"


def test_manifest_locale_wiring():
    man = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert man.get("default_locale") == "en"
    msgs = json.loads((EXT / "_locales" / "en" / "messages.json").read_text(encoding="utf-8"))
    placeholders = []
    for field in (man.get("name"), man.get("description"),
                  (man.get("sidebar_action") or {}).get("default_title"),
                  (man.get("action") or {}).get("default_title"),
                  *(c.get("description") for c in (man.get("commands") or {}).values())):
        if isinstance(field, str) and field.startswith("__MSG_"):
            placeholders.append(field[len("__MSG_"):-2])
    assert placeholders, "manifest не использует __MSG__ (название/описание интернационализируемы)"
    for ph in placeholders:
        assert ph in msgs, f"_locales/en: нет сообщения {ph}"
        ru_msgs = json.loads((EXT / "_locales" / "ru" / "messages.json").read_text(encoding="utf-8"))
        assert ph in ru_msgs, f"_locales/ru: нет сообщения {ph}"
    # i18n.js обязан грузиться раньше потребителей
    assert man["background"]["scripts"][0] == "i18n.js"
    for cs in man["content_scripts"]:
        if "content.js" in cs["js"]:
            assert cs["js"][0] == "i18n.js", "content_scripts: i18n.js не первым перед content.js"
        if "stealth.js" in cs["js"]:
            # SK-14: stealth живёт в MAIN-мире и не несёт UI-строк — словарь ему не нужен
            assert "i18n.js" not in cs["js"], "MAIN-мир не должен тянуть расширение внутрь страницы"


def test_pages_load_i18n_first():
    for page, consumer in (("sidebar.html", "sidebar.js"), ("options.html", "options.js")):
        s = (EXT / page).read_text(encoding="utf-8")
        i_i18n, i_cons = s.find('src="i18n.js"'), s.find(f'src="{consumer}"')
        assert i_i18n != -1, f"{page}: i18n.js не подключён"
        assert i_i18n < i_cons, f"{page}: i18n.js подключается после {consumer}"


def test_options_switcher_present():
    """Переключатель языка в options: select#lang + запись uiLang + live-применение."""
    h = (EXT / "options.html").read_text(encoding="utf-8")
    js = (EXT / "options.js").read_text(encoding="utf-8")
    assert 'id="lang"' in h
    assert 'value="en"' in h and 'value="ru"' in h and 'value="system"' in h
    assert "Русский" in h, "RU подписан эндемонимом (языковые селекторы так делают)"
    assert re.search(r'uiLang', js), "options.js не пишет uiLang"
    assert "I18N" in js, "options.js не трогает I18N (live-переключение)"


def test_sidebar_renders_live_on_language_change():
    s = (EXT / "sidebar.js").read_text(encoding="utf-8")
    assert "I18N.watchStorage" in s, "сайдбар не слушает смену языка"
    assert "Feed.setT" in s, "лента не переключается на новый словарь"


def test_content_scripts_wired_for_live():
    c = (EXT / "content.js").read_text(encoding="utf-8")
    assert "I18N.initFromRuntime()" in c
    assert "I18N.watchRuntime()" in c, "контент не подхватывает смену языка живьём"
    b = (EXT / "background.js").read_text(encoding="utf-8")
    assert '"i18n_get"' in b and "I18N.answerGet" in b, "фон не отвечает контенту за словарём"


def test_language_broadcast_not_gated_by_getviews():
    """Регресс live-switch (приёмка I5b): в MV3 event-page getViews пуст —
    broadcast обязан идти без этого гейта, иначе контент молчит до перезагрузки."""
    core = (EXT / "i18n.js").read_text(encoding="utf-8")
    watch = core[core.index("function watchStorage"):core.index("function watchRuntime")]
    assert "broadcast();" in watch, "watchStorage не зовёт broadcast"
    assert "getViews" not in watch, "гейт getViews убивает рассылку в MV3 (Firefox)"


# ---- node-юнит ядра i18n ---------------------------------------------------------

NODE_I18N = r"""
// i18n.js в node: браузерные API заглушены, fetch читает настоящие locales/*.json
const fs = require("fs");
const EN_JSON = JSON.parse(fs.readFileSync(__ENPATH__, "utf8"));
const RU_JSON = JSON.parse(fs.readFileSync(__RUPATH__, "utf8"));
const PREF = __PREF__;
global.browser = {
  storage: {
    local: { get: () => Promise.resolve(PREF ? { uiLang: PREF } : {}) },
    onChanged: { addListener() {} },
  },
  runtime: {
    getURL: (p) => "moz-extension://x/" + p,
    sendMessage: () => Promise.resolve(null),
    onMessage: { addListener() {} },
    getViews: () => [],
  },
  tabs: { query: () => Promise.resolve([]) },
};
global.fetch = (u) => Promise.resolve({
  ok: true,
  json: () => Promise.resolve(u.endsWith("ru.json") ? RU_JSON : EN_JSON),
});
const I18N = require(__CORE__);
I18N.load().then(() => {
  const out = {};
  out.lang = I18N.getLang();
  out.locale = I18N.locale();
  out.new_chat = I18N.t("sbt.new_chat");
  out.tagline = I18N.t("sbt.tagline");
  out.param = I18N.t("feed.thought", { d: "12 sec" });
  out.missing = I18N.t("no.such.key.at.all");
  // apply: статическая разметка из словаря
  const mk = (attrs) => ({
    attrs: Object.assign({}, attrs), text: "",
    getAttribute(k) { return this.attrs[k] !== undefined ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = v; },
    set textContent(v) { this.text = v; }, get textContent() { return this.text; },
  });
  const span = mk({ "data-i18n": "sbt.new_chat" });
  const btn = mk({ "data-i18n-title": "sbt.t_send" });
  const inp = mk({ "data-i18n-placeholder": "sbt.ph_composer" });
  const aria = mk({ "data-i18n-aria": "sbt.aria_send" });
  const doc = {
    documentElement: {},
    querySelectorAll(sel) {
      if (sel === "[data-i18n]") return [span];
      if (sel === "[data-i18n-title]") return [btn];
      if (sel === "[data-i18n-placeholder]") return [inp];
      if (sel === "[data-i18n-aria]") return [aria];
      return [];
    },
  };
  I18N.apply(doc);
  out.applied = [span.textContent, btn.attrs.title, inp.attrs.placeholder, aria.attrs["aria-label"]];
  out.langAttr = doc.documentElement.lang;
  console.log(JSON.stringify(out));
}).catch((e) => { console.log(JSON.stringify({ fatal: String(e && e.stack || e) })); process.exit(1); });
"""


def _run_i18n_node(pref, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не в PATH")
    harness = tmp_path / "h.js"
    harness.write_text(
        NODE_I18N.replace("__ENPATH__", json.dumps(str(EXT / "locales" / "en.json")))
        .replace("__RUPATH__", json.dumps(str(EXT / "locales" / "ru.json")))
        .replace("__CORE__", json.dumps(str(EXT / "i18n.js")))
        .replace("__PREF__", json.dumps(pref)),
        encoding="utf-8")
    r = subprocess.run([node, str(harness)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"node упал: {r.stderr[:300]}"
    return json.loads(r.stdout)


def test_i18n_core_default_english(tmp_path):
    out = _run_i18n_node(None, tmp_path)
    assert out["lang"] == "en" and out["locale"] == "en-US"
    assert out["new_chat"] == EN["sbt.new_chat"]
    assert out["tagline"] == EN["sbt.tagline"]
    assert out["param"] == EN["feed.thought"].replace("{{d}}", "12 sec")
    assert out["missing"] == "no.such.key.at.all", "ключ наружу отдаётся как есть (greppable)"
    assert out["applied"] == [EN["sbt.new_chat"], EN["sbt.t_send"],
                              EN["sbt.ph_composer"], EN["sbt.aria_send"]]
    assert out["langAttr"] == "en"


def test_i18n_core_russian_pref(tmp_path):
    out = _run_i18n_node("ru", tmp_path)
    assert out["lang"] == "ru" and out["locale"] == "ru-RU"
    assert out["new_chat"] == RU["sbt.new_chat"]
    assert out["param"] == RU["feed.thought"].replace("{{d}}", "12 sec")
    assert out["missing"] == "no.such.key.at.all", "fallback тоже не должен терять ключ"


def test_i18n_core_system_pref(tmp_path):
    # node не имеет navigator -> system обязан безопасно падать в en
    out = _run_i18n_node("system", tmp_path)
    assert out["lang"] == "en"
    assert out["new_chat"] == EN["sbt.new_chat"]
