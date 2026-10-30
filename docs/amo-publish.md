# Публикация Sidekick в Firefox Add-ons Store (AMO)

Пошаговый маршрут для владельца аккаунта. Всё, что можно было подготовить
без аккаунта, подготовлено карточкой SK-68: артефакты в `dist/`, манифест
пройден `web-ext lint` с 0 ошибок, тексты ниже готовы к вставке.
Факты о требованиях AMO сверены с официальными страницами
(extensionworkshop.com: submitting-an-add-on, add-on-policies,
source-code-submission, FAQ, firefox-builtin-data-consent) и с исходным
кодом сервера AMO (лимиты полей).

## Артефакты

| Файл | Байты | Что это |
|---|---|---|
| `dist/sidekick-0.2.xpi` | 143 722 | устанавливаемое расширение (web-ext build, 19 файлов) |
| `dist/sidekick-0.2-source.xpi` | 151 638 | zip исходников для AMO: `extension/` + `README.md` + `LICENSE` (21 файл, без node_modules/.env/hub-runtime — секрет-греп 0) |

`package.json` в репо нет — расширение не имеет npm-зависимостей и шагов
сборки, поэтому архив исходников = сам код. Это прямо соответствует
требованию AMO «source code package + build instructions»: инструкции —
`cp -r extension/`.

Версия в манифесте: `0.2` — валидна для AMO (формат «числа через точку»,
хвостов нет). Для каждого нового загрузки версию нужно поднимать.

## 1. Аккаунт разработчика (делает только пользователь)

1. Открыть <https://addons.mozilla.org/developers/> → войти через
   **Firefox Account** (Mozilla-аккаунт; e-mail подтверждается).
2. При первой записи — профиль разработчика + согласие с
   **Firefox Add-ons Distribution Agreement** (обязательное окно).
   Ключи API (для подписи, раздел 6) создаются там же:
   <https://addons.mozilla.org/developers/keys>.
3. Витрина AMO не анонимная: имя разработчика и (по настройке) e-mail
   публикуются в листинге. E-mail в профиле можно скрыть — листинг тогда
   ссылается на форму AMO.

## 2. Загрузка

Форма: <https://addons.mozilla.org/developers/submit/>

1. **Desktop Browser for Firefox** → **Extension**.
2. Поднять файл: `dist/sidekick-0.2.xpi`. Валидатор AMO прогонит те же
   проверки, что `web-ext lint` (канал amo). Предупреждения (см. раздел 7)
   не блокируют загрузку; ошибки — блокируют (у нас 0).
3. **Платформы: только Desktop.** Android не отмечать: `sidebar_action`
   и локальный мост — десктопные, на Android расширение неработоспособно.
4. **Source code: Yes** → `dist/sidekick-0.2-source.xpi`.
   AMO требует исходники на каждую подачу (политика 3.1; FAQ: «Yes,
   especially if minified/transpiled» — у нас код чистый JS, архив есть).
5. **Describe Add-on** — поля ниже.
6. **Submit Version** → письмо-подтверждение, статус «Awaiting Review».

## 3. Поля листинга (лимиты проверены по коду сервера AMO)

- **Name:** `Hermes Sidekick` (≤50; должно совпадать с `name` манифеста —
  совпадает, иначе листинг получает варнинг сверки).
- **Summary** (≤250, ссылки запрещены полем NoURLsField) — готовый текст
  (243 байта):

> Browser agent inside your Firefox: an AI assistant that visits sites on your behalf via your own local bridge — reads, clicks, fills forms — with per-action permission gates, a live step feed, and a local journal. No cloud, no data collection.

- **Description** (≤15 000): вставить готовый текст раздела 5 — он короче
  лимита и содержит обязательные раскрытия (permissions, stealth,
  локальность). Полный README.md (15 721 B) в лимит не влезает.
- **Experimental:** отметить (версия 0.2, мост не публикуется как
  независимый продукт — листинг с пониженной видимостью, честный статус).
  Снять при 1.0.
- **Requires payment / external paid service:** **No** (софт бесплатный;
  мост — инфраструктура пользователя).
- **Categories (до 2):** `Utility` + `How-to & Tips`. Категория
  «Productivity» в AMO отсутствует — ближайшие по смыслу эти.
- **Tags:** `automation`, `sidebar`, `ai`, `agent`, `privacy`,
  `developer`.
- **Support email:** ваш (публикуется в листинге; скрыть можно настройкой
  профиля). **Support website:** `https://github.com/j3ngoos/sidekick`.
- **License:** Custom → вставить текст MIT из `LICENSE` (в репо канон
  MIT 1078 B, правообладатель-плейсхолдер «Sidekick contributors»).
- **Privacy policy: Yes** → `https://github.com/j3ngoos/sidekick/blob/master/docs/PRIVACY.md`
  (репо публичное). По форме AMO политика обязательна при передаче данных
  наружу; мы не передаём ничего, но для расширения с широкими
  разрешениями публичная политика — требование практики ревью и
  recommended practice AMO (хостить на AMO не обязательно — FAQ).
- **Data collection (consent questionnaire):** **No data is collected.**
  Манифест декларирует `"data_collection_permissions": {"required":
  ["none"]}` — каноническое декларации «нет сбора» (Firefox built-in
  consent docs). На Firefox 139–144 ключ игнорируется (сбора нет и там).
- **Notes for Reviewers** — вставить раздел 6.

## 4. Обоснования разрешений (N разрешений → N причин)

Host-permissions: ровно одно — `<all_urls>` (+ тот же паттерн в
`content_scripts.matches`). Никаких отдельных доменов нет.

1. **host_permissions `<all_urls>`** — Sidekick — агент: по поручению
   пользователя он работает на произвольных сайтах (чтение DOM, формы,
   клики, навигация). Функция невозможна с узким списком хостов. Весь
   обмен идёт в мост на localhost пользователя; внешних серверов у
   расширения нет (privacy policy).
2. **content_scripts `<all_urls>`** — `content.js` = руки агента на любой
   странице задания (SoM-метки, действия, пермисс-гейты); `stealth.js`
   (document_start, MAIN world) — см. раскрытие в разделе 5/6.
3. **tabs** — заголовок/URL вкладок для журнала, привязки «задача →
   вкладка» и счётчика вкладок агента.
4. **activeTab** — агент действует во вкладке, активированной пользователем
   (жест тулбара Alt+Shift+S).
5. **storage** — настройки (адрес моста, токен) и локальный журнал; всё в
   browser.storage.local, никуда не уходит.
6. **alarms** — переподключение к мосту, heartbeat, таймауты сессий
   (60 с молчания = пауза агента).
7. **scripting** — динамическая инъекция `stealth.js`/скриптов действий во
   вкладки агента (manifest-файлы из самого XPI, без удалённого кода).
8. **tabGroups** — группировка вкладок агента, чтобы человек видел его
   рабочие вкладки отдельной группой.
9. **webNavigation** — отслеживание навигаций (SPA-смены URL, back/forward)
   для ленты шагов и инвалидации состояния вкладок.
10. **notifications** — локальные уведомления о завершённых/проваленных
    задачах и пермисс-запросах; рисует их ОС/браузер, без push-сервисов.

## 5. Готовый Description (EN, вставлять в поле Description)

```
Hermes Sidekick puts an AI browser agent inside your Firefox.

WHAT IT DOES
You run your own local WebSocket bridge (default ws://127.0.0.1:8791)
and your own agent on your own machine. The extension gives the agent
hands in your browser: it opens tabs, reads pages, clicks, fills forms
and navigates — on your instruction, as your delegate. Every sensitive
class of action (passwords, cards, payments, OAuth, 2FA, sending text)
passes a human permission gate; a 60-second silence is an automatic
refusal; a Stop button and a login handoff (during handoff the agent
cannot read the page) are always available. The sidebar shows a live
step feed, a full journal, and the connection status.

PRIVACY & LOCALITY
No cloud, no accounts, no analytics, no data collection (the manifest
declares data_collection_permissions: required ["none"]). The only
network peer of the extension is the bridge endpoint you configure —
loopback by default. Settings, journal, screenshots and audit traces
stay in your browser storage and on your disk. Removing the extension
deletes its local data. Privacy policy: the repository, docs/PRIVACY.md.

WHY <all_urls> AND THESE PERMISSIONS
The agent works on any site you task it with — reading DOM, acting on
forms, navigating — so site access cannot be narrowed to a domain
list. tabs/activeTab/webNavigation/tabs state feed the journal and the
task-tab binding; scripting injects the extension's own shipped scripts
(never remote code); tabGroups isolates agent tabs; alarms drive
reconnects and session timeouts; notifications are rendered locally.
There is no eval() and no new Function anywhere in the code; the
bridge exchanges JSON commands from a fixed set, not code.

STEALTH FEATURE DISCLOSURE (No Surprises)
When the browser is being driven through WebDriver/Marionette (i.e.
when the agent is automating it), pages can see navigator.webdriver =
true and block your own agent as a "bot". stealth.js replaces that
flag with false ONLY in that case: on normal browsing (native getter
returns false) the Navigator prototype is not touched at all. The
patch is 15 lines, fully readable in source, and is disclosed here
because it is a core part of "an agent that visits sites for you".

GETTING STARTED
1. Run the bridge (source and docs: github.com/j3ngoos/sidekick).
2. Open the Sidekick sidebar and set the bridge URL + session token.
3. Without a bridge the extension stays in a clear offline mode:
   the sidebar opens, the journal works, connection state is shown.

Status: version 0.2, experimental. MIT licensed. Source:
https://github.com/j3ngoos/sidekick
```

## 6. Готовые Notes for Reviewers (EN)

```
- The extension is the browser-side UI of a self-hosted agent stack.
  The WebSocket bridge (github.com/j3ngoos/sidekick, MIT) is a plain
  Python daemon on the user's machine; it is NOT required for install:
  out of the box the extension targets ws://127.0.0.1:8791, finds no
  bridge, and stays in an explicit offline mode (sidebar opens, status
  shown, journal renders). All UI is functional offline.
- Source package = the extension/ directory verbatim + README/LICENSE.
  There are no build steps and no npm dependencies; to reproduce the
  XPI from the source: zip the extension/ directory.
- navigator.webdriver stealth (stealth.js, 15 lines): active ONLY when
  the browser is under WebDriver/Marionette automation (the agent's own
  automation). On normal browsing the prototype is untouched. Disclosed
  in the public description (No Surprises).
- No remote code: zero eval()/new Function; scripting.executeScript
  injects only manifest-shipped files; the bridge sends JSON actions
  from a fixed command set. CSP: script-src 'self'.
- The extension declares data_collection_permissions: required ["none"].
- Lint: web-ext lint 10.8.0 → 0 errors; remaining 6 warnings are
  explained below (2 innerHTML assignments use static template strings
  /a whitelisted icon map; 2 APIs are feature-guarded onLocationChange
  behind `if (browser.webNavigation.onLocationChange)` and
  getPermissionLevel inside try/catch; the min-version key warnings
  refer to data_collection_permissions, which older Firefox ignores).
- Support: https://github.com/j3ngoos/sidekick
```

## 7. web-ext lint: 0 ошибок, 7 предупреждений (краткая расшифровка)

| Код | Где | Суть / что делать |
|---|---|---|
| UNSAFE_VAR_ASSIGNMENT (innerHTML) | content.js:122 | присваивается статическая константа `TEMPLATE` (литерал этого же файла) — безопасно, оставить |
| UNSAFE_VAR_ASSIGNMENT (innerHTML) | sidebar.js:216 | значение берётся из захардкоженного whitelist-мапа `BELL_KIND_ICO`, юзер-данные не попадают — безопасно |
| UNSUPPORTED_API notifications.getPermissionLevel | background.js:1077 | вызов внутри `try/catch` (Chrome-специфичный API, на Firefox gracefully) |
| UNSUPPORTED_API webNavigation.onLocationChange ×2 | background.js:1164–1165 | за feature-check `if (browser.webNavigation.onLocationChange)` |
| KEY_FIREFOX_UNSUPPORTED_BY_MIN_VERSION | manifest | `data_collection_permissions` введён в Firefox 145; на 139–144 ключ игнорируется (не сбор, ключ для будущей обязательности AMO) |
| KEY_FIREFOX_ANDROID_UNSUPPORTED_BY_MIN_VERSION | manifest | то же для Android — нас не касается: подаём Desktop-only |
| (было) MISSING_DATA_COLLECTION_PERMISSIONS | manifest | ИСПРАВЛЕНО в SK-68: добавлен канон `"required": ["none"]` |

## 8. Сроки, статусы, частые отказы

- **Сроки.** AMO не публикует регламентных сроков. Практика 2025–2026 по
  тредом discourse («Stuck in review», апрель 2026): первичное ревью
  нового листинга — недели, очередь; самоподписанная (unlisted) подпись —
  минуты. Планируйте запас ≥ 2–4 недели на первую приёмку listed.
- **Статусы:** Unlisted → Awaiting Review → Approved; после приёмки
  возможно повторное выборочное ревью. Апелляции — веб-форма из
  developer hub.
- **Частые причины отказов и как они закрыты у нас:**
  1. *Незапрошенное/необъявленное-collection (политика 6.1)* — сбора нет,
     манифест декларирует none; скриншоты/журнал только на диске юзера.
  2. *Скрытые функции (No Surprises 1.1)* — stealth раскрыт в
     Description + Notes; включается только при активной автоматизации.
     Остаточный риск: AMO чувствителен к anti-bot тематике (известны
     прецеденты дискейба с формулировкой «Deceptive») — формулировки
     раздела 5 максимально буквальны; решение о тоне принимает панель.
  3. *Избыточные разрешения* — минимизм: ни одного домена сверх all_urls,
     8 API-разрешений, все с функцией (раздел 4).
  4. *Нет privacy policy* — docs/PRIVACY.md + ссылка в листинге.
  5. *Удалённый код* — нет eval/new Function; CSP script-src 'self'.
  6. *Неревьюируемые исходники* — чистый JS без сборки, source zip +
     инструкция «cp -r extension/».
  7. *Несовпадение имени листинга и манифеста* — Name == manifest name.

## 9. Self-hosted обновления (путь вне листинга AMO)

- **Listed на AMO:** обновления обслуживает AMO; `update_url` в манифесте
  быть не должно (для AMO-загрузки это ошибка валидатора).
- **Самохостинг (unlisted):**
  1. Создать API-ключ: <https://addons.mozilla.org/developers/keys>
     (Create API Key; secret показывается один раз) — делает пользователь.
  2. Подписать (плейсхолдеры; ключи — только в переменных текущего
     шелла, в дерево/env не сохранять):
     `export AMO_API_KEY="<KEY>" AMO_API_SECRET="<SECRET>"`
     `npx -y web-ext sign -s extension --api-key "$AMO_API_KEY" --api-secret "$AMO_API_SECRET" --artifacts-dir dist/signed`
  3. Для автообновлений добавить в манифест
     `browser_specific_settings.gecko.update_url` на свой update manifest
     (на AMO-listed подаче этот ключ убирать) и хостить `.xpi` +
     `updates.json` (формат: MDN Add-ons/Updates).

## 10. После приёмки

- Ссылка листинга: `https://addons.mozilla.org/firefox/addon/<slug>/`.
- Превью: 5 скриншотов `docs/images/*.png` (прошли PII/OCR-аудит SK-64 —
  в пикселях только 127.0.0.1/192.168.0.50).
- Релизы: поднимать `version` в манифесте, пересобирать, грузить XPI +
  source на страницу аддона (не новую подачу).
