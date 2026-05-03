# Playerok Universal Contract Gaps

## Цель
Фиксация несовпадений между DDCRM worker v2 контрактом и текущим surface API `playerok-universal`.

## Текущий статус

1. `products.create` и `products.update` требуют provider-specific payload:
   - DDCRM контракт не описывает жёстко поля Playerok для создания/обновления лота.
   - Для рабочей интеграции в `playerok.item.v1` адаптер ожидает:
     - `attributes.gameCategoryId` (обязательно для create),
     - `attributes.obtainingTypeId` (опционально; если не задан, берётся первый доступный в категории),
     - `attributes.options` (опционально, объект `field -> value` или массив `{field,value}`),
     - `attributes.dataFields` (опционально, объект `fieldId -> value` или массив `{fieldId,value}`),
     - `attributes.removeAttachmentIds` / `attributes.addAttachmentPaths` (опционально для update).
   - Статус: `partial` (реализовано через соглашение в `attributes`, но это не формализовано в OpenAPI схемой строго).

2. `products.update.status`:
   - Унифицированный контракт допускает смену `status`.
   - `playerok-universal` не предоставляет простой универсальный status-transition в рамках `update_item` (для publish требуется отдельный flow с приоритетом/оплатой).
   - Статус: `deferred` (адаптер возвращает `WORKER_RUNTIME_CONFLICT` при попытке изменить status).

3. `conversations.messages.send` attachments:
   - DDCRM контракт допускает `attachments`.
   - В `playerok-universal` вложения отправляются через upload локального файла перед `send_message`.
   - Текущая реализация `send` поддерживает только текст; attachments допускаются только в `products` media path в виде локальных путей.
   - Статус: `deferred`.

4. `products.list` полнота каталога:
   - В API Playerok получение товаров пользователя идёт через профиль пользователя и курсоры.
   - Полный набор полей товара иногда требует дополнительного `get_item` на каждый элемент (дороже по latency).
   - Статус: `partial` (адаптер обогащает элементы по возможности, но SLA/latency зависит от размера каталога).

5. Auth contract детализация:
   - DDCRM глобально разрешает `marketplaceAuth.scheme` (`golden_key|cookies|tokens|login_password`).
   - Для этого worker поддерживаются только `tokens` и `cookies`.
   - `tokens` требует `token` + `ddg5`; `cookies` требует `cookies`.
   - Статус: `partial` (ограничение валидируется runtime-ом воркера и зафиксировано здесь).

6. Worker account binding model:
   - Текущий API worker (`/internal/v2/worker/account|conversations|products`) не передаёт `accountId` в запросе.
   - Адаптер работает в single-tenant модели (один контейнер на один DDCRM account) и читает auth/прокси по bound account scope.
   - Binding фиксируется через env (`PLAYEROK_WORKER_ACCOUNT_ID`/`DDCRM_WORKER_ACCOUNT_ID`) или на первом `ext.account.*.apply`.
   - Статус: `partial` (single-tenant закрыт в v1, multi-tenant read-scope остаётся deferred).

7. Product metadata helper (`ext.playerok.products.metadata`):
   - Для улучшения UX формы товара worker предоставляет extension-action с метаданными:
     - `games`, `categories`, `options`, `obtainingTypes`, `dataFields`.
   - API `playerok-universal` не гарантирует консистентность/доступность metadata в каждый момент (network/rate-limit), поэтому UI обязан иметь graceful fallback на ручной ввод provider-полей.
   - Статус: `implemented-with-fallback` (action реализован, но metadata не является hard dependency для `products.create/update`).
