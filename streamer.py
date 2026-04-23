#!/usr/bin/env python3
"""
StreamControl — Streamer Agent v1.0.0
=====================================
רץ על X96 Max (Android/Linux) עם Python 3.8+

התקנה:
  pip install requests schedule pygame

הרצה:
  python streamer.py

קובץ הגדרות: config.json (נוצר אוטומטית בהרצה ראשונה)
"""

import json, os, time, random, threading, schedule, logging, hashlib
import requests
from datetime import datetime
from pathlib import Path

# ─── הגדרות ───────────────────────────────────────────────
VERSION = "1.0.0"
CONFIG_FILE = "config.json"
CACHE_DIR   = Path("music_cache")
LOG_FILE    = "streamer.log"
ALLOWED_EXT = [".mp3"]
HEARTBEAT_SEC = 30
POLL_SEC      = 3
PRIORITY_COUNT = 10  # מוריד קודם X קטעים לפני שמתחיל לנגן

DEFAULT_CONFIG = {
    "device_id":       "",          # ← מלא: SN-001
    "device_name":     "",          # ← מלא: סניף תל אביב
    "device_location": "",          # ← מלא: תל אביב — דיזנגוף
    "firebase_url":    "",          # ← מלא: https://xxx.firebaseio.com
    "firebase_secret": "",          # ← מלא: סוד Firebase
    "dropbox_token":   "",          # ← מלא: sl.xxxxxxxxxx
    "volume":          70,
    "auto_start":      True,
}

# ─── לוגינג ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("streamer")


# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
def load_config():
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        log.warning(f"📝 קובץ config.json נוצר — מלא את הפרטים והפעל מחדש.")
        exit(0)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in ["device_id","firebase_url","firebase_secret","dropbox_token"] if not cfg.get(k)]
    if missing:
        log.error(f"❌ שדות חסרים ב-config.json: {missing}")
        exit(1)
    return cfg


# ══════════════════════════════════════════════════════════
# FIREBASE
# ══════════════════════════════════════════════════════════
class Firebase:
    def __init__(self, url: str, secret: str):
        self.url    = url.rstrip("/")
        self.secret = secret
        self._auth  = {"auth": secret}

    def get(self, path: str):
        try:
            r = requests.get(f"{self.url}/{path}.json", params=self._auth, timeout=10)
            return r.json() if r.ok else None
        except Exception as e:
            log.warning(f"Firebase GET error: {e}")
            return None

    def put(self, path: str, data):
        try:
            requests.put(f"{self.url}/{path}.json", params=self._auth,
                         json=data, timeout=10)
        except Exception as e:
            log.warning(f"Firebase PUT error: {e}")

    def patch(self, path: str, data):
        try:
            requests.patch(f"{self.url}/{path}.json", params=self._auth,
                           json=data, timeout=10)
        except Exception as e:
            log.warning(f"Firebase PATCH error: {e}")

    def poll_command(self, device_id: str, last_ts: int):
        """בדוק פקודה חדשה — מחזיר (data, ts) אם יש חדשה, אחרת (None, last_ts)"""
        data = self.get(f"commands/{device_id}")
        if data and isinstance(data, dict):
            ts = data.get("timestamp", 0)
            if ts > last_ts:
                return data, ts
        return None, last_ts


# ══════════════════════════════════════════════════════════
# DROPBOX
# ══════════════════════════════════════════════════════════
class Dropbox:
    API  = "https://api.dropboxapi.com/2"
    CONT = "https://content.dropboxapi.com/2"

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}"}

    def list_folder(self, path: str):
        """רשימת קבצי MP3 בתיקייה"""
        try:
            r = requests.post(f"{self.API}/files/list_folder",
                              headers={**self.headers,"Content-Type":"application/json"},
                              json={"path": path, "recursive": True}, timeout=15)
            if r.ok:
                entries = r.json().get("entries", [])
                return [e for e in entries
                        if e[".tag"] == "file"
                        and any(e["name"].lower().endswith(ext) for ext in ALLOWED_EXT)]
        except Exception as e:
            log.warning(f"Dropbox list error: {e}")
        return []

    def temp_link(self, path: str) -> str | None:
        """קישור זמני להשמעה ישירה (4 שעות תוקף)"""
        try:
            r = requests.post(f"{self.API}/files/get_temporary_link",
                              headers={**self.headers,"Content-Type":"application/json"},
                              json={"path": path}, timeout=10)
            return r.json().get("link") if r.ok else None
        except:
            return None

    def download(self, path: str, local: Path) -> bool:
        """הורד קובץ לאחסון מקומי"""
        try:
            r = requests.post(f"{self.CONT}/files/download",
                              headers={**self.headers,
                                       "Dropbox-API-Arg": json.dumps({"path": path})},
                              stream=True, timeout=60)
            if r.ok:
                local.parent.mkdir(parents=True, exist_ok=True)
                with open(local, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                return True
        except Exception as e:
            log.warning(f"Dropbox download error: {e}")
        return False


# ══════════════════════════════════════════════════════════
# PLAYER
# ══════════════════════════════════════════════════════════
class Player:
    def __init__(self, cfg: dict, dropbox: Dropbox):
        self.cfg     = cfg
        self.dropbox = dropbox
        self.playlist: list[dict] = []
        self.queue:    list[dict] = []   # shuffled play queue
        self.current_index = 0
        self.is_playing    = False
        self.volume        = cfg.get("volume", 70)
        self._lock         = threading.Lock()
        self._init_pygame()
        CACHE_DIR.mkdir(exist_ok=True)

    def _init_pygame(self):
        try:
            import pygame
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=4096)
            self._pygame = pygame
            self._backend = "pygame"
            log.info("✅ Backend: pygame")
        except Exception:
            self._pygame = None
            self._backend = "subprocess"
            log.info("✅ Backend: subprocess (mpv/ffplay)")

    # ── Playlist ──────────────────────────────────────────
    def load_playlist(self, tracks: list[dict]):
        """טען רשימה ועשה Shuffle"""
        with self._lock:
            # סנן רק MP3 תקין
            valid   = [t for t in tracks if self._is_valid(t.get("path",""))]
            invalid = [t for t in tracks if not self._is_valid(t.get("path",""))]
            if invalid:
                for t in invalid:
                    log.warning(f"⛔ חסום (לא MP3): {t.get('path','')}")
            self.playlist = valid
            self.queue    = valid.copy()
            random.shuffle(self.queue)
            self.current_index = 0
        log.info(f"📋 פלייליסט טעון: {len(valid)} קטעים (Shuffle), {len(invalid)} חסומים")

    def _is_valid(self, path: str) -> bool:
        return any(path.lower().endswith(ext) for ext in ALLOWED_EXT)

    # ── Download ──────────────────────────────────────────
    def download_playlist_progressive(self):
        """
        Progressive Download:
        1. הורד PRIORITY_COUNT קטעים ראשונים
        2. התחל לנגן
        3. הורד את השאר ברקע
        """
        if not self.queue:
            return
        priority = self.queue[:PRIORITY_COUNT]
        rest     = self.queue[PRIORITY_COUNT:]

        log.info(f"⬇ מוריד {len(priority)} קטעים ראשונים (Priority)...")
        for t in priority:
            self._download_track(t)

        log.info("▶ מתחיל לנגן — ממשיך להוריד ברקע")
        if self.cfg.get("auto_start"):
            self.play()

        # הורד שאר ברקע
        def bg():
            for t in rest:
                if not self._local_path(t).exists():
                    self._download_track(t)
                time.sleep(0.5)  # מרווח קטן לא לעמוס רשת
            log.info("✅ כל הספרייה הורדה!")

        threading.Thread(target=bg, daemon=True).start()

    def _download_track(self, track: dict) -> bool:
        local = self._local_path(track)
        if local.exists():
            return True
        path  = track.get("path","")
        log.info(f"⬇ מוריד: {track.get('name','?')}")
        ok = self.dropbox.download(path, local)
        if not ok:
            log.warning(f"⚠ לא הורד: {path}")
        return ok

    def _local_path(self, track: dict) -> Path:
        safe = hashlib.md5(track.get("path","").encode()).hexdigest()
        return CACHE_DIR / f"{safe}.mp3"

    # ── Playback ──────────────────────────────────────────
    def play(self, index: int = None):
        with self._lock:
            if index is not None:
                self.current_index = index % max(len(self.queue), 1)
            if not self.queue:
                log.warning("פלייליסט ריק")
                return
            track = self.queue[self.current_index]

        local = self._local_path(track)
        if not local.exists():
            # נסה temp link ישירות מ-Dropbox
            url = self.dropbox.temp_link(track.get("path",""))
            if not url:
                log.error(f"❌ לא ניתן לנגן: {track.get('name','?')}")
                self._next_auto()
                return
            src = url
        else:
            src = str(local)

        log.info(f"▶ מנגן: {track.get('name','?')}")
        self._play_src(src)
        self.is_playing = True

        # האזן לסיום שיר → הבא
        threading.Thread(target=self._watch_end, daemon=True).start()

    def _play_src(self, src: str):
        if self._backend == "pygame":
            try:
                pygame = self._pygame
                pygame.mixer.music.load(src)
                pygame.mixer.music.set_volume(self.volume / 100)
                pygame.mixer.music.play()
                return
            except Exception as e:
                log.warning(f"pygame error: {e}")
        # fallback subprocess
        self._subprocess_play(src)

    def _subprocess_play(self, src: str):
        import subprocess
        if hasattr(self, "_proc") and self._proc:
            try: self._proc.terminate()
            except: pass
        for player in ["mpv", "ffplay", "cvlc"]:
            try:
                args = {
                    "mpv":    ["mpv", "--no-video", f"--volume={self.volume}", src],
                    "ffplay": ["ffplay", "-nodisp", "-autoexit", "-volume", str(self.volume), src],
                    "cvlc":   ["cvlc", "--no-video", src],
                }[player]
                self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._current_player = player
                return
            except FileNotFoundError:
                continue
        log.error("❌ לא נמצא נגן מדיה (mpv/ffplay/cvlc). התקן אחד מהם.")

    def _watch_end(self):
        """ממתין לסיום שיר ומנגן הבא אוטומטית"""
        time.sleep(1)
        if self._backend == "pygame" and self._pygame:
            while self._pygame.mixer.music.get_busy():
                time.sleep(0.5)
        elif hasattr(self, "_proc") and self._proc:
            self._proc.wait()
        if self.is_playing:
            self._next_auto()

    def _next_auto(self):
        """עבור לשיר הבא — בין שירים: בדוק פרסומות"""
        self._check_ads_between_tracks()
        with self._lock:
            self.current_index = (self.current_index + 1) % max(len(self.queue), 1)
            if self.current_index == 0:
                random.shuffle(self.queue)
                log.info("🔀 Shuffle חדש")
        self.play()

    def pause(self):
        if self._backend == "pygame" and self._pygame:
            self._pygame.mixer.music.pause()
        elif hasattr(self, "_proc") and self._proc:
            import signal; self._proc.send_signal(signal.SIGSTOP)
        self.is_playing = False
        log.info("⏸ מושהה")

    def resume(self):
        if self._backend == "pygame" and self._pygame:
            self._pygame.mixer.music.unpause()
        elif hasattr(self, "_proc") and self._proc:
            import signal; self._proc.send_signal(signal.SIGCONT)
        self.is_playing = True
        log.info("▶ ממשיך")

    def stop(self):
        if self._backend == "pygame" and self._pygame:
            self._pygame.mixer.music.stop()
        elif hasattr(self, "_proc") and self._proc:
            try: self._proc.terminate()
            except: pass
        self.is_playing = False
        log.info("⏹ עצור")

    def next_track(self):
        self.stop()
        self._next_auto()

    def prev_track(self):
        with self._lock:
            self.current_index = (self.current_index - 1) % max(len(self.queue), 1)
        self.play()

    def shuffle(self):
        with self._lock:
            random.shuffle(self.queue)
            self.current_index = 0
        log.info("🔀 ערבוב")

    def set_volume(self, vol: int):
        self.volume = max(0, min(100, int(vol)))
        if self._backend == "pygame" and self._pygame:
            self._pygame.mixer.music.set_volume(self.volume / 100)
        log.info(f"🔊 עוצמה: {self.volume}%")

    def current_track(self) -> dict:
        if self.queue and 0 <= self.current_index < len(self.queue):
            return self.queue[self.current_index]
        return {}

    # ── Ads ───────────────────────────────────────────────
    def _ads: list = []

    def load_ads(self, ads: list[dict]):
        self._ads = [a for a in ads if a.get("active") and self._is_valid(a.get("path",""))]
        invalid   = [a for a in ads if not self._is_valid(a.get("path",""))]
        for a in invalid:
            log.warning(f"⛔ פרסומת חסומה (לא MP3): {a.get('path','')}")

    def _check_ads_between_tracks(self):
        """בדוק אם צריך לנגן פרסומת עכשיו (בין שירים)"""
        now = datetime.now()
        for ad in self._ads:
            try:
                from_d = datetime.strptime(ad["from"], "%Y-%m-%d")
                to_d   = datetime.strptime(ad["to"],   "%Y-%m-%d")
                if not (from_d <= now <= to_d):
                    continue
                freq = ad.get("freq","1")
                if freq == "custom":
                    times_day = int(ad.get("customTimes", 5))
                    interval_sec = int(86400 / times_day)
                else:
                    interval_sec = int(3600 / int(freq))
                last_played = ad.get("_last_played", 0)
                if time.time() - last_played >= interval_sec:
                    self._play_ad(ad)
                    ad["_last_played"] = time.time()
                    return  # רק פרסומת אחת בין שירים
            except Exception as e:
                log.warning(f"Ad check error: {e}")

    def _play_ad(self, ad: dict):
        log.info(f"📢 פרסומת: {ad.get('name','?')}")
        local = CACHE_DIR / f"ad_{hashlib.md5(ad.get('path','').encode()).hexdigest()}.mp3"
        if not local.exists():
            self.dropbox.download(ad["path"], local)
        if local.exists():
            self._play_src(str(local))
            # ממתין לסיום פרסומת
            if self._backend == "pygame" and self._pygame:
                while self._pygame.mixer.music.get_busy():
                    time.sleep(0.3)
            elif hasattr(self, "_proc") and self._proc:
                self._proc.wait()


# ══════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════
class Scheduler:
    DAY_MAP = {"א":6,"ב":0,"ג":1,"ד":2,"ה":3,"ו":4,"ש":5}

    def __init__(self, player: Player):
        self.player    = player
        self.schedules: list[dict] = []

    def load(self, schedules: list[dict]):
        schedule.clear()
        self.schedules = schedules
        for s in schedules:
            schedule.every().day.at(s["start"]).do(self._start, s)
            schedule.every().day.at(s["end"]).do(self._stop)
        log.info(f"📅 {len(schedules)} לוחות זמנים נטענו")
        self.check_now()  # בדוק אם אמורים לנגן עכשיו

    def _day_active(self, sched: dict) -> bool:
        today = datetime.now().weekday()
        return any(self.DAY_MAP.get(d) == today for d in sched.get("days", []))

    def _start(self, sched: dict):
        if self._day_active(sched):
            log.info(f"⏰ לוח זמנים — מתחיל: {sched['start']}")
            self.player.set_volume(sched.get("vol", 70))
            if not self.player.is_playing:
                self.player.play()

    def _stop(self):
        log.info("⏰ לוח זמנים — מסיים")
        self.player.stop()

    def check_now(self):
        """האם אנחנו עכשיו בתוך חלון זמן פעיל?"""
        now = datetime.now().strftime("%H:%M")
        for s in self.schedules:
            if self._day_active(s) and s["start"] <= now <= s["end"]:
                log.info(f"🕐 בתוך חלון {s['start']}–{s['end']}, מפעיל השמעה")
                self.player.set_volume(s.get("vol", 70))
                self.player.play()
                return

    def run(self):
        def loop():
            while True:
                schedule.run_pending()
                time.sleep(20)
        threading.Thread(target=loop, daemon=True).start()


# ══════════════════════════════════════════════════════════
# COMMAND HANDLER
# ══════════════════════════════════════════════════════════
def handle_command(cmd: dict, player: Player, scheduler: Scheduler,
                   firebase: Firebase, cfg: dict):
    action = cmd.get("action","")
    log.info(f"📡 פקודה: {action}")

    if   action == "play":       player.play(cmd.get("trackIndex"))
    elif action == "pause":      player.pause()
    elif action == "resume":     player.resume()
    elif action == "stop":       player.stop()
    elif action == "next":       player.next_track()
    elif action == "prev":       player.prev_track()
    elif action == "shuffle":    player.shuffle(); player.play(0)
    elif action == "volume":     player.set_volume(cmd.get("value", 70))
    elif action == "playlist_update":
        tracks = cmd.get("tracks", [])
        player.load_playlist(tracks)
        threading.Thread(target=player.download_playlist_progressive, daemon=True).start()
    elif action == "schedule_update":
        scheduler.load(cmd.get("schedules", []))
    elif action == "ads_update":
        player.load_ads(cmd.get("ads", []))
    elif action == "sync":
        threading.Thread(target=player.download_playlist_progressive, daemon=True).start()
    elif action == "ota_update":
        _ota_update(cmd.get("url",""), cmd.get("version",""), player)

    # עדכן סטטוס
    _report_status(firebase, cfg, player)


def _report_status(firebase: Firebase, cfg: dict, player: Player):
    track = player.current_track()
    firebase.patch(f"devices/{cfg['device_id']}", {
        "online":       True,
        "playing":      track.get("name","—"),
        "is_playing":   player.is_playing,
        "volume":       player.volume,
        "lib_local":    len([f for f in CACHE_DIR.glob("*.mp3")]) if CACHE_DIR.exists() else 0,
        "last_seen":    datetime.now().strftime("%d.%m.%Y %H:%M"),
        "version":      VERSION,
    })


def _ota_update(url: str, new_version: str, player: Player):
    if not url:
        return
    log.info(f"🔄 OTA: מוריד גרסה {new_version}...")
    try:
        r = requests.get(url, timeout=30)
        if r.ok:
            with open("streamer_new.py","wb") as f:
                f.write(r.content)
            log.info("✅ OTA: גרסה חדשה הורדה — תופעל בסיום נגינה")
    except Exception as e:
        log.warning(f"OTA error: {e}")


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
def main():
    cfg      = load_config()
    dev_id   = cfg["device_id"]
    firebase = Firebase(cfg["firebase_url"], cfg["firebase_secret"])
    dropbox  = Dropbox(cfg["dropbox_token"])
    player   = Player(cfg, dropbox)
    sched    = Scheduler(player)

    log.info(f"🎵 StreamControl Streamer v{VERSION}")
    log.info(f"📺 התקן: {cfg.get('device_name','')} ({dev_id})")
    log.info(f"📍 מיקום: {cfg.get('device_location','')}")

    # ── הכרזה כ-Online ──────────────────────────────────
    firebase.put(f"devices/{dev_id}", {
        "device_id":   dev_id,
        "name":        cfg.get("device_name",""),
        "location":    cfg.get("device_location",""),
        "sn":          dev_id,
        "online":      True,
        "version":     VERSION,
        "connected_at": datetime.now().isoformat(),
        "lib_local":   0,
    })

    # ── משוך הגדרות מ-Firebase ──────────────────────────
    log.info("📋 טוען הגדרות מ-Firebase...")

    # פלייליסט — לפי לקוח שמשויך להתקן
    dev_data = firebase.get(f"devices/{dev_id}")
    client_id = (dev_data or {}).get("clientId","")
    playlist_data = firebase.get(f"playlists/{dev_id}") or {}
    tracks = playlist_data.get("tracks", [])

    if tracks:
        player.load_playlist(tracks)
    else:
        log.warning("⚠ אין פלייליסט ב-Firebase — ממתין לסנכרון מהממשק")

    # לוחות זמנים
    sched_data = firebase.get("schedules") or {}
    all_scheds = list(sched_data.values()) if isinstance(sched_data, dict) else []
    # סנן לפי התקן/לקוח הספציפי + ברירת מחדל
    my_scheds = [s for s in all_scheds if
                 s.get("target") == "all" or
                 s.get("target") == f"client:{client_id}" or
                 s.get("target") == f"device:{dev_id}"]
    sched.load(my_scheds)
    sched.run()

    # פרסומות
    ads_data = firebase.get("ads") or {}
    all_ads = list(ads_data.values()) if isinstance(ads_data, dict) else []
    player.load_ads(all_ads)

    # ── Progressive Download ─────────────────────────────
    if tracks:
        threading.Thread(target=player.download_playlist_progressive, daemon=True).start()

    # ── Heartbeat ────────────────────────────────────────
    def heartbeat():
        while True:
            try:
                _report_status(firebase, cfg, player)
            except Exception as e:
                log.warning(f"Heartbeat error: {e}")
            time.sleep(HEARTBEAT_SEC)

    threading.Thread(target=heartbeat, daemon=True).start()

    # ── Command Poll Loop ────────────────────────────────
    log.info("👂 מאזין לפקודות מרחוק...")
    last_ts = 0
    while True:
        try:
            cmd, last_ts = firebase.poll_command(dev_id, last_ts)
            if cmd:
                handle_command(cmd, player, sched, firebase, cfg)
        except Exception as e:
            log.warning(f"Poll error: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()


# ══════════════════════════════════════════════════════════
# LOCAL HTTP SERVER — מגיש config לממשק הלקוח על HDMI
# ══════════════════════════════════════════════════════════
def start_local_server(cfg: dict, player, scheduler):
    """
    מגיש ב-localhost:8765:
      /config  — הגדרות לממשק הלקוח
      /status  — סטטוס נגן נוכחי
    """
    import http.server
    import socketserver
    import json as _json

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            if self.path == '/config':
                data = {
                    'firebase_url':    cfg.get('firebase_url', ''),
                    'firebase_secret': cfg.get('firebase_secret', ''),
                    'device_id':       cfg.get('device_id', ''),
                    'volume':          player.volume,
                    'allowed_genres':  cfg.get('allowed_genres', []),
                    'active_genres':   cfg.get('active_genres', []),
                }
            elif self.path == '/status':
                track = player.current_track()
                data = {
                    'playing':    player.is_playing,
                    'track':      track.get('name', '—'),
                    'volume':     player.volume,
                }
            else:
                data = {}

            self.wfile.write(_json.dumps(data, ensure_ascii=False).encode())

        def log_message(self, *args):
            pass  # שקט

    def run():
        with socketserver.TCPServer(('localhost', 8765), Handler) as httpd:
            log.info('🌐 Local server: http://localhost:8765')
            httpd.serve_forever()

    threading.Thread(target=run, daemon=True).start()


# קריאה ל-start_local_server בתוך main() לפני לולאת הפקודות:
# start_local_server(cfg, player, sched)
