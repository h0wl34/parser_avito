package dev.h0wl34.avitopushbridge;

import android.content.Intent;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.util.Log;

import org.json.JSONObject;

public final class AvitoNotificationListener extends NotificationListenerService {
    public static final String ACTION_EVENT_CAPTURED = "dev.h0wl34.avitopushbridge.EVENT_CAPTURED";
    private static final String TAG = "AvitoPushBridge";

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        if (sbn == null) {
            return;
        }
        AppConfig config = new AppConfig(this);
        if (!config.isAllowedPackage(sbn.getPackageName())) {
            return;
        }
        try {
            JSONObject payload = NotificationPayload.from(sbn, config);
            String eventId = payload.getString("event_id");
            String title = payload.isNull("title") ? null : payload.optString("title", null);
            String text = payload.isNull("text") ? null : payload.optString("text", null);
            boolean inserted = NotificationEventStore.get(this).enqueue(
                    eventId,
                    payload.toString(),
                    sbn.getPackageName(),
                    title,
                    text,
                    payload.getLong("captured_at_ms"));
            if (inserted) {
                DeliveryScheduler.schedule(this);
                Intent changed = new Intent(ACTION_EVENT_CAPTURED).setPackage(getPackageName());
                sendBroadcast(changed);
            }
        } catch (Exception e) {
            Log.e(TAG, "Could not capture notification", e);
        }
    }

    @Override
    public void onListenerConnected() {
        super.onListenerConnected();
        Log.i(TAG, "Notification listener connected");
    }

    @Override
    public void onListenerDisconnected() {
        super.onListenerDisconnected();
        Log.w(TAG, "Notification listener disconnected; requesting rebind");
        requestRebind(new android.content.ComponentName(this, AvitoNotificationListener.class));
    }
}
