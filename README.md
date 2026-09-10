# mmWMS

Своя WMS для MM-Express (фулфилмент WB FBS, Москва) — замена Odoo, интеграция с Wildberries внутри одной транзакции, вместо шлюза + четырёх очередей.

## Начать здесь

1. **[docs/00-master-context.md](docs/00-master-context.md)** — читает каждый, кто работает над проектом, целиком и первым. Архитектура as-is, диагностика боевого контура, физический процесс склада, решения владельца, целевая схема данных, инварианты.
2. **[docs/01-stream-0-contracts-and-stand.md](docs/01-stream-0-contracts-and-stand.md)** — поток 0: контракты, схема, стенд. Один исполнитель, первым, до старта остальных.
3. После потока 0 — три параллельных потока, каждый от `main`, каждый в своей ветке:
   - **[docs/02-stream-a-wms-wildberries.md](docs/02-stream-a-wms-wildberries.md)** → ветка `stream-a`
   - **[docs/03-stream-b-picker-app.md](docs/03-stream-b-picker-app.md)** → ветка `stream-b`
   - **[docs/04-stream-c-billing-accounts.md](docs/04-stream-c-billing-accounts.md)** → ветка `stream-c`

## Справочные материалы

- **[reference/wb-fbs-gateway-template/](reference/wb-fbs-gateway-template/)** — шаблон сервиса, снятый read-only с боевого `wb-fbs-gateway` (Dockerfile, зависимости, конфиг тестов, паттерны config/metrics/secrets). Для потока 0, пункт 8. Только чтение, не собирать.

## Стенд

Сервер `151.245.140.225` (8 vCPU / 32 ГБ / 240 ГБ NVMe, Ubuntu 24.04, Docker). Доступ только по SSH-ключу, пароль root отключён. Реквизиты — приложение F в `docs/00-master-context.md`.

## Правила

- Один поток — один каталог в `services/`. Чужие файлы не редактируются.
- Контракт (`services/wms/contracts/*`) после заморозки потоком 0 меняется только через поток 0.
- Ежедневный полный прогон (16 проверок, раздел 9.6 мастер-контекста) на стенде — красный блокирует мерж.
- Мерж в `main` — через интегратора, не напрямую между ветками потоков.

Полностью — раздел 9.5 `docs/00-master-context.md`.
