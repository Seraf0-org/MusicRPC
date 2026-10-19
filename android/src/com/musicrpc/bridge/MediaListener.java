package com.musicrpc.bridge;

import android.content.ComponentName;
import android.app.Notification;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.Icon;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSession;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 端末で再生中のメディアセッションを監視し、変化があった時だけ PC へ送る。
 * 通知の文字は読まない (メディアセッションの一覧を取るのに通知リスナー権限が必要なだけ)。
 * 再生情報に画像が無いアプリは、再生通知の画像だけを使う (YouTube アプリはどちらにも画像を出さない)。
 */
public class MediaListener extends NotificationListenerService {

    static volatile String lastStatus = "まだ送信していません";

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Map<MediaSession.Token, Pair> watched = new LinkedHashMap<>();
    private MediaSessionManager manager;
    private ComponentName component;
    private String lastSignature = "";
    private long lastStartMs = 0;

    private final Runnable publish = this::publish;
    private final MediaSessionManager.OnActiveSessionsChangedListener sessionsChanged = this::watch;

    private static final class Pair {
        final MediaController controller;
        final MediaController.Callback callback;

        Pair(MediaController controller, MediaController.Callback callback) {
            this.controller = controller;
            this.callback = callback;
        }
    }

    static volatile MediaListener instance;

    /** 設定画面の「今すぐ送信」用。前回と同じ内容でも送り直す。 */
    static void resend() {
        MediaListener l = instance;
        if (l != null) {
            l.handler.post(() -> {
                l.lastSignature = "";
                l.schedule();
            });
        }
    }

    @Override
    public void onListenerConnected() {
        instance = this;
        manager = getSystemService(MediaSessionManager.class);
        component = new ComponentName(this, MediaListener.class);
        try {
            manager.addOnActiveSessionsChangedListener(sessionsChanged, component, handler);
            watch(manager.getActiveSessions(component));
        } catch (SecurityException e) {
            lastStatus = "通知へのアクセスが許可されていません";
        }
    }

    @Override
    public void onListenerDisconnected() {
        instance = null;
        if (manager != null) {
            manager.removeOnActiveSessionsChangedListener(sessionsChanged);
        }
        unwatchAll();
    }

    private void unwatchAll() {
        for (Pair p : watched.values()) {
            p.controller.unregisterCallback(p.callback);
        }
        watched.clear();
    }

    private void watch(List<MediaController> controllers) {
        unwatchAll();
        if (controllers != null) {
            for (MediaController c : controllers) {
                MediaController.Callback cb = new MediaController.Callback() {
                    @Override
                    public void onMetadataChanged(MediaMetadata metadata) {
                        schedule();
                    }

                    @Override
                    public void onPlaybackStateChanged(PlaybackState state) {
                        schedule();
                    }

                    @Override
                    public void onSessionDestroyed() {
                        schedule();
                    }
                };
                c.registerCallback(cb, handler);
                watched.put(c.getSessionToken(), new Pair(c, cb));
            }
        }
        schedule();
    }

    /** 曲送り直後などはイベントが連続するので、まとめてから送る。 */
    private void schedule() {
        handler.removeCallbacks(publish);
        handler.postDelayed(publish, 500);
    }

    private void publish() {
        MediaController best = null;
        int bestRank = Integer.MAX_VALUE;
        // getActiveSessions は優先度順なので、同じ状態なら先頭を採用する
        int order = 0;
        for (Pair p : watched.values()) {
            PlaybackState ps = p.controller.getPlaybackState();
            MediaMetadata md = p.controller.getMetadata();
            if (ps == null || md == null || title(md).isEmpty()) {
                continue;
            }
            int s = ps.getState();
            int rank;
            if (s == PlaybackState.STATE_PLAYING || s == PlaybackState.STATE_BUFFERING) {
                rank = 0;
            } else if (s == PlaybackState.STATE_PAUSED) {
                rank = 100;
            } else {
                continue;
            }
            rank += order++;
            if (rank < bestRank) {
                bestRank = rank;
                best = p.controller;
            }
        }

        try {
            JSONObject o = new JSONObject();
            if (best == null) {
                o.put("state", "stopped");
                send(o, "stopped", null, null);
                return;
            }
            MediaMetadata md = best.getMetadata();
            PlaybackState ps = best.getPlaybackState();
            boolean playing = ps.getState() != PlaybackState.STATE_PAUSED;

            long pos = ps.getPosition();
            if (playing && ps.getLastPositionUpdateTime() > 0) {
                pos += (long) ((SystemClock.elapsedRealtime() - ps.getLastPositionUpdateTime()) * ps.getPlaybackSpeed());
            }
            long duration = md.getLong(MediaMetadata.METADATA_KEY_DURATION);
            if (duration > 0) {
                pos = Math.min(Math.max(pos, 0), duration);
            }

            String pkg = best.getPackageName();
            o.put("state", playing ? "playing" : "paused");
            o.put("pkg", pkg);
            o.put("app", appLabel(pkg));
            o.put("title", title(md));
            o.put("artist", text(md, MediaMetadata.METADATA_KEY_ARTIST, MediaMetadata.METADATA_KEY_DISPLAY_SUBTITLE));
            o.put("album", text(md, MediaMetadata.METADATA_KEY_ALBUM));
            o.put("album_artist", text(md, MediaMetadata.METADATA_KEY_ALBUM_ARTIST));
            o.put("track", md.getLong(MediaMetadata.METADATA_KEY_TRACK_NUMBER));
            o.put("track_count", md.getLong(MediaMetadata.METADATA_KEY_NUM_TRACKS));
            o.put("duration", duration / 1000.0);
            o.put("position", pos / 1000.0);
            String art = text(md, MediaMetadata.METADATA_KEY_ALBUM_ART_URI, MediaMetadata.METADATA_KEY_ART_URI,
                    MediaMetadata.METADATA_KEY_DISPLAY_ICON_URI);
            Bitmap bitmap = null;
            if (art.startsWith("https://")) {
                o.put("art", art);
            } else {
                bitmap = md.getBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART);
                if (bitmap == null) bitmap = md.getBitmap(MediaMetadata.METADATA_KEY_ART);
                if (bitmap == null) bitmap = md.getBitmap(MediaMetadata.METADATA_KEY_DISPLAY_ICON);
                // YouTube などは再生情報に画像を入れないので、再生通知の画像を使う
                if (bitmap == null) bitmap = notificationArt(best);
            }

            // 同じ曲・同じ状態で、シークもしていなければ送らない (画像が後から届いた時は送り直す)
            long startMs = System.currentTimeMillis() - pos;
            String track = pkg + "\n" + o.optString("title") + "\n" + o.optString("artist");
            String sig = track + "\n" + o.optString("state") + "\n" + (bitmap != null || o.has("art"));
            boolean seeked = playing && Math.abs(startMs - lastStartMs) > 3000;
            if (sig.equals(lastSignature) && !seeked) {
                return;
            }
            lastStartMs = startMs;
            send(o, sig, bitmap, track);
        } catch (Exception e) {
            lastStatus = "エラー: " + e;
        }
    }

    private void send(JSONObject o, String sig, Bitmap art, String track) throws Exception {
        if (sig.equals("stopped") && lastSignature.equals("stopped")) {
            return;
        }
        lastSignature = sig;
        Sender.send(this, o, art, track);
    }

    /** 再生通知が後から出ることがあるので、監視中のアプリの通知が出たら送り直しを検討する。 */
    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        for (Pair p : watched.values()) {
            if (p.controller.getPackageName().equals(sbn.getPackageName())) {
                schedule();
                return;
            }
        }
    }

    /** そのメディアセッションの再生通知に付いている画像 (YouTube なら動画のサムネイル)。文字は読まない。 */
    private Bitmap notificationArt(MediaController c) {
        try {
            for (StatusBarNotification sbn : getActiveNotifications()) {
                if (!sbn.getPackageName().equals(c.getPackageName())) {
                    continue;
                }
                Notification n = sbn.getNotification();
                Object token = n.extras.get(Notification.EXTRA_MEDIA_SESSION);
                if (token == null || !token.equals(c.getSessionToken())) {
                    continue;
                }
                Icon icon = n.getLargeIcon();
                if (icon != null) {
                    Drawable d = icon.loadDrawable(this);
                    if (d instanceof BitmapDrawable) {
                        return ((BitmapDrawable) d).getBitmap();
                    }
                    if (d != null && d.getIntrinsicWidth() > 0 && d.getIntrinsicHeight() > 0) {
                        Bitmap b = Bitmap.createBitmap(d.getIntrinsicWidth(), d.getIntrinsicHeight(), Bitmap.Config.ARGB_8888);
                        Canvas canvas = new Canvas(b);
                        d.setBounds(0, 0, canvas.getWidth(), canvas.getHeight());
                        d.draw(canvas);
                        return b;
                    }
                }
            }
        } catch (Exception e) {
            // 通知を取れない時は画像なしで送る
        }
        return null;
    }

    private String appLabel(String pkg) {
        try {
            PackageManager pm = getPackageManager();
            return pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString();
        } catch (Exception e) {
            return pkg;
        }
    }

    private static String title(MediaMetadata md) {
        return text(md, MediaMetadata.METADATA_KEY_TITLE, MediaMetadata.METADATA_KEY_DISPLAY_TITLE);
    }

    private static String text(MediaMetadata md, String... keys) {
        for (String k : keys) {
            CharSequence v = md.getText(k);
            if (v != null && v.length() > 0) {
                return v.toString();
            }
        }
        return "";
    }
}
