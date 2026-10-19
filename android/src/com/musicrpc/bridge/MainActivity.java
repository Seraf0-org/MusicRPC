package com.musicrpc.bridge;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.util.TypedValue;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

/** 設定画面: 通知へのアクセスの許可、中継サーバーと合言葉の入力、送信状況の表示。 */
public class MainActivity extends Activity {

    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView access;
    private TextView status;
    private EditText relay;
    private EditText token;

    private final Runnable refresh = new Runnable() {
        @Override
        public void run() {
            boolean granted = hasAccess();
            access.setText(granted ? "通知へのアクセス: 許可済み" : "通知へのアクセス: 未許可 (下のボタンから許可してください)");
            access.setTextColor(granted ? Color.rgb(80, 220, 120) : Color.rgb(240, 180, 60));
            status.setText(MediaListener.lastStatus);
            handler.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        SharedPreferences prefs = Sender.prefs(this);

        // adb から設定を渡せるようにする: am start -n com.musicrpc.bridge/.MainActivity --es token XXX --es relay URL
        Intent in = getIntent();
        if (in != null && (in.hasExtra("token") || in.hasExtra("relay"))) {
            SharedPreferences.Editor e = prefs.edit();
            if (in.hasExtra("token")) e.putString("token", in.getStringExtra("token"));
            if (in.hasExtra("relay")) e.putString("relay", in.getStringExtra("relay"));
            e.apply();
        }

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        root.setPadding(pad, pad, pad, pad);

        TextView title = new TextView(this);
        title.setText("MusicRPC Bridge");
        title.setTextSize(TypedValue.COMPLEX_UNIT_SP, 24);
        root.addView(title);

        TextView about = label("スマホで再生中の曲を PC の MusicRPC に送り、Discord に表示します。");
        root.addView(about);

        access = label("");
        root.addView(access);

        Button openAccess = new Button(this);
        openAccess.setText("通知へのアクセスを開く");
        openAccess.setOnClickListener(v -> startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)));
        root.addView(openAccess);

        root.addView(label("合言葉 (PC の config.json の phone_token)"));
        token = field(prefs.getString("token", ""), InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);
        root.addView(token);

        root.addView(label("中継サーバー URL (別のネットワークでも使う場合。空なら同じ Wi-Fi のみ)"));
        relay = field(prefs.getString("relay", ""), InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        relay.setHint("https://musicrpc-relay.xxxx.workers.dev");
        root.addView(relay);

        Button save = new Button(this);
        save.setText("保存して今の曲を送信");
        save.setOnClickListener(v -> {
            prefs.edit()
                    .putString("token", token.getText().toString().trim())
                    .putString("relay", relay.getText().toString().trim())
                    .apply();
            MediaListener.resend();
            Toast.makeText(this, "保存しました", Toast.LENGTH_SHORT).show();
        });
        root.addView(save);

        root.addView(label("送信状況"));
        status = label("");
        root.addView(status);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(root);
        setContentView(scroll);
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(refresh);
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(refresh);
    }

    private boolean hasAccess() {
        String enabled = Settings.Secure.getString(getContentResolver(), "enabled_notification_listeners");
        return enabled != null && enabled.contains(new ComponentName(this, MediaListener.class).flattenToString());
    }

    private TextView label(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setPadding(0, dp(12), 0, dp(4));
        return t;
    }

    private EditText field(String value, int type) {
        EditText e = new EditText(this);
        e.setText(value);
        e.setInputType(type);
        e.setSingleLine(true);
        e.setLayoutParams(new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        return e;
    }

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }
}
