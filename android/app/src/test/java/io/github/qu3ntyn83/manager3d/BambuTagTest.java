package io.github.qu3ntyn83.manager3d;

import static org.junit.Assert.assertEquals;

import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;

import org.junit.Test;

public class BambuTagTest {
    @Test
    public void keysMatchReferenceScript() {
        // Werte aus deriveKeys.py (Bambu-Research-Group/RFID-Tag-Guide) für UID 75886B1D
        byte[][] k = BambuTag.deriveKeys(new byte[]{0x75, (byte) 0x88, 0x6B, 0x1D});
        assertEquals("6E5B0EC6EF7C", BambuTag.hex(k[0]));
        assertEquals("4CE96076285F", BambuTag.hex(k[1]));
        assertEquals(16, k.length);
    }

    @Test
    public void parsesBlocks() {
        Map<Integer, byte[]> b = new HashMap<>();
        b.put(1, pad("A00-K0\0\0GFA00"));
        b.put(4, pad("PLA Basic"));
        byte[] b5 = new byte[16];
        b5[3] = (byte) 0xFF; b5[4] = (byte) 0xE8; b5[5] = 0x03;
        b.put(5, b5);
        byte[] b9 = new byte[16];
        for (int i = 0; i < 16; i++) b9[i] = (byte) (0xA0 + i);
        b.put(9, b9);
        Map<String, Object> o = BambuTag.parse(b);
        assertEquals("GFA00", o.get("material_id"));
        assertEquals("PLA Basic", o.get("detail"));
        assertEquals("000000FF", o.get("color"));
        assertEquals(1000, o.get("weight"));
        assertEquals("A0A1A2A3A4A5A6A7A8A9AAABACADAEAF", o.get("tray_uuid"));
    }

    private static byte[] pad(String s) {
        byte[] out = new byte[16];
        byte[] in = s.getBytes(StandardCharsets.US_ASCII);
        System.arraycopy(in, 0, out, 0, Math.min(16, in.length));
        return out;
    }
}
