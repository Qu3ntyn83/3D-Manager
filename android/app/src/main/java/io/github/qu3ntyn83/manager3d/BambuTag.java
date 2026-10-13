package io.github.qu3ntyn83.manager3d;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Bambu-Lab-Filament-Tags (MIFARE Classic 1K): Schlüsselableitung und Block-Format.
 * Quelle: https://github.com/Bambu-Research-Group/RFID-Tag-Guide (deriveKeys.py, BambuLabRfid.md)
 * Reines Java ohne Android-Abhängigkeiten, damit es auf der JVM testbar ist.
 */
public final class BambuTag {
    private BambuTag() {}

    private static final byte[] MASTER = {
            (byte) 0x9a, 0x75, (byte) 0x9c, (byte) 0xf2, (byte) 0xc4, (byte) 0xf7, (byte) 0xca, (byte) 0xff,
            0x22, 0x2c, (byte) 0xb9, 0x76, (byte) 0x9b, 0x41, (byte) 0xbc, (byte) 0x96};
    private static final byte[] INFO = "RFID-A\0".getBytes(StandardCharsets.US_ASCII);

    /** Datenblöcke, die gelesen werden (Sektor 0–4, ohne Sektor-Trailer). */
    public static final int[] BLOCKS = {1, 2, 4, 5, 6, 8, 9, 10, 12, 14, 16};

    /** HKDF-SHA256(ikm = UID, salt = MASTER, info = "RFID-A\0") → 16 Schlüssel A à 6 Byte (Sektor 0–15). */
    public static byte[][] deriveKeys(byte[] uid) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(MASTER, "HmacSHA256"));
            byte[] prk = mac.doFinal(uid);
            mac.init(new SecretKeySpec(prk, "HmacSHA256"));
            byte[] okm = new byte[16 * 6];
            byte[] t = new byte[0];
            int pos = 0;
            for (int i = 1; pos < okm.length; i++) {
                mac.update(t);
                mac.update(INFO);
                mac.update((byte) i);
                t = mac.doFinal();
                int n = Math.min(t.length, okm.length - pos);
                System.arraycopy(t, 0, okm, pos, n);
                pos += n;
            }
            byte[][] keys = new byte[16][6];
            for (int s = 0; s < 16; s++) System.arraycopy(okm, s * 6, keys[s], 0, 6);
            return keys;
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** Gelesene Blöcke (Blocknummer → 16 Byte) in Felder übersetzen. Fehlende Blöcke werden übersprungen. */
    public static Map<String, Object> parse(Map<Integer, byte[]> b) {
        Map<String, Object> o = new LinkedHashMap<>();
        if (b.get(1) != null) {
            o.put("variant_id", str(b.get(1), 0, 8));
            o.put("material_id", str(b.get(1), 8, 8));
        }
        if (b.get(2) != null) o.put("type", str(b.get(2), 0, 16));
        if (b.get(4) != null) o.put("detail", str(b.get(4), 0, 16));
        if (b.get(5) != null) {
            byte[] x = b.get(5);
            o.put("color", hex(x, 0, 4));
            o.put("weight", u16(x, 4));
            float dia = le(x, 8).getFloat();
            if (dia > 1f && dia < 4f) o.put("diameter", Math.round(dia * 100) / 100.0);
        }
        if (b.get(6) != null) {
            byte[] x = b.get(6);
            o.put("dry_temp", u16(x, 0));
            o.put("dry_hours", u16(x, 2));
            o.put("bed_temp", u16(x, 6));
            o.put("nozzle_max", u16(x, 8));
            o.put("nozzle_min", u16(x, 10));
        }
        if (b.get(9) != null) o.put("tray_uuid", hex(b.get(9), 0, 16));
        if (b.get(10) != null) {
            int w = u16(b.get(10), 4);
            if (w > 0) o.put("spool_width_mm", w / 100.0);
        }
        if (b.get(12) != null) o.put("prod_date", str(b.get(12), 0, 16));
        if (b.get(14) != null) {
            int len = u16(b.get(14), 4);
            if (len > 0) o.put("length_m", len);
        }
        if (b.get(16) != null) {
            byte[] x = b.get(16);
            if (u16(x, 0) == 2 && u16(x, 2) > 1) {
                // zweite Farbe ist rückwärts gespeichert (ABGR)
                o.put("color2", String.format("%02X%02X%02X%02X", x[7], x[6], x[5], x[4]));
            }
        }
        return o;
    }

    public static String hex(byte[] a) {
        return hex(a, 0, a.length);
    }

    static String hex(byte[] a, int off, int len) {
        StringBuilder sb = new StringBuilder();
        for (int i = off; i < off + len; i++) sb.append(String.format("%02X", a[i]));
        return sb.toString();
    }

    private static String str(byte[] a, int off, int len) {
        int end = off;
        while (end < off + len && a[end] != 0) end++;
        return new String(a, off, end - off, StandardCharsets.US_ASCII).trim();
    }

    private static int u16(byte[] a, int off) {
        return (a[off] & 0xff) | ((a[off + 1] & 0xff) << 8);
    }

    private static ByteBuffer le(byte[] a, int off) {
        return ByteBuffer.wrap(a, off, 4).order(ByteOrder.LITTLE_ENDIAN);
    }

    /** Minimaler JSON-Serializer (String/Number/Boolean/null). */
    public static String toJson(Map<String, Object> m) {
        StringBuilder sb = new StringBuilder("{");
        for (Map.Entry<String, Object> e : m.entrySet()) {
            if (sb.length() > 1) sb.append(',');
            sb.append(q(e.getKey())).append(':');
            Object v = e.getValue();
            if (v == null) sb.append("null");
            else if (v instanceof Number || v instanceof Boolean) sb.append(v);
            else sb.append(q(v.toString()));
        }
        return sb.append('}').toString();
    }

    private static String q(String s) {
        StringBuilder sb = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') sb.append('\\').append(c);
            else if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
            else sb.append(c);
        }
        return sb.append('"').toString();
    }
}
