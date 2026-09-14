package dev.h0wl34.avitopushbridge;

import android.content.Context;

import androidx.annotation.NonNull;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;
import javax.net.ssl.HttpsURLConnection;

public final class DeliveryWorker extends Worker {
    private static final int CONNECT_TIMEOUT_MS = 10_000;
    private static final int READ_TIMEOUT_MS = 10_000;
    private static final int MAX_PER_RUN = 100;

    public DeliveryWorker(@NonNull Context appContext, @NonNull WorkerParameters params) {
        super(appContext, params);
    }

    @NonNull
    @Override
    public Result doWork() {
        AppConfig config = new AppConfig(getApplicationContext());
        String endpoint = config.getEndpoint().trim();
        String secret = config.getSecret();
        if (endpoint.isEmpty() || secret.isEmpty()) {
            // Capture still works before the server is configured. Saving valid
            // settings from MainActivity schedules another delivery pass.
            return Result.success();
        }
        if (!endpoint.startsWith("https://")) {
            return Result.failure();
        }

        NotificationEventStore store = NotificationEventStore.get(getApplicationContext());
        int processed = 0;
        while (processed < MAX_PER_RUN) {
            List<NotificationEventStore.EventRow> pending = store.pending(Math.min(25, MAX_PER_RUN - processed));
            if (pending.isEmpty()) {
                return Result.success();
            }
            for (NotificationEventStore.EventRow row : pending) {
                processed++;
                try {
                    int status = post(endpoint, secret, row.payload);
                    if ((status >= 200 && status < 300) || status == 409) {
                        store.markSent(row.eventId);
                    } else if (status == 400 || status == 413 || status == 422) {
                        store.markPermanentFailure(row.eventId, "HTTP " + status);
                    } else {
                        store.markRetry(row.eventId, "HTTP " + status);
                        return Result.retry();
                    }
                } catch (Exception e) {
                    store.markRetry(row.eventId, e.getClass().getSimpleName() + ": " + safeMessage(e));
                    return Result.retry();
                }
            }
        }

        // More than 100 notifications is exceptional, but preserve reliability.
        DeliveryScheduler.schedule(getApplicationContext());
        return Result.success();
    }

    private int post(String endpoint, String secret, String payload) throws Exception {
        byte[] body = payload.getBytes(StandardCharsets.UTF_8);
        String timestamp = Long.toString(System.currentTimeMillis() / 1000L);
        String signature = HmacSigner.sign(secret, timestamp, body);

        URL url = new URL(endpoint);
        if (!"https".equalsIgnoreCase(url.getProtocol())) {
            throw new IllegalArgumentException("HTTPS endpoint required");
        }
        HttpsURLConnection connection = (HttpsURLConnection) url.openConnection();
        try {
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
            connection.setReadTimeout(READ_TIMEOUT_MS);
            connection.setDoOutput(true);
            connection.setUseCaches(false);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("X-Phone-Timestamp", timestamp);
            connection.setRequestProperty("X-Phone-Signature", signature);
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(body);
            }
            int status = connection.getResponseCode();
            // Drain a small response so keep-alive/TLS resources can be released cleanly.
            InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            drain(stream);
            return status;
        } finally {
            connection.disconnect();
        }
    }

    private void drain(InputStream stream) {
        if (stream == null) {
            return;
        }
        try (InputStream input = stream; ByteArrayOutputStream sink = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[1024];
            int total = 0;
            int read;
            while (total < 8192 && (read = input.read(buffer, 0, Math.min(buffer.length, 8192 - total))) >= 0) {
                sink.write(buffer, 0, read);
                total += read;
            }
        } catch (Exception ignored) {
            // Response body is diagnostic only; delivery status already exists.
        }
    }

    private String safeMessage(Exception e) {
        String message = e.getMessage();
        return message == null ? "no message" : message;
    }
}
