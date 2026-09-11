# SAFE MODE

SAFE MODE — консервативный режим Laptop Deal Watcher. Его задача — ограничивать объём запросов и прекращать работу при явном отклонении трафика, а не обходить защиту Avito.

## Рекомендуемый `config.toml`

```toml
[avito]
use_own_cookies = false
use_bypass_api = false
proxy_change_url = ""
parse_views = false
parse_phone = false
```

При `require_anonymous = true` watcher откажется стартовать, если включены `use_own_cookies`, `use_bypass_api` или ротация proxy через `proxy_change_url`.

При `disable_enrichment_requests = true` watcher также откажется стартовать с `parse_views = true` или `parse_phone = true`, потому что они создают дополнительные запросы к карточкам.

## Настройки `deal_watcher.toml`

```toml
[safety]
enabled = true
require_anonymous = true
disable_enrichment_requests = true
stop_on_block = true
min_profile_interval_seconds = 300
max_requests_per_hour = 120
interval_jitter_ratio = 0.25
startup_spread_seconds = 180
cooldown_after_block_seconds = 21600
block_statuses = [403, 429, 439]
```

### Jitter

`interval_jitter_ratio = 0.25` добавляет случайный разброс ±25% к базовому интервалу каждого профиля.

Например:

```toml
interval_seconds = 600
```

даёт следующий запуск примерно через 450–750 секунд (7.5–12.5 минут). Новый интервал вычисляется заново после каждого успешного прохода.

`startup_spread_seconds = 180` разносит первые запуски профилей во времени. MARKET получает первое окно старта, FAST — следующее, чтобы все запросы не уходили одним пакетом.

Jitter используется для сглаживания нагрузки, а не для имитации поведения человека.

## Глобальный бюджет

`max_requests_per_hour = 120` — консервативный rolling budget на ожидаемые запросы страниц каталога. Профиль с `pages = 3` резервирует три единицы бюджета на проход. Если бюджет исчерпан, запуск откладывается до освобождения окна плюс небольшой случайный запас.

В SAFE MODE `max_count_of_retry` для поисковых профилей принудительно ограничивается одним сетевым запросом на попытку.

## Реакция на блокировку

При HTTP `403`, `429` или `439` сетевой клиент поднимает `BlockedAccessError` и не запускает штатную upstream-логику смены cookies/IP.

Scheduler:

1. прекращает текущий профиль;
2. отправляет служебное Telegram-уведомление, если Telegram настроен;
3. ставит **все** поисковые профили на global cooldown;
4. по умолчанию ждёт 21 600 секунд (6 часов);
5. после cooldown снова разносит профили во времени через startup spread.

То есть SAFE MODE не делает цепочку «403 → новый IP/cookies → повтор».

## Рекомендуемые интервалы на старте

Для первой недели:

- FAST: `interval_seconds = 600`, `pages = 1`;
- MARKET: `interval_seconds = 43200`, `pages = 3`;
- 3 широких MARKET-профиля (RTX 4070/5060/5070);
- начать примерно с 6–10 FAST-профилей и расширять только если всё стабильно.

С ±25% jitter FAST с базовыми 10 минутами реально будет ходить примерно раз в 7.5–12.5 минут.

## Основной Avito-аккаунт

Основной аккаунт не должен использоваться watcher'ом: не переносить его cookies, не логиниться им на сервере и не включать `use_own_cookies`.

SAFE MODE снижает риск для основного профиля тем, что он вообще не участвует в работе watcher. Это не означает, что автоматизированный доступ становится официально разрешённым или что IP сервера не может получить ограничение.
