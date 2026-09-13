# SAFE MODE

SAFE MODE — консервативный режим Laptop Deal Watcher. Его задача — получать сигналы о новых объявлениях без постоянного прямого опроса Avito и прекращать прямой доступ при первом явном отклонении трафика. Это не механизм обхода антибот-защиты.

## Архитектура по умолчанию

Продакшен-путь теперь такой:

```text
Avito saved search / authorized external source
                    |
             email / JSONL
                    |
          deal_feed_runner.py
                    |
      normalize -> score -> SQLite
                    |
                 Telegram
```

`deal_feed_runner.py` сам не делает запросов к Avito. Он принимает уже возникшее событие объявления и использует существующие normalizer/scoring/storage/Telegram модули.

Поддержаны два входа:

- JSONL — универсальная точка интеграции для любого разрешённого внешнего источника;
- IMAP — извлечение ссылок на объявления из писем-уведомлений сохранённого поиска. Парсер письма работает локально и не разрешает redirect URL сетевым запросом.

FAST feed events могут породить Telegram-уведомление. MARKET feed events уведомления не отправляют и используются как допустимые наблюдения для динамического baseline.

## Рекомендуемый `config.toml`

```toml
[avito]
use_own_cookies = false
use_bypass_api = false
proxy_change_url = ""
parse_views = false
parse_phone = false
```

Основной Avito-аккаунт не должен передавать watcher'у cookies или данные авторизации. Если используются сохранённые поиски, watcher получает только уведомление через выбранный feed-канал и не автоматизирует вход в Avito.

## Настройки `deal_watcher.toml`

```toml
[safety]
enabled = true
require_anonymous = true
disable_enrichment_requests = true
stop_on_block = true
allow_direct_avito_requests = false
min_profile_interval_seconds = 900
max_requests_per_hour = 12
interval_jitter_ratio = 0.20
startup_spread_seconds = 900
cooldown_after_block_seconds = 86400
max_cooldown_after_block_seconds = 604800
block_backoff_multiplier = 2.0
block_statuses = [403, 429, 439]
```

Главная настройка — `allow_direct_avito_requests = false`. При ней `deal_watcher_runner.py` откажется запускать прямой Avito polling и укажет использовать `deal_feed_runner.py`.

## Feed configuration

Скопируйте:

```bash
cp deal_sources.toml.example deal_sources.toml
```

`deal_sources.toml` gitignored.

### JSONL

Минимальный event:

```json
{"id":1234567890,"title":"Lenovo Legion RTX 4070","price":99000,"url":"https://www.avito.ru/..._1234567890","profile":"fast-any-4070","mode":"fast"}
```

`mode="market"` записывает наблюдение в baseline без Telegram-алерта. `mode="fast"` участвует в обычном score/notify pipeline.

### IMAP

В `deal_sources.toml` задаются только host/port/folder и имена environment variables. Логин и пароль не должны храниться в TOML:

```bash
export AVITO_MAIL_USER='...'
export AVITO_MAIL_PASSWORD='...'
```

После этого:

```bash
python deal_feed_runner.py
```

Если текущий шаблон письма не содержит доступной прямой ссылки, title и цены, событие fail-closed пропускается с warning. В таком случае нужно адаптировать email parser по реальному сырому примеру письма, а не делать дополнительный запрос к карточке Avito.

## Legacy direct source

`deal_watcher_runner.py` сохранён только как диагностический/legacy источник. Чтобы включить его осознанно, необходимо вручную поставить:

```toml
allow_direct_avito_requests = true
```

При SAFE mode действуют жёсткие ограничения:

- один сетевой retry на попытку;
- не более 12 ожидаемых catalog page requests в rolling hour;
- минимум 15 минут между проходами одного профиля;
- отсутствие startup burst;
- MARKET запускаются после FAST;
- `one_time_start=true` проверяет только один FAST-профиль и одну страницу, а не все поиски;
- при `403`, `429` или `439` никаких автоматических cookies/IP rotations не выполняется.

## Persistent circuit breaker

Block state хранится в `database.db` в `deal_runtime_state` и переживает restart.

Первый явный block открывает circuit на 24 часа. Повторные блоки увеличивают cooldown экспоненциально: 24h -> 48h -> 96h и так далее, максимум до 7 суток. Успешно завершённый profile постепенно уменьшает block history.

Таким образом restart процесса или systemd не может случайно обнулить защиту и немедленно отправить новый запрос после 403/429/439.

## Почему прямой polling больше не является production default

В 2026 upstream parser неоднократно адаптировался к изменениям Avito, а летом пользователи сообщали о мгновенных 403 даже при специализированных proxy/cookie схемах. Первый smoke этого watcher также получил HTTP 439 на первом catalog request. Поэтому уменьшать интервал недостаточно: production acquisition не должен зависеть от устойчивости undocumented internal endpoint.

Feed-first схема сохраняет главное — быстрый scoring, deal thresholds, price history, dynamic baseline и Telegram — но разрывает зависимость между intelligence layer и способом получения объявления.
