// SK-41: webdriver patch — ONLY when automation is actually active.
//
// The old version always replaced the getter on Navigator.prototype — and a page
// could see the tell: non-native toString on the getter and a lost enumerable.
// Now:
//   * the getter natively returns false (a normal browser with no Marionette/
//     WebDriver session) — we do NOT touch the prototype at all, no tells;
//   * the getter returned true (automation is active) — we replace it, keeping
//     the original's enumerable/configurable/set (descriptor attributes match the
//     native ones; the non-native getter toString stays a known residual tell —
//     patching Function.prototype.toString is riskier than the tell itself,
//     see ~/.hermes/notes/sidekick-stealth-2026-10.md).
// Runs at document_start in the MAIN world (content_scripts, world: MAIN).
(() => {
  try {
    const d = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
    if (!d || typeof d.get !== 'function') return;
    if (d.get.call(navigator) !== true) return; // not automated — leave the prototype alone
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => false,
      set: d.set,
      enumerable: d.enumerable,
      configurable: d.configurable,
    });
  } catch (e) {}
})();
