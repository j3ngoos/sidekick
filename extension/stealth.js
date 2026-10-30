// SK-41: webdriver-патч — ТОЛЬКО когда автоматизация реально активна.
//
// Прежняя версия всегда подменяла геттер на Navigator.prototype — и страница
// могла увидеть след: non-native toString у геттера и потерянный enumerable.
// Теперь:
//   * геттер нативно возвращает false (обычный браузер без сессии Marionette/
//     WebDriver) — прототип НЕ трогаем вообще, следов нет;
//   * геттер вернул true (автоматизация активна) — подменяем, сохраняя
//     enumerable/configurable/set оригинала (атрибуты дескриптора совпадают
//     с нативными; non-native toString геттера остаётся известным остаточным
//     следом — патч Function.prototype.toString рискованнее самого следа,
//     см. ~/.hermes/notes/sidekick-stealth-2026-10.md).
// Вызывается document_start в MAIN world (content_scripts, world: MAIN).
(() => {
  try {
    const d = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
    if (!d || typeof d.get !== 'function') return;
    if (d.get.call(navigator) !== true) return; // не автоматизированы — прототип не трогаем
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => false,
      set: d.set,
      enumerable: d.enumerable,
      configurable: d.configurable,
    });
  } catch (e) {}
})();
