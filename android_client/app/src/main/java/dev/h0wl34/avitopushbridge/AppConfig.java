package dev.h0wl34.avitopushbridge;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.UUID;

public final class AppConfig {
    private static final String PREFS = "bridge_config";
    private static final String KEY_ENDPOINT = "endpoint";
    private static final String KEY_PACKAGES = "packages";
    private static final String KEY_DEVICE_ID = "device_id";
    public static final String DEFAULT_PACKAGE = "com.avito.android";

    private final Context context;
    private final SharedPreferences prefs;

    public AppConfig(Context context) {
        this.context = context.getApplicationContext();
        this.prefs = this.context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    public String getEndpoint() {
        return prefs.getString(KEY_ENDPOINT, "");
    }

    public String getSecret() {
        return SecureSecretStore.get(context);
    }

    public String getPackagesText() {
        return prefs.getString(KEY_PACKAGES, DEFAULT_PACKAGE);
    }

    public String getDeviceId() {
        String current = prefs.getString(KEY_DEVICE_ID, null);
        if (current != null && !current.isEmpty()) {
            return current;
        }
        String created = UUID.randomUUID().toString();
        prefs.edit().putString(KEY_DEVICE_ID, created).apply();
        return created;
    }

    public void save(String endpoint, String secret, String packages) throws Exception {
        String cleanPackages = packages == null || packages.trim().isEmpty()
                ? DEFAULT_PACKAGE
                : packages.trim();
        prefs.edit()
                .putString(KEY_ENDPOINT, endpoint == null ? "" : endpoint.trim())
                .putString(KEY_PACKAGES, cleanPackages)
                .apply();
        SecureSecretStore.put(context, secret == null ? "" : secret);
    }

    public boolean isAllowedPackage(String packageName) {
        if (packageName == null) {
            return false;
        }
        return getAllowedPackages().contains(packageName.toLowerCase(Locale.ROOT));
    }

    public Set<String> getAllowedPackages() {
        Set<String> result = new HashSet<>();
        String raw = getPackagesText();
        for (String item : raw.split("[,\\n\\r\\t ]+")) {
            String normalized = item.trim().toLowerCase(Locale.ROOT);
            if (!normalized.isEmpty()) {
                result.add(normalized);
            }
        }
        if (result.isEmpty()) {
            result.add(DEFAULT_PACKAGE);
        }
        return result;
    }
}
