package dev.h0wl34.avitopushbridge;

import static org.junit.Assert.assertEquals;

import java.nio.charset.StandardCharsets;
import org.junit.Test;

public class HmacSignerTest {
    @Test
    public void signsTimestampDotRawBody() throws Exception {
        assertEquals(
                "654f06c856baf080af3fa272934823257a542d35cf1f88099338f850a60601a4",
                HmacSigner.sign(
                        "secret",
                        "1700000000",
                        "{\"hello\":\"world\"}".getBytes(StandardCharsets.UTF_8)));
    }
}
