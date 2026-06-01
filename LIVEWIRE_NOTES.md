# gotsms ЛК — реверс Livewire (для bulk-покупки до 25/раз)

Снято вживую через CDP-перехват на app.gotsms.org. Весь сайт — Laravel **Livewire 3**
(не REST). Цель: покупать до `maxQuantity` (=25 везде) номеров одним запросом, в обход
лимита публичного API (30 req/min).

## Транспорт
- Эндпоинт: `POST /livewire-{hash}/update` (hash меняется, берётся из HTML страницы)
- Заголовки: `X-Livewire: true`, CSRF (`<meta name="csrf-token">`), Cookie сессии
- Cookies: `gotsms_session`, `XSRF-TOKEN`
- Тело: `{"_token": "<csrf>", "components": [{"snapshot": "...", "updates": {...}, "calls": [...]}]}`
- snapshot подписан `memo.checksum` (HMAC сервера) — нельзя слепить вручную,
  только брать из предыдущего ответа (effects[].snapshot / wire:snapshot в HTML).

## Flow покупки (3 шага)
1. `GET /rents/create` → из HTML вытащить: csrf `_token`, livewire hash,
   snapshot компонентов `livewire-ui-modal` и `app.rent.pages.create-rent-page`.
   planId сервиса берётся из `@click`: `Livewire.dispatch('openModal', {component:'app.rent.modals.area-code-rent-modal', arguments:{planId:'...'}})`.
2. Открыть модалку — call к `livewire-ui-modal`:
   `{"method":"__dispatch","params":["openModal",{"component":"app.rent.modals.area-code-rent-modal","arguments":{"planId":"<UUID>"}}]}`
   → ответ содержит snapshot нового компонента `app.rent.modals.area-code-rent-modal`
   (поля: planId, quantity, maxQuantity, basePrice, totalPrice, selectedAreaCode...).
3. Купить — на компоненте `app.rent.modals.area-code-rent-modal`:
   - `updates: {"selectedAreaCode":"", "quantity": N}`  (N ≤ maxQuantity=25)
   - `calls: [{"method":"rent","params":[]}]`
   → покупает N номеров за ОДИН запрос.

## Логин (тоже Livewire)
- `GET /login` → csrf meta + cookies (XSRF-TOKEN, gotsms_session). input-полей в HTML нет —
  форма на Livewire-компоненте. Авто-логин = эмулировать login-компонент
  (snapshot + call login с email/password). TODO: снять точный компонент логина.

## Открытые вопросы
- ❓ Лимит на `/livewire/update` — режет ли так же 30/мин? (web-группа Laravel обычно без throttle).
  Это определяет итоговую ценность. Проверить на прототипе.
- Хрупкость: livewire hash, component names, snapshot-структура и checksum-логика
  меняются при обновлении сайта → модуль потребует периодической починки.

## Проверено
- maxQuantity = 25 (везде). Тестовая покупка 2×101Sweets прошла (method `rent`, quantity=2),
  оба номера рефанднуты через API `POST /api/rents/{id}/refund` (баланс вернулся).
