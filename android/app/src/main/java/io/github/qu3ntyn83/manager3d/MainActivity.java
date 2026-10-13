package io.github.qu3ntyn83.manager3d;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.nfc.NfcAdapter;
import android.nfc.Tag;
import android.nfc.tech.MifareClassic;
import android.os.Build;
import android.os.Bundle;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.text.InputType;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;

import java.util.HashMap;
import java.util.Map;

/**
 * 3D Manager als App: zeigt die Weboberfläche und liest Bambu-RFID-Tags per Handy-NFC.
 * Ein gelesener Tag wird an die Seite übergeben: window.on3dmNfc({...}).
 */
public class MainActivity extends Activity implements NfcAdapter.ReaderCallback {
    private static final int REQ_FILE = 7;
    private static final String PREFS = "3dm";

    private WebView web;
    private NfcAdapter nfc;
    private SharedPreferences prefs;
    private String pendingNfc;
    private ValueCallback<Uri[]> fileCallback;
    private long lastTagAt;
    private String lastTagUid;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        nfc = NfcAdapter.getDefaultAdapter(this);

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#141311"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setUserAgentString(s.getUserAgentString() + " 3DManagerApp/" + versionName());
        web.addJavascriptInterface(new Bridge(), "Android3DM");
        web.setWebViewClient(new Client());
        web.setWebChromeClient(new Chrome());
        web.setDownloadListener((url, ua, cd, mime, len) -> openExternal(url));
        FrameLayout root = new FrameLayout(this);
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        if (state != null) web.restoreState(state);
        else if (serverUrl() == null) askServer(true);
        else web.loadUrl(serverUrl());
        handleIntent(getIntent());
    }

    // ------------------------------------------------------------------ NFC
    @Override
    protected void onResume() {
        super.onResume();
        if (nfc != null && nfc.isEnabled()) {
            Bundle opts = new Bundle();
            opts.putInt(NfcAdapter.EXTRA_READER_PRESENCE_CHECK_DELAY, 500);
            nfc.enableReaderMode(this, this,
                    NfcAdapter.FLAG_READER_NFC_A | NfcAdapter.FLAG_READER_SKIP_NDEF_CHECK | NfcAdapter.FLAG_READER_NO_PLATFORM_SOUNDS, opts);
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (nfc != null) nfc.disableReaderMode(this);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleIntent(intent);
    }

    private void handleIntent(Intent intent) {
        if (intent == null || !NfcAdapter.ACTION_TECH_DISCOVERED.equals(intent.getAction())) return;
        Tag tag = intent.getParcelableExtra(NfcAdapter.EXTRA_TAG);
        if (tag != null) new Thread(() -> onTagDiscovered(tag)).start();
    }

    /** Läuft im NFC-Thread. */
    @Override
    public void onTagDiscovered(Tag tag) {
        String uid = BambuTag.hex(tag.getId());
        long now = System.currentTimeMillis();
        if (uid.equals(lastTagUid) && now - lastTagAt < 2500) return;  // Doppel-Erkennung beim Halten
        lastTagUid = uid;
        lastTagAt = now;
        Map<String, Object> out = readTag(tag);
        String json = BambuTag.toJson(out);
        runOnUiThread(() -> {
            vibrate();
            deliver(json);
        });
    }

    private Map<String, Object> readTag(Tag tag) {
        Map<String, Object> o = new HashMap<>();
        byte[] id = tag.getId();
        boolean mifareSupported = getPackageManager().hasSystemFeature("com.nxp.mifare");
        MifareClassic m = null;
        try {
            m = MifareClassic.get(tag);
        } catch (RuntimeException ignored) {
            // Telefone ohne NXP-Chip werfen hier teils Fehler statt null
        }
        Map<String, Object> parsed = null;
        boolean auth = false;
        if (m != null) {
            try {
                m.connect();
                byte[][] keys = BambuTag.deriveKeys(id);
                Map<Integer, byte[]> blocks = new HashMap<>();
                for (int sector = 0; sector < 5; sector++) {
                    if (!m.authenticateSectorWithKeyA(sector, keys[sector])) continue;
                    auth = true;
                    for (int blk : BambuTag.BLOCKS) {
                        if (m.blockToSector(blk) != sector) continue;
                        blocks.put(blk, m.readBlock(blk));
                    }
                }
                if (!blocks.isEmpty()) parsed = BambuTag.parse(blocks);
            } catch (Exception ignored) {
                // Tag zu früh entfernt o. ä. – UID reicht für die Zuordnung
            } finally {
                try { m.close(); } catch (Exception ignored) { }
            }
        }
        if (parsed != null) o.putAll(parsed);
        // Reihenfolge der Schlüssel spielt keine Rolle (JSON-Objekt)
        o.put("uid", BambuTag.hex(id));
        o.put("mifare", m != null);
        o.put("nxp", mifareSupported);
        if (m != null) o.put("auth", auth);
        o.put("source", "android");
        return o;
    }

    private void deliver(String json) {
        String js = "(function(d){if(window.on3dmNfc){window.on3dmNfc(d);return 1}return 0})(" + json + ")";
        web.evaluateJavascript(js, v -> {
            if (!"1".equals(v)) pendingNfc = json;  // Seite lädt noch → nach onPageFinished zustellen
        });
    }

    private void vibrate() {
        try {
            Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            if (v == null) return;
            if (Build.VERSION.SDK_INT >= 26) v.vibrate(VibrationEffect.createOneShot(40, VibrationEffect.DEFAULT_AMPLITUDE));
            else v.vibrate(40);
        } catch (Exception ignored) { }
    }

    // ------------------------------------------------------------------ Server-Adresse
    private String serverUrl() {
        return prefs.getString("url", null);
    }

    private void askServer(boolean first) {
        EditText in = new EditText(this);
        in.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        in.setHint("http://192.168.x.x:8095");
        in.setText(serverUrl() != null ? serverUrl() : "http://");
        in.setSelection(in.getText().length());
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        FrameLayout box = new FrameLayout(this);
        box.setPadding(pad, pad / 2, pad, 0);
        box.addView(in);
        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle("Adresse des 3D Manager")
                .setMessage("Wie im Browser, z. B. http://192.168.178.42:8095")
                .setView(box)
                .setCancelable(!first)
                .setPositiveButton("Verbinden", (d, w) -> {
                    String u = in.getText().toString().trim();
                    if (!u.matches("(?i)^https?://.*")) u = "http://" + u;
                    while (u.endsWith("/")) u = u.substring(0, u.length() - 1);
                    prefs.edit().putString("url", u).apply();
                    web.loadUrl(u + "/");
                });
        if (!first) b.setNegativeButton("Abbrechen", null);
        b.show();
    }

    private void openExternal(String url) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (Exception e) {
            Toast.makeText(this, "Kann nicht geöffnet werden", Toast.LENGTH_SHORT).show();
        }
    }

    private String versionName() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException e) {
            return "1";
        }
    }

    // ------------------------------------------------------------------ WebView
    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
            Uri u = req.getUrl();
            Uri base = serverUrl() != null ? Uri.parse(serverUrl()) : null;
            if (base != null && u.getHost() != null && u.getHost().equals(base.getHost())) return false;
            openExternal(u.toString());  // Shop-/Hersteller-Links im normalen Browser
            return true;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (pendingNfc != null) {
                String j = pendingNfc;
                pendingNfc = null;
                view.postDelayed(() -> deliver(j), 800);  // App-Skripte brauchen einen Moment
            }
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
            if (!req.isForMainFrame()) return;
            String html = "<html><body style='background:#141311;color:#ECE8E0;font-family:sans-serif;padding:32px'>"
                    + "<h2>3D Manager nicht erreichbar</h2><p style='color:#a9a39a'>" + req.getUrl() + "<br>" + err.getDescription() + "</p>"
                    + "<p>Bist du im Heimnetz (WLAN)?</p>"
                    + "<p><button style='font-size:17px;padding:12px 18px;margin-right:8px' onclick='location.href=\"" + serverUrl() + "/\"'>Erneut versuchen</button>"
                    + "<button style='font-size:17px;padding:12px 18px' onclick='Android3DM.changeServer()'>Adresse ändern</button></p></body></html>";
            view.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
        }
    }

    private class Chrome extends WebChromeClient {
        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> cb, FileChooserParams params) {
            if (fileCallback != null) fileCallback.onReceiveValue(null);
            fileCallback = cb;
            try {
                startActivityForResult(params.createIntent(), REQ_FILE);
            } catch (Exception e) {
                fileCallback = null;
                return false;
            }
            return true;
        }
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        if (req == REQ_FILE && fileCallback != null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(res, data));
            fileCallback = null;
            return;
        }
        super.onActivityResult(req, res, data);
    }

    /** Für die Weboberfläche: window.Android3DM.* */
    private class Bridge {
        @JavascriptInterface
        public void changeServer() {
            runOnUiThread(() -> askServer(false));
        }

        @JavascriptInterface
        public String nfcState() {
            if (nfc == null) return "none";
            return nfc.isEnabled() ? "on" : "off";
        }

        @JavascriptInterface
        public void openNfcSettings() {
            runOnUiThread(() -> startActivity(new Intent(android.provider.Settings.ACTION_NFC_SETTINGS)));
        }
    }

    // ------------------------------------------------------------------ Zurück-Taste
    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        web.evaluateJavascript("(window.on3dmBack&&window.on3dmBack())?1:0", v -> {
            if ("1".equals(v)) return;
            if (web.canGoBack()) web.goBack();
            else finish();
        });
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }
}
