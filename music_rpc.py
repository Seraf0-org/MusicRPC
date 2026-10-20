"""Amazon Music / YouTube -> Discord Rich Presence

Windows のメディアコントロール (SMTC) から再生情報を読み取り Discord に表示する。
Android の MusicRPC Bridge からの再生情報も受け取る (同じ Wi-Fi は UDP、それ以外は中継サーバー経由)。
優先順位: 再生中 (PC の Amazon Music > PC の YouTube > スマホ) > 一時停止中 (同じ順)

  pythonw music_rpc.py           タスクトレイ常駐
  python  music_rpc.py           コンソールで実行 (ログを表示)
  python  music_rpc.py --probe   SMTC の生データを表示して終了
  python  music_rpc.py --make-assets   Discord にアップロードする画像を assets/ に生成
"""

import asyncio
import ctypes
import ctypes.wintypes
import json
import logging
import os
import re
import struct
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
LOG_PATH = BASE / "rpc.log"

DEFAULT_CONFIG = {
    "client_id": "",
    "youtube_client_id": "",
    "youtube_music_client_id": "",
    "amazon_match": "Amazon.Music",
    "browser_match": ["chrome", "msedge", "brave", "comet", "vivaldi", "opera", "chromium", "firefox",
                      "308046b0af4a39cb"],
    "detect_other_browsers": True,
    "youtube_only": True,
    "youtube_activity": "listening",
    "poll_interval_sec": 1.0,
    "status_display": "details",
    "artwork_lookup": True,
    "itunes_countries": ["JP", "US"],
    "amazon_music_domain": "music.amazon.co.jp",
    "show_buttons": True,
    "show_when_paused": True,
    "clear_after_paused_min": 10,
    "phone_enabled": True,
    "phone_port": 47615,
    "phone_token": "",
    "relay_url": "",
    "upload_thumbnails": True,
    "youtube_search_thumbnails": False,
    "phone_packages": {
        "com.amazon.mp3": "amazon",
        "com.google.android.youtube": "youtube",
        "com.google.android.apps.youtube.music": "ytmusic",
    },
    "images": {
        "amazon": "amazon_music",
        "youtube": "youtube",
        "ytmusic": "youtube",
        "playing": "playing",
        "paused": "paused",
    },
}

SOURCE_LABEL = {"amazon": "Amazon Music", "youtube": "YouTube", "ytmusic": "YouTube Music"}

log = logging.getLogger("musicrpc")


def new_token():
    import secrets
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # 見間違えやすい文字を除く
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(5))


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    user = {}
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception as e:
            log.error("config.json を読めません: %s", e)
            return cfg
    for key in ("images", "phone_packages"):
        cfg[key] = {**cfg[key], **user.pop(key, {})}
    cfg.update(user)
    if not CONFIG_PATH.exists() or not cfg["phone_token"]:
        cfg["phone_token"] = cfg["phone_token"] or new_token()
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg


# ---------------------------------------------------------------- Discord IPC

class DiscordError(Exception):
    pass


class DiscordIPC:
    """Discord クライアントのローカル IPC (名前付きパイプ) に直接話す最小実装。"""

    OP_HANDSHAKE, OP_FRAME, OP_CLOSE = 0, 1, 2

    def __init__(self, client_id):
        self.client_id = str(client_id)
        self.pipe = None
        self.user = {}

    @property
    def connected(self):
        return self.pipe is not None

    def connect(self):
        for i in range(10):
            try:
                self.pipe = open(rf"\\.\pipe\discord-ipc-{i}", "r+b", buffering=0)
                break
            except OSError:
                continue
        else:
            raise DiscordError("Discord が起動していません")
        try:
            self._send(self.OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
            op, data = self._recv()
        except Exception:
            self.close()
            raise
        if op == self.OP_CLOSE or data.get("evt") != "READY":
            self.close()
            raise DiscordError(f"ハンドシェイク失敗 (client_id を確認): {data.get('message') or data}")
        self.user = data.get("data", {}).get("user", {})

    def close(self):
        if self.pipe:
            try:
                self.pipe.close()
            except OSError:
                pass
        self.pipe = None

    def _send(self, op, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.pipe.write(struct.pack("<II", op, len(body)) + body)

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.pipe.read(n - len(buf))
            if not chunk:
                raise DiscordError("Discord との接続が切れました")
            buf += chunk
        return buf

    def _recv(self):
        op, n = struct.unpack("<II", self._read_exact(8))
        return op, json.loads(self._read_exact(n).decode("utf-8"))

    def set_activity(self, activity):
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        }
        try:
            self._send(self.OP_FRAME, payload)
            op, data = self._recv()
        except (OSError, struct.error, ValueError) as e:
            self.close()
            raise DiscordError(f"送信失敗: {e}") from e
        if op == self.OP_CLOSE:
            self.close()
            raise DiscordError(f"Discord が接続を閉じました: {data}")
        if data.get("evt") == "ERROR":
            raise ValueError(data.get("data", {}).get("message", data))
        return data


# ---------------------------------------------------------------- browser tab detection

_user32 = ctypes.windll.user32
_EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def window_titles():
    titles = []

    def cb(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            n = _user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                titles.append(buf.value)
        return True

    _user32.EnumWindows(_EnumProc(cb), 0)
    return titles


_site_cache = {}


def youtube_tab_visible():
    return any(" - YouTube" in wt and "YouTube Music" not in wt for wt in window_titles())


def browser_site(title, album, guess=True):
    """ブラウザのメディアがどのサイトのものか推定する。見つかったらタイトルごとに覚えておく。

    返り値: "youtube" / "ytmusic" / "other" / None(タブが裏にあって判別不能)
    guess=True なら、判別できない時にアルバム名の有無で YouTube Music と推定する。
    """
    site = _site_cache.get(title)
    if site is None:
        for wt in window_titles() if len(title) >= 4 else ():
            if title in wt:
                site = "ytmusic" if "YouTube Music" in wt else "youtube" if "YouTube" in wt else "other"
                break
        if site is not None:
            if len(_site_cache) > 200:
                _site_cache.clear()
            _site_cache[title] = site
    if site is None and guess and album:
        return "ytmusic"  # YouTube Music はアルバム名を出す。YouTube は出さない
    return site


# ---------------------------------------------------------------- SMTC

STATUS_NAMES = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}
REPEAT_NAMES = {0: None, 1: "1曲", 2: "全曲"}


@dataclass
class MediaState:
    source: str = "amazon"
    app_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    track_number: int = 0
    track_count: int = 0
    genres: list = field(default_factory=list)
    status: str = "closed"
    position: float | None = None
    duration: float | None = None
    shuffle: bool | None = None
    repeat: str | None = None
    asin: str = ""
    album_asin: str = ""
    artwork: str | None = None
    genre: str | None = None
    quality: str | None = None
    device: str = "pc"  # "pc" / "phone"
    thumbnail: object = field(default=None, repr=False, compare=False)  # SMTC のサムネイル (画像データへの参照)
    link: str | None = None  # 曲・動画そのもののページ (分かる場合)

    @property
    def key(self):
        return (self.device, self.source, self.title, self.artist, self.album)

    @property
    def label(self):
        return SOURCE_LABEL[self.source] + (" (スマホ)" if self.device == "phone" else "")

    @property
    def playing(self):
        return self.status == "playing"


def _seconds(td):
    return td.total_seconds() if td is not None else 0.0


async def read_session(session, source):
    st = MediaState(source=source, app_id=session.source_app_user_model_id or "")
    info = session.get_playback_info()
    st.status = STATUS_NAMES.get(int(info.playback_status), "closed")
    if st.status not in ("playing", "paused"):
        return st
    try:
        p = await session.try_get_media_properties_async()
        st.title = p.title or ""
        st.artist = p.artist or ""
        st.album = p.album_title or ""
        st.album_artist = p.album_artist or ""
        st.track_number = p.track_number or 0
        st.track_count = p.album_track_count or 0
        st.genres = [g for g in (p.genres or []) if g]
        st.thumbnail = p.thumbnail
    except Exception as e:
        log.debug("media properties error: %s", e)
    st.shuffle = info.is_shuffle_active
    try:
        repeat_mode = info.auto_repeat_mode
        st.repeat = REPEAT_NAMES.get(int(repeat_mode)) if repeat_mode is not None else None
    except Exception as e:
        # Optional playback details must not block updates for every session.
        log.debug("repeat mode error (%s): %s", st.app_id, e)

    t = session.get_timeline_properties()
    end = _seconds(t.end_time) - _seconds(t.start_time)
    if end > 0:
        pos = _seconds(t.position) - _seconds(t.start_time)
        updated = t.last_updated_time
        if st.playing and updated and updated.year > 2000:
            pos += (datetime.now(timezone.utc) - updated).total_seconds()
        st.duration = end
        st.position = max(0.0, min(pos, end))
    return st


def media_rank(st):
    """小さいほど優先。再生中 > 一時停止、同じ状態なら PC の Amazon Music > PC の YouTube > スマホ。"""
    order = 2 if st.device == "phone" else (0 if st.source == "amazon" else 1)
    return (0 if st.playing else 10) + order


async def pick_media(manager, cfg, enrich_amazon=None, extra=()):
    amazon = cfg["amazon_match"].lower()
    browsers = [b.lower() for b in cfg["browser_match"]]
    candidates = []
    for s in manager.get_sessions():
        app_id = (s.source_app_user_model_id or "").lower()
        known_browser = False
        if amazon and amazon in app_id:
            source = "amazon"
        elif any(b in app_id for b in browsers):
            source, known_browser = "youtube", True
        elif cfg["detect_other_browsers"]:
            source = "youtube"  # 一覧に無いブラウザ: YouTube のウィンドウが見つかった時だけ採用
        else:
            continue
        st = await read_session(s, source)
        if st.status not in ("playing", "paused"):
            continue
        if source == "amazon" and enrich_amazon:
            await enrich_amazon(st)
        if not st.title:
            continue
        if source == "youtube":
            site = browser_site(st.title, st.album, guess=known_browser)
            if not known_browser and site not in ("youtube", "ytmusic"):
                continue  # Spotify など、ブラウザ以外のアプリ
            if site is None and (st.duration or 0) < 180 and youtube_tab_visible():
                continue  # 広告: タブの題名は動画のままで、再生中の題名だけが広告になる
            if site == "other" and cfg["youtube_only"]:
                continue
            st.source = site if site in ("youtube", "ytmusic") else "youtube"
        candidates.append(st)
    candidates += [st for st in extra if st is not None]
    if not candidates:
        return None
    return min(candidates, key=media_rank)


# ---------------------------------------------------------------- Amazon Music local data
#
# Amazon Music (v9.x) は SMTC にアーティスト名しか出さないので、アプリ自身のファイルから補う。
#   Logs/AmazonMusic.log    再生開始 (ASIN)・再生位置 (10秒フラグメント)・一時停止・音質・リピート/シャッフル
#   Data/Library/*.ldb|log  ASIN → 曲名/アルバム/アート/ジャンル/長さ (ライブラリ・キャッシュ済みの曲)
#   Data/Local Storage/*    ASIN → 曲名/長さ/アルバム/アート (再生イベントの記録。ライブラリ外の曲用)

AMAZON_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Amazon Music"

_LOG_TS = re.compile(r"^(\d{6}):(\d{6})")
_LOG_PLAY = re.compile(r"updateStreamingStatus for uri: asin(?:_downloaded)?://([A-Z0-9]{10}), status: PLAY")
_LOG_FRAG = re.compile(r"track \((\d+)\) changed to fragment (\d+) of (\d+)")
_LOG_PAUSE = re.compile(r"playbackPaused : Received callback with paused state: ([01])")
_LOG_QUAL = re.compile(r"Audio Quality: ([A-Z]+)\d*, bitrate: (\d+), bit depth: (\d+), sample rate: (\d+)")
_LOG_SETTINGS = re.compile(r"repeat = (\w+) , shuffle = (\w+)")


class AmazonLog:
    """AmazonMusic.log の末尾を追いかけて、今の曲と再生位置を保持する。"""

    FRAGMENT_SEC = 10.0

    def __init__(self, path=AMAZON_DIR / "Logs" / "AmazonMusic.log"):
        self.path = path
        self.offset = 0
        self.asin = None
        self.started = 0.0
        self.frag_track = None
        self.frag = None  # (index, count)
        self.quality = None
        self.repeat = None
        self.shuffle = None
        self._pos, self._base, self._running = 0.0, 0.0, False
        self._prime()

    def _prime(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        self.offset = max(0, size - 2_000_000)  # 起動時は直近 2MB だけ読んで状態を復元
        self.poll()

    def poll(self):
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset = 0  # ログがローテートされた
            if size == self.offset:
                return
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(size - self.offset)
        except OSError:
            return
        cut = chunk.rfind(b"\n") + 1  # 書きかけの行は次回に回す
        self.offset += cut
        for line in chunk[:cut].decode("utf-8", "replace").splitlines():
            self._line(line)

    @staticmethod
    def _time(line):
        m = _LOG_TS.match(line)
        if not m:
            return None
        d, t = m.groups()
        dt = datetime(2000 + int(d[:2]), int(d[2:4]), int(d[4:]), int(t[:2]), int(t[2:4]), int(t[4:]),
                      tzinfo=timezone.utc)
        return dt.timestamp() + 0.5  # ログは秒単位で切り捨てなので中央値に寄せる

    def _line(self, line):
        if "Browser INFO" not in line and "PlayerSettings" not in line:
            return
        if m := _LOG_PLAY.search(line):
            t = self._time(line)
            self.asin, self.started, self.frag_track, self.frag = m[1], t, None, None
            self._pos, self._base, self._running = 0.0, t, True
        elif m := _LOG_FRAG.search(line):
            if self.frag_track is None:
                self.frag_track = m[1]
            if m[1] == self.frag_track:
                self.frag = (int(m[2]), int(m[3]))
                self._pos, self._base, self._running = int(m[2]) * self.FRAGMENT_SEC, self._time(line), True
        elif m := _LOG_PAUSE.search(line):
            t = self._time(line)
            if m[1] == "1" and self._running:
                self._pos += t - self._base
                self._running = False
            elif m[1] == "0" and not self._running:
                self._base, self._running = t, True
        elif m := _LOG_QUAL.search(line):
            self.quality = format_quality(m[1], int(m[3]), int(m[4]))
        elif m := _LOG_SETTINGS.search(line):
            self.repeat = {"ONE": "1曲", "ALL": "全曲"}.get(m[1].upper())
            self.shuffle = m[2].lower() == "true"

    def position(self):
        return self._pos + (time.time() - self._base if self._running else 0.0)


def format_quality(kind, depth, rate):
    if not depth or not rate:
        return None
    label = {"UHD": "Ultra HD", "HD": "HD", "SD": "SD"}.get(kind, kind)
    return f"{label} {depth}bit/{rate / 1000:g}kHz"


def amazon_art(url, size=600):
    m = re.match(r"(https://m\.media-amazon\.com/images/I/[^._/]+)", url or "")
    return f"{m[1]}._SX{size}_.jpg" if m else None


def _mapped_files(folder, patterns):
    files = []
    for pat in patterns:
        files += folder.glob(pat)
    # .log が一番新しく、.ldb は番号が大きいほど新しい
    return sorted(files, key=lambda p: (p.suffix == ".log", p.stem), reverse=True)


def _scan(files, needle, parse):
    import mmap
    for path in files:
        try:
            with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as buf:
                i = len(buf)
                while (i := buf.rfind(needle, 0, i)) >= 0:
                    if result := parse(buf, i):
                        return result
        except (OSError, ValueError):
            continue
    return None


def _library_record(buf, i):
    start = max(0, buf.rfind(b"serialization::archive", max(0, i - 4000), i))
    end = buf.find(b"serialization::archive", i)
    end = end if 0 <= end - i < 4000 else min(len(buf), i + 4000)

    def text(name):
        key = struct.pack("<I", len(name)) + name + b"\x00\x00\x00\x00"
        j = buf.find(key, start, end)
        if j < 0:
            return ""
        j += len(key)
        for skip in range(10):  # albumArt などは長さの前に数バイトの型情報が入る
            n = struct.unpack_from("<I", buf, j + skip)[0]
            if 0 < n < 2000:
                try:
                    return bytes(buf[j + skip + 4: j + skip + 4 + n]).decode("utf-8")
                except UnicodeDecodeError:
                    return ""
        return ""

    title = text(b"name")
    if not title:
        return None
    j = buf.find(struct.pack("<I", 8) + b"duration" + struct.pack("<I", 6), start, end)
    duration = struct.unpack_from("<I", buf, j + 16)[0] if j >= 0 else 0
    return {
        "title": title,
        "artist": text(b"artistName"),
        "album": text(b"albumName"),
        "album_artist": text(b"albumArtistName"),
        "album_asin": text(b"albumAsin"),
        "artwork": amazon_art(text(b"albumArt")),
        "genre": text(b"albumPrimaryGenre").split("/")[0] or None,
        "duration": duration if 0 < duration < 36000 else None,
    }


def _utf16_tokens(buf, i, limit=2500, max_tokens=24):
    out, end = [], min(len(buf), i + limit)
    while i < end - 4 and len(out) < max_tokens:
        n = struct.unpack_from("<I", buf, i)[0]
        if 0 < n <= 512 and i + 4 + 2 * n <= end:
            try:
                s = bytes(buf[i + 4: i + 4 + 2 * n]).decode("utf-16-le")
                if s.isprintable():
                    out.append(s)
                    i += 4 + 2 * n
                    continue
            except UnicodeDecodeError:
                pass
        i += 1
    return out


def _metric_record(asin):
    def parse(buf, i):
        toks = _utf16_tokens(buf, i + 14, limit=2500)
        if len(toks) < 4 or asin not in toks[:4]:
            return None
        art = next((k for k, t in enumerate(toks) if t.startswith("https://m.media-amazon.com/images/")), None)
        album = toks[art - 2] if art and art >= 2 and re.fullmatch(r"B0[A-Z0-9]{8}", toks[art - 1]) else ""
        return {
            "title": toks[0],
            "album": album,
            "album_asin": toks[art - 1] if album else "",
            "artwork": amazon_art(toks[art]) if art is not None else None,
            "duration": int(toks[1]) if toks[1].isdigit() else None,
        }
    return parse


def amazon_lookup(asin):
    data = AMAZON_DIR / "Data"
    needle = struct.pack("<I", 4) + b"asin" + b"\x00\x00\x00\x00" + struct.pack("<I", 10) + asin.encode()
    rec = _scan(_mapped_files(data / "Library", ["*.log", "*.ldb"]), needle, _library_record)
    if rec:
        return rec
    files = _mapped_files(data / "Local Storage", ["*.log", "*.ldb"])
    files += _mapped_files(data / "App Cache" / "Local Storage" / "leveldb", ["*.log", "*.ldb"])
    return _scan(files, struct.pack("<I", 10) + asin.encode(), _metric_record(asin))


# ---------------------------------------------------------------- thumbnails
#
# ブラウザが SMTC に渡すサムネイルは画像データだけで URL が無い。Discord には URL が必要なので、
# 中継サーバーに置いてその URL を使う。

async def read_thumbnail(ref):
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
    stream = await ref.open_read_async()
    size = int(stream.size)
    if not 0 < size <= 8_000_000:
        return None
    buf = Buffer(size)
    await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
    return bytes(buf)


def square_jpeg(data, size=512):
    """黒帯を除いて中央を正方形に切り抜き、JPEG にする。"""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    bbox = im.convert("L").point(lambda p: 255 if p > 24 else 0).getbbox()
    if bbox and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > im.width * im.height * 0.5:
        im = im.crop(bbox)
    side = min(im.size)
    left, top = (im.width - side) // 2, (im.height - side) // 2
    im = im.crop((left, top, left + side, top + side))
    if side > size:
        im = im.resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue()


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


def _http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "ja,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def youtube_search(title, channel):
    """YouTube の検索結果から、題名が一致する動画の ID を探す。

    検索ページの自動取得は YouTube の利用規約で認められていないため、既定ではオフ
    (config の youtube_search_thumbnails)。新しい動画ごとに 1 回だけ呼ぶ。
    """
    channel = clean_artist(channel)
    html = _http_get("https://www.youtube.com/results?" + urllib.parse.urlencode(
        {"search_query": f"{title} {channel}".strip()})).decode("utf-8", "replace")
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    if not m:
        return None
    stack, videos = [json.loads(m[1])], []
    while stack and len(videos) < 20:
        node = stack.pop()
        if isinstance(node, dict):
            if "videoRenderer" in node:
                v = node["videoRenderer"]
                runs = lambda k: "".join(r.get("text", "") for r in v.get(k, {}).get("runs", []))
                videos.append((v.get("videoId"), runs("title"), runs("ownerText") or runs("longBylineText")))
            else:
                stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    nt, nc = _norm(title), _norm(channel)
    for vid, vtitle, vchannel in videos:  # 題名が同じもの (チャンネルも同じならなお良い)
        if vid and _norm(vtitle) == nt and (not nc or nc in _norm(vchannel) or _norm(vchannel) in nc):
            return vid
    for vid, vtitle, vchannel in videos:
        if vid and _norm(vtitle) == nt:
            return vid
    for vid, vtitle, vchannel in videos:  # YouTube Music の曲名は動画題名の一部のことが多い
        if vid and len(nt) > 3 and nt in _norm(vtitle) and nc and (nc in _norm(vchannel) or _norm(vchannel) in nc):
            return vid
    return None


def youtube_thumbnail(video_id):
    for name in ("maxresdefault", "hqdefault"):
        try:
            return _http_get(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg")
        except urllib.error.HTTPError:
            continue
    return None


def upload_art(relay_url, token, jpeg):
    req = urllib.request.Request(relay_url.rstrip("/") + "/art", data=jpeg, method="PUT", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "image/jpeg", "User-Agent": "MusicRPC/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["url"]


# ---------------------------------------------------------------- Android (MusicRPC Bridge)
#
# スマホのアプリは曲・再生状態・シーク位置が変わった時だけ送ってくる。
# 同じ Wi-Fi なら UDP ブロードキャスト、中継サーバーがあれば WebSocket でも届くので、
# (session, seq) で重複を捨てる。

class PhoneHub:
    STALE_SEC = 15 * 60  # 曲の長さが分からない場合に、更新が無ければ消すまでの時間

    def __init__(self):
        self.msg = None
        self.received = 0.0
        self.last_id = (None, -1)

    def update(self, msg, received):
        if not isinstance(msg, dict) or msg.get("test"):
            return
        session, seq = msg.get("session"), int(msg.get("seq") or 0)
        if session == self.last_id[0] and seq <= self.last_id[1]:
            return  # もう一方の経路で受け取り済み
        self.last_id = (session, seq)
        self.msg, self.received = msg, received
        log.info("スマホ: %s %s - %s", msg.get("state"), msg.get("artist", ""), msg.get("title", ""))

    def current(self, cfg):
        m = self.msg
        if not m or m.get("state") not in ("playing", "paused"):
            return None
        source = cfg["phone_packages"].get(m.get("pkg", ""))
        if source not in SOURCE_LABEL:
            return None
        playing = m["state"] == "playing"
        elapsed = time.time() - self.received
        duration = float(m.get("duration") or 0) or None
        position = float(m.get("position") or 0) + (elapsed if playing else 0)
        if playing and duration and position > duration + 30:
            return None  # 曲が終わったのに続報が無い (圏外になった等)
        if playing and not duration and elapsed > self.STALE_SEC:
            return None
        art = m.get("art") or ""
        return MediaState(
            source=source, device="phone", app_id=m.get("pkg", ""),
            title=m.get("title", ""), artist=m.get("artist", ""), album=m.get("album", ""),
            album_artist=m.get("album_artist", ""),
            track_number=int(m.get("track") or 0), track_count=int(m.get("track_count") or 0),
            status=m["state"], position=min(position, duration) if duration else position, duration=duration,
            artwork=art if art.startswith("https://") else None,
        )


class PhoneUdp(asyncio.DatagramProtocol):
    def __init__(self, hub, token):
        self.hub, self.token = hub, token

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8"))
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.pop("token", None) != self.token:
            return  # 合言葉が違う端末からは受け取らない
        self.hub.update(msg, time.time())


async def relay_loop(hub, url, token, stop):
    try:
        import websockets
    except ImportError:
        log.warning("websockets が無いため中継サーバーを使えません (pip install websockets)")
        return
    ws_url = re.sub(r"^http", "ws", url.rstrip("/")) + "/ws"
    headers = {"Authorization": f"Bearer {token}"}
    delay = 5
    while not stop.is_set():
        try:
            try:
                conn = websockets.connect(ws_url, additional_headers=headers, ping_interval=60, open_timeout=15)
            except TypeError:  # websockets < 14
                conn = websockets.connect(ws_url, extra_headers=headers, ping_interval=60, open_timeout=15)
            async with conn as ws:
                log.info("中継サーバーに接続しました")
                delay = 5
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    hub.update(msg, time.time() - float(msg.get("age_ms") or 0) / 1000)
        except Exception as e:
            if stop.is_set():
                return
            log.info("中継サーバーとの接続が切れました (%s)。%d 秒後に再接続します", type(e).__name__, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


class PlayClock:
    """SMTC が再生位置を出さない場合のための自前ストップウォッチ。"""

    def __init__(self):
        self.key = None
        self.elapsed = 0.0
        self.since = None

    def update(self, key, playing):
        now = time.time()
        if key != self.key:
            self.key, self.elapsed = key, 0.0
            self.since = now if playing else None
        elif playing and self.since is None:
            self.since = now
        elif not playing and self.since is not None:
            self.elapsed += now - self.since
            self.since = None

    def position(self):
        return self.elapsed + (time.time() - self.since if self.since else 0.0)

    def wrap(self, duration):
        # 同じ曲のリピート再生で時間が溢れたら巻き戻す
        if duration and self.position() > duration + 3:
            self.elapsed, self.since = 0.0, time.time()


# ---------------------------------------------------------------- iTunes lookup (artwork / duration)

def _norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[\(\[（【].*?[\)\]）】]", "", s)
    s = re.sub(r"\b(feat|ft)\..*$", "", s)
    return re.sub(r"[\W_]+", "", s)


def clean_artist(s):
    """YouTube のチャンネル名から余計な飾りを外す ("X - Topic", "XVEVO", "X Official")."""
    s = re.sub(r"\s*-\s*(Topic|トピック)$", "", s or "")
    s = re.sub(r"(VEVO|\s+Official(\s+(Channel|YouTube))?|\s+公式(チャンネル)?)$", "", s, flags=re.I)
    return s.strip()


def _first_artist(s):
    return re.split(r",|&|、|/| x | feat\.| ft\.", s or "", maxsplit=1)[0].strip()


@dataclass
class TrackMeta:
    artwork: str | None = None
    duration: float | None = None
    year: str | None = None
    genre: str | None = None
    itunes_id: int | None = None


def itunes_lookup(title, artist, album, countries):
    nt, na, nal = _norm(title), _norm(_first_artist(artist)), _norm(album)
    if not nt:
        return TrackMeta()
    for country in countries:
        query = urllib.parse.urlencode({
            "term": f"{_first_artist(artist)} {title}", "entity": "song", "limit": 15, "country": country,
        })
        try:
            req = urllib.request.Request(f"https://itunes.apple.com/search?{query}",
                                         headers={"User-Agent": "MusicRPC/1.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                results = json.load(r).get("results", [])
        except Exception as e:
            log.debug("iTunes lookup failed (%s): %s", country, e)
            continue
        best, best_score = None, 0
        for res in results:
            rt, ra, ral = _norm(res.get("trackName")), _norm(res.get("artistName")), _norm(res.get("collectionName"))
            if not (nt == rt or (len(nt) > 3 and rt and (nt in rt or rt in nt))):
                continue
            score = 3 + (3 if nt == rt else 0)
            if na and ra and (na in ra or ra in na):
                score += 2
            if nal and ral and (nal == ral or nal in ral or ral in nal):
                score += 2
            if score > best_score:
                best, best_score = res, score
        if best and best_score >= 5:
            art = best.get("artworkUrl100") or ""
            return TrackMeta(
                artwork=re.sub(r"/\d+x\d+bb\.", "/600x600bb.", art) or None,
                duration=(best.get("trackTimeMillis") or 0) / 1000 or None,
                year=(best.get("releaseDate") or "")[:4] or None,
                genre=best.get("primaryGenreName"),
                itunes_id=best.get("trackId"),
            )
    return TrackMeta()


# ---------------------------------------------------------------- presence building

def clip(s, n=128):
    s = (s or "").strip()
    if 0 < len(s) < 2:
        s += " "
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_time(sec):
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def track_url(st, cfg):
    if st.link:
        return st.link
    if st.source == "amazon" and st.asin:
        domain = cfg["amazon_music_domain"]
        if st.album_asin:
            return f"https://{domain}/albums/{st.album_asin}?trackAsin={st.asin}"
        return f"https://{domain}/tracks/{st.asin}"
    return search_url(st.source, f"{st.title} {clean_artist(st.artist)}", cfg)


def search_url(source, query, cfg, limit=256):
    """検索ページの URL。Discord の URL 上限 (256 文字) に収まるよう検索語を短くする。"""
    if source == "amazon":
        base = f"https://{cfg['amazon_music_domain']}/search/"
    elif source == "ytmusic":
        base = "https://music.youtube.com/search?q="
    else:
        base = "https://www.youtube.com/results?search_query="
    query = query.strip()
    while query and len(base) + len(urllib.parse.quote(query)) > limit:
        query = query[:-1]
    return base + urllib.parse.quote(query.strip())


def build_activity(st: MediaState, meta: TrackMeta, position, duration, cfg):
    src = st.source
    label = SOURCE_LABEL[src]
    images = cfg["images"]
    artist = (clean_artist(st.artist) if src != "amazon" else st.artist) or st.album_artist or "Unknown Artist"
    url = lambda q: search_url(src, q, cfg)
    activity_type = 3 if src == "youtube" and cfg["youtube_activity"] == "watching" else 2

    act = {
        "type": activity_type,  # 2 = Listening, 3 = Watching
        "status_display_type": {"name": 0, "state": 1, "details": 2}.get(cfg["status_display"], 2),
        "details": clip(st.title),
        "details_url": track_url(st, cfg),
        "state": clip(artist),
        "state_url": url(artist),
    }

    if st.album:
        bits = [st.album + (f" ({meta.year})" if meta.year else "")]
    else:
        bits = [label] if not meta.year else [f"{label} ({meta.year})"]
    if st.track_number:
        bits.append(f"Track {st.track_number}" + (f"/{st.track_count}" if st.track_count else ""))
    genre = st.genre or meta.genre or (st.genres[0] if st.genres else None)
    if genre:
        bits.append(genre)

    assets = {
        "large_image": st.artwork or meta.artwork or images[src],
        "large_text": clip(" · ".join(bits)),
    }
    if st.album_asin:
        assets["large_url"] = f"https://{cfg['amazon_music_domain']}/albums/{st.album_asin}"
    elif st.album:
        assets["large_url"] = url(f"{st.album} {st.album_artist or artist}")

    flags = (["スマホ"] if st.device == "phone" else []) + ([label] if src != "amazon" else [])
    if st.quality:
        flags.append(st.quality)
    if st.shuffle:
        flags.append("シャッフル")
    if st.repeat:
        flags.append(f"リピート: {st.repeat}")
    if st.playing:
        assets["small_image"] = images["playing"]
        small = ["再生中"] + ([fmt_time(duration)] if duration else [])
        start = time.time() - (position or 0)
        act["timestamps"] = {"start": int(start * 1000)}
        if duration:
            act["timestamps"]["end"] = int((start + duration) * 1000)
    else:
        assets["small_image"] = images["paused"]
        small = ["一時停止中"]
        if position is not None:
            small.append(f"{fmt_time(position)} / {fmt_time(duration)}" if duration else fmt_time(position))
    assets["small_text"] = clip(" · ".join(small + flags))
    act["assets"] = assets

    if cfg["show_buttons"]:
        verb = "で見る" if src == "youtube" else "で聴く"
        buttons = [{"label": f"{label}{verb}"[:32], "url": track_url(st, cfg)}]
        if meta.itunes_id:
            buttons.append({"label": "他のサービスで聴く", "url": f"https://song.link/i/{meta.itunes_id}"})
        act["buttons"] = buttons
    return act


NEW_FIELDS = ("details_url", "state_url", "status_display_type")
NEW_ASSET_FIELDS = ("large_url", "small_url")


def strip_new_fields(act):
    act = {k: v for k, v in act.items() if k not in NEW_FIELDS}
    if "assets" in act:
        act["assets"] = {k: v for k, v in act["assets"].items() if k not in NEW_ASSET_FIELDS}
    return act


# ---------------------------------------------------------------- app

class App:
    MIN_SEND_INTERVAL = 2.5
    SEEK_RESEND_SEC = 15

    def __init__(self):
        self.cfg = load_config()
        self.enabled = True
        self.status_text = "起動中…"
        self.on_status = None
        self._stop = threading.Event()
        self.ipc = None
        self.clock = PlayClock()
        self.meta_cache = {}
        self.pending_lookup = set()
        self.last_sent = None  # (signature, start_ts) / "cleared"
        self.last_send_time = 0.0
        self.paused_since = None
        self.legacy_fields = False
        self.next_connect_try = 0.0
        self.amazon_log = AmazonLog()
        self.amazon_cache = {}
        self.phone = PhoneHub()
        self.art_urls = {}  # 曲 → 中継サーバーに置いたサムネイルの URL (失敗時は None)
        self.art_pending = set()
        self.art_seen = (None, 0.0)
        self.yt_cache = {}  # (題名, チャンネル) → {"url", "art"} / None
        self.yt_pending = set()

    def youtube_info(self, st):
        """YouTube 検索で見つけた動画のページとサムネイル。返り値: (検索済みか, 情報)"""
        if st.source not in ("youtube", "ytmusic") or not self.cfg["youtube_search_thumbnails"]:
            return True, None
        key = (st.title, clean_artist(st.artist))
        if key in self.yt_cache:
            return True, self.yt_cache[key]
        if key not in self.yt_pending:
            self.yt_pending.add(key)
            asyncio.create_task(self._youtube_lookup(key, st.source))
        return False, None

    async def _youtube_lookup(self, key, source):
        info = None
        try:
            vid = await asyncio.to_thread(youtube_search, *key)
            if vid:
                site = "https://music.youtube.com" if source == "ytmusic" else "https://www.youtube.com"
                info = {"url": f"{site}/watch?v={vid}", "art": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}
                if self.cfg["relay_url"]:
                    try:  # 黒帯を除いて正方形にしたものを置く。失敗したら YouTube の画像をそのまま使う
                        data = await asyncio.to_thread(youtube_thumbnail, vid)
                        if data:
                            jpeg = await asyncio.to_thread(square_jpeg, data)
                            info["art"] = await asyncio.to_thread(
                                upload_art, self.cfg["relay_url"], self.cfg["phone_token"], jpeg)
                    except Exception as e:
                        log.warning("サムネイルを置けませんでした: %s", e)
            log.info("YouTube 検索: %s → %s", key[0], vid)
        except Exception as e:
            log.warning("YouTube 検索に失敗しました: %s", e)
        finally:
            if len(self.yt_cache) > 300:
                self.yt_cache.clear()
            self.yt_cache[key] = info
            self.yt_pending.discard(key)

    def thumbnail_url(self, st):
        """ブラウザのサムネイルを中継サーバーに置き、その URL を返す (置き終わるまでは None)。"""
        cfg = self.cfg
        if st.device != "pc" or st.source not in ("youtube", "ytmusic"):
            return None
        if not (cfg["upload_thumbnails"] and cfg["relay_url"]):
            return None
        key = st.key
        if key in self.art_urls:
            return self.art_urls[key]
        if self.art_seen[0] != key:
            self.art_seen = (key, time.time())
        # 曲が変わった直後は前の曲の画像が残っていることがあるので、少し待ってから読む
        if st.thumbnail is not None and key not in self.art_pending and time.time() - self.art_seen[1] >= 2:
            self.art_pending.add(key)
            asyncio.create_task(self._upload_thumbnail(key, st.thumbnail))
        return None

    async def _upload_thumbnail(self, key, ref):
        url = None
        try:
            data = await read_thumbnail(ref)
            if data:
                jpeg = await asyncio.to_thread(square_jpeg, data)
                url = await asyncio.to_thread(upload_art, self.cfg["relay_url"], self.cfg["phone_token"], jpeg)
                log.info("サムネイルを置きました: %s (%d bytes)", key[2], len(jpeg))
        except Exception as e:
            log.warning("サムネイルを置けませんでした: %s", e)
        finally:
            if len(self.art_urls) > 300:
                self.art_urls.clear()
            self.art_urls[key] = url
            self.art_pending.discard(key)

    async def enrich_amazon(self, st):
        """SMTC に出てこない曲名などを Amazon Music のローカルデータで補う。"""
        lg = self.amazon_log
        lg.poll()
        if not lg.asin:
            return
        if lg.asin not in self.amazon_cache:
            try:
                self.amazon_cache[lg.asin] = await asyncio.to_thread(amazon_lookup, lg.asin)
            except Exception:
                log.exception("amazon lookup")
                self.amazon_cache[lg.asin] = None
            log.info("Amazon Music: %s → %s", lg.asin, self.amazon_cache[lg.asin])
        rec = self.amazon_cache[lg.asin] or {}
        # SMTC が曲名を出している場合は、ログの曲と同じかを確かめてから使う
        if st.title and rec.get("title") and _norm(st.title) != _norm(rec["title"]):
            return
        st.asin = lg.asin
        st.title = st.title or rec.get("title", "")
        st.artist = st.artist or rec.get("artist", "")
        st.album = st.album or rec.get("album", "")
        st.album_artist = st.album_artist or rec.get("album_artist", "")
        st.album_asin = rec.get("album_asin", "")
        st.artwork = rec.get("artwork")
        st.genre = rec.get("genre")
        st.duration = st.duration or rec.get("duration")
        if st.position is None:
            pos = lg.position()
            st.position = min(pos, st.duration) if st.duration else pos
        st.quality = lg.quality
        st.repeat = st.repeat or lg.repeat
        st.shuffle = st.shuffle if st.shuffle is not None else lg.shuffle
        if not st.title:  # ローカルに情報が無い曲: アーティストだけでも出す
            st.title, st.artist = st.artist or "Amazon Music", "Amazon Music"

    def client_id_for(self, source):
        c = self.cfg
        cid = {"amazon": c["client_id"],
               "youtube": c["youtube_client_id"],
               "ytmusic": c["youtube_music_client_id"] or c["youtube_client_id"]}.get(source)
        return str(cid or c["client_id"]).strip()

    def set_status(self, text):
        if text != self.status_text:
            self.status_text = text
            log.info(text)
            if self.on_status:
                try:
                    self.on_status()
                except Exception:
                    pass

    def stop(self):
        self._stop.set()

    def toggle_enabled(self, *_):
        self.enabled = not self.enabled
        self.last_sent = None

    # -- discord helpers (blocking calls run in a thread with timeout)
    async def _ipc_call(self, fn, *args):
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=10)
        except asyncio.TimeoutError:
            self.ipc.close()
            raise DiscordError("Discord が応答しません")

    async def ensure_discord(self, cid):
        if self.ipc and self.ipc.connected:
            if self.ipc.client_id == cid:
                return True
            self.ipc.close()  # ソースごとに別アプリ (表示名) を使う場合は繋ぎ直す
            self.next_connect_try = 0.0
        if time.time() < self.next_connect_try:
            return False
        self.next_connect_try = time.time() + 10
        if not cid.isdigit():
            self.set_status("config.json に client_id を設定してください")
            return False
        self.ipc = DiscordIPC(cid)
        try:
            await self._ipc_call(self.ipc.connect)
        except DiscordError as e:
            self.set_status(str(e))
            return False
        log.info("Discord に接続: %s (app %s)", self.ipc.user.get("username"), cid)
        self.last_sent = None
        return True

    async def send(self, activity, cid):
        if not await self.ensure_discord(cid):
            return False
        if activity and self.legacy_fields:
            activity = strip_new_fields(activity)
        try:
            await self._ipc_call(self.ipc.set_activity, activity)
        except ValueError as e:
            if activity and not self.legacy_fields and "is not allowed" in str(e):
                log.warning("Discord が新しいフィールドを拒否、互換モードで再送: %s", e)
                self.legacy_fields = True
                return await self.send(activity, cid)
            log.error("SET_ACTIVITY エラー: %s", e)
            return False
        except DiscordError as e:
            self.set_status(str(e))
            return False
        self.last_send_time = time.time()
        return True

    async def clear(self):
        if self.last_sent != "cleared" and self.ipc and self.ipc.connected:
            if await self.send(None, self.ipc.client_id):
                self.last_sent = "cleared"

    # -- metadata
    def get_meta(self, st):
        if not self.cfg["artwork_lookup"]:
            return TrackMeta()
        key = st.key
        if key in self.meta_cache:
            return self.meta_cache[key]
        if key not in self.pending_lookup:
            self.pending_lookup.add(key)
            artist = st.artist or st.album_artist
            if st.source != "amazon":
                artist = clean_artist(artist)

            async def job():
                try:
                    meta = await asyncio.to_thread(itunes_lookup, st.title, artist, st.album,
                                                   self.cfg["itunes_countries"])
                except Exception:
                    meta = TrackMeta()
                finally:
                    self.pending_lookup.discard(key)
                if len(self.meta_cache) > 300:
                    self.meta_cache.clear()
                self.meta_cache[key] = meta
                log.info("メタデータ: %s - %s → artwork=%s", artist, st.title, bool(meta.artwork))

            asyncio.create_task(job())
        return TrackMeta()

    # -- main loop
    async def run(self):
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
        manager = await Manager.request_async()
        await self.start_phone()
        while not self._stop.is_set():
            try:
                await self.tick(manager)
            except Exception:
                log.exception("tick error")
            await asyncio.sleep(float(self.cfg["poll_interval_sec"]))
        await self.clear()
        if self.ipc:
            self.ipc.close()

    async def start_phone(self):
        cfg = self.cfg
        if not cfg["phone_enabled"]:
            return
        token = str(cfg["phone_token"])
        try:
            await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: PhoneUdp(self.phone, token), local_addr=("0.0.0.0", int(cfg["phone_port"])))
        except OSError as e:
            log.warning("スマホ用の UDP ポート %s を開けません: %s", cfg["phone_port"], e)
        if cfg["relay_url"]:
            self.relay_task = asyncio.create_task(relay_loop(self.phone, cfg["relay_url"], token, self._stop))

    async def tick(self, manager):
        if not self.enabled:
            self.set_status("非表示中")
            return await self.clear()
        st = await pick_media(manager, self.cfg, self.enrich_amazon, extra=[self.phone.current(self.cfg)])
        if st is None:
            self.set_status("再生していません")
            self.paused_since = None
            return await self.clear()

        self.clock.update(st.key, st.playing)
        meta = self.get_meta(st)
        searched, yt = self.youtube_info(st)
        if yt:
            st.artwork = st.artwork or yt["art"]
            st.link = yt["url"]
        if searched:  # 検索で見つからなかった時だけ、ブラウザの小さいサムネイルを使う
            st.artwork = st.artwork or self.thumbnail_url(st)
        duration = st.duration or meta.duration
        if st.position is not None:
            position = st.position
        else:
            self.clock.wrap(duration)
            position = self.clock.position()

        label = st.label
        if st.playing:
            self.paused_since = None
        else:
            self.paused_since = self.paused_since or time.time()
            limit = float(self.cfg["clear_after_paused_min"]) * 60
            if not self.cfg["show_when_paused"] or (limit > 0 and time.time() - self.paused_since > limit):
                self.set_status(f"[{label}] 一時停止中 (非表示): {st.title}")
                return await self.clear()

        icon = "▶" if st.playing else "⏸"
        self.set_status(f"[{label}] {icon} {st.artist} - {st.title}")

        sig = (st.key, st.status, st.shuffle, st.repeat, st.quality, st.artwork or meta.artwork, st.link, duration,
               self.legacy_fields)
        start_ts = time.time() - position
        if self.last_sent and self.last_sent != "cleared":
            last_sig, last_start = self.last_sent
            # 曲の終わりでは再生位置が長さで頭打ちになり、開始時刻がずれていくのでシーク扱いしない
            at_end = bool(duration) and position >= duration - 1
            seek = st.playing and not at_end and abs(start_ts - last_start) > 3
            if last_sig == sig and not seek:
                return
            if last_sig == sig and time.time() - self.last_send_time < self.SEEK_RESEND_SEC:
                return  # シークだけの送り直しは間隔をあける (Discord が更新を受け付けなくなるため)
        if time.time() - self.last_send_time < self.MIN_SEND_INTERVAL:
            return  # Discord のレート制限対策: 次の tick で送る
        activity = build_activity(st, meta, position, duration, self.cfg)
        if await self.send(activity, self.client_id_for(st.source)):
            self.last_sent = (sig, start_ts)
            log.info("送信: [%s] %s / 画像 %s", label, st.title, activity["assets"]["large_image"][-48:])


# ---------------------------------------------------------------- tray / startup

def make_icon_image(size=64, color=(37, 209, 218), symbol="note"):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.ellipse((2 * s, 2 * s, 62 * s, 62 * s), fill=(20, 24, 33, 255), outline=color, width=max(1, int(3 * s)))
    if symbol == "note":
        d.ellipse((16 * s, 38 * s, 30 * s, 50 * s), fill=color)
        d.ellipse((34 * s, 34 * s, 48 * s, 46 * s), fill=color)
        d.rectangle((26 * s, 16 * s, 30 * s, 44 * s), fill=color)
        d.rectangle((44 * s, 12 * s, 48 * s, 40 * s), fill=color)
        d.polygon([(26 * s, 16 * s), (48 * s, 12 * s), (48 * s, 20 * s), (26 * s, 24 * s)], fill=color)
    elif symbol == "play":
        d.polygon([(24 * s, 18 * s), (24 * s, 46 * s), (48 * s, 32 * s)], fill=color)
    elif symbol == "pause":
        d.rectangle((21 * s, 18 * s, 29 * s, 46 * s), fill=color)
        d.rectangle((35 * s, 18 * s, 43 * s, 46 * s), fill=color)
    elif symbol == "video":
        d.rounded_rectangle((12 * s, 18 * s, 52 * s, 46 * s), radius=8 * s, fill=color)
        d.polygon([(27 * s, 24 * s), (27 * s, 40 * s), (40 * s, 32 * s)], fill=(20, 24, 33, 255))
    return img


def make_assets():
    out = BASE / "assets"
    out.mkdir(exist_ok=True)
    make_icon_image(512, symbol="note").save(out / "amazon_music.png")
    make_icon_image(512, color=(235, 60, 60), symbol="video").save(out / "youtube.png")
    make_icon_image(512, color=(80, 220, 120), symbol="play").save(out / "playing.png")
    make_icon_image(512, color=(240, 180, 60), symbol="pause").save(out / "paused.png")
    print(f"生成しました: {out}")


STARTUP_VBS = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\MusicRPC.vbs"


def startup_enabled():
    return STARTUP_VBS.exists()


def toggle_startup(*_):
    if STARTUP_VBS.exists():
        STARTUP_VBS.unlink()
        return
    pyw = Path(sys.executable).with_name("pythonw.exe")
    cmd = f'"""{pyw}"" ""{Path(__file__).resolve()}"""'
    STARTUP_VBS.write_text(f'CreateObject("WScript.Shell").Run {cmd}, 0, False\n', encoding="utf-8")


def run_tray(app):
    import pystray

    def quit_(icon, _item):
        app.stop()
        icon.stop()

    icon = pystray.Icon(
        "MusicRPC", make_icon_image(), "Music RPC",
        menu=pystray.Menu(
            pystray.MenuItem(lambda _i: app.status_text[:60], None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Discord に表示", app.toggle_enabled, checked=lambda _i: app.enabled),
            pystray.MenuItem("Windows 起動時に開始", toggle_startup, checked=lambda _i: startup_enabled()),
            pystray.MenuItem("設定ファイルを開く", lambda: os.startfile(CONFIG_PATH)),
            pystray.MenuItem("ログを開く", lambda: os.startfile(LOG_PATH)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了", quit_),
        ),
    )

    def refresh():
        icon.title = f"Music RPC\n{app.status_text}"[:127]
        icon.update_menu()

    app.on_status = refresh
    worker = threading.Thread(target=lambda: asyncio.run(app.run()), daemon=True)
    worker.start()
    icon.run()
    app.stop()
    worker.join(timeout=5)


def single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\MusicDiscordRPC")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


async def probe():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager
    app = App()
    cfg = app.cfg
    manager = await Manager.request_async()
    for s in manager.get_sessions():
        print("session:", s.source_app_user_model_id, await read_session(s, "?"))
    st = await pick_media(manager, cfg, app.enrich_amazon)
    print("\npicked:", st)
    if st:
        artist = st.artist if st.source == "amazon" else clean_artist(st.artist)
        meta = itunes_lookup(st.title, artist, st.album, cfg["itunes_countries"])
        print("meta:", meta)
        print("activity:", json.dumps(build_activity(st, meta, st.position or 0, st.duration or meta.duration, cfg),
                                      ensure_ascii=False, indent=2))


def main():
    args = set(sys.argv[1:])
    console = "--console" in args or (sys.executable.lower().endswith("python.exe") and "--tray" not in args)
    handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    if console and sys.stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    if "--make-assets" in args:
        return make_assets()
    if "--probe" in args:
        return asyncio.run(probe())
    if not single_instance():
        log.info("既に起動しています")
        return

    app = App()
    if not str(app.cfg.get("client_id", "")).strip().isdigit():
        msg = "config.json の client_id に Discord アプリケーション ID を設定してください。"
        log.error(msg)
        if not console:
            ctypes.windll.user32.MessageBoxW(None, msg, "Music RPC", 0x40)
            os.startfile(CONFIG_PATH)
        return

    if console:
        try:
            asyncio.run(app.run())
        except KeyboardInterrupt:
            pass
    else:
        run_tray(app)


if __name__ == "__main__":
    main()
