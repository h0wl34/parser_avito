package dev.h0wl34.avitopushbridge;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class SecureSecretStore {
    private static final String PREFS = "secure_config";
    private static final String PREF_SECRET = "endpoint_secret";
    private static final String KEY_ALIAS = "avito_push_bridge_secret_v1";
    private static final String ANDROID_KEYSTORE = "AndroidKeyStore";

    private SecureSecretStore() {}

    static void put(Context context, String value) throws Exception {
        if (value == null || value.isEmpty()) {
            context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                    .edit().remove(PREF_SECRET).apply();
            return;
        }
        SecretKey key = getOrCreateKey();
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key);
        byte[] iv = cipher.getIV();
        byte[] ciphertext = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
        ByteBuffer packed = ByteBuffer.allocate(4 + iv.length + ciphertext.length);
        packed.putInt(iv.length);
        packed.put(iv);
        packed.put(ciphertext);
        String encoded = Base64.encodeToString(packed.array(), Base64.NO_WRAP);
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().putString(PREF_SECRET, encoded).apply();
    }

    static String get(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String encoded = prefs.getString(PREF_SECRET, null);
        if (encoded == null || encoded.isEmpty()) {
            return "";
        }
        try {
            byte[] packedBytes = Base64.decode(encoded, Base64.NO_WRAP);
            ByteBuffer packed = ByteBuffer.wrap(packedBytes);
            int ivLength = packed.getInt();
            if (ivLength < 12 || ivLength > 32 || packed.remaining() <= ivLength) {
                throw new IllegalArgumentException("invalid encrypted secret");
            }
            byte[] iv = new byte[ivLength];
            packed.get(iv);
            byte[] ciphertext = new byte[packed.remaining()];
            packed.get(ciphertext);

            SecretKey key = getOrCreateKey();
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, key, new GCMParameterSpec(128, iv));
            return new String(cipher.doFinal(ciphertext), StandardCharsets.UTF_8);
        } catch (Exception ignored) {
            // Keystore keys can be invalidated by lock-screen/security changes.
            // Do not silently send with a corrupt secret; force the user to save it again.
            prefs.edit().remove(PREF_SECRET).apply();
            return "";
        }
    }

    private static SecretKey getOrCreateKey() throws Exception {
        KeyStore keyStore = KeyStore.getInstance(ANDROID_KEYSTORE);
        keyStore.load(null);
        if (keyStore.containsAlias(KEY_ALIAS)) {
            return ((KeyStore.SecretKeyEntry) keyStore.getEntry(KEY_ALIAS, null)).getSecretKey();
        }
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE);
        generator.init(new KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build());
        return generator.generateKey();
    }
}
