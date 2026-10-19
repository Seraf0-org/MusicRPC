package com.musicrpc.bridge;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;

import org.json.JSONObject;

import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.LinkedHashSet;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** 再生情報を PC へ送る。同じ Wi-Fi ならブロードキャスト、中継サーバーが設定されていればそちらにも。 */
final class Sender {

    static final int PORT = 47615;
    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();
    private static long seq = 0;
    private static final String SESSION = Long.toHexString(System.nanoTime());

    private Sender() {
    }

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences("settings", Context.MODE_PRIVATE);
    }

    static void send(Context ctx, JSONObject o) throws Exception {
        Context app = ctx.getApplicationContext();
        SharedPreferences p = prefs(app);
        String token = p.getString("token", "").trim();
        String relay = p.getString("relay", "").trim();
        o.put("v", 1);
        o.put("session", SESSION);
        o.put("seq", ++seq);
        o.put("device", android.os.Build.MODEL);

        EXECUTOR.execute(() -> {
            StringBuilder result = new StringBuilder();
            try {
                JSONObject lan = new JSONObject(o.toString());
                lan.put("token", token);
                int n = broadcast(app, lan.toString().getBytes(StandardCharsets.UTF_8));
                result.append(n > 0 ? "Wi-Fi ✓" : "Wi-Fi なし");
            } catch (Exception e) {
                result.append("Wi-Fi ✗");
            }
            if (!relay.isEmpty()) {
                try {
                    int code = post(relay, token, o.toString().getBytes(StandardCharsets.UTF_8));
                    result.append(code == 200 ? " / 中継 ✓" : " / 中継 ✗ (" + code + ")");
                } catch (Exception e) {
                    result.append(" / 中継 ✗");
                }
            }
            String time = new SimpleDateFormat("HH:mm:ss", Locale.JAPAN).format(new Date());
            MediaListener.lastStatus = time + " " + describe(o) + "\n" + result;
        });
    }

    private static String describe(JSONObject o) {
        String state = o.optString("state");
        if (state.equals("stopped")) {
            return "停止";
        }
        return (state.equals("playing") ? "▶ " : "⏸ ") + o.optString("artist") + " - " + o.optString("title");
    }

    /** Wi-Fi / 有線のネットワークにだけブロードキャストする。送った宛先の数を返す。 */
    private static int broadcast(Context ctx, byte[] data) throws Exception {
        ConnectivityManager cm = ctx.getSystemService(ConnectivityManager.class);
        Set<InetAddress> targets = new LinkedHashSet<>();
        for (Network n : cm.getAllNetworks()) {
            NetworkCapabilities caps = cm.getNetworkCapabilities(n);
            LinkProperties lp = cm.getLinkProperties(n);
            if (caps == null || lp == null) {
                continue;
            }
            if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                    && !caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) {
                continue;
            }
            for (LinkAddress la : lp.getLinkAddresses()) {
                if (la.getAddress() instanceof Inet4Address) {
                    targets.add(broadcastAddress((Inet4Address) la.getAddress(), la.getPrefixLength()));
                }
            }
        }
        if (targets.isEmpty()) {
            return 0;
        }
        try (DatagramSocket s = new DatagramSocket()) {
            s.setBroadcast(true);
            for (InetAddress a : targets) {
                s.send(new DatagramPacket(data, data.length, a, PORT));
            }
        }
        return targets.size();
    }

    private static InetAddress broadcastAddress(Inet4Address addr, int prefix) throws Exception {
        byte[] b = addr.getAddress();
        int ip = ((b[0] & 0xff) << 24) | ((b[1] & 0xff) << 16) | ((b[2] & 0xff) << 8) | (b[3] & 0xff);
        int mask = prefix == 0 ? 0 : -1 << (32 - prefix);
        int bc = ip | ~mask;
        return InetAddress.getByAddress(new byte[]{
                (byte) (bc >>> 24), (byte) (bc >>> 16), (byte) (bc >>> 8), (byte) bc});
    }

    private static int post(String relay, String token, byte[] body) throws Exception {
        String base = relay.endsWith("/") ? relay.substring(0, relay.length() - 1) : relay;
        HttpURLConnection c = (HttpURLConnection) new URL(base + "/np").openConnection();
        try {
            c.setConnectTimeout(5000);
            c.setReadTimeout(5000);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            c.setRequestProperty("Authorization", "Bearer " + token);
            try (OutputStream out = c.getOutputStream()) {
                out.write(body);
            }
            return c.getResponseCode();
        } finally {
            c.disconnect();
        }
    }
}
