package dev.h0wl34.avitopushbridge;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.util.ArrayList;
import java.util.List;

public final class NotificationEventStore extends SQLiteOpenHelper {
    private static final String DB_NAME = "notification_events.db";
    private static final int DB_VERSION = 1;
    private static volatile NotificationEventStore instance;

    public static final class EventRow {
        public final String eventId;
        public final String payload;
        public final String packageName;
        public final String title;
        public final String text;
        public final long capturedAtMs;
        public final String state;
        public final int attempts;
        public final String lastError;

        EventRow(String eventId, String payload, String packageName, String title, String text,
                 long capturedAtMs, String state, int attempts, String lastError) {
            this.eventId = eventId;
            this.payload = payload;
            this.packageName = packageName;
            this.title = title;
            this.text = text;
            this.capturedAtMs = capturedAtMs;
            this.state = state;
            this.attempts = attempts;
            this.lastError = lastError;
        }
    }

    public static NotificationEventStore get(Context context) {
        if (instance == null) {
            synchronized (NotificationEventStore.class) {
                if (instance == null) {
                    instance = new NotificationEventStore(context.getApplicationContext());
                }
            }
        }
        return instance;
    }

    private NotificationEventStore(Context context) {
        super(context, DB_NAME, null, DB_VERSION);
        setWriteAheadLoggingEnabled(true);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE events (" +
                "event_id TEXT PRIMARY KEY," +
                "payload TEXT NOT NULL," +
                "package_name TEXT NOT NULL," +
                "title TEXT," +
                "text_value TEXT," +
                "captured_at_ms INTEGER NOT NULL," +
                "state TEXT NOT NULL DEFAULT 'pending'," +
                "attempts INTEGER NOT NULL DEFAULT 0," +
                "last_error TEXT," +
                "sent_at_ms INTEGER" +
                ")");
        db.execSQL("CREATE INDEX idx_events_state_time ON events(state, captured_at_ms)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        throw new IllegalStateException("No database migration defined from " + oldVersion + " to " + newVersion);
    }

    public boolean enqueue(String eventId, String payload, String packageName, String title,
                           String text, long capturedAtMs) {
        ContentValues values = new ContentValues();
        values.put("event_id", eventId);
        values.put("payload", payload);
        values.put("package_name", packageName);
        values.put("title", title);
        values.put("text_value", text);
        values.put("captured_at_ms", capturedAtMs);
        values.put("state", "pending");
        return getWritableDatabase().insertWithOnConflict(
                "events", null, values, SQLiteDatabase.CONFLICT_IGNORE) != -1;
    }

    public List<EventRow> pending(int limit) {
        return queryRows("state='pending'", Math.max(1, Math.min(limit, 100)), "captured_at_ms ASC");
    }

    public List<EventRow> recent(int limit) {
        return queryRows(null, Math.max(1, Math.min(limit, 100)), "captured_at_ms DESC");
    }

    private List<EventRow> queryRows(String where, int limit, String orderBy) {
        List<EventRow> rows = new ArrayList<>();
        try (Cursor cursor = getReadableDatabase().query(
                "events",
                new String[]{"event_id", "payload", "package_name", "title", "text_value",
                        "captured_at_ms", "state", "attempts", "last_error"},
                where, null, null, null,
                orderBy,
                Integer.toString(limit))) {
            while (cursor.moveToNext()) {
                rows.add(new EventRow(
                        cursor.getString(0), cursor.getString(1), cursor.getString(2),
                        cursor.getString(3), cursor.getString(4), cursor.getLong(5),
                        cursor.getString(6), cursor.getInt(7), cursor.getString(8)));
            }
        }
        return rows;
    }

    public void markSent(String eventId) {
        ContentValues values = new ContentValues();
        values.put("state", "sent");
        values.put("last_error", (String) null);
        values.put("sent_at_ms", System.currentTimeMillis());
        getWritableDatabase().update("events", values, "event_id=?", new String[]{eventId});
    }

    public void markPermanentFailure(String eventId, String error) {
        ContentValues values = new ContentValues();
        values.put("state", "dead");
        values.put("last_error", JsonUtils.truncate(error));
        values.put("attempts", getAttempts(eventId) + 1);
        getWritableDatabase().update("events", values, "event_id=?", new String[]{eventId});
    }

    public void markRetry(String eventId, String error) {
        ContentValues values = new ContentValues();
        values.put("last_error", JsonUtils.truncate(error));
        values.put("attempts", getAttempts(eventId) + 1);
        getWritableDatabase().update("events", values, "event_id=?", new String[]{eventId});
    }

    private int getAttempts(String eventId) {
        try (Cursor cursor = getReadableDatabase().query(
                "events", new String[]{"attempts"}, "event_id=?", new String[]{eventId},
                null, null, null, "1")) {
            return cursor.moveToFirst() ? cursor.getInt(0) : 0;
        }
    }
}
