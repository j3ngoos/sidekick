# Privacy Policy — Hermes Sidekick

**Add-on:** Hermes Sidekick (https://github.com/j3ngoos/sidekick)
**License:** MIT · **Last updated:** 2026-10-28

## English

Hermes Sidekick is a browser agent UI that works with a **local** WebSocket
bridge you run on your own machine. Its privacy stance is simple:

1. **No data collection.** The extension collects nothing and transmits
   nothing to the add-on author or to any third party. The manifest declares
   `"data_collection_permissions": { "required": ["none"] }`. There are no
   analytics, no telemetry, no crash reporting, no cookies set by the
   extension, no ad SDKs.
2. **No cloud.** The extension's only network peer is the bridge endpoint
   you configure yourself (default `ws://127.0.0.1:8791` — loopback on your
   own machine). The agent behind that bridge is your own software running
   on your own hardware. If you never configure a bridge, the extension
   stays in offline mode and talks to no one.
3. **Data at rest.** Settings (bridge URL, session token), the journal,
   screenshots and audit traces live only in the extension's local browser
   storage and on your own disk. They are uploaded nowhere. The bridge
   session token is never displayed in the UI and never leaves the
   extension.
4. **Website content.** With `<all_urls>` the extension can read and act on
   web pages — that is its core function, performed on your instruction and
   visible in the live step feed. Page content read during agent tasks is
   sent only to your own local bridge, never to the add-on author.
5. **Notifications** are rendered locally via the browser; no push service
   is involved.
6. **Deletion.** Removing the extension deletes its local storage
   (browser-standard behaviour). Files written by the bridge live under
   your chosen bridge directory and are yours to delete.
7. **Contact.** Security reports: see SECURITY.md in the repository.
   Questions: GitHub Issues.

## По-русски

Hermes Sidekick — интерфейс браузерного агента, работающий с **локальным**
WebSocket-мостом, который вы сами запускаете на своей машине.

1. **Никакого сбора данных.** Расширение ничего не собирает и никуда не
   передаёт — ни автору, ни третьим лицам. В манифесте заявлено
   `"data_collection_permissions": { "required": ["none"] }`. Нет аналитики,
   телеметрии, отчётов о сбоях, cookies от расширения, рекламных SDK.
2. **Никакого облака.** Единственный сетевой адресат расширения — адрес
   моста, который настроите вы (по умолчанию `ws://127.0.0.1:8791`,
   loopback вашей машины). Агент за мостом — ваше собственное ПО на вашем
   железе. Без настроенного моста расширение работает в офлайн-режиме.
3. **Данные покое.** Настройки (URL моста, токен сессии), журнал, скриншоты
   и следы аудита хранятся только в локальном хранилище расширения и на
   вашем диске. Токен сессии не показывается в интерфейсе и не покидает
   расширение.
4. **Содержимое сайтов.** С `<all_urls>` расширение читает и изменяет
   веб-страницы — это его основная функция, выполняемая по вашему
   поручению и видимая в ленте шагов. Прочитанное отправляется только в
   ваш локальный мост, никогда — автору расширения.
5. **Уведомления** рисует сам браузер, без push-сервисов.
6. **Удаление.** Удаление расширения стирает его локальное хранилище
   (стандартное поведение браузера). Файлы моста лежат в вашей папке моста.
7. **Контакты.** Уязвимости — SECURITY.md в репозитории; вопросы — GitHub
   Issues.
