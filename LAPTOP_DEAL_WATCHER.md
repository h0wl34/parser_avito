# Laptop Deal Watcher

Надстройка над `Duff89/parser_avito` для поиска аномально выгодных ноутбуков на Avito.

Цель не в том, чтобы присылать каждое новое объявление. Watcher собирает текущий рынок, нормализует характеристики ноутбука, отсеивает подозрительные цены и отправляет в Telegram только объявления с высоким `deal score`.

## Что уже делает

- использует сетевой/антиблоковый слой upstream parser без переписывания;
- распознаёт бренд, серию, SKU, CPU, RTX GPU, RAM и SSD из title/description;
- классифицирует состояние: `NEW_CONFIRMED`, `NEW_LIKELY`, `UNKNOWN`, `USED`, `REFURBISHED`, `BROKEN`;
- ловит bait-фразы: trade-in, цена при кредите, первоначальный взнос, под заказ, после ремонта и т.д.;
- хранит объявления и историю цен в SQLite;
- строит рынок по цепочке `SKU+GPU -> family+GPU -> brand+GPU -> GPU`;
- в рыночной статистике учитывает только последнюю цену каждого объявления;
- исключает из baseline used/refurbished/broken и объявления с высоким risk score;
- реагирует на снижение цены уже известного объявления;
- поддерживает независимые FAST и MARKET профили;
- дедупликация идёт по `(profile, avito_id, price)`, поэтому MARKET не может «съесть» объявление у FAST;
- MARKET профили никогда не отправляют Telegram-уведомления;
- Telegram использует штатный notifier upstream и сохраняет фотографию объявления.

## Быстрый запуск

Установить зависимости upstream:

```bash
pip install -r requirements.txt
```

Проверить тесты:

```bash
python -m unittest discover -s tests -v
```

Настроить Telegram, cookies/proxy/SPFA и остальные общие параметры в обычном `config.toml` так же, как в upstream проекте.

Watcher-настройки находятся в `deal_watcher.toml`.

Запуск:

```bash
python deal_watcher_runner.py
```

Если файла `deal_searches.toml` нет, runner использует обычный список `urls` из `config.toml` и общий цикл upstream.

## FAST и MARKET

Скопировать шаблон:

```bash
cp deal_searches.toml.example deal_searches.toml
```

### MARKET

MARKET нужен для обучения текущей цене. Он молча собирает объявления в SQLite и ничего не отправляет в Telegram.

Рекомендуемая схема для начала:

- отдельный широкий поиск Москва + RTX 4070;
- Москва + RTX 5060;
- Москва + RTX 5070;
- 3–5 страниц;
- раз в 4–6 часов;
- `max_age_seconds` около 30 дней.

Пример:

```toml
[[search]]
name = "market-rtx4070-moscow"
url = "<готовый URL выдачи Avito>"
mode = "market"
interval_seconds = 21600
pages = 5
max_age_seconds = 2592000
enabled = true
```

### FAST

FAST — это боевые поиски. Сюда нужно вставлять готовые URL Avito с уже выставленными географией, категорией, запросом и при необходимости ценой.

Для нашей задачи логично иметь отдельные FAST-профили для Багратионовской и Савёловской по RTX 4070 / 5060 / 5070, а также несколько модельных выдач (`ThinkBook 16+`, `ThinkBook 16p`, `Legion`, `Zephyrus`, `TUF`).

Обычно достаточно первой страницы каждые 2–5 минут:

```toml
[[search]]
name = "fast-bagrationovskaya-4070"
url = "<точный URL, скопированный из браузера Avito>"
mode = "fast"
interval_seconds = 180
pages = 1
max_age_seconds = 3600
enabled = true
```

При старте scheduler сначала запускает MARKET-профили, затем FAST. Для каждого профиля сохраняется собственный parser instance, HTTP session и cookies provider.

Если один профиль падает, остальные продолжают работать; упавший профиль повторяется через не более чем 60 секунд.

## Deal score

Скоринг сейчас детерминированный, без LLM:

- отклонение от медианы рынка — до 40 баллов;
- абсолютная цена относительно GPU-порога — до 15;
- качество серии — примерно от -18 до +15;
- состояние — от -40 до +10;
- GPU/RAM/SSD — до 10;
- заметное снижение цены — бонус;
- bait/risk-флаги — штрафы.

Итог ограничивается диапазоном 0–100:

- `90–100`: `IMMEDIATE`;
- `80–89`: `STRONG`;
- `70–79`: `INTERESTING`;
- ниже 70: `IGNORE`.

По умолчанию Telegram-порог `notify_score = 80`.

Все веса находятся в `deal_watcher.toml` и могут меняться без правки Python-кода.

Текущие абсолютные GPU-пороги являются cold-start страховкой. После накопления достаточной выборки главным сигналом становится реальная медиана Avito.

## SQLite

Watcher использует тот же файл `database.db`, но свои таблицы:

- `deal_listings` — последнее состояние объявления и нормализованные характеристики;
- `deal_prices` — история цены;
- `deal_seen` — seen-state отдельно для каждого search profile.

Upstream-таблица `viewed` не меняется.

## Почему MARKET не портится дешёвыми фейками

В baseline не входят:

- `USED`;
- `REFURBISHED`;
- `BROKEN`;
- объявления с `risk_score > 10`.

То есть `RTX 4070 — 69 990 ₽ при оформлении кредита` может быть сохранён как наблюдение, но не потянет рыночную медиану вниз.

## Проверка перед реальным запуском

1. Заполнить `tg_token` и `tg_chat_id` в `config.toml`.
2. Настроить рабочий способ cookies/proxy по документации upstream.
3. Создать `deal_searches.toml` из шаблона и вставить реальные Avito URLs.
4. На первом запуске дать MARKET профилям пройти свои страницы.
5. Проверить `logs/app.log` и несколько первых Telegram-карточек.
6. После накопления данных откалибровать `notify_score`, series bonuses и absolute GPU thresholds.

## Тесты

Тесты покрывают:

- TUF / ThinkBook normalizer;
- CPU/GPU/RAM/SSD/SKU extraction;
- condition и bait detection;
- scoring выгодного и плохого объявления;
- market fallback;
- price drop;
- отсутствие overweight из-за price history одного объявления;
- исключение bait/used цен из baseline;
- независимый seen-state FAST/MARKET;
- загрузку и валидацию search profiles.
