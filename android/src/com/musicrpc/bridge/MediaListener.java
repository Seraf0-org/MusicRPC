package com.musicrpc.bridge;

import android.content.ComponentName;
import android.content.pm.PackageManager;
import android.media.MediaMetadata;
import android.media.session.MediaController;
import android.media.session.MediaSession;
import android.media.session.MediaSessionManager;
import android.media.session.PlaybackState;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.service.notification.NotificationListenerService;

import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 端末で再生中のメディアセッションを監視し、変化があった時だけ PC へ送る。
 * 通知そのものは読まない (メディアセッションの一覧を取るのに通知リスナー権限が必要なだけ)。
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
                send(o, "stopped");
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
            if (art.startsWith("https://")) {
                o.put("art", art);
            }

            // 同じ曲・同じ状態で、シークもしていなければ送らない
            long startMs = System.currentTimeMillis() - pos;
            String sig = pkg + "\n" + o.optString("title") + "\n" + o.optString("artist") + "\n" + o.optString("state");
            boolean seeked = playing && Math.abs(startMs - lastStartMs) > 3000;
            if (sig.equals(lastSignature) && !seeked) {
                return;
            }
            lastStartMs = startMs;
            send(o, sig);
        } catch (Exception e) {
            lastStatus = "エラー: " + e;
        }
    }

    private void send(JSONObject o, String sig) throws Exception {
        if (sig.equals("stopped") && lastSignature.equals("stopped")) {
            return;
        }
        lastSignature = sig;
        Sender.send(this, o);
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
