# MusicRPC

Amazon Music デスクトップアプリと、Chrome で再生中の YouTube / YouTube Music の再生情報を
Discord の Rich Presence (「○○を再生中」) に表示する Windows 用の常駐ツールです。
Android スマホで再生中の曲も、付属のアプリ (MusicRPC Bridge) を入れれば表示できます。

- ブラウザ拡張不要。Windows のメディアコントロール (SMTC) と Amazon Music のローカルファイルを読むだけ
- 軽量: CPU 約 0.2%、メモリ約 25MB (毎秒の確認で読むのはログの増分だけ)
- タスクトレイ常駐、Windows 起動時の自動開始に対応
- スマホ: 同じ Wi-Fi なら直接、別のネットワーク (モバイル回線など) でも自分の Cloudflare 中継サーバー経由で届く

## 表示内容

| 場所 | 内容 |
|---|---|
| ヘッダー | `Amazon Music を再生中` / `YouTube を再生中` (Discord アプリ名) |
| 1 行目 / 2 行目 | 曲名 / アーティスト (クリックで曲・アーティストのページ) |
| 大きい画像 | ジャケット (Amazon のアート → iTunes → アイコンの順で取得) |
| 大きい画像のホバー | アルバム (年) · Track n/m · ジャンル |
| 小さい画像のホバー | 再生中 · 曲の長さ · 音質 (例: Ultra HD 24bit/48kHz) · シャッフル · リピート |
| プログレスバー | 経過 / 残り時間 (シーク・一時停止に追従) |
| ボタン | 「Amazon Music で聴く」(その曲に直接) / 「YouTube で見る」、「他のサービスで聴く」(song.link) |

ボタンは他の人から見たときだけ表示されます (Discord の仕様)。

**優先順位:** 再生中 (PC の Amazon Music > PC の YouTube > スマホ) > 一時停止中 (同じ順)

スマホの再生は小さい画像のホバーに「スマホ」と表示されます。

## 動作環境

- Windows 10 / 11
- Python 3.10 以上
- Discord デスクトップアプリ
- Amazon Music デスクトップアプリ (v9.x で確認) / Google Chrome
- スマホ連携: Android 8.0 以上。別のネットワークでも使う場合は Cloudflare アカウント (無料プランで可) と Node.js

## セットアップ

### 1. インストール

```
git clone <このリポジトリ>
cd MusicRPC
pip install -r requirements.txt
copy config.example.json config.json
```

### 2. Discord アプリを作る

1. [Discord Developer Portal](https://discord.com/developers/applications) で **New Application** を作成
   - 名前がヘッダーの「○○を再生中」になります。Amazon Music 用は `Amazon Music` などにします
2. **Application ID** を `config.json` の `client_id` に設定
3. YouTube 用に別の名前で表示したい場合は、`YouTube` という名前のアプリをもう 1 つ作り
   `youtube_client_id` に設定 (空なら `client_id` のアプリで表示)

### 3. アイコン画像をアップロード (任意)

```
python music_rpc.py --make-assets
```

`assets/` にできた画像を、各アプリの **Rich Presence → Art Assets** に同じ名前でアップロードします。

| ファイル | 用途 | アップロード先 |
|---|---|---|
| `amazon_music.png` | ジャケットが見つからないときの画像 | Amazon Music 用アプリ |
| `youtube.png` | 同上 (YouTube) | YouTube 用アプリ |
| `playing.png` / `paused.png` | 再生中 / 一時停止の小さいアイコン | 両方 |

### 4. 起動

```
start.bat
```

タスクトレイにアイコンが出ます。右クリックで以下を操作できます。

- Discord に表示 (ON / OFF)
- Windows 起動時に開始
- 設定ファイルを開く / ログを開く
- 終了

動作確認用のコマンド:

```
python music_rpc.py           # コンソールでログを見ながら実行
python music_rpc.py --probe   # 今取得できている再生情報と送信内容を表示して終了
```

## スマホ連携 (Android)

### 1. アプリをビルド

```
cd android
.\build.ps1
```

Android SDK (platforms/android-34 と build-tools) と JDK 8 以上があればビルドできます (Gradle 不要)。
Android Studio の SDK、Unity に付属の SDK / OpenJDK は自動で見つけます。
`android\build\MusicRPC-Bridge.apk` ができます。署名鍵は初回に `android\keystore\` に作られます
(同じ鍵でないと上書きインストールできないので消さないでください)。

### 2. (別のネットワークでも使う場合) 中継サーバーをデプロイ

```
cd relay
npx wrangler login
npx wrangler deploy
npx wrangler secret put RELAY_TOKEN
```

`secret put` では `config.json` の `phone_token` と同じ文字列を入力します
(`phone_token` は初回起動時に自動で作られます)。
表示された `https://musicrpc-relay.<アカウント>.workers.dev` を `config.json` の `relay_url` に設定します。

中継サーバーは最後の再生情報を 1 件だけ保持し、接続中の PC に転送するだけです。
無料プランの範囲で動きます (WebSocket は Hibernation API を使うので待機中は課金されません)。

### 3. スマホにインストール

USB デバッグを有効にしたスマホを USB で接続して:

```
adb install android\build\MusicRPC-Bridge.apk
adb shell am start -n com.musicrpc.bridge/.MainActivity --es token <phone_token> --es relay <relay_url>
```

2 行目で合言葉と中継サーバーが設定されます (アプリの画面で手入力しても OK)。

アプリを開き、「通知へのアクセスを開く」から **MusicRPC Bridge** を許可します。
通知の中身は読みません。再生中の曲 (メディアセッション) を取得するのに、この権限が必要なだけです。

APK ファイルを直接スマホに送ってインストールした場合、Android 13 以降は
「設定 → アプリ → MusicRPC Bridge → ⋮ → 制限付き設定を許可」をしないと通知へのアクセスを許可できません。

### しくみ

- スマホ: 曲・再生 / 一時停止・シーク位置が変わった時だけ送信 (定期送信なし、電池にほぼ影響なし)
- 同じ Wi-Fi: UDP ブロードキャスト (ポート 47615)。合言葉が一致しないものは無視
- 別のネットワーク: 中継サーバーに HTTPS で送信し、PC は WebSocket で受け取る
- 両方から届いた同じ更新は 1 回だけ処理
- 対象アプリは `phone_packages` で指定 (既定: Amazon Music / YouTube / YouTube Music)

## 設定 (`config.json`)

| キー | 既定値 | 説明 |
|---|---|---|
| `client_id` | | Amazon Music 用 Discord アプリ ID (必須) |
| `youtube_client_id` | | YouTube 用 Discord アプリ ID |
| `youtube_music_client_id` | | YouTube Music 用 (空なら `youtube_client_id`) |
| `browser_match` | `["chrome"]` | YouTube を拾うブラウザ。Edge も使うなら `"msedge"` を追加 |
| `youtube_only` | `true` | ブラウザで再生中の YouTube 以外のサイトを無視 |
| `youtube_activity` | `listening` | `watching` にすると YouTube を「視聴中」で表示 |
| `status_display` | `details` | メンバーリストに出す内容: `name` (アプリ名) / `state` (アーティスト) / `details` (曲名) |
| `artwork_lookup` | `true` | iTunes 検索で年・ジャンル・ジャケットを補完 (曲名とアーティスト名が Apple に送信されます) |
| `itunes_countries` | `["JP", "US"]` | iTunes を検索するストアの国 |
| `amazon_music_domain` | `music.amazon.co.jp` | リンク先の Amazon Music ドメイン |
| `show_buttons` | `true` | ボタンを表示 |
| `show_when_paused` | `true` | 一時停止中も表示 |
| `clear_after_paused_min` | `10` | 一時停止がこの分数続いたら非表示 (0 で消さない) |
| `poll_interval_sec` | `1.0` | 確認間隔 (秒) |
| `phone_enabled` | `true` | スマホからの受信を有効にする |
| `phone_port` | `47615` | 同じ Wi-Fi で受信する UDP ポート |
| `phone_token` | (自動生成) | スマホと共通の合言葉。中継サーバーの `RELAY_TOKEN` にも使う |
| `relay_url` | | 中継サーバーの URL (空なら同じ Wi-Fi のみ) |
| `phone_packages` | | スマホの対象アプリ (パッケージ名 → `amazon` / `youtube` / `ytmusic`) |
| `images` | | Art Assets のキー名 |

## しくみ

### YouTube (Chrome)

Chrome が Windows のメディアコントロール (SMTC) に出している曲名・チャンネル名・再生位置を読みます。
YouTube かどうかは Chrome のウィンドウタイトルで判定します (曲が変わったときだけ)。

### Amazon Music

Amazon Music v9.x は SMTC にアーティスト名しか出さないため、Amazon Music 自身のファイルを
**読み取り専用で**参照して補います。書き込みや Amazon への通信は行いません。

| ファイル (`%LOCALAPPDATA%\Amazon Music\`) | 取得する情報 | 読むタイミング |
|---|---|---|
| `Logs\AmazonMusic.log` | 再生中の曲 (ASIN)、再生位置、一時停止、音質、リピート / シャッフル | 毎秒、増えた分だけ |
| `Data\Library\` | ASIN → 曲名・アルバム・アート・ジャンル・長さ | 曲が変わったとき (数 ms) |
| `Data\Local Storage\` | 同上 (ライブラリ外の曲用) | 同上 |

### Discord

Discord クライアントのローカル IPC (名前付きパイプ `discord-ipc-N`) に直接送信します。
送信は曲・状態・シークが変わったときだけで、Discord のレート制限を守るよう間隔を空けています。

## 制限

- YouTube の動画サムネイルは取得できません。iTunes で曲が見つかればそのジャケットを表示し、
  見つからない場合 (MV、歌ってみた など) はアイコンを表示します
- YouTube のタブが裏にあるとサイトを判別できないため、YouTube として扱います
- Amazon Music のアップデートでファイル形式が変わると、曲名などが取得できなくなることがあります
  (その場合はアーティスト名だけを表示します)

## ファイル構成

```
music_rpc.py          本体
config.example.json   設定のひな形 (config.json にコピーして使う)
requirements.txt      依存パッケージ
start.bat             タスクトレイで起動
assets/               Discord にアップロードするアイコン画像
android/              スマホ用アプリ MusicRPC Bridge (build.ps1 でビルド)
relay/                Cloudflare Workers の中継サーバー
```

---

Amazon Music は Amazon.com, Inc. またはその関連会社の、YouTube は Google LLC の商標です。
本ツールはこれらの企業とは関係ありません。
