# Laptop Deal Watcher — production runbook

Этот документ описывает production-схему feed-first watcher без прямого polling Avito.

## 1. Компоненты

Основной сервер:

- `deal_feed_runner.py` — durable queue -> normalizer -> scoring -> Telegram;
- `deal_webhook_ingress.py` — подписанный webhook -> SQLite queue;
- `deal_health_monitor.py` — low-noise health/incident monitor;
- `deal-relay-tunnel.service` — persistent SSH local-forward до emergency relay на VPS;
- `database.db` — listings, prices, queue, incidents и outbox.

VPS с Amnezia/SOCKS:

- SOCKS5 используется обычным deal Telegram notifier;
- `deal_alert_relay.py` работает **вне SOCKS-контейнера** и отправляет системные алерты в Telegram напрямую;
- `deal_external_watchdog.py` с VPS проверяет основной сервер снаружи.

Короткий crash лечит systemd. Health alert создаётся только после debounce/grace window.

## 2. Failure domains

### Обычный deal Telegram

Основной сервер -> SOCKS5 на VPS -> Telegram.

### Системный alert при падении SOCKS

Основной сервер -> SSH local-forward -> relay на VPS host -> Telegram напрямую.

Relay слушает только `127.0.0.1:8770` на VPS и не требует публичного порта. На основном сервере туннель поднимает `127.0.0.1:18770 -> VPS 127.0.0.1:8770`.

Для relay рекомендуется отдельный Telegram bot token. Тогда неправильный/заблокированный основной bot token не ломает системные алерты.

### Основной сервер умер целиком

VPS -> public `/healthz` основного ingress -> отдельный system bot.

Если умер **сам VPS целиком**, SOCKS, relay и VPS-watchdog пропадут вместе. Для покрытия этого failure domain нужен второй независимый host/uptime monitor; тот же VPS физически не может сообщить о собственной полной смерти.

## 3. Main server

```bash
cd ~/parser_avito
source .venv-avito/bin/activate
git checkout feat/laptop-deal-watcher
git pull --ff-only
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Все тесты должны завершиться `OK`.

Создать локальный env:

```bash
cp deploy/deal-watcher.env.example .env.deal-watcher
chmod 600 .env.deal-watcher
nano .env.deal-watcher
```

В production нужны:

```text
AVIGRAM_CALLBACK_SECRET=<random secret>
DEAL_ALERT_RELAY_URL=http://127.0.0.1:18770/alert
DEAL_ALERT_RELAY_SECRET=<same relay secret as VPS>
DEAL_RELAY_SSH_HOST=<VPS IP/host>
DEAL_RELAY_SSH_USER=root
DEAL_RELAY_SSH_KEY=/home/<user>/.ssh/deal_relay_ed25519
DEAL_RELAY_LOCAL_PORT=18770
DEAL_RELAY_REMOTE_PORT=8770
```

Секрет удобно сгенерировать:

```bash
openssl rand -hex 32
```

### SSH key для relay tunnel

На основном сервере:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/deal_relay_ed25519 -N '' -C 'deal-watcher-relay'
ssh-copy-id -i ~/.ssh/deal_relay_ed25519.pub root@<VPS>
```

Проверить именно non-interactive login:

```bash
ssh -i ~/.ssh/deal_relay_ed25519 -o BatchMode=yes root@<VPS> 'echo RELAY_SSH_OK'
```

Перед systemd один раз установить host key обычным SSH или `ssh-keyscan` с ручной проверкой fingerprint; tunnel использует `StrictHostKeyChecking=yes`.

### systemd --user

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/deal-feed.service ~/.config/systemd/user/
cp deploy/systemd/deal-webhook.service ~/.config/systemd/user/
cp deploy/systemd/deal-health.service ~/.config/systemd/user/
cp deploy/systemd/deal-relay-tunnel.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

Сначала поднять tunnel:

```bash
systemctl --user enable --now deal-relay-tunnel.service
curl http://127.0.0.1:18770/healthz
```

Ожидание:

```json
{"status":"ok"}
```

После настройки реального webhook:

```bash
systemctl --user enable --now deal-feed.service deal-webhook.service deal-health.service
```

Чтобы user services жили после logout/reboot:

```bash
sudo loginctl enable-linger "$USER"
```

Проверка:

```bash
systemctl --user status deal-relay-tunnel deal-feed deal-webhook deal-health --no-pager
journalctl --user -u deal-relay-tunnel -u deal-feed -u deal-webhook -u deal-health -n 200 --no-pager
```

## 4. VPS relay/watchdog

На VPS не нужен полный watcher runtime/Playwright. Достаточно лёгкого venv:

```bash
cd ~/parser_avito
python3 -m venv .venv-relay
.venv-relay/bin/python -m pip install 'requests==2.32.3' 'loguru==0.7.0'
```

Создать env:

```bash
cp deploy/deal-relay.env.example .env.deal-relay
chmod 600 .env.deal-relay
nano .env.deal-relay
```

Нужны:

```text
DEAL_RELAY_SECRET=<same random secret as main server>
DEAL_RELAY_BOT_TOKEN=<separate system-alert bot token>
DEAL_RELAY_CHAT_ID=<your chat id>
DEAL_RELAY_BIND_HOST=127.0.0.1
DEAL_RELAY_PORT=8770
DEAL_WATCHDOG_URL=https://watcher.example.com/healthz
DEAL_WATCHDOG_INTERVAL=30
DEAL_WATCHDOG_FAILURES=3
DEAL_WATCHDOG_REPEAT=21600
```

Установка user services:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/deal-alert-relay.service ~/.config/systemd/user/
cp deploy/systemd/deal-external-watchdog.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now deal-alert-relay.service deal-external-watchdog.service
sudo loginctl enable-linger "$USER"
```

Relay остаётся host-level process и **не помещается в Amnezia/Docker failure domain**.

## 5. Public ingress / TLS

Публичным должен быть только webhook/health endpoint основного watcher, потому что внешний provider и VPS watchdog должны достучаться до него.

Пример reverse proxy на основном сервере:

```text
https://watcher.example.com/avigram-callback -> 127.0.0.1:8765/avigram-callback
https://watcher.example.com/healthz          -> 127.0.0.1:8765/healthz
```

Emergency relay **не публикуется**: он доступен основному серверу только через SSH local-forward.

Рекомендуется firewall/reverse-proxy ACL:

- `/avigram-callback` доступен provider'у, но защищён HMAC callback secret;
- main `/healthz` по возможности разрешён только с IP VPS;
- Python port `8765` не выставлять в Internet напрямую, если используется reverse proxy;
- VPS relay `8770` оставлять на `127.0.0.1`.

## 6. Low-noise health policy

Default `deal_watcher.toml`:

- health check раз в 30 s;
- обычный failure открывает incident только после >=3 плохих наблюдений и >=120 s;
- reminder активной проблемы — максимум раз в 6 h;
- recovery -> одно `RECOVERED`;
- stale alarm, который не удалось доставить до recovery, не отправляется задним числом;
- dead-letter >=1 — CRITICAL сразу;
- oldest pending >10 min — CRITICAL;
- pending >=25 продолжительное время — WARNING;
- disk <1 GiB — WARNING, <256 MiB — CRITICAL;
- SQLite `quick_check` — раз в час;
- Telegram Bot API probe через normal SOCKS — раз в минуту;
- emergency relay probe через SSH tunnel — раз в 5 min.

Health-monitor следит за heartbeat worker/ingress, а не за частотой новых объявлений. Тишина на рынке не является аварией.

## 7. Обязательные fault tests после deployment

### Короткий worker crash — без спама

```bash
systemctl --user kill deal-feed.service
```

`Restart=always` должен поднять service примерно через 5 s. Системного alert быть не должно.

### Неисправимый worker failure

```bash
systemctl --user stop deal-feed.service
```

Через несколько минут должен прийти один `CRITICAL [feed_worker]`. После:

```bash
systemctl --user start deal-feed.service
```

должен прийти один `RECOVERED`.

### SOCKS outage

На VPS временно остановить именно SOCKS/Amnezia service/container, не emergency relay.

Ожидание:

1. обычный Telegram path перестаёт проходить;
2. одиночные failures молчат;
3. persistent failure открывает `CRITICAL [telegram_path]`;
4. сообщение приходит через SSH tunnel -> emergency relay -> system bot;
5. после восстановления SOCKS приходит `RECOVERED`.

### SSH tunnel/relay outage

При рабочем основном Telegram остановить tunnel на main server:

```bash
systemctl --user stop deal-relay-tunnel.service
```

Health-monitor через persistent probes должен прислать WARNING обычным Telegram. После запуска tunnel — RECOVERED.

Отдельно можно остановить relay на VPS:

```bash
systemctl --user stop deal-alert-relay.service
```

Результат должен быть тем же.

### Whole main endpoint outage

На основном сервере:

```bash
systemctl --user stop deal-webhook.service
```

VPS external watchdog после нескольких плохих checks должен отправить system-bot CRITICAL напрямую. После запуска ingress — RECOVERED.

## 8. Queue diagnostics

```bash
python deal_queue_admin.py stats
python deal_queue_admin.py dead
```

После исправления причины dead-letter можно вернуть событие:

```bash
python deal_queue_admin.py retry-dead <event-key-or-prefix>
```

## 9. Real source / Avigram

Названия внешних searches должны совпадать с watcher profile names.

FAST:

- `fast-any-4060`
- `fast-any-5060`
- `fast-any-4070`
- `fast-any-5070`

MARKET:

- `market-new-4060`
- `market-new-5060`
- `market-new-4070`
- `market-new-5070`

`market-...` автоматически становится silent baseline event. FAST может вызвать deal alert.

Перед оплатой постоянного плана сначала использовать trial и измерить реальную задержку:

```text
listing published -> provider callback -> SQLite queue -> Telegram
```

Нужно измерять минимум несколько десятков событий в разное время суток. Если provider окажется медленным/ненадёжным, intelligence/queue/storage/health менять не нужно: заменяется только source adapter.

## 10. Docker decision

Текущая production-схема намеренно host-native/systemd для аварийного пути.

Допустимая следующая итерация:

- `deal_feed_runner.py`, `deal_webhook_ingress.py`, `deal_health_monitor.py` -> один Docker image + Compose services;
- SQLite/config -> bind mounts/volume на локальном filesystem;
- `deal-relay-tunnel.service` остаётся host-level systemd;
- VPS `deal_alert_relay.py` и `deal_external_watchdog.py` остаются host-level systemd и вне Amnezia/Docker failure domain.

Не помещать emergency relay в тот же Docker daemon/Compose, что и SOCKS: падение Docker daemon тогда уничтожит и основной, и аварийный Telegram paths одновременно.

## 11. Security invariants

- основной Avito account/cookies watcher'у не передавать;
- `allow_direct_avito_requests=false` оставлять в production;
- secrets только в gitignored env/config;
- callback/relay HMAC secrets разные от Telegram bot tokens;
- для system alerts предпочтителен отдельный Telegram bot;
- SSH key для relay tunnel отдельный от обычных administrative keys;
- после случайного появления token/password в log/chat credential ротировать.
