"""SK-41: stealth webdriver-патч — ПОВЕДЕНЧЕСКИЙ тест в node + статические пины.

Главная претензия к прежней версии: она подменяла геттер ВСЕГДА, и сайт видел
перманентный след (non-native getter) даже в браузере без автоматизации.
Здесь прогоняем реальный код stealth.js в node с поддельным Navigator.prototype:
  A) webdriver=false (обычный пользователь) — прототип НЕ тронут вообще;
  B) webdriver=true (автоматизация) — подмена на false, атрибуты дескриптора
     (enumerable/configurable/set) сохранены как у оригинала.
"""
import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
STEALTH = (ROOT / "extension" / "stealth.js").read_text()

NODE_RUNNER = r"""
const fs = require("fs");
const mode = process.argv[1];              // "false" | "true"  (с -e индексы сдвинуты)
const src = fs.readFileSync(process.argv[2], "utf8");

// эмулируем нативный геттер: нативность помечаем символом + toString-меткой
const nativeTag = "function webdriver() {\n    [native code]\n}";
function makeNativeGetter(ret) {
  const g = function webdriver() { return ret; };
  g.toString = () => nativeTag;
  return g;
}
function Navigator() {}
const origGet = makeNativeGetter(mode === "true");
const origSet = function (v) {};
Object.defineProperty(Navigator.prototype, "webdriver",
  { get: origGet, set: origSet, enumerable: true, configurable: true });
const navigator = Object.create(Navigator.prototype);

eval(src);   // stealth document_start в MAIN world

const d = Object.getOwnPropertyDescriptor(Navigator.prototype, "webdriver");
console.log(JSON.stringify({
  value: navigator.webdriver,
  touched: d.get !== origGet,
  native_ts: String(d.get.toString()).includes("[native code]"),
  enumerable: d.enumerable,
  configurable: d.configurable,
  has_set: typeof d.set === "function",
}));
"""


def _run(mode: str) -> dict:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(STEALTH)
        path = f.name
    try:
        r = subprocess.run(["node", "-e", NODE_RUNNER, mode, path],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"node упал: {r.stderr[:300]}"
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        import os
        os.unlink(path)


def test_no_automation_prototype_untouched():
    """Пассивный пользователь: следов нет — прототип не тронут, webdriver нативен."""
    out = _run("false")
    assert out["touched"] is False, "stealth трогает Navigator.prototype без автоматизации — перманентный след!"
    assert out["value"] is False
    assert out["native_ts"] is True


def test_automation_patched_with_native_attrs():
    """Автоматизация активна: webdriver скрыт, дескриптор не выдаёт подмену атрибутами."""
    out = _run("true")
    assert out["touched"] is True, "при активном webdriver патч не сработал"
    assert out["value"] is False
    assert out["enumerable"] is True and out["configurable"] is True and out["has_set"] is True


# ---- статические пины ----------------------------------------------------------

def test_conditional_guard_present():
    assert "!== true" in STEALTH, "пропал условный гейтер (патч только при webdriver===true)"


def test_no_unconditional_patch_pattern():
    # прежний след: defineProperty без предварительной проверки значения геттера
    assert "if (!d) return;" not in STEALTH


def test_closed_shadow_and_lazy_host():
    js = (ROOT / "extension" / "content.js").read_text()
    assert 'host.attachShadow({ mode: "closed" })' in js, "shadow оверлея должен быть closed"
    assert 'mode: "open"' not in js
    # хост создаётся лениво: appendChild(host) вызывается из ensure(), а не на верхнем уровне
    assert "const $ = (id) => ensure().getElementById(id)" in js, "$ обязан идти через ensure()"
    top = js.split("\n")
    top_level_append = [ln for ln in top
                        if ln and not ln[0].isspace() and "appendChild(host)" in ln and not ln.lstrip().startswith("//")]
    assert not top_level_append, "appendChild(host) вернулся на верхний уровень — хост лезет в каждую загрузку"
