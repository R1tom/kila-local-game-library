"""Local Game Genesys background service: records play sessions for games on the scan drives,
watches achievement files and shows a notification for every new unlock.
Started at logon (Startup folder) and by the GameTracker window. Log: data\\gametracker.log
"""
import ctypes
import ctypes.wintypes
import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psutil  # noqa: E402

import core  # noqa: E402

log = core.log
REFRESH_EVERY = 30 * 60
HEARTBEAT = 30
SLEEP_GAP = 120
RELOAD_FLAG = core.DATA / "reload.flag"


def single_instance():
    h = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\LocalGameGenesysTracker")
    if ctypes.windll.kernel32.GetLastError() == 183:
        sys.exit(0)
    return h


def fmt_dur(s):
    s = int(s)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


AUMID = "LocalGameGenesys.App"


def register_aumid():
    """Unpackaged apps need an AppUserModelId in the registry or Windows drops their toasts."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{AUMID}") as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "Local Game Genesys")
        winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, str(core.HERE / "ui" / "icon.png"))
        winreg.SetValueEx(k, "IconBackgroundColor", 0, winreg.REG_SZ, "FF5B8CFF")


ACH_SOUND = core.HERE / "sounds" / "achievement.wav"
ACH_SOUND_RARE = core.HERE / "sounds" / "achievement-rare.wav"  # the full, longer Xbox One rare jingle


def ach_sound(pct=None):
    """Rare (under 10% of players) unlocks get the long rare jingle, everything else the normal one."""
    if pct is not None and pct < 10 and ACH_SOUND_RARE.exists():
        return ACH_SOUND_RARE
    return ACH_SOUND


def play_achievement_sound(pct=None):
    """Xbox achievement sound. Returns True if it played (the toast then stays silent)."""
    snd = ach_sound(pct)
    if not core.load_config().get("achievement_sound", True) or not snd.exists():
        return False
    try:
        import winsound
        winsound.PlaySound(str(snd), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        return True
    except Exception as e:
        log.warning("achievement sound failed: %s", e)
        return False


def _patch_winotify():
    """winotify starts powershell.exe without CREATE_NO_WINDOW, which can flash a console over a game:
    swap in the same runner with the flag (site-packages stays untouched)."""
    import subprocess
    import winotify
    if getattr(winotify._run_ps, "_no_window", False):
        return

    def run_ps(*, file="", command=""):
        if bool(file) == bool(command):
            raise ValueError
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        cmd = ["powershell.exe", "-ExecutionPolicy", "Bypass"] + (["-file", file] if file else ["-Command", command])
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         startupinfo=si, creationflags=0x08000000)  # CREATE_NO_WINDOW
    run_ps._no_window = True
    winotify._run_ps = run_ps


def toast(title, msg, icon=None, silent=False):
    if not core.load_config().get("notifications", True):
        return
    try:
        _patch_winotify()
        from winotify import Notification, audio
        app = core.HERE / "Local Game Genesys.lnk"
        n = Notification(app_id=AUMID, title=title, msg=msg,
                         icon=str(icon) if icon and Path(icon).exists() else str(core.HERE / "ui" / "icon.png"),
                         launch=str(app) if app.exists() else "")
        n.set_audio(audio.Silent if silent else audio.Default, loop=False)
        n.show()
    except Exception as e:
        log.warning("toast failed: %s", e)


def unlock_toast(game_name, a):
    played = play_achievement_sound(a.get("global_pct"))
    toast("Achievement unlocked", f"{a['name']}\n{game_name}" + (f"\n{a['description']}" if a.get("description") else ""),
          a.get("icon"), silent=played)


POPUPS = queue.Queue()


def wait_game_settled(prefix, timeout=90, still=3.0):
    """A just-launched game creates its window and switches mode/resolution for a few seconds, which makes
    a pop-up drawn over it blink: wait until the game's window is in front and has kept its size for `still` s."""
    if not prefix:
        return
    u32 = ctypes.windll.user32
    end, since, last = time.time() + timeout, None, None
    while time.time() < end:
        hwnd = u32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            ours = psutil.Process(pid.value).exe().lower().startswith(prefix)
        except (psutil.Error, OSError):
            ours = False
        r = ctypes.wintypes.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(r))
        key = (hwnd, r.left, r.top, r.right, r.bottom) if ours else None
        if key and key == last:
            if time.time() - since >= still:
                return
        else:
            last, since = key, time.time()
        time.sleep(0.5)


def popup_worker():
    """Shows the top-middle pop-ups one after another; falls back to a Windows toast if one fails.
    A burst of more than 4 unlocks shows the first 3, then one summary pop-up for the rest."""
    import overlay
    while True:
        items = [POPUPS.get()]
        time.sleep(0.3)  # the unlocks of one sync arrive together
        while True:
            try:
                items.append(POPUPS.get_nowait())
            except queue.Empty:
                break
        for game_name, a in items:
            if a.get("welcome"):
                try:
                    wait_game_settled(a.get("dir"))
                    overlay.show(**a["args"])
                except Exception as e:
                    log.warning("welcome pop-up failed: %s", e)
        unlocks = [(g, a) for g, a in items if not a.get("welcome")]
        more = unlocks[3:] if len(unlocks) > 4 else []
        cfg = core.load_config()
        sound_on = cfg.get("achievement_sound", True)
        sound = ACH_SOUND if sound_on else None
        for game_name, a in unlocks[:3] if more else unlocks:
            try:
                overlay.show(a["name"], game_name, a.get("icon"), a.get("global_pct"),
                             ach_sound(a.get("global_pct")) if sound_on else None)
            except Exception as e:
                log.warning("achievement pop-up failed: %s", e)
                unlock_toast(game_name, a)
        if more:
            names = {g for g, _ in more}
            title, game = f"+{len(more)} more achievements", more[0][0] if len(names) == 1 else f"{len(names)} games"
            try:
                overlay.show(title, game, None, None, sound, head="ACHIEVEMENTS UNLOCKED")
            except Exception as e:
                log.warning("achievement pop-up failed: %s", e)
                toast("Achievements unlocked", f"{title}\n{game}", silent=play_achievement_sound())


def game_art(c, gid):
    r = c.execute("SELECT cover, custom_cover FROM games WHERE id=?", (gid,)).fetchone()
    return core.cover_file(r) if r else None


def on_unlock(game_name, a):
    log.info("UNLOCK %s: %s (%s)", game_name, a["name"], a["source"])
    cfg = core.load_config()
    if not cfg.get("notifications", True):
        return
    if cfg.get("unlock_popup", "overlay") == "overlay":
        POPUPS.put((game_name, a))
    else:
        unlock_toast(game_name, a)


class Tracker:
    def __init__(self):
        self.cfg = core.load_config()
        self.c = core.db()
        self.games = []          # [(prefix, gid, name)] longest prefix first
        self.games_at = 0
        self.active = {}         # gid -> {"sid", "start", "last", "exe", "beat"}
        self.exe_cache = {}      # pid -> (create_time, exe or None)
        self.watch = {}          # gid -> (files, files_at, signature)
        self.recent = {}         # gid -> time the game exited (keep watching achievements a while)
        self.last_tick = time.time()
        self.refreshing = False
        self.welcomed = {}       # gid -> time of the last welcome pop-up

    # ---------------------------------------------------------- games
    def load_games(self):
        rows = self.c.execute("SELECT id, name, custom_name, install_dir FROM games WHERE installed=1 AND hidden=0").fetchall()
        g = [(r["install_dir"].lower().rstrip("\\") + "\\", r["id"], core.display_name(r)) for r in rows]
        self.games = sorted(g, key=lambda x: -len(x[0]))
        self.games_at = time.time()

    def game_for(self, exe):
        low = exe.lower()
        for prefix, gid, name in self.games:
            if low.startswith(prefix):
                return gid, name
        return None

    def running(self):
        excl = [p.lower() for p in self.cfg["exclude_patterns"]]
        found, alive = {}, set()
        for p in psutil.process_iter(["pid", "create_time"]):
            pid, ct = p.info["pid"], p.info["create_time"]
            alive.add(pid)
            cached = self.exe_cache.get(pid)
            if not cached or cached[0] != ct:
                try:
                    exe = p.exe()
                except (psutil.Error, OSError):
                    exe = None
                cached = self.exe_cache[pid] = (ct, exe)
            exe = cached[1]
            if not exe:
                continue
            hit = self.game_for(exe)
            if hit:
                stem = Path(exe).stem.lower()
                if any(x in stem for x in excl):
                    continue
                f = found.setdefault(hit[0], [hit[1], exe, ct])
                if ct and (not f[2] or ct < f[2]):  # oldest process start = when the game was launched
                    f[2] = ct
        for pid in list(self.exe_cache):
            if pid not in alive:
                del self.exe_cache[pid]
        return found

    # ---------------------------------------------------------- sessions
    def start_session(self, gid, name, exe, now, launched=None):
        cur = self.c.execute("INSERT INTO sessions(game_id,start,end,exe) VALUES(?,?,?,?)", (gid, now, now, exe))
        self.active[gid] = {"sid": cur.lastrowid, "start": now, "last": now, "beat": now, "name": name, "exe": exe}
        log.info("PLAY %s (%s)", name, exe)
        if not launched or now - launched <= 60:  # not after sleep/resume or a tracker restart mid-game
            self.welcome(gid, name, now)

    def welcome(self, gid, name, now):
        cfg = core.load_config()
        try:
            again = max(0.0, float(cfg.get("welcome_cooldown_minutes", 5))) * 60
        except (TypeError, ValueError):
            again = 300
        if not cfg.get("welcome_popup", True) or now - self.welcomed.get(gid, 0) < again:
            return
        self.welcomed[gid] = now
        try:
            icon = game_art(self.c, gid)
        except Exception:
            icon = None
        prefix = next((p for p, g, _ in self.games if g == gid), None)
        POPUPS.put((name, {"welcome": True, "dir": prefix, "args": core.welcome_popup_args(cfg, name, icon)}))

    def end_session(self, gid, end, summary=True):
        s = self.active.pop(gid)
        dur = end - s["start"]
        if dur < self.cfg["min_session_seconds"]:
            self.c.execute("DELETE FROM sessions WHERE id=?", (s["sid"],))
            log.info("STOP %s after %ds (too short, dropped)", s["name"], dur)
            return
        self.c.execute("UPDATE sessions SET end=? WHERE id=?", (end, s["sid"]))
        log.info("STOP %s after %s", s["name"], fmt_dur(dur))
        self.recent[gid] = time.time()
        total = self.c.execute("SELECT SUM(end-start) FROM sessions WHERE game_id=?", (gid,)).fetchone()[0] or 0
        base = self.c.execute("SELECT baseline_seconds FROM games WHERE id=?", (gid,)).fetchone()[0] or 0
        if summary and self.cfg.get("session_summary", True):
            toast(f"Played {s['name']}", f"This session: {fmt_dur(dur)}\nTotal: {fmt_dur(total + base)}")
        # Steam uploads unlocks to the profile a little after exit
        threading.Timer(25, self.safe, args=(lambda: core.sync_progress(gid, notify=on_unlock, online=True),)).start()

    def tick(self):
        now = time.time()
        if now - self.last_tick > SLEEP_GAP:  # PC slept or hung: close sessions where they really stopped
            for gid in list(self.active):
                log.info("gap of %ds, splitting session", now - self.last_tick)
                self.end_session(gid, self.active[gid]["last"], summary=False)  # no toast over a resumed game
        self.last_tick = now
        if now - self.games_at > 60 or RELOAD_FLAG.exists():
            if RELOAD_FLAG.exists():
                self.cfg = core.load_config()
                RELOAD_FLAG.unlink(missing_ok=True)
            self.load_games()
        found = self.running()
        for gid, (name, exe, launched) in found.items():
            if gid not in self.active:
                self.start_session(gid, name, exe, now, launched)
            s = self.active[gid]
            s["last"] = now
            self.welcomed[gid] = now
            if now - s["beat"] >= HEARTBEAT:
                self.c.execute("UPDATE sessions SET end=? WHERE id=?", (now, s["sid"]))
                s["beat"] = now
        for gid in [g for g in self.active if g not in found]:
            self.end_session(gid, self.active[gid]["last"])
        self.write_live(now)

    def write_live(self, now):
        live = {"heartbeat": now, "pid": os.getpid(), "refreshing": self.refreshing,
                "playing": [{"id": g, "name": s["name"], "since": s["start"]} for g, s in self.active.items()]}
        tmp = core.LIVE_PATH.with_suffix(".tmp")
        for _ in range(5):  # a reader holding live.json open blocks the swap for a moment: retry, never fail the tick
            try:
                tmp.write_text(json.dumps(live), encoding="utf-8")
                os.replace(tmp, core.LIVE_PATH)
                return
            except OSError:
                time.sleep(0.05)
        log.warning("live.json busy, skipped this update")

    # ---------------------------------------------------------- achievements
    def check_achievements(self, all_games=False):
        now = time.time()
        ids = set(self.active) | {g for g, t in self.recent.items() if now - t < 600}
        if all_games:
            ids |= {gid for _, gid, _ in self.games}
        for gid in ids:
            files, at, sig = self.watch.get(gid, (None, 0, None))
            if files is None or now - at > 60:
                files, at = core.watched_files(gid), now
            new_sig = core.files_signature(files)
            if new_sig != sig:
                if sig is not None:
                    files = core.watched_files(gid)  # a new emulator file may have appeared
                    new_sig = core.files_signature(files)
                    core.sync_progress(gid, notify=on_unlock)
            self.watch[gid] = (files, at, new_sig)

    # ---------------------------------------------------------- background refresh
    def safe(self, fn):
        try:
            fn()
        except Exception:
            log.error("background task failed:\n%s", traceback.format_exc())

    def refresh_loop(self):
        while True:
            while self.active:  # never scan drives / go online mid-game: wait until it ends
                time.sleep(15)
            self.refreshing = True
            self.safe(lambda: core.full_refresh(self.cfg))
            self.refreshing = False
            self.games_at = 0
            time.sleep(REFRESH_EVERY)

    def run(self):
        log.info("tracker started (pid %d)", os.getpid())
        self.load_games()
        n = 0
        while True:
            try:
                self.tick()
                self.check_achievements(all_games=(n % 20 == 0))
            except Exception:
                log.error("tick failed:\n%s", traceback.format_exc())
            if n == 0:  # after the first tick, so a game already running defers the refresh
                threading.Thread(target=self.refresh_loop, daemon=True).start()
            n += 1
            time.sleep(self.cfg["poll_seconds"])


if __name__ == "__main__":
    core.setup_logging("tracker")
    _mutex = single_instance()
    try:
        register_aumid()
    except OSError as e:
        log.warning("AUMID registration failed: %s", e)
    threading.Thread(target=popup_worker, daemon=True).start()
    Tracker().run()
