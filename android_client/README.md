# Avito Push Bridge (Android)

Tiny Android client for the Laptop Deal Watcher. It does **not** query Avito, does
not use Avito cookies, and does not automate the Avito UI. It only receives the
notifications Android already delivered to the official Avito app through
`NotificationListenerService`.

## What it does

1. Watches only explicitly configured Android package names (default:
   `com.avito.android`).
2. Captures useful notification fields plus safe primitive values from
   `Notification.extras`; Bitmap/RemoteViews/other binary Parcelable internals
   are deliberately not serialized.
3. Durably stores each event in the app's private SQLite database before any
   network operation.
4. Uses WorkManager to deliver pending events when the network is available.
5. Signs the **exact UTF-8 request body** with
   `HMAC-SHA256(secret, timestamp + "." + rawBody)`.
6. Server retries are idempotent because `event_id` is deterministic and unique.
7. The shared secret is encrypted with an AES key held in Android Keystore.
8. Only HTTPS endpoints are accepted; clear-text traffic is disabled in the
   Android network security config.

Until we see real Avito notification payloads, the server stores these events as
raw phone notifications instead of guessing how to convert them into
`ListingCandidate`. This is intentional.

## Open and build

Open **this `android_client/` directory** in Android Studio (not the repository
root) and let Gradle sync. The project uses:

- Android Gradle Plugin 8.10.1
- Gradle 8.11.1
- JDK 17
- compileSdk 36 / targetSdk 35 / minSdk 26
- WorkManager 2.11.2

The repository does not vendor the binary Gradle wrapper JAR. `gradlew` and
`gradlew.bat` bootstrap the official Gradle 8.11.1 wrapper JAR on first use and
verify its published SHA-256 before executing it. Android Studio therefore only
needs normal Internet access for the first Gradle/dependency sync.

Build from Android Studio with **Build > Build APK(s)**, or from a terminal:

```bash
./gradlew :app:assembleDebug
```

Debug APK:

```text
app/build/outputs/apk/debug/app-debug.apk
```

## First run on Xiaomi 13 Ultra / HyperOS

1. Install and open **Avito Push Bridge**.
2. Tap **Дать доступ к уведомлениям** and enable the bridge.
3. Tap **Настройки батареи / фоновой работы** and set the bridge to unrestricted
   background/battery behavior.
4. In HyperOS also enable **Autostart / Автозапуск** for the bridge. Locking it in
   Recents is useful on aggressive HyperOS builds.
5. Leave the package field as `com.avito.android` initially.
6. You may leave endpoint/secret empty for the first diagnostic run. Captured
   notifications still stay in the local SQLite queue and appear in the app.

If Avito notifications appear on the phone but the bridge captures nothing,
verify the installed Avito package name over ADB:

```bash
adb shell pm list packages | grep -i avito
```

Put the real package name into the app's package field. Multiple names can be
separated with spaces or commas.

## Inspect the real Avito payload before server setup

Wait for a real Avito notification. It should appear under **Последние события**.
Use **Копировать последний JSON** to copy the complete normalized diagnostic
payload. This is the data we will use to write the exact `push -> ListingCandidate`
extractor.

Note: during this diagnostic phase the bridge forwards every notification from
configured Avito package(s), including potentially chat/order notifications.
The data stays on your phone unless an endpoint is configured, and when sent it
goes only to your own server.

## Server endpoint

The repository contains `deal_phone_ingress.py`. On the watcher host it listens
on loopback by default:

```text
http://127.0.0.1:8767/phone-notification
```

Generate a dedicated secret (do not reuse Telegram or relay secrets):

```bash
openssl rand -hex 32
```

Put it in `~/parser_avito/.env.deal-watcher`:

```text
DEAL_PHONE_SECRET=<secret>
```

Install/start the user service:

```bash
cp deploy/systemd/deal-phone-ingress.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now deal-phone-ingress.service
curl http://127.0.0.1:8767/healthz
```

For a phone outside the LAN, expose only `/phone-notification` over HTTPS. The
provided `deal-phone-tunnel.service` creates a reverse SSH tunnel to the existing
VPS, and `deploy/Caddyfile.phone.example` shows the public TLS route. Do **not**
expose the raw Python port directly to the Internet.

Then configure the Android endpoint as:

```text
https://YOUR_HOST/phone-notification
```

and enter the same `DEAL_PHONE_SECRET` into the app. Tap **Сохранить настройки**
and then **Добавить тестовое событие и отправить**.

## Inspect events on the server

```bash
python deal_queue_admin.py phone-recent --limit 10
python deal_queue_admin.py phone-recent --limit 1 --json
```

The second command prints the full raw payload of the newest captured event.

## HMAC contract

Request headers:

```text
X-Phone-Timestamp: <unix seconds at delivery time>
X-Phone-Signature: <lowercase hex HMAC-SHA256>
Content-Type: application/json; charset=utf-8
```

Signature input:

```text
timestamp + "." + exact_raw_request_body
```

The server accepts a five-minute clock skew. An event captured offline can be
sent hours later because the signature timestamp is generated at **delivery**
time while the original `captured_at_ms` stays in the payload.
