package dev.h0wl34.avitopushbridge;

import android.app.Activity;
import android.app.NotificationManager;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.text.DateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

public final class MainActivity extends Activity {
    private EditText endpointInput;
    private EditText secretInput;
    private EditText packagesInput;
    private TextView statusText;
    private TextView eventsText;
    private AppConfig config;
    private NotificationEventStore store;

    private final BroadcastReceiver eventReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            refresh();
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        config = new AppConfig(this);
        store = NotificationEventStore.get(this);

        endpointInput = findViewById(R.id.endpoint_input);
        secretInput = findViewById(R.id.secret_input);
        packagesInput = findViewById(R.id.packages_input);
        statusText = findViewById(R.id.status_text);
        eventsText = findViewById(R.id.events_text);

        secretInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        endpointInput.setText(config.getEndpoint());
        secretInput.setText(config.getSecret());
        packagesInput.setText(config.getPackagesText());

        findViewById(R.id.save_button).setOnClickListener(v -> saveSettings());
        findViewById(R.id.notification_access_button).setOnClickListener(v -> openNotificationAccess());
        findViewById(R.id.battery_button).setOnClickListener(v -> openBatterySettings());
        findViewById(R.id.test_button).setOnClickListener(v -> enqueueTest());
        findViewById(R.id.refresh_button).setOnClickListener(v -> refresh());
        findViewById(R.id.copy_button).setOnClickListener(v -> copyLatestJson());

        refresh();
    }

    @Override
    protected void onStart() {
        super.onStart();
        IntentFilter filter = new IntentFilter(AvitoNotificationListener.ACTION_EVENT_CAPTURED);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(eventReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(eventReceiver, filter);
        }
    }

    @Override
    protected void onStop() {
        super.onStop();
        unregisterReceiver(eventReceiver);
    }

    @Override
    protected void onResume() {
        super.onResume();
        refresh();
    }

    private void saveSettings() {
        String endpoint = endpointInput.getText().toString().trim();
        String secret = secretInput.getText().toString();
        String packages = packagesInput.getText().toString().trim();

        if (!endpoint.isEmpty() && !endpoint.startsWith("https://")) {
            toast("Endpoint должен начинаться с https://");
            return;
        }
        if (!endpoint.isEmpty() && secret.isEmpty()) {
            toast("Для настроенного endpoint нужен shared secret");
            return;
        }
        try {
            config.save(endpoint, secret, packages);
            DeliveryScheduler.schedule(this);
            toast("Настройки сохранены");
            refresh();
        } catch (Exception e) {
            toast("Не удалось сохранить secret: " + e.getClass().getSimpleName());
        }
    }

    private void openNotificationAccess() {
        try {
            startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));
        } catch (Exception e) {
            startActivity(new Intent("android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS"));
        }
    }

    private void openBatterySettings() {
        try {
            startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        } catch (Exception e) {
            startActivity(new Intent(Settings.ACTION_SETTINGS));
        }
    }

    private void enqueueTest() {
        try {
            JSONObject payload = NotificationPayload.syntheticTest(config);
            String eventId = payload.getString("event_id");
            boolean inserted = store.enqueue(
                    eventId,
                    payload.toString(),
                    AppConfig.DEFAULT_PACKAGE,
                    payload.optString("title", null),
                    payload.optString("text", null),
                    payload.getLong("captured_at_ms"));
            if (inserted) {
                DeliveryScheduler.schedule(this);
                toast("Тестовое событие добавлено в очередь");
            }
            refresh();
        } catch (Exception e) {
            toast("Ошибка тестового события: " + e.getClass().getSimpleName());
        }
    }

    private void copyLatestJson() {
        List<NotificationEventStore.EventRow> rows = store.recent(1);
        if (rows.isEmpty()) {
            toast("Событий пока нет");
            return;
        }
        ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        clipboard.setPrimaryClip(ClipData.newPlainText("Avito notification JSON", rows.get(0).payload));
        toast("JSON последнего события скопирован");
    }

    private void refresh() {
        if (statusText == null || eventsText == null) {
            return;
        }
        boolean access = hasNotificationAccess();
        String endpointState = config.getEndpoint().isEmpty() ? "не настроен" : "настроен";
        String secretState = config.getSecret().isEmpty() ? "нет" : "есть (Android Keystore)";
        statusText.setText(String.format(Locale.ROOT,
                "Доступ к уведомлениям: %s\nEndpoint: %s\nSecret: %s\nDevice ID: %s",
                access ? "✅ выдан" : "❌ не выдан",
                endpointState,
                secretState,
                config.getDeviceId()));

        List<NotificationEventStore.EventRow> rows = store.recent(20);
        if (rows.isEmpty()) {
            eventsText.setText("Пока нет перехваченных уведомлений Avito.");
            return;
        }
        StringBuilder out = new StringBuilder();
        DateFormat dateFormat = DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.MEDIUM);
        for (NotificationEventStore.EventRow row : rows) {
            out.append(dateFormat.format(new Date(row.capturedAtMs)))
                    .append("  [").append(row.state).append("]\n")
                    .append(row.packageName).append("\n")
                    .append(row.title == null ? "<без title>" : row.title).append("\n")
                    .append(row.text == null ? "<без text>" : row.text).append("\n");
            if (row.lastError != null && !row.lastError.isEmpty()) {
                out.append("error: ").append(row.lastError).append("\n");
            }
            out.append("id: ").append(row.eventId, 0, Math.min(12, row.eventId.length())).append("…\n\n");
        }
        eventsText.setText(out.toString());
    }

    private boolean hasNotificationAccess() {
        ComponentName component = new ComponentName(this, AvitoNotificationListener.class);
        if (Build.VERSION.SDK_INT >= 27) {
            NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            return manager.isNotificationListenerAccessGranted(component);
        }
        String enabled = Settings.Secure.getString(getContentResolver(), "enabled_notification_listeners");
        if (enabled == null) {
            return false;
        }
        for (String item : enabled.split(":")) {
            ComponentName current = ComponentName.unflattenFromString(item);
            if (component.equals(current)) {
                return true;
            }
        }
        return false;
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }
}
