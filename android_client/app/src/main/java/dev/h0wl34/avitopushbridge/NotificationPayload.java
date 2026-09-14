package dev.h0wl34.avitopushbridge;

import android.app.Notification;
import android.app.RemoteInput;
import android.os.Build;
import android.os.Bundle;
import android.service.notification.StatusBarNotification;

import org.json.JSONArray;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.UUID;

final class NotificationPayload {
    private NotificationPayload() {}

    static JSONObject from(StatusBarNotification sbn, AppConfig config) throws Exception {
        Notification notification = sbn.getNotification();
        Bundle extras = notification.extras;
        long capturedAt = System.currentTimeMillis();

        String title = extraString(extras, Notification.EXTRA_TITLE);
        String text = extraString(extras, Notification.EXTRA_TEXT);
        String bigText = extraString(extras, Notification.EXTRA_BIG_TEXT);
        String titleBig = extraString(extras, Notification.EXTRA_TITLE_BIG);
        String subText = extraString(extras, Notification.EXTRA_SUB_TEXT);
        String summaryText = extraString(extras, Notification.EXTRA_SUMMARY_TEXT);
        String infoText = extraString(extras, Notification.EXTRA_INFO_TEXT);

        String fingerprint = sbn.getPackageName() + "|" + sbn.getKey() + "|" + sbn.getPostTime()
                + "|" + nullToEmpty(title) + "|" + nullToEmpty(text) + "|" + nullToEmpty(bigText);
        String eventId = sha256Hex(fingerprint);

        JSONObject root = new JSONObject();
        root.put("schema_version", 1);
        root.put("event_id", eventId);
        root.put("device_id", config.getDeviceId());
        root.put("source", "android_notification_listener");
        root.put("package", sbn.getPackageName());
        root.put("notification_key", sbn.getKey());
        root.put("notification_id", sbn.getId());
        root.put("tag", nullable(sbn.getTag()));
        root.put("post_time_ms", sbn.getPostTime());
        root.put("captured_at_ms", capturedAt);
        root.put("channel_id", nullable(notification.getChannelId()));
        root.put("category", nullable(notification.category));
        root.put("group_key", nullable(sbn.getGroupKey()));
        root.put("sort_key", nullable(notification.getSortKey()));
        root.put("flags", notification.flags);
        root.put("visibility", notification.visibility);
        root.put("when_ms", notification.when);
        root.put("number", notification.number);
        root.put("ticker", notification.tickerText == null ? JSONObject.NULL : JsonUtils.truncate(notification.tickerText.toString()));
        root.put("has_content_intent", notification.contentIntent != null);
        root.put("has_delete_intent", notification.deleteIntent != null);

        putNullable(root, "title", title);
        putNullable(root, "title_big", titleBig);
        putNullable(root, "text", text);
        putNullable(root, "big_text", bigText);
        putNullable(root, "sub_text", subText);
        putNullable(root, "summary_text", summaryText);
        putNullable(root, "info_text", infoText);

        root.put("actions", actionsToJson(notification.actions));
        root.put("extras", JsonUtils.bundleToJson(extras));

        JSONObject device = new JSONObject();
        device.put("manufacturer", Build.MANUFACTURER);
        device.put("model", Build.MODEL);
        device.put("sdk", Build.VERSION.SDK_INT);
        device.put("app_version", BuildConfig.VERSION_NAME);
        root.put("bridge", device);
        return root;
    }

    static JSONObject syntheticTest(AppConfig config) throws Exception {
        long now = System.currentTimeMillis();
        String eventId = sha256Hex(config.getDeviceId() + "|test|" + now + "|" + UUID.randomUUID());
        JSONObject root = new JSONObject();
        root.put("schema_version", 1);
        root.put("event_id", eventId);
        root.put("device_id", config.getDeviceId());
        root.put("source", "android_notification_listener");
        root.put("package", AppConfig.DEFAULT_PACKAGE);
        root.put("notification_key", "synthetic-test-" + eventId.substring(0, 12));
        root.put("notification_id", -1);
        root.put("post_time_ms", now);
        root.put("captured_at_ms", now);
        root.put("title", "Avito Push Bridge test");
        root.put("text", "Тестовая доставка с телефона");
        root.put("big_text", JSONObject.NULL);
        root.put("actions", new JSONArray());
        root.put("extras", new JSONObject().put("bridge.synthetic", true));
        return root;
    }

    private static JSONArray actionsToJson(Notification.Action[] actions) {
        JSONArray result = new JSONArray();
        if (actions == null) {
            return result;
        }
        int limit = Math.min(actions.length, 20);
        for (int i = 0; i < limit; i++) {
            Notification.Action action = actions[i];
            if (action == null) {
                continue;
            }
            try {
                JSONObject item = new JSONObject();
                item.put("title", action.title == null ? JSONObject.NULL : JsonUtils.truncate(action.title.toString()));
                item.put("has_intent", action.actionIntent != null);
                if (Build.VERSION.SDK_INT >= 28) {
                    item.put("semantic_action", action.getSemanticAction());
                }
                JSONArray remoteInputs = new JSONArray();
                RemoteInput[] inputs = action.getRemoteInputs();
                if (inputs != null) {
                    for (RemoteInput input : inputs) {
                        if (input != null) {
                            remoteInputs.put(input.getResultKey());
                        }
                    }
                }
                item.put("remote_inputs", remoteInputs);
                result.put(item);
            } catch (Exception ignored) {
                // One odd action should never make us lose the whole notification.
            }
        }
        return result;
    }

    private static String extraString(Bundle extras, String key) {
        if (extras == null) {
            return null;
        }
        Object value = extras.get(key);
        return value == null ? null : JsonUtils.truncate(value.toString());
    }

    private static void putNullable(JSONObject target, String key, String value) throws Exception {
        target.put(key, value == null ? JSONObject.NULL : value);
    }

    private static Object nullable(String value) {
        return value == null ? JSONObject.NULL : JsonUtils.truncate(value);
    }

    private static String nullToEmpty(String value) {
        return value == null ? "" : value;
    }

    private static String sha256Hex(String input) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] bytes = digest.digest(input.getBytes(StandardCharsets.UTF_8));
        StringBuilder hex = new StringBuilder(bytes.length * 2);
        for (byte b : bytes) {
            hex.append(String.format("%02x", b & 0xff));
        }
        return hex.toString();
    }
}
