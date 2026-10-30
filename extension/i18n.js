// Hermes Sidekick — i18n core (SK-71). Shared by sidebar/options pages, the
// background worker (listed first in manifest background.scripts) and content
// scripts (listed first in content_scripts.js). One dictionary, two locales:
// en (default) and ru. No language-detection library: uiLang setting
// "en"|"ru"|"system" -> navigator language when "system".
//
// Loading rules per context:
//  * pages + background worker: fetch(chrome.runtime.getURL("locales/x.json"));
//  * content scripts: cannot fetch extension resources (web-accessible hole),
//    so they ask the background ("i18n_get") and re-ask on "i18n_changed".
// Language change: options writes storage.uiLang -> background sees
// storage.onChanged, reloads its own dictionary and broadcasts
// "i18n_changed" to every tab; pages reload the pack in-place and call their
// onLang hook (sidebar re-renders the feed without a page reload).
var I18N = (function () {
  "use strict";

  var LANGS = { en: 1, ru: 1 };
  var DEFAULT_LANG = "en";           // SK-71: new installs open in English
  var KEY = "uiLang";                // storage key (options switcher writes it)
  var LOCALES = {                  // UI tag for Intl formatting per language
    en: "en-US",
    ru: "ru-RU",
  };

  var lang = DEFAULT_LANG;
  var dict = {};                   // active locale pack
  var fallback = {};               // en pack (missing keys resolve here)
  var readyP = null;

  function resolve(pref) {
    if (LANGS[pref]) return pref;
    if (pref === "system") {
      var nav = (typeof navigator !== "undefined" && navigator.language) || "";
      var base = String(nav).toLowerCase().split("-")[0];
      return LANGS[base] ? base : DEFAULT_LANG;
    }
    return DEFAULT_LANG;
  }

  function fetchPack(l) {
    return fetch(browser.runtime.getURL("locales/" + l + ".json"))
      .then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; });
  }

  // Pages and the background worker load dictionaries from the extension
  // package. Content scripts use initFromRuntime (they cannot fetch).
  function load() {
    if (readyP) return readyP;
    readyP = Promise.resolve()
      .then(function () {
        var get;
        if (typeof browser !== "undefined" && browser.storage && browser.storage.local) {
          get = browser.storage.local.get(KEY).catch(function () { return {}; });
        } else {
          get = Promise.resolve({});
        }
        return get;
      })
      .then(function (st) {
        lang = resolve(st && st[KEY]);
        return Promise.all([fetchPack(lang), lang === "en" ? null : fetchPack("en")]);
      })
      .then(function (p) {
        dict = p[0] || {};
        fallback = p[1] || p[0] || {};
      });
    return readyP;
  }

  // Content script bootstrap: the background owns the packs (content cannot
  // fetch extension URLs), so ask it and subscribe to changes.
  function initFromRuntime() {
    if (readyP) return readyP;
    readyP = browser.runtime.sendMessage({ type: "i18n_get" })
      .then(function (r) {
        if (r && r.lang) {
          lang = r.lang;
          dict = r.dict || {};
          fallback = r.fallback || {};
        }
        return null;
      })
      .catch(function () { return null; });
    return readyP;
  }

  function pick(key) {
    if (Object.prototype.hasOwnProperty.call(dict, key)) return dict[key];
    if (Object.prototype.hasOwnProperty.call(fallback, key)) return fallback[key];
    return null;
  }

  // t("feed.thought", {d: "12 sec"}) -> "Thought (12 sec)".
  // {{param}} placeholders; a missing translation yields the key (visible in
  // the harness => greppable, never a silent empty string).
  function t(key, args) {
    var s = pick(key);
    if (s === null) return key;
    if (args) {
      s = s.replace(/\{\{(\w+)\}\}/g, function (_, name) {
        return Object.prototype.hasOwnProperty.call(args, name) ? String(args[name]) : "";
      });
    }
    return s;
  }

  function has(key) { return pick(key) !== null; }
  function getLang() { return lang; }
  function locale() { return LOCALES[lang] || LOCALES.en; }

  // Static markup: data-i18n="key" (text), data-i18n-title,
  // data-i18n-placeholder, data-i18n-aria (aria-label).
  function apply(root) {
    var r = root || (typeof document !== "undefined" ? document : null);
    if (!r || !r.querySelectorAll) return;
    var m = [
      ["[data-i18n]", function (el, k) { el.textContent = t(k); }],
      ["[data-i18n-title]", function (el, k) { el.setAttribute("title", t(k)); }],
      ["[data-i18n-placeholder]", function (el, k) { el.setAttribute("placeholder", t(k)); }],
      ["[data-i18n-aria]", function (el, k) { el.setAttribute("aria-label", t(k)); }],
    ];
    for (var i = 0; i < m.length; i++) {
      var list = r.querySelectorAll(m[i][0]);
      for (var j = 0; j < list.length; j++) m[i][1](list[j], list[j].getAttribute("data-i18n") || list[j].getAttribute("data-i18n-title") || list[j].getAttribute("data-i18n-placeholder") || list[j].getAttribute("data-i18n-aria"));
    }
    if (r.documentElement) r.documentElement.lang = lang;
  }

  // Answer content scripts (background context only).
  function answerGet(sendResponse) {
    load().then(function () {
      sendResponse({ lang: lang, dict: dict, fallback: fallback });
    });
    return true; // async
  }

  // Broadcast a language change to every tab's content scripts.
  function broadcast() {
    if (typeof browser === "undefined" || !browser.tabs) return;
    browser.tabs.query({}).then(function (tabs) {
      for (var i = 0; i < tabs.length; i++) {
        browser.tabs.sendMessage(tabs[i].id, { type: "i18n_changed" }).catch(function () {});
      }
    }).catch(function () {});
  }

  // storage -> reload packs in this context; `changed` fires in pages that
  // want to re-render (sidebar). Background additionally broadcasts to tabs.
  function watchStorage(changed) {
    if (typeof browser === "undefined" || !browser.storage) return;
    browser.storage.onChanged.addListener(function (ch, area) {
      if (area !== "local" || !ch[KEY]) return;
      readyP = null;
      load().then(function () {
        if (changed) changed(lang);
        // SK-71: no context gate here — in the MV3 event page of Firefox the
        // old gate never fired, so content scripts kept a stale dictionary
        // until reload. Every context with tabs access broadcasts; content
        // re-pulls are idempotent (i18n_get).
        broadcast();
      });
    });
  }

  // Content scripts: language changed -> re-ask the background and notify.
  function watchRuntime(changed) {
    browser.runtime.onMessage.addListener(function (m) {
      if (m && m.type === "i18n_changed") {
        readyP = null;
        initFromRuntime().then(function () { if (changed) changed(lang); });
      }
    });
  }

  return {
    KEY: KEY,
    load: load,
    initFromRuntime: initFromRuntime,
    t: t, has: has,
    getLang: getLang, locale: locale,
    apply: apply,
    answerGet: answerGet,
    watchStorage: watchStorage,
    watchRuntime: watchRuntime,
  };
})();

// Classic scripts (background worker, content scripts) share ONE global lexical
// scope, so the L() alias lives here exactly once: pages that load this file
// first resolve L from the global. `var` (not const) keeps it assignment-safe
// across script boundaries; module pages (sidebar/options) bind their own alias.
var L = function (k, a) {
  return (typeof I18N !== "undefined" && I18N.t) ? I18N.t(k, a) : k;
};

// Node contract harnesses (feed.js/audit.js patterns) never load this file;
// pages/background/content call I18N.load()/initFromRuntime() at startup.
if (typeof module !== "undefined" && module.exports) module.exports = I18N;
