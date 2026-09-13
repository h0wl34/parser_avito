# Laptop Deal Watcher

Надстройка над `Duff89/parser_avito` для поиска аномально выгодных ноутбуков. Intelligence layer отделён от acquisition layer: нормализация, риск, рынок, score, price history и Telegram больше не зависят от того, как именно получено объявление.

## Production architecture

Рекомендуемый путь теперь — **feed-first**:

```text
saved-search notification / webhook provider / authorized feed
                          |
                    webhook / email
                          |
                   durable JSONL spool
                          |
                  deal_feed_runner.py
                          |
             normalize -> risk -> score
                          |
                       SQLite
                          |
                      Telegram
```

В этом режиме сервер watcher не делает запросов к Avito вообще.

Поддержаны:

- signed Avigram webhook -> durable JSONL;
- generic JSONL events от любого внешнего источника;
- IMAP saved-search notification emails как fallback;
- старый direct Avito source сохранён только как явно включаемый legacy diagnostic.

## Что делает intelligence layer

- распознаёт бренд, серию, SKU, CPU, RTX GPU, RAM и SSD из title/description;
- классифицирует состояние: `NEW_CONFIRMED`, `NEW_LIKELY`, `LIKE_NEW`, `UNKNOWN`, `USED`, `REFURBISHED`, `BROKEN`;
- ловит bait/incomplete/catalog/refurbished признаки;
- хранит объявления и историю цен в SQLite;
- строит рынок по `SKU+GPU -> family+GPU -> brand+GPU -> GPU`;
- использует только последнюю цену каждого объявления;
- исключает used/like-new/refurbished/broken/high-risk из NEW baseline;
- различает `FAST` и `MARKET` observations;
- FAST-only данные не загрязняют MARKET baseline;
- дедуплицирует по `(profile, avito_id, price)`;
- реагирует на снижение цены;
- отправляет Telegram только при `score >= notify_score`.

## Quick start: local feed smoke test

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
cp deal_sources.toml.example deal_sources.toml
```

Оставьте в `deal_sources.toml`:

```toml
[jsonl]
enabled = true
path = "deal_feed.jsonl"
poll_seconds = 2
start_at_end = false

[webhook]
enabled = false

[imap]
enabled = false
```

Запустите:

```bash
python deal_feed_runner.py
```

В другом терминале добавьте тестовое событие:

```bash
cat >> deal_feed.jsonl <<'EOF'
{"id":1234567890,"title":"Lenovo Legion 5 RTX 4070 32GB 1TB","price":99000,"url":"https://www.avito.ru/test_1234567890","profile":"fast-any-4070","mode":"fast","source":"manual-smoke"}
EOF
```

Событие проходит тот же normalizer/scoring/storage pipeline, но без единого Avito HTTP request.

## High-speed webhook path

`deal_webhook_ingress.py` реализует durable ingress для документированного callback API Avigram.

В `deal_sources.toml`:

```toml
[jsonl]
enabled = true
path = "deal_feed.jsonl"
poll_seconds = 2
start_at_end = false

[webhook]
enabled = true
provider = "avigram"
bind_host = "127.0.0.1"
port = 8765
path = "/avigram-callback"
require_signature = true
secret_env = "AVIGRAM_CALLBACK_SECRET"
max_skew_seconds = 300
max_body_bytes = 1048576
market_name_prefixes = ["market-", "market_", "market "]
```

Секрет хранится только в environment:

```bash
export AVIGRAM_CALLBACK_SECRET='...'
```

Запускаются два процесса:

```bash
python deal_webhook_ingress.py
python deal_feed_runner.py
```

Ingress проверяет HMAC `timestamp + "." + rawBody`, нормализует payload и **сначала fsync'ит событие в JSONL**, только после этого отвечает `202`. Telegram и scoring работают во втором процессе, поэтому временный сбой downstream не заставляет callback ждать и не теряет уже подтверждённое событие.

По умолчанию ingress слушает только `127.0.0.1`; для внешнего callback перед ним нужен обычный HTTPS reverse proxy. Не публикуйте Python HTTP server напрямую в интернет.

### FAST / MARKET через webhook

Имя поиска у provider становится `profile`.

Рекомендуемые имена:

```text
fast-any-4060
fast-any-5060
fast-any-4070
fast-any-5070
market-new-4060
market-new-5060
market-new-4070
market-new-5070
```

Если search name начинается с `market-`, событие получает `mode="market"`: оно записывается в baseline и не создаёт Telegram alert. Остальные поиски считаются FAST.

Это позволяет сохранить исходную идею из 8 поисков, но сам watcher больше не выполняет 16 catalog requests на startup.

## Generic JSONL contract

Минимальный формат:

```json
{
  "id": 1234567890,
  "title": "Lenovo Legion RTX 4070",
  "price": 99000,
  "url": "https://www.avito.ru/..._1234567890",
  "profile": "fast-any-4070",
  "mode": "fast"
}
```

Дополнительно принимаются `description`, `seller_id`, `source`, `published_at`.

`mode="market"` означает silent baseline observation. `mode="fast"` означает alert candidate.

## IMAP fallback

Если используются обычные email notifications сохранённых поисков, включите `[imap]` в `deal_sources.toml`. Credentials задаются environment variables `AVITO_MAIL_USER` / `AVITO_MAIL_PASSWORD` или другими именами из config.

Email parser fail-closed: он создаёт event только если может достоверно извлечь listing URL/id и цену из самого письма. Он не ходит по redirect и не открывает карточку объявления для «досбора» данных. Если реальный шаблон письма отличается, адаптируйте parser по сырому примеру письма.

## Legacy direct Avito source

`deal_watcher_runner.py` больше не является production default.

SAFE config содержит:

```toml
allow_direct_avito_requests = false
```

Поэтому попытка прямого запуска завершится до сетевого запроса и предложит `deal_feed_runner.py`.

Для отдельной диагностической проверки можно осознанно временно поставить `true`. Тогда работают дополнительные ограничения:

- один request retry;
- максимум 12 catalog-page units / hour;
- минимум 15 минут на профиль;
- startup spread;
- FAST before MARKET;
- `one_time_start=true` запускает только **один** FAST profile / одну страницу;
- HTTP 403/429/439 открывает persistent circuit breaker;
- block state хранится в SQLite и переживает restart;
- cooldown 24h -> 48h -> 96h ... максимум 7 суток;
- никакой автоматической смены IP/cookies или CAPTCHA bypass.

## Deal score

Скоринг детерминированный:

- рыночная аномалия — до 40;
- абсолютный GPU threshold — до 15;
- bonus/penalty серии;
- condition;
- GPU/RAM/SSD;
- price drop;
- risk flags.

Итог:

```text
90-100 IMMEDIATE
80-89  STRONG
70-79  INTERESTING
<70    IGNORE
```

По умолчанию `notify_score = 80`.

Текущие cold-start GPU thresholds:

```text
RTX 4060  95 000
RTX 5050  90 000
RTX 5060 110 000
RTX 4070 100 000
RTX 5070 125 000
RTX 4080 125 000
```

## SQLite

Основные watcher tables:

- `deal_listings` — последнее состояние объявления;
- `deal_prices` — price history;
- `deal_seen` — profile-scoped dedupe;
- `deal_runtime_state` — persistent direct-source circuit breaker.

`baseline_eligible=1` получают только MARKET observations.

## Security

- `config.toml`, `deal_sources.toml`, `deal_feed.jsonl`, databases и logs gitignored;
- Telegram notifier proxy credentials маскируются в config logs;
- webhook secret и IMAP password берутся из environment;
- HMAC webhook timestamp ограничен default skew 5 минут;
- webhook body ограничен 1 MiB;
- ingress ACK отправляется только после durable append;
- feed notifications не скачивают Avito image URL, чтобы не создавать скрытый дополнительный Avito request.

Подробнее: `SAFE_MODE.md`.
