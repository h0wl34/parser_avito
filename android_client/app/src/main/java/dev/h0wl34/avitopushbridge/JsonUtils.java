package dev.h0wl34.avitopushbridge;

import android.os.Bundle;

import org.json.JSONArray;
import org.json.JSONObject;

import java.lang.reflect.Array;
import java.util.ArrayList;

final class JsonUtils {
    private static final int MAX_DEPTH = 3;
    private static final int MAX_STRING = 8192;
    private static final int MAX_COLLECTION = 100;

    private JsonUtils() {}

    static JSONObject bundleToJson(Bundle bundle) {
        JSONObject object = new JSONObject();
        if (bundle == null) {
            return object;
        }
        for (String key : bundle.keySet()) {
            try {
                object.put(key, safeValue(bundle.get(key), 0));
            } catch (Exception e) {
                try {
                    object.put(key, "<unreadable:" + e.getClass().getSimpleName() + ">");
                } catch (Exception ignored) {
                    // JSONObject put should not fail for this string.
                }
            }
        }
        return object;
    }

    private static Object safeValue(Object value, int depth) {
        if (value == null) {
            return JSONObject.NULL;
        }
        if (value instanceof CharSequence) {
            return truncate(value.toString());
        }
        if (value instanceof Number || value instanceof Boolean) {
            return value;
        }
        if (depth >= MAX_DEPTH) {
            return "<" + value.getClass().getName() + ">";
        }
        if (value instanceof Bundle) {
            JSONObject nested = new JSONObject();
            Bundle bundle = (Bundle) value;
            for (String key : bundle.keySet()) {
                try {
                    nested.put(key, safeValue(bundle.get(key), depth + 1));
                } catch (Exception ignored) {
                    // Skip malformed nested values rather than losing the notification.
                }
            }
            return nested;
        }
        if (value instanceof ArrayList<?>) {
            JSONArray array = new JSONArray();
            ArrayList<?> list = (ArrayList<?>) value;
            for (int i = 0; i < Math.min(list.size(), MAX_COLLECTION); i++) {
                array.put(safeValue(list.get(i), depth + 1));
            }
            return array;
        }
        Class<?> type = value.getClass();
        if (type.isArray()) {
            JSONArray array = new JSONArray();
            int length = Math.min(Array.getLength(value), MAX_COLLECTION);
            for (int i = 0; i < length; i++) {
                array.put(safeValue(Array.get(value, i), depth + 1));
            }
            return array;
        }
        // Avoid serializing Bitmap/RemoteViews/Parcelable binary internals. Class name
        // is still useful when reverse-engineering what Avito exposes in extras.
        return "<" + value.getClass().getName() + ">";
    }

    static String truncate(String value) {
        if (value == null) {
            return null;
        }
        if (value.length() <= MAX_STRING) {
            return value;
        }
        return value.substring(0, MAX_STRING) + "…<truncated>";
    }
}
