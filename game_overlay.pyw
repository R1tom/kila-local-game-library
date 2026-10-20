"""KILA Local Game Library - in-game overlay.

Press the controller's Guide / PS button (or the keyboard shortcut, default Alt+G) while a game is
running and a panel slides in at the side of the screen:

  * FPS and a frame-time graph, from the AMD driver (ADLX, the numbers Adrenalin itself shows)
  * Radeon usage, clock, temperature, hotspot, board power, VRAM, fan
  * CPU and RAM
  * OBS recording: on / off, length and file size
  * achievements for the game: progress, unlocked this session, latest unlock
  * session and total playtime, clock

Every part, its position, size, opacity, colour and the shortcut are set in the app:
Settings -> Game overlay (keys overlay_* in config.json, re-read when the file changes).

Costs nothing while no game runs: it only checks data\\live.json every 2 s. During a game it reads
the controller 30 times a second (microseconds) and the driver FPS twice a second for the graph;
GPU stats are read only while the panel or the small FPS counter is on screen. The panel is a
click-through, never-focused window, so the game keeps its input and focus.
"""
import ctypes
import ctypes.wintypes as W
import json
import os
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

import psutil
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import overlay as ov  # noqa: E402  (shared fonts / layered-window plumbing of the achievement pop-up)

user32, gdi32, kernel32 = ov.user32, ov.gdi32, ov.kernel32
user32.GetWindow.argtypes = [W.HWND, W.UINT]
user32.GetWindow.restype = W.HWND
user32.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
user32.IsWindowVisible.argtypes = user32.IsIconic.argtypes = [W.HWND]
user32.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
user32.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT]
PREVIEW_FLAG = core.DATA / "overlay_preview.flag"
RECORDER = None  # optional companion recorder; without one, OBS is detected by its growing file
GAMING_FLAG = core.DATA / "gaming.flag"  # written by an optional game-mode helper
LOG = core.DATA / "game_overlay.log"

for _try in range(40 if "--restart" in sys.argv else 1):  # a restart waits for the old copy to exit
    _mutex = kernel32.CreateMutexW(None, False, "Local\\KilaGameOverlay")
    if kernel32.GetLastError() != 183:
        break
    kernel32.CloseHandle(_mutex)
    time.sleep(0.25)
else:
    raise SystemExit


def log(msg):
    try:
        if LOG.exists() and LOG.stat().st_size > 500_000:
            LOG.unlink()
        with LOG.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- controller (Guide button)
class _Pad(ctypes.Structure):
    _fields_ = [("buttons", W.WORD), ("lt", ctypes.c_ubyte), ("rt", ctypes.c_ubyte),
                ("lx", ctypes.c_short), ("ly", ctypes.c_short), ("rx", ctypes.c_short), ("ry", ctypes.c_short)]


class _State(ctypes.Structure):
    _fields_ = [("packet", W.DWORD), ("pad", _Pad)]


GUIDE = 0x0400
try:
    _xget = ctypes.WinDLL("xinput1_4")[100]  # XInputGetStateEx: the only call that reports Guide
    _xget.argtypes = [W.DWORD, ctypes.POINTER(_State)]
    _xget.restype = W.DWORD
except (OSError, AttributeError):
    _xget = None


UP, DOWN, LEFT, RIGHT, BTN_A, BTN_B = 0x1, 0x2, 0x4, 0x8, 0x1000, 0x2000


class PadInput:
    """Buttons of every pad: XInput slots OR'd with what ABridge DUALXBOX publishes in the shared link
    (it keeps publishing the real buttons while the game gets a neutral pad). Gives press edges
    with auto-repeat for the D-pad. Empty XInput slots are re-probed only every 3 s."""

    def __init__(self):
        import mmap
        try:
            self.link = mmap.mmap(-1, 16, tagname="Local\\ABridgePadLink")
        except Exception:
            self.link = None
        self.connected, self.probe_at = set(), 0.0
        self.prev, self.repeat_at = 0, {}

    def held(self):
        b = 0
        if self.link is not None:
            b |= self.link[0] | (self.link[1] << 8)
        if _xget:
            now = time.monotonic()
            slots = range(4) if now >= self.probe_at else self.connected
            if now >= self.probe_at:
                self.probe_at = now + 3
            st = _State()
            for i in list(slots):
                if _xget(i, ctypes.byref(st)) == 0:
                    self.connected.add(i)
                    b |= st.pad.buttons
                else:
                    self.connected.discard(i)
        return b

    def pressed(self):
        """Buttons newly pressed since the last call (D-pad repeats while held)."""
        b, now = self.held(), time.monotonic()
        new = b & ~self.prev
        for d in (UP, DOWN, LEFT, RIGHT):
            if new & d:
                self.repeat_at[d] = now + 0.4
            elif b & d and now >= self.repeat_at.get(d, 1e18):
                new |= d
                self.repeat_at[d] = now + 0.12
        self.prev = b
        return new

    def set_link(self, hold, guide):
        if self.link is not None:
            self.link[2], self.link[3] = int(bool(hold)), int(bool(guide))


# ---------------------------------------------------------------- keyboard shortcut
MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
NAMED = {"space": 0x20, "tab": 0x09, "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
         "pageup": 0x21, "pagedown": 0x22, "pause": 0x13, "scrolllock": 0x91, "`": 0xC0}


def parse_hotkey(text):
    mods, vk = 0, None
    for part in [p.strip().lower() for p in str(text or "").split("+") if p.strip()]:
        if part in MODS:
            mods |= MODS[part]
        elif part in NAMED:
            vk = NAMED[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x6F + int(part[1:])
    return (mods, vk) if vk else None


# ---------------------------------------------------------------- data
def read_live():
    try:
        live = json.loads(core.LIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if time.time() - live.get("heartbeat", 0) > 90 or not live.get("playing"):
        return None
    return live["playing"][0]


def last_played():
    """The most recently played game, for the panel when nothing is running (since=None)."""
    try:
        c = sqlite3.connect(f"file:{core.DB_PATH}?mode=ro", uri=True, timeout=2)
        r = c.execute("SELECT game_id FROM sessions ORDER BY end DESC LIMIT 1").fetchone()
        c.close()
        if r:
            return {"id": r[0], "name": "", "since": None}
    except sqlite3.Error:
        pass
    return {"id": "", "name": "", "since": None}


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, m = divmod(sec // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def ago(ts):
    d = time.time() - ts
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    return time.strftime("%d %b", time.localtime(ts))


class GameData:
    """Achievements and playtime for the running game, straight from tracker.db (read-only)."""

    def __init__(self):
        self.cache_at, self.cache = 0, {}

    def get(self, play):
        if time.time() - self.cache_at < 5 and self.cache.get("id") == play["id"]:
            return self.cache
        out = {"id": play["id"], "name": play.get("name") or "Game"}
        try:
            c = sqlite3.connect(f"file:{core.DB_PATH}?mode=ro", uri=True, timeout=2)
            c.row_factory = sqlite3.Row
            g = c.execute("SELECT * FROM games WHERE id=?", (play["id"],)).fetchone()
            if g:
                out["name"] = g["custom_name"] or g["name"] or out["name"]
                played = c.execute("SELECT COALESCE(SUM(end-start),0) FROM sessions WHERE game_id=?",
                                   (play["id"],)).fetchone()[0]
                out["total"] = (g["baseline_seconds"] or 0) + played
            out["ach_total"] = c.execute("SELECT COUNT(*) FROM achievements WHERE game_id=?", (play["id"],)).fetchone()[0]
            rows = c.execute("SELECT a.name, u.unlocked_at FROM unlocks u JOIN achievements a "
                             "ON a.game_id=u.game_id AND a.api=u.api WHERE u.game_id=? ORDER BY u.unlocked_at DESC",
                             (play["id"],)).fetchall()
            out["ach_done"] = len(rows)
            since = play.get("since") or float("inf")  # no running session -> nothing "this session"
            out["ach_session"] = sum(1 for r in rows if (r["unlocked_at"] or 0) >= since)
            out["ach_latest"] = (rows[0]["name"], rows[0]["unlocked_at"]) if rows else None
            nxt = c.execute("SELECT name, global_pct FROM achievements a WHERE game_id=? AND hidden=0 AND NOT EXISTS "
                            "(SELECT 1 FROM unlocks u WHERE u.game_id=a.game_id AND u.api=a.api) "
                            "ORDER BY global_pct DESC LIMIT 1", (play["id"],)).fetchone()
            out["ach_next"] = (nxt["name"], nxt["global_pct"]) if nxt else None
            c.close()
        except sqlite3.Error as e:
            log(f"db: {e}")
        self.cache_at, self.cache = time.time(), out
        return out


def recording_dirs():
    """The OBS recording folder (config overlay_recording_folder, default: your Videos folder) and its subfolders."""
    root = Path(core.load_config().get("overlay_recording_folder") or Path.home() / "Videos")
    try:
        return [root, *(d for d in root.iterdir() if d.is_dir())]
    except OSError:
        return []


def growing_recording():
    """An .mp4/.mkv in the recording folder (or a subfolder) written to in the last 10 s means OBS is recording."""
    try:
        newest = None
        for d in recording_dirs():
            if d.is_dir():
                for f in [*d.glob("*.mp4"), *d.glob("*.mkv")]:
                    st = f.stat()
                    if newest is None or st.st_mtime > newest[1].st_mtime:
                        newest = (f, st)
        if newest and time.time() - newest[1].st_mtime < 10:
            return ("rec", newest[1].st_ctime, newest[1].st_size)
    except OSError:
        pass
    return None


def recording_status():
    st = _recording_status()
    if st and st[0] == "off":
        return growing_recording() or st
    return st


def _recording_status():
    """('rec', since, size) / ('paused',) / ('off',) from a companion recorder's log, or OBS's growing file."""
    if RECORDER is None or not RECORDER.exists():
        return growing_recording()
    if (RECORDER / "paused").exists():
        return ("paused",)
    try:
        with (RECORDER / "recorder.log").open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 6000))
            tail = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return ("off",)
    for line in reversed(tail):
        if " STOPPED " in line:
            return ("off",)
        if " RECORDING " in line:
            try:
                since = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                since = None
            folder = line.rsplit("-> ", 1)[-1].strip()
            size = None
            try:
                files = sorted(Path(folder).glob("*.mp4"), key=lambda p: p.stat().st_mtime)
                size = files[-1].stat().st_size if files else None
            except OSError:
                pass
            if not any(p.info["name"] and p.info["name"].lower() == "obs64.exe" for p in psutil.process_iter(["name"])):
                return ("off",)
            return ("rec", since, size)
    return ("off",)


# ---------------------------------------------------------------- AMD
class Amd:
    """ADLX connection, opened once and kept for the life of the process: amdadlx64.dll can't be
    initialised again after ADLXTerminate in the same process (access violation, tested 2026-10-17).
    An open connection costs nothing while nobody asks it for data. If it ever stops answering
    (e.g. after a driver reset), the whole overlay restarts itself to get a fresh one."""

    def __init__(self):
        self.a, self.tried, self.errors = None, False, 0

    def ok(self):
        if self.a is None and not self.tried:
            self.tried = True
            try:
                import adlx
                self.a = adlx.ADLX()
                log(f"ADLX ready: {self.a.name}, {self.a.vram_total} MB")
            except Exception as e:
                log(f"ADLX unavailable, restarting the overlay: {e}")
                restart_self()
        return self.a is not None

    def _call(self, fn, default):
        try:
            if not self.ok():
                return default
            out = fn()
            self.errors = 0
            return out
        except Exception as e:
            self.errors += 1
            log(f"ADLX call failed ({self.errors}): {e!r}")
            if self.errors >= 5:
                restart_self()
            return default

    def fps(self):
        return self._call(lambda: self.a.fps(), None)

    def gpu(self):
        return self._call(lambda: self.a.gpu_metrics(), {})

    def read_3d(self):
        out = self._call(lambda: self.a.read_3d(), {})
        if self.a and not out:  # all settings missing = the connection went stale
            self.errors += 1
            if self.errors >= 5:
                restart_self()
        return out

    def set_3d(self, key, enabled=None, value=None):
        return self._call(lambda: self.a.set_3d(key, enabled, value), False)


AMD = Amd()
_restarting = False


def restart_self():
    """Start a fresh copy of the overlay and exit this one (the new copy waits for our mutex).
    Never more than once a minute, so a missing driver can't cause a restart loop."""
    global _restarting
    if _restarting:
        return
    _restarting = True
    stamp = core.DATA / "overlay_restart.stamp"
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < 60:
            log("restart skipped: restarted less than a minute ago")
            return
        stamp.touch()
        import subprocess
        try:
            mm = __import__("mmap").mmap(-1, 16, tagname="Local\\ABridgePadLink")
            mm[2] = 0  # never leave the game's controller held neutral
        except Exception:
            pass
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--restart"], cwd=str(core.HERE),
                         creationflags=0x08000000)
        log("restarting the overlay")
        os._exit(0)
    except Exception as e:
        log(f"restart failed: {e!r}")


# ---------------------------------------------------------------- per-game AMD profiles
PROFILES = core.DATA / "amd_profiles.json"
SETTING_KEYS = ["afmf", "antilag", "rsr", "sharpening", "boost", "chill", "frtc", "esync", "vsync"]


class AmdProfiles:
    """ADLX only changes Adrenalin's GLOBAL settings, so per-game settings are done here:
    when a game starts, the current global values are saved as the baseline and the game's own
    profile (what was changed during earlier sessions) is applied; when it closes the baseline is
    put back. The baseline is on disk, so a crash mid-game is undone at the next start."""

    def __init__(self, amd):
        self.amd = amd
        try:
            self.data = json.loads(PROFILES.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}
        self.data.setdefault("games", {})
        self.data.setdefault("baseline", None)
        self.active = None

    def save(self):
        try:
            tmp = PROFILES.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            os.replace(tmp, PROFILES)
        except OSError as e:
            log(f"profiles save: {e}")

    @staticmethod
    def snapshot(cur):
        return {k: {"enabled": v.get("enabled"), "value": v.get("value")}
                for k, v in cur.items() if v.get("supported")}

    def _apply(self, wanted, label):
        cur = self.amd.read_3d()
        changed = []
        for k, st in wanted.items():
            c = cur.get(k)
            if not c or not c.get("supported"):
                continue
            if c.get("enabled") != st.get("enabled") or (st.get("value") is not None and c.get("value") != st.get("value")):
                if k == "vsync":
                    self.amd.set_3d(k, value=st.get("value"))
                else:
                    self.amd.set_3d(k, enabled=st.get("enabled"), value=st.get("value"))
                changed.append(k)
        if changed:
            log(f"AMD {label}: {', '.join(changed)}")

    def game_started(self, gid):
        if self.data["baseline"]:  # left over from a crash: put the normal settings back first
            self._apply(self.data["baseline"], "restore leftover baseline")
        self.data["baseline"] = self.snapshot(self.amd.read_3d())
        self.data["baseline_game"] = gid
        self.active = gid
        self.save()
        prof = self.data["games"].get(gid)
        if prof:
            self._apply(prof, f"profile for {gid}")

    def game_stopped(self):
        if self.data["baseline"]:
            self._apply(self.data["baseline"], "restore normal settings")
        self.data["baseline"], self.active = None, None
        self.save()

    def changed(self, key, state):
        """The user changed `key` in the panel: remember it for the running game."""
        if self.active:
            self.data["games"].setdefault(self.active, {})[key] = {"enabled": state.get("enabled"),
                                                                   "value": state.get("value")}
            self.save()

    def reset_game(self):
        if self.active and self.data["games"].pop(self.active, None) is not None:
            self._apply(self.data["baseline"] or {}, "profile reset")
            self.save()

    def has_profile(self):
        return bool(self.active and self.data["games"].get(self.active))


# ---------------------------------------------------------------- drawing
def hex_rgb(s, default=(16, 124, 16)):
    s = str(s or "").lstrip("#")
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4)) if len(s) == 6 else default
    except ValueError:
        return default


WHITE, DIM, FAINT = (255, 255, 255), (165, 168, 176), (90, 94, 104)
PANEL_BG, TILE_BG, TRACK = (16, 17, 21, 238), (32, 34, 41, 255), (52, 55, 64, 255)
RED, AMBER = (232, 72, 72), (240, 180, 76)


class Painter:
    def __init__(self, s, accent):
        self.s, self.acc = s, accent
        f = ov.font
        self.f_title = f(["segoeuib.ttf", "seguisb.ttf"], round(19 * s))
        self.f_big = f(["seguisb.ttf", "segoeuib.ttf"], round(34 * s))
        self.f_val = f(["seguisb.ttf", "segoeuib.ttf"], round(15 * s))
        self.f_txt = f(["segoeui.ttf"], round(13 * s))
        self.f_small = f(["segoeui.ttf"], round(11.5 * s))
        self.f_head = f(["seguisb.ttf", "segoeuib.ttf"], round(11 * s))
        try:
            self.logo = Image.open(core.HERE / "ui" / "icon.png").convert("RGBA").resize((round(22 * s),) * 2, Image.LANCZOS)
        except Exception:
            self.logo = None

    def S(self, v):
        return round(v * self.s)

    def head(self, d, x, y, text):
        for ch in text.upper():
            d.text((x, y), ch, font=self.f_head, fill=self.acc_light)
            x += d.textlength(ch, font=self.f_head) + 1.3 * self.s
        return y + self.S(20)

    @property
    def acc_light(self):
        return tuple(round(c + (255 - c) * 0.55) for c in self.acc)

    def bar(self, d, x, y, w, frac, color=None, h=6):
        h = self.S(h)
        d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=TRACK)
        fw = round(w * max(0.0, min(1.0, frac)))
        if fw > h:
            d.rounded_rectangle((x, y, x + fw, y + h), radius=h // 2, fill=color or self.acc)

    def tile(self, d, x, y, w, label, value, warn=False):
        h = self.S(50)
        d.rounded_rectangle((x, y, x + w, y + h), radius=self.S(8), fill=TILE_BG)
        d.text((x + self.S(10), y + self.S(7)), label, font=self.f_small, fill=DIM)
        d.text((x + self.S(10), y + self.S(24)), value, font=self.f_val, fill=AMBER if warn else WHITE)
        return h


PIN_LABELS = {  # everything that can be pinned to stay on screen after the overlay closes
    "fps": "FPS", "frametime": "Frame time", "gpu_usage": "GPU usage", "gpu_temp": "GPU temperature",
    "gpu_hotspot": "GPU hotspot", "gpu_clock": "GPU clock", "gpu_power": "Board power", "gpu_fan": "GPU fan",
    "vram": "VRAM", "cpu": "CPU", "ram": "RAM", "rec": "Recording", "ach": "Achievements",
}


def draw_pin(p, d, x, y, on):
    """Small push-pin: filled in the accent colour when pinned."""
    S = p.S
    r = S(4)
    d.ellipse((x, y, x + 2 * r, y + 2 * r), fill=p.acc_light if on else None, outline=p.acc_light if on else FAINT,
              width=max(1, S(1)))
    d.line((x + r, y + 2 * r, x + r, y + 2 * r + S(5)), fill=p.acc_light if on else FAINT, width=max(1, S(1)))


def render_panel(p, cfg, info):
    """Stats panel. Returns (image, pin hit boxes [(x0, y0, x1, y1, key)], pin keys in screen order)."""
    s, S = p.s, p.S
    Wd, pad = S(392), S(18)
    inner = Wd - 2 * pad
    img = Image.new("RGBA", (Wd, S(1400)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = pad
    pins, focus = info.get("pins") or set(), info.get("focus")
    hits, order = [], []

    def pin(key, box, icon):
        """Register a pinnable item: click/select box, pin icon position, focus outline."""
        hits.append((*box, key))
        order.append(key)
        draw_pin(p, d, icon[0], icon[1], key in pins)
        if key == focus:
            d.rounded_rectangle((box[0] - S(4), box[1] - S(4), box[2] + S(4), box[3] + S(4)), radius=S(8),
                                outline=p.acc_light, width=max(2, S(2)))

    # header: logo, app name, clock / game, playtime, boost
    if p.logo:
        img.alpha_composite(p.logo, (pad, y))
    d.text((pad + S(30), y + S(3)), "KILA Local Game Library", font=p.f_head, fill=DIM)
    clock = time.strftime("%H:%M")
    d.text((Wd - pad - d.textlength(clock, font=p.f_title), y - S(2)), clock, font=p.f_title, fill=WHITE)
    y += S(34)
    gd = info.get("game", {})
    d.text((pad, y), ov.fit(d, gd.get("name", "No game"), p.f_title, inner), font=p.f_title, fill=WHITE)
    y += S(28)
    bits = []
    if info.get("idle"):
        bits.append("Not playing · last game" if gd.get("id") else "Not playing")
    if info.get("since"):
        bits.append("Session " + fmt_dur(time.time() - info["since"]))
    if gd.get("total") is not None:
        bits.append("Total " + fmt_dur(gd["total"] + (time.time() - info["since"] if info.get("since") else 0)))
    if info.get("boost"):
        bits.append("Game mode on")
    d.text((pad, y), "  ·  ".join(bits) or " ", font=p.f_txt, fill=DIM)
    y += S(30)

    def divider():
        nonlocal y
        d.line((pad, y, Wd - pad, y), fill=(44, 46, 54), width=max(1, S(1)))
        y += S(14)

    if cfg.get("overlay_show_fps", True) and info.get("idle"):
        divider()
        y = p.head(d, pad, y, "Performance")
        d.text((pad, y), "FPS and the frame-time graph appear while a game runs.", font=p.f_txt, fill=DIM)
        y += S(30)
    elif cfg.get("overlay_show_fps", True):
        divider()
        y = p.head(d, pad, y, "Performance")
        hist = [f for f in info.get("fps_hist", []) if f]
        fps = info.get("fps")
        pin("fps", (pad, y - S(4), Wd - pad, y + S(38)), (Wd - pad - S(10), y - S(4)))
        if fps:
            txt = str(fps)
            d.text((pad, y - S(6)), txt, font=p.f_big, fill=WHITE)
            d.text((pad + d.textlength(txt, font=p.f_big) + S(6), y + S(16)), "FPS", font=p.f_txt, fill=DIM)
            if hist:
                avg = sum(hist) / len(hist)
                low = sorted(hist)[max(0, len(hist) // 100)]
                stat = f"avg {avg:.0f}   low {low:.0f}   ·   {1000 / fps:.1f} ms"
                d.text((Wd - pad - d.textlength(stat, font=p.f_txt), y + S(14)), stat, font=p.f_txt, fill=DIM)
            y += S(44)
        else:
            d.text((pad, y), "Waiting for FPS from the AMD driver…", font=p.f_txt, fill=DIM)
            d.text((pad, y + S(18)), "It appears once the game is in front and rendering.", font=p.f_small, fill=FAINT)
            y += S(44)
        # frame-time graph (ms per frame from the driver FPS, one point per half second)
        gh = S(78)
        d.rounded_rectangle((pad, y, Wd - pad, y + gh), radius=S(8), fill=TILE_BG)
        ms = [1000 / f for f in info.get("fps_hist", []) if f]
        top = max(40.0, (max(ms) * 1.15) if ms else 40.0)
        gx0, gx1, gy0, gy1 = pad + S(34), Wd - pad - S(8), y + S(8), y + gh - S(8)
        for ref in (16.7, 33.3):
            if ref < top:
                ry = gy1 - (gy1 - gy0) * ref / top
                d.line((gx0, ry, gx1, ry), fill=(58, 61, 72), width=1)
                d.text((pad + S(6), ry - S(7)), f"{ref:.0f}", font=p.f_small, fill=FAINT)
        if len(ms) >= 2:
            n = info.get("fps_hist").maxlen or len(ms)
            step = (gx1 - gx0) / max(1, n - 1)
            x0 = gx1 - step * (len(ms) - 1)
            pts = [(x0 + i * step, gy1 - (gy1 - gy0) * min(v, top) / top) for i, v in enumerate(ms)]
            fill = tuple(round(t + (a - t) * 0.3) for t, a in zip(TILE_BG[:3], p.acc)) + (255,)  # opaque: draw replaces pixels
            d.polygon(pts + [(pts[-1][0], gy1), (pts[0][0], gy1)], fill=fill)
            d.line(pts, fill=p.acc_light, width=max(2, S(2)), joint="curve")
        d.text((gx1 - S(16) - d.textlength("frame time (ms)", font=p.f_small), gy0), "frame time (ms)",
               font=p.f_small, fill=FAINT)
        pin("frametime", (pad, y, Wd - pad, y + gh), (Wd - pad - S(16), y + S(6)))
        y += gh + S(16)

    if cfg.get("overlay_show_gpu", True):
        divider()
        g, vt = info.get("gpu") or {}, info.get("vram_total") or 0
        y = p.head(d, pad, y, info.get("gpu_name") or "AMD Radeon")
        if g:
            tw = (inner - S(16)) // 3
            vals = [
                ("gpu_usage", "Usage", f"{g.get('usage', 0):.0f}%", False),
                ("gpu_temp", "Temperature", f"{g.get('temp', 0):.0f}°C", g.get("temp", 0) >= 85),
                ("gpu_hotspot", "Hotspot", f"{g.get('hotspot', 0):.0f}°C", g.get("hotspot", 0) >= 100),
                ("gpu_clock", "Clock", f"{g.get('clock', 0)} MHz", False),
                ("gpu_power", "Board power", f"{g.get('board_power', g.get('power', 0)):.0f} W", False),
                ("gpu_fan", "Fan", f"{g.get('fan', 0)} rpm" if g.get("fan") else "off (quiet)", False),
            ]
            for i, (key, lab, val, warn) in enumerate(vals):
                x = pad + (i % 3) * (tw + S(8))
                if i % 3 == 0 and i:
                    y += S(58)
                p.tile(d, x, y, tw, lab, val, warn)
                pin(key, (x, y, x + tw, y + S(50)), (x + tw - S(14), y + S(6)))
            y += S(62)
            if vt:
                used = g.get("vram", 0)
                d.text((pad, y), "VRAM", font=p.f_txt, fill=DIM)
                pin("vram", (pad, y, Wd - pad, y + S(30)), (pad + d.textlength("VRAM", font=p.f_txt) + S(8), y + S(2)))
                vtxt = f"{used / 1024:.1f} / {vt / 1024:.0f} GB"
                d.text((Wd - pad - d.textlength(vtxt, font=p.f_txt), y), vtxt, font=p.f_txt, fill=WHITE)
                p.bar(d, pad, y + S(22), inner, used / vt, AMBER if used / vt > 0.92 else None)
                y += S(38)
        else:
            d.text((pad, y), "AMD driver data not available.", font=p.f_txt, fill=DIM)
            y += S(26)

    if cfg.get("overlay_show_system", True):
        divider()
        y = p.head(d, pad, y, "System")
        cpu, vm = info.get("cpu", 0), psutil.virtual_memory()
        for key, lab, frac, txt in (("cpu", "CPU", cpu / 100, f"{cpu:.0f}%"),
                                    ("ram", "RAM", vm.percent / 100, f"{vm.used / 2**30:.1f} / {vm.total / 2**30:.1f} GB")):
            d.text((pad, y), lab, font=p.f_txt, fill=DIM)
            pin(key, (pad, y, Wd - pad, y + S(30)), (pad + d.textlength(lab, font=p.f_txt) + S(8), y + S(2)))
            d.text((Wd - pad - d.textlength(txt, font=p.f_txt), y), txt, font=p.f_txt, fill=WHITE)
            p.bar(d, pad, y + S(22), inner, frac, RED if frac >= 0.9 else None)
            y += S(38)

    rec = info.get("rec")
    if cfg.get("overlay_show_recording", True) and rec:
        divider()
        y = p.head(d, pad, y, "Recording")
        pin("rec", (pad, y - S(20), Wd - pad, y + S(24)), (Wd - pad - S(10), y - S(20)))
        if rec[0] == "rec":
            dot = S(10)
            d.ellipse((pad, y + S(5), pad + dot, y + S(5) + dot), fill=RED)
            txt = "Recording" + (f"  ·  {fmt_dur(time.time() - rec[1])}" if rec[1] else "")
            if rec[2]:
                txt += f"  ·  {rec[2] / 2**30:.1f} GB"
            d.text((pad + dot + S(10), y), txt, font=p.f_val, fill=WHITE)
        elif rec[0] == "paused":
            d.text((pad, y), "Game Recorder is switched OFF", font=p.f_val, fill=AMBER)
        else:
            d.text((pad, y), "Not recording", font=p.f_val, fill=DIM)
        y += S(30)

    if cfg.get("overlay_show_achievements", True):
        divider()
        y = p.head(d, pad, y, "Achievements")
        pin("ach", (pad, y - S(20), Wd - pad, y + S(40)), (Wd - pad - S(10), y - S(20)))
        tot, done = gd.get("ach_total", 0), gd.get("ach_done", 0)
        if tot:
            txt = f"{done} / {tot}"
            d.text((pad, y - S(2)), txt, font=p.f_title, fill=WHITE)
            pct = f"{done * 100 / tot:.0f}%"
            d.text((Wd - pad - d.textlength(pct, font=p.f_title), y - S(2)), pct, font=p.f_title, fill=p.acc_light)
            p.bar(d, pad, y + S(30), inner, done / tot, h=8)
            y += S(46)
            if gd.get("ach_session"):
                d.text((pad, y), f"+{gd['ach_session']} unlocked this session", font=p.f_txt, fill=p.acc_light)
                y += S(22)
            if gd.get("ach_latest"):
                n, ts = gd["ach_latest"]
                d.text((pad, y), ov.fit(d, f"Latest: {n}", p.f_txt, inner - S(80)), font=p.f_txt, fill=WHITE)
                a = ago(ts) if ts else ""
                d.text((Wd - pad - d.textlength(a, font=p.f_small), y + S(2)), a, font=p.f_small, fill=DIM)
                y += S(22)
            if gd.get("ach_next"):
                n, g_pct = gd["ach_next"]
                tail = f"  ·  {g_pct:.0f}% of players" if g_pct is not None else ""
                d.text((pad, y), ov.fit(d, f"Easiest next: {n}{tail}", p.f_txt, inner), font=p.f_txt, fill=DIM)
                y += S(22)
        else:
            d.text((pad, y), "No achievement list for this game.", font=p.f_txt, fill=DIM)
            y += S(26)

    y += S(6)
    hint = info.get("hint", "")
    d.text(((Wd - d.textlength(hint, font=p.f_small)) / 2, y), hint, font=p.f_small, fill=FAINT)
    y += S(22)

    out = Image.new("RGBA", (Wd, y), (0, 0, 0, 0))
    ImageDraw.Draw(out).rounded_rectangle((0, 0, Wd - 1, y - 1), radius=S(16), fill=PANEL_BG,
                                          outline=p.acc + (150,), width=max(1, S(1)))
    out.alpha_composite(img.crop((0, 0, Wd, y)))
    return out, hits, order


def setting_value_text(key, st):
    import adlx
    if key == "vsync":
        v = st.get("value") or 0
        return adlx.VSYNC_MODES[v] if 0 <= v < 4 else str(v)
    if st.get("value") is None:
        return ""
    return f"{st['value']}{adlx.SETTINGS[key].get('unit', '')}"


def render_settings(p, rows, sel, game_name, has_profile, note="", adr_title=""):
    """Left panel. rows: [(key, label, state)] + ("reset", ...). Returns (image, hit boxes)."""
    s, S = p.s, p.S
    Wd, pad = S(372), S(18)
    inner = Wd - 2 * pad
    img = Image.new("RGBA", (Wd, S(1700)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    hits = []
    y = pad
    if p.logo:
        img.alpha_composite(p.logo, (pad, y))
    d.text((pad + S(30), y + S(3)), "AMD SETTINGS", font=p.f_head, fill=p.acc_light)
    y += S(32)
    d.text((pad, y), ov.fit(d, game_name, p.f_title, inner), font=p.f_title, fill=WHITE)
    y += S(28)
    sub = adr_title or "Your Adrenalin settings for this game · changes are saved to Adrenalin"
    for line in _wrap(d, sub, p.f_small, inner):
        d.text((pad, y), line, font=p.f_small, fill=DIM)
        y += S(16)
    y += S(10)
    rh = S(54)
    adr_head = True  # single section now: no divider / second heading
    for i, row in enumerate(rows):
        x0, x1 = pad - S(6), Wd - pad + S(6)
        if row[0] in ("adr", "adr_none"):
            if not adr_head:  # the game's own profile as saved in Adrenalin (switchable, saved to Adrenalin)
                adr_head = True
                y += S(4)
                d.line((pad, y, Wd - pad, y), fill=(44, 46, 54), width=max(1, S(1)))
                y += S(12)
                y = p.head(d, pad, y, "Saved in Adrenalin" + (f" · {adr_title}" if adr_title else ""))
            if row[0] == "adr_none":
                for line in _wrap(d, "This game has no settings of its own in Adrenalin yet, so it uses the global "
                                     "settings. Open it in Adrenalin (Gaming > Games) and change any option once.",
                                  p.f_small, inner):
                    d.text((pad, y), line, font=p.f_small, fill=DIM)
                    y += S(16)
                continue
            y = _adr_row(p, d, hits, i, row, sel, y, Wd, pad)
            continue
        if i == sel:
            d.rounded_rectangle((x0, y, x1, y + rh - S(4)), radius=S(10), fill=tuple(
                round(t + (a - t) * 0.28) for t, a in zip(TILE_BG[:3], p.acc)) + (255,))
        hits.append((x0, y, x1, y + rh, ("select", i)))
        if row[0] == "reset":
            d.text((pad, y + S(16)), "Reset this game to my normal settings", font=p.f_txt, fill=WHITE if i == sel else DIM)
            hits.append((x0, y, x1, y + rh, ("reset", None)))
            y += rh
            continue
        key, label, st = row
        sup = st.get("supported")
        d.text((pad, y + S(7)), label, font=p.f_val, fill=WHITE if sup else FAINT)
        val = setting_value_text(key, st) if sup else "not supported here"
        adjustable = sup and st.get("range") and (key == "vsync" or st.get("enabled"))
        if adjustable:
            ty, th = y + S(32), S(5)  # arrows drawn as triangles (Segoe UI has no ◀ ▶ glyphs)
            d.polygon([(pad, ty + th), (pad + S(8), ty), (pad + S(8), ty + 2 * th)], fill=p.acc_light)
            vx = pad + S(16)
            d.text((vx, y + S(28)), val, font=p.f_txt, fill=WHITE)
            ax = vx + d.textlength(val, font=p.f_txt) + S(8)
            d.polygon([(ax + S(8), ty + th), (ax, ty), (ax, ty + 2 * th)], fill=p.acc_light)
            hits.append((pad - S(6), y + S(24), pad + S(14), y + rh, ("dec", key)))
            hits.append((ax - S(6), y + S(24), ax + S(18), y + rh, ("inc", key)))
        else:
            txt = "not supported here" if not sup else (val or "On") if st.get("enabled") else "Off"
            d.text((pad, y + S(28)), txt, font=p.f_txt, fill=p.acc_light if sup and st.get("enabled") else DIM)
        if sup and key != "vsync":
            on = bool(st.get("enabled"))
            sw_w, sw_h = S(42), S(24)
            sx, sy = Wd - pad - sw_w, y + (rh - S(4) - sw_h) // 2
            d.rounded_rectangle((sx, sy, sx + sw_w, sy + sw_h), radius=sw_h // 2, fill=p.acc if on else TRACK)
            k = sw_h - S(6)
            kx = sx + sw_w - k - S(3) if on else sx + S(3)
            d.ellipse((kx, sy + S(3), kx + k, sy + S(3) + k), fill=WHITE)
            hits.append((sx - S(8), y, sx + sw_w + S(8), y + rh, ("toggle", key)))
        y += rh
    y += S(8)
    if note:
        d.text((pad, y), ov.fit(d, note, p.f_small, inner), font=p.f_small, fill=AMBER)
        y += S(20)
    hint = "D-pad up/down select · A on/off · left/right adjust · B close"
    d.text(((Wd - d.textlength(hint, font=p.f_small)) / 2, y), hint, font=p.f_small, fill=FAINT)
    y += S(24)
    out = Image.new("RGBA", (Wd, y), (0, 0, 0, 0))
    ImageDraw.Draw(out).rounded_rectangle((0, 0, Wd - 1, y - 1), radius=S(16), fill=PANEL_BG,
                                          outline=p.acc + (150,), width=max(1, S(1)))
    out.alpha_composite(img.crop((0, 0, Wd, y)))
    return out, hits


def _adr_row(p, d, hits, i, row, sel, y, Wd, pad):
    """One compact Adrenalin-profile row: label, then a switch (on/off) or arrows around the value."""
    S = p.S
    _, prop, e = row
    rh = S(44)
    x0, x1 = pad - S(6), Wd - pad + S(6)
    if i == sel:
        d.rounded_rectangle((x0, y, x1, y + rh - S(4)), radius=S(8), fill=tuple(
            round(t + (a - t) * 0.28) for t, a in zip(TILE_BG[:3], p.acc)) + (255,))
    hits.append((x0, y, x1, y + rh, ("select", i)))
    d.text((pad, y + S(10)), e["label"], font=p.f_val, fill=WHITE)
    txt, glob = e["text"], e["raw"] == "" or e["text"] == "Global"
    if e["kind"] == "bool" and not glob:
        on = txt == "On"
        sw_w, sw_h = S(38), S(20)
        sx, sy = Wd - pad - sw_w, y + (rh - S(4) - sw_h) // 2
        d.rounded_rectangle((sx, sy, sx + sw_w, sy + sw_h), radius=sw_h // 2, fill=p.acc if on else TRACK)
        k = sw_h - S(6)
        kx = sx + sw_w - k - S(3) if on else sx + S(3)
        d.ellipse((kx, sy + S(3), kx + k, sy + S(3) + k), fill=WHITE)
        hits.append((sx - S(8), y, sx + sw_w + S(8), y + rh, ("adr_toggle", prop)))
    elif glob:
        d.text((Wd - pad - d.textlength("Global", font=p.f_small), y + S(10)), "Global", font=p.f_small, fill=FAINT)
    else:
        tw = d.textlength(txt, font=p.f_txt)
        rx = Wd - pad - S(8)
        vx = rx - S(8) - tw
        ty, th = y + S(12), S(5)
        d.polygon([(rx + S(8), ty + th), (rx, ty), (rx, ty + 2 * th)], fill=p.acc_light)
        d.text((vx, y + S(8)), txt, font=p.f_txt, fill=WHITE)
        lx = vx - S(16)
        d.polygon([(lx, ty + th), (lx + S(8), ty), (lx + S(8), ty + 2 * th)], fill=p.acc_light)
        hits.append((lx - S(6), y, lx + S(14), y + rh, ("adr_dec", prop)))
        hits.append((rx - S(6), y, rx + S(18), y + rh, ("adr_inc", prop)))
    return y + rh


def _wrap(d, text, f, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + [cur] if cur else lines


def render_pins(p, keys, data):
    """Compact strip of the pinned stats, shown while playing with the overlay closed."""
    S = p.S
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    chips = []  # (label, value, colour) or ("graph", ms list)
    g, fps, gd, rec = data.get("gpu") or {}, data.get("fps"), data.get("game") or {}, data.get("rec")
    for k in keys:
        if k == "fps":
            chips.append(("FPS", str(fps) if fps else "--", WHITE))
        elif k == "frametime":
            chips.append(("graph", [1000 / f for f in data.get("fps_hist", []) if f]))
            chips.append(("ms", f"{1000 / fps:.1f}" if fps else "--", WHITE))
        elif k == "gpu_usage":
            chips.append(("GPU", f"{g.get('usage', 0):.0f}%" if g else "--", WHITE))
        elif k == "gpu_temp":
            chips.append(("GPU", f"{g.get('temp', 0):.0f}°C" if g else "--", AMBER if g.get("temp", 0) >= 85 else WHITE))
        elif k == "gpu_hotspot":
            chips.append(("HOT", f"{g.get('hotspot', 0):.0f}°C" if g else "--",
                          AMBER if g.get("hotspot", 0) >= 100 else WHITE))
        elif k == "gpu_clock":
            chips.append(("MHz", str(g.get("clock", "--")), WHITE))
        elif k == "gpu_power":
            chips.append(("W", f"{g.get('board_power', g.get('power', 0)):.0f}" if g else "--", WHITE))
        elif k == "gpu_fan":
            chips.append(("FAN", f"{g.get('fan', 0)}" if g.get("fan") else "off", WHITE))
        elif k == "vram":
            chips.append(("VRAM", f"{g.get('vram', 0) / 1024:.1f} GB" if g else "--", WHITE))
        elif k == "cpu":
            chips.append(("CPU", f"{data.get('cpu', 0):.0f}%", WHITE))
        elif k == "ram":
            vm = psutil.virtual_memory()
            chips.append(("RAM", f"{vm.used / 2**30:.1f} GB", RED if vm.percent >= 90 else WHITE))
        elif k == "rec":
            if rec and rec[0] == "rec":
                chips.append(("REC", fmt_dur(time.time() - rec[1]) if rec[1] else "on", RED))
            else:
                chips.append(("REC", "off" if not rec or rec[0] == "off" else "paused", DIM))
        elif k == "ach":
            chips.append(("ACH", f"{gd.get('ach_done', 0)}/{gd.get('ach_total', 0)}" if gd.get("ach_total") else "--",
                          WHITE))
    if not chips:
        chips = [("FPS", str(fps) if fps else "--", WHITE)]
    h, gap, gw = S(30), S(14), S(90)
    w = S(10)
    for c in chips:
        w += (gw if c[0] == "graph" else round(probe.textlength(c[1], font=p.f_val) + probe.textlength(c[0], font=p.f_small)) + S(5)) + gap
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=S(8), fill=(14, 15, 18, 205))
    x = S(10)
    for c in chips:
        if c[0] == "graph":
            ms = c[1][-45:]
            gy0, gy1 = S(6), h - S(6)
            d.rounded_rectangle((x, gy0 - S(2), x + gw, gy1 + S(2)), radius=S(4), fill=(28, 30, 36, 255))
            if len(ms) >= 2:
                top = max(40.0, max(ms) * 1.15)
                step = gw / 44
                x0 = x + gw - step * (len(ms) - 1)
                pts = [(x0 + i * step, gy1 - (gy1 - gy0) * min(v, top) / top) for i, v in enumerate(ms)]
                d.line(pts, fill=p.acc_light, width=max(1, S(2)))
            x += gw + gap
            continue
        lab, val, col = c
        d.text((x, S(5)), val, font=p.f_val, fill=col)
        x += probe.textlength(val, font=p.f_val) + S(5)
        d.text((x, S(10)), lab, font=p.f_small, fill=p.acc_light)
        x += probe.textlength(lab, font=p.f_small) + gap
    return img


# ---------------------------------------------------------------- layered window
CLICKS = []  # (hwnd, x, y) left clicks on clickable layers, handled by the main loop


def _click_proc(h, m, w, l):
    if m == 0x0021:  # WM_MOUSEACTIVATE: take the click but never the focus (the game keeps it)
        return 3     # MA_NOACTIVATE
    if m == 0x0201:  # WM_LBUTTONDOWN
        CLICKS.append((h, ctypes.c_short(l & 0xFFFF).value, ctypes.c_short((l >> 16) & 0xFFFF).value))
        return 0
    return user32.DefWindowProcW(h, m, w, l)


_click_wndproc = ov.WNDPROC(_click_proc)


class Layer:
    _classes = {}

    def __init__(self, title, clickable=False):
        hinst = kernel32.GetModuleHandleW(None)
        cls = "KilaGameOverlayClick" if clickable else "KilaGameOverlay"
        if cls not in Layer._classes:
            wc = ov.WNDCLASSEXW(cbSize=ctypes.sizeof(ov.WNDCLASSEXW),
                                lpfnWndProc=_click_wndproc if clickable else ov._wndproc,
                                hInstance=hinst, lpszClassName=cls,
                                hCursor=user32.LoadCursorW(None, ctypes.c_void_p(32512)))  # IDC_ARROW
            user32.RegisterClassExW(ctypes.byref(wc))
            Layer._classes[cls] = wc
        ex = 0x80000 | 0x8 | 0x80 | 0x8000000  # LAYERED|TOPMOST|TOOLWINDOW|NOACTIVATE
        if not clickable:
            ex |= 0x20  # TRANSPARENT: clicks fall through to the game
        self.hwnd = user32.CreateWindowExW(ex, cls, title, 0x80000000, 0, 0, 1, 1, None, None, hinst, None)
        self.pos = (0, 0)
        self.size, self.visible = None, False
        self.screen = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.screen)
        self.dib = self.old = None
        self.bits = ctypes.c_void_p()

    def show(self, im, x, y, alpha=1.0):
        if self.size != im.size:
            if self.dib:
                gdi32.SelectObject(self.mem, self.old)
                gdi32.DeleteObject(self.dib)
            bmi = ov.BITMAPINFOHEADER(biSize=40, biWidth=im.width, biHeight=-im.height, biPlanes=1, biBitCount=32)
            self.dib = gdi32.CreateDIBSection(self.screen, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
            self.old = gdi32.SelectObject(self.mem, self.dib)
            self.size = im.size
        data = ov._bgra_premultiplied(im)
        ctypes.memmove(self.bits, data, len(data))
        blend = ov.BLENDFUNCTION(0, 0, max(30, min(255, round(alpha * 255))), 1)
        user32.UpdateLayeredWindow(self.hwnd, self.screen, ctypes.byref(W.POINT(x, y)), ctypes.byref(W.SIZE(*im.size)),
                                   self.mem, ctypes.byref(W.POINT(0, 0)), 0, ctypes.byref(blend), 2)
        if not self.visible:  # only when appearing: re-asserting TOPMOST every update can minimize a fullscreen game
            user32.SetWindowPos(self.hwnd, W.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)  # TOPMOST, show
        self.visible, self.pos = True, (x, y)

    def hide(self):
        if self.visible:
            user32.ShowWindow(self.hwnd, 0)
            self.visible = False

    def covered(self):
        """True if a visible window of another process sits above us and overlaps us (e.g. a fullscreen
        game that made itself TOPMOST after we appeared, which leaves the pinned strip hidden behind it)."""
        if not self.visible or not self.size:
            return False
        me = W.DWORD()
        user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(me))
        x0, y0 = self.pos
        x1, y1 = x0 + self.size[0], y0 + self.size[1]
        h, r, pid = user32.GetWindow(self.hwnd, 3), W.RECT(), W.DWORD()  # GW_HWNDPREV: the window just above
        for _ in range(500):
            if not h:
                return False
            if user32.IsWindowVisible(h) and not user32.IsIconic(h) and user32.GetWindowRect(h, ctypes.byref(r)):
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                if pid.value != me.value and r.left < x1 and r.right > x0 and r.top < y1 and r.bottom > y0:
                    return True
            h = user32.GetWindow(h, 3)
        return False

    def raise_top(self):
        """Back to the top of the TOPMOST band, never activating (only called when actually covered)."""
        user32.SetWindowPos(self.hwnd, W.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0200)

    def destroy(self):
        """Free the window and GDI objects (an App rebuilt after a crash must not leave this on screen)."""
        user32.DestroyWindow(self.hwnd)
        if self.dib:
            gdi32.SelectObject(self.mem, self.old)
            gdi32.DeleteObject(self.dib)
        gdi32.DeleteDC(self.mem)
        user32.ReleaseDC(None, self.screen)
        self.visible, self.dib = False, None


# ---------------------------------------------------------------- main loop
FPS_STEP = {"frtc": 5, "chill": 5, "rsr": 10}  # bigger steps than the driver's 1 for these


class App:
    def __init__(self):
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        self.cfg, self.cfg_stamp = core.load_config(), None
        self.panel, self.counter = Layer("Game Overlay", clickable=True), Layer("Game Overlay FPS")
        self.focus = "left"            # which panel the D-pad drives: left = AMD settings, right = stats/pins
        self.pin_sel, self.pin_hits, self.pin_order = 0, [], []
        self.hud_gpu, self.hud_rec, self.hud_rec_at = {}, None, 0.0
        self.counter_raised_at, self.counter_raises = 0.0, 0
        self.side = Layer("Game Overlay AMD", clickable=True)
        self.haze = Layer("Game Overlay Haze")  # click-through veil behind the panels
        self.pad, self.amd, self.games = PadInput(), AMD, GameData()  # one ADLX connection per process
        self.profiles = AmdProfiles(self.amd)
        self.fps_hist = deque(maxlen=120)  # 60 s at 2 samples/s
        self.open_at = None
        self.preview_until = 0
        self.hotkey = None
        self.last_fps = self.last_render = self.last_counter = self.s3d_at = 0.0
        self.fps_now = None
        self.play, self.cur_game = None, None
        self.live_misses = 0  # consecutive live.json reads without a game (a busy file must not end the game)
        self.s3d, self.sel, self.hits, self.note = {}, 0, [], ""
        self.link_state = None
        self._exe_cache, self._adl, self._aprof, self._aprof_at, self._aprof_game = {}, None, None, 0.0, None
        self.adr_dirty = False  # Adrenalin's file was edited: restart Adrenalin so it reloads instead of overwriting
        psutil.cpu_percent()
        self.reload()
        if self.profiles.data.get("baseline") and not any(read_live() or time.sleep(0.5) for _ in range(3)):
            self.profiles.game_stopped()  # we died during a game last time: put the normal settings back

    def close(self):
        """Remove this App's windows (before a new App is built after a crash)."""
        for layer in (self.panel, self.counter, self.side, self.haze):
            try:
                layer.destroy()
            except Exception as e:
                log(f"close: {e!r}")

    # settings ------------------------------------------------------------
    def reload(self):
        try:
            stamp = core.CONFIG_PATH.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp == self.cfg_stamp:
            return
        self.cfg_stamp, self.cfg = stamp, core.load_config()
        s = (user32.GetDpiForSystem() / 96 if hasattr(user32, "GetDpiForSystem") else 1) * \
            min(1.6, max(0.7, float(self.cfg.get("overlay_size", 1) or 1)))
        self.painter = Painter(s, hex_rgb(self.cfg.get("overlay_color")))
        self.set_hotkey()

    def set_hotkey(self):
        want = parse_hotkey(self.cfg.get("overlay_hotkey")) if self.cfg.get("overlay_enabled", True) else None
        if want == self.hotkey:
            return
        if self.hotkey:
            user32.UnregisterHotKey(None, 1)
        self.hotkey = None
        if want and user32.RegisterHotKey(None, 1, want[0] | 0x4000, want[1]):  # MOD_NOREPEAT
            self.hotkey = want
        elif want:
            log(f"hotkey {self.cfg.get('overlay_hotkey')!r} is taken by another app")

    def hint(self):
        keys = []
        if self.cfg.get("overlay_guide_button", True):
            keys.append("PS")
        if self.hotkey:
            keys.append(self.cfg.get("overlay_hotkey"))
        return "Pin: click a stat or RB + A" + (f"  ·  {' / '.join(keys)} closes" if keys else "")

    def pins(self):
        pins = [k for k in (self.cfg.get("overlay_pins") or []) if k in PIN_LABELS]
        if not pins and self.cfg.get("overlay_fps_counter"):  # the old "small FPS counter" setting
            pins = ["fps"]
        return pins

    def toggle_pin(self, key):
        cfg = core.load_config()
        pins = [k for k in (cfg.get("overlay_pins") or []) if k in PIN_LABELS]
        pins = [k for k in pins if k != key] if key in pins else pins + [key]
        cfg["overlay_pins"] = sorted(pins, key=list(PIN_LABELS).index)
        cfg["overlay_fps_counter"] = False
        core.save_config(cfg)
        self.cfg_stamp = None
        self.reload()
        self.note = ""
        self.last_render = 0
        log(f"pins: {', '.join(cfg['overlay_pins']) or 'none'}")

    def show_side(self):
        return self.cfg.get("overlay_show_amd_settings", True)

    # game start / stop: per-game AMD profile ----------------------------------
    def game_changed(self):
        gid = self.play["id"] if self.play else None
        if gid == self.cur_game:
            return
        if self.cur_game:
            self.profiles.game_stopped()
            log(f"game closed: {self.cur_game}")
            if self.adr_dirty:
                self.restart_adrenalin()
        self.cur_game = gid
        if gid:
            self.profiles.game_started(gid)
            log(f"game started: {gid}")
        self.s3d_at = 0

    # panel open / close ---------------------------------------------------------
    def toggle(self):
        if self.open_at:
            self.open_at, self.preview_until = None, 0
            self.panel.hide()
            self.side.hide()
            self.haze.hide()
            if self.adr_dirty and not self.play:  # mid-game it waits until the game closes
                self.restart_adrenalin()
        else:
            self.open_at = time.time()
            self.last_render = self.s3d_at = 0
            self.sel, self.note = 0, ""
            self.draw_haze()

    def draw_haze(self):
        """Dark, see-through veil over the whole screen behind the panels, a bit deeper at the sides
        where the panels sit. Drawn once per opening (a still image: no cost while open)."""
        strength = max(0.0, min(0.85, float(self.cfg.get("overlay_haze", 0.45) or 0)))
        if strength <= 0:
            return
        try:
            w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            ramp = Image.new("L", (256, 1))
            ramp.putdata([round(255 * strength * (0.75 + 0.25 * (abs(x - 127.5) / 127.5) ** 2)) for x in range(256)])
            alpha = ramp.resize((w, h), Image.BILINEAR)
            veil = Image.new("RGBA", (w, h), (8, 10, 14, 0))
            veil.putalpha(alpha)
            self.haze.show(veil, 0, 0, 1.0)
        except Exception as e:
            log(f"haze: {e!r}")

    def rows(self):
        """Only the game's own settings as set up in Adrenalin (one row per option)."""
        return self.adrenalin_rows() or [("adr_none", None, None)]

    def act(self, action, arg):
        """Panel actions from the controller or a mouse click."""
        rows = self.rows()
        if action == "select":
            self.sel = max(0, min(len(rows) - 1, arg))
            return
        if action.startswith("adr_"):
            self.adr_change(arg, action)
        elif action == "reset":
            self.profiles.reset_game()
            self.note = "Back to your normal settings for this game"
        elif action in ("toggle", "inc", "dec"):
            st = self.s3d.get(arg) or {}
            if not st.get("supported"):
                return
            if action == "toggle" and arg != "vsync":
                ok = self.amd.set_3d(arg, enabled=not st.get("enabled"),
                                     value=st.get("value") if not st.get("enabled") else None)
            else:
                mn, mx, step = st.get("range") or (0, 0, 1)
                if arg == "vsync":
                    new = ((st.get("value") or 0) + (1 if action == "inc" else -1)) % 4
                elif not st.get("enabled"):
                    return
                else:
                    step = max(step, FPS_STEP.get(arg, step))
                    new = max(mn, min(mx, (st.get("value") or mn) + (step if action == "inc" else -step)))
                    if new == st.get("value"):
                        return
                ok = self.amd.set_3d(arg, value=new)
            self.s3d = self.amd.read_3d()
            if ok:
                self.profiles.changed(arg, self.s3d.get(arg, {}))
                self.note = ""
            else:
                self.note = "Adrenalin didn't accept that (another setting may block it)"
        self.s3d_at = time.time()
        self.last_render = 0

    def handle_pad(self, pressed):
        if not self.open_at:
            return
        if pressed & BTN_B:
            self.toggle()
            return
        if pressed & 0x0200 or (pressed & 0x0100):  # RB / LB: switch between the AMD panel and the stats panel
            self.focus = "right" if self.focus == "left" or not (self.show_side() and self.side.visible) else "left"
            self.last_render = 0
            return
        if self.focus == "right" or not (self.show_side() and self.side.visible):
            if self.pin_order:
                if pressed & (UP | LEFT):
                    self.pin_sel = (self.pin_sel - 1) % len(self.pin_order)
                if pressed & (DOWN | RIGHT):
                    self.pin_sel = (self.pin_sel + 1) % len(self.pin_order)
                if pressed & BTN_A:
                    self.toggle_pin(self.pin_order[self.pin_sel % len(self.pin_order)])
                if pressed & (UP | DOWN | LEFT | RIGHT):
                    self.last_render = 0
            return
        if not (self.show_side() and self.side.visible):
            return
        rows = self.rows()
        if pressed & UP:
            self.act("select", (self.sel - 1) % len(rows))
        if pressed & DOWN:
            self.act("select", (self.sel + 1) % len(rows))
        key = rows[self.sel][0] if self.sel < len(rows) else None
        if key == "adr":
            prop = rows[self.sel][1]
            for btn, act in ((BTN_A, "adr_toggle"), (LEFT, "adr_dec"), (RIGHT, "adr_inc")):
                if pressed & btn:
                    self.act(act, prop)
            return
        if key in ("adr_none", None):
            return
        if pressed & BTN_A and key:
            self.act("reset" if key == "reset" else "toggle", key)
        if pressed & LEFT and key and key != "reset":
            self.act("dec", key)
        if pressed & RIGHT and key and key != "reset":
            self.act("inc", key)
        if pressed & (UP | DOWN):
            self.last_render = 0

    def handle_clicks(self):
        while CLICKS:
            hwnd, x, y = CLICKS.pop(0)
            if hwnd == self.panel.hwnd:  # stats panel: click an item to pin / unpin it
                for x0, y0, x1, y1, key in self.pin_hits:
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        self.focus, self.pin_sel = "right", self.pin_order.index(key) if key in self.pin_order else 0
                        self.toggle_pin(key)
                        break
                continue
            if hwnd != self.side.hwnd:
                continue
            self.focus = "left"
            hit = None
            for x0, y0, x1, y1, a in self.hits:  # later (smaller) boxes win over the row box
                if x0 <= x <= x1 and y0 <= y <= y1:
                    hit = a
            if hit:
                if hit[0] == "select":
                    self.act("select", hit[1])
                else:
                    self.act(*hit)
                self.last_render = 0

    # loop ------------------------------------------------------------------
    def run(self):
        msg = W.MSG()
        idle_check = 0.0
        while True:
            hot = False
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x0312:  # WM_HOTKEY
                    hot = True
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            now = time.time()
            if now >= idle_check:
                idle_check = now + 2
                self.reload()
                live = read_live() if self.cfg.get("overlay_enabled", True) else None
                self.live_misses = 0 if live else self.live_misses + 1
                if live or self.live_misses >= 3 or not self.cfg.get("overlay_enabled", True):
                    self.play = live  # a game counts as closed only after 3 misses in a row (~6 s)
                self.game_changed()
                if PREVIEW_FLAG.exists():
                    PREVIEW_FLAG.unlink(missing_ok=True)
                    self.preview_until, self.open_at, self.last_render, self.s3d_at = now + 12, now, 0, 0
                    self.draw_haze()
                if not self.play:
                    self.counter.hide()
                    self.fps_hist.clear()
                    self.fps_now = None
            enabled = self.cfg.get("overlay_enabled", True)
            guide_on = enabled and self.cfg.get("overlay_guide_button", True)
            link = (bool(self.open_at), guide_on)  # games get a neutral pad while open; Guide is ours
            if link != self.link_state:
                self.pad.set_link(*link)
                self.link_state = link
            if not enabled:
                if self.open_at:
                    self.toggle()
                time.sleep(0.5)
                continue
            # the panel opens any time; with no game it shows the PC and the last game played
            pressed = self.pad.pressed()
            guide = guide_on and pressed & GUIDE
            try:  # a routine error in an action must not crash (and rebuild) the overlay
                if hot or guide:
                    self.toggle()
                    log(f"{'opened' if self.open_at else 'closed'} by {'hotkey' if hot else 'Guide button'}")
                else:
                    self.handle_pad(pressed)
                self.handle_clicks()
            except Exception as e:
                log(f"input: {e!r}")
            if self.play and (self.cfg.get("overlay_show_fps", True) or {"fps", "frametime"} & set(self.pins())):
                if now - self.last_fps >= 0.5:
                    self.last_fps = now
                    self.fps_now = self.amd.fps()
                    self.fps_hist.append(self.fps_now)
            auto = float(self.cfg.get("overlay_auto_hide", 0) or 0)
            if self.open_at and auto > 0 and now - self.open_at > auto:
                self.toggle()
            if self.preview_until and now > self.preview_until:  # "Test overlay" closes itself
                self.preview_until = 0
                if self.open_at:
                    self.toggle()
            if self.open_at and now - self.last_render >= 0.5:
                self.last_render = now
                self.draw_panel()
                if self.show_side():
                    self.draw_side()
                else:
                    self.side.hide()
            if self.play and self.pins() and not self.open_at:
                if now - self.last_counter >= 1:
                    self.last_counter = now
                    self.draw_counter()
            else:
                self.counter.hide()
            time.sleep(1 / 30 if self.play or self.open_at else 1 / 15)  # a button press lasts >100 ms

    def work_area(self):
        wa = W.RECT()
        user32.SystemParametersInfoW(0x30, 0, ctypes.byref(wa), 0)
        return wa

    def draw_panel(self):
        try:
            play = self.play or last_played()
            info = {"since": play.get("since"), "boost": GAMING_FLAG.exists(), "hint": self.hint(),
                    "fps": self.fps_now, "fps_hist": self.fps_hist, "cpu": psutil.cpu_percent(),
                    "idle": not self.play, "pins": set(self.pins())}
            if self.focus == "right" and self.pin_order:
                info["focus"] = self.pin_order[self.pin_sel % len(self.pin_order)]
            info["game"] = self.games.get(play) if play.get("id") else {"name": "No game running"}
            if self.cfg.get("overlay_show_gpu", True):
                info["gpu"] = self.amd.gpu()
                if self.amd.a:
                    info["gpu_name"], info["vram_total"] = self.amd.a.name, self.amd.a.vram_total
            if self.cfg.get("overlay_show_recording", True):
                info["rec"] = recording_status()
            im, self.pin_hits, self.pin_order = render_panel(self.painter, self.cfg, info)
            wa, m = self.work_area(), self.painter.S(24)
            pos = self.cfg.get("overlay_position", "right")
            x = wa.left + m if pos == "left" else (wa.left + wa.right - im.width) // 2 if pos == "center" else wa.right - im.width - m
            y = wa.top + max(m, (wa.bottom - wa.top - im.height) // 2)
            self.panel.show(im, x, y, float(self.cfg.get("overlay_opacity", 0.92) or 0.92))
        except Exception as e:
            log(f"draw: {e!r}")

    def game_exe(self, gid):
        """Exe of the running game: a live process inside its install folder, else its last session's exe."""
        if self._exe_cache.get(gid):
            return self._exe_cache[gid]
        exe = None
        try:
            c = sqlite3.connect(f"file:{core.DB_PATH}?mode=ro", uri=True, timeout=2)
            row = c.execute("SELECT install_dir FROM games WHERE id=?", (gid,)).fetchone()
            last = c.execute("SELECT exe FROM sessions WHERE game_id=? AND exe LIKE '%.exe' ORDER BY end DESC LIMIT 1",
                             (gid,)).fetchone()
            c.close()
            root = (row[0] or "").lower().rstrip("\\") + "\\" if row and row[0] else None
            if root:
                best = 0
                for pr in psutil.process_iter(["exe", "memory_info"]):
                    pe = (pr.info["exe"] or "").lower()
                    mem = pr.info["memory_info"].rss if pr.info["memory_info"] else 0
                    if pe.startswith(root) and mem > best:
                        exe, best = pr.info["exe"], mem
            exe = exe or (last[0] if last else None)
        except Exception as e:
            log(f"game_exe: {e!r}")
        if exe:
            self._exe_cache[gid] = exe
        return exe

    def adr_target(self):
        """The game whose Adrenalin profile is shown: the running one, else the last one played."""
        return self.play or last_played()

    def adrenalin_rows(self):
        """[("adr", prop, entry)] for the target game's Adrenalin profile, [("adr_none",..)] when it has
        none, [] when unknown. Values come from the AMD driver (ADL); cached for 5 s."""
        target = self.adr_target()
        gid = target.get("id")
        if not gid:
            return []
        if time.time() - self._aprof_at < 5 and self._aprof_game == gid and self._aprof is not None:
            return self._aprof
        self._aprof_at, self._aprof_game, self._aprof = time.time(), gid, []
        try:
            import adl_profiles as ap
            if self._adl is None:
                self._adl = ap.ADLProfiles()
            exe = self.game_exe(gid)
            if exe:
                prof = self._adl.read(exe)
                if not prof:
                    self._aprof = [("adr_none", None, None)]
                else:
                    for prop, label, kind in ap.KNOWN:
                        raw = (prof.get(prop) or "").rstrip("\0")
                        self._aprof.append(("adr", prop, {"label": label, "kind": kind, "raw": raw,
                                                          "text": ap.describe(kind, ap.ADLProfiles.value(raw))}))
        except Exception as e:
            log(f"adrenalin profile: {e!r}")
        return self._aprof

    LIVE_TWIN = {"Dlg_PFEnable": "antilag", "UFG_PFEnabled": "afmf", "Ris_PFEnable": "sharpening",
                 "Bst_PFEnable": "boost", "Chil_PFEnable": "chill", "TurboSync": "esync", "VSyncControl": "vsync"}

    def adr_change(self, prop, action):
        """Change one setting in the game's Adrenalin profile (file edit, see adrenalin_blb.py), make the
        driver reload it, and also apply it live when the game is running."""
        import adl_profiles as ap
        import adrenalin_blb as ab
        entry = next((r[2] for r in self.adrenalin_rows() if r[0] == "adr" and r[1] == prop), None)
        if not entry:
            return
        raw, kind = entry["raw"], entry["kind"]
        v = ap.ADLProfiles.value(raw) or ""
        if not raw or v == "":
            self.note = "Follows Global in Adrenalin: set it On/Off there once, then change it here"
            return
        step = -1 if action == "adr_dec" else 1
        try:
            if kind == "bool":
                if action != "adr_toggle":
                    return
                new = "0" if v not in ("0", "false") else "1"
            elif kind == "vsync":
                new = str((int(v) + step) % 4)
            elif kind == "num":
                new = str(max(30, min(300, int(v) + 5 * step)))
                other = {"Chil_MinFRate": "Chil_MaxFRate", "Chil_MaxFRate": "Chil_MinFRate"}.get(prop)
                if other:  # Chill needs min < max
                    o = next((r[2] for r in self.adrenalin_rows() if r[0] == "adr" and r[1] == other), None)
                    ov_ = ap.ADLProfiles.value(o["raw"]) if o else ""
                    if ov_ and ov_.isdigit() and ((prop == "Chil_MinFRate" and int(new) >= int(ov_)) or
                                                  (prop == "Chil_MaxFRate" and int(new) <= int(ov_))):
                        self.note = "Chill min FPS has to stay below max FPS"
                        return
            elif kind == "fraction":
                new = f"{max(0.1, min(0.9, float(v) + 0.1 * step)):.1f}"
            else:
                return
        except ValueError:
            return
        if new == v:
            return
        if len(new) != len(v):
            self.note = f"Can't change {entry['label']} from {v} to {new} here; do that one in Adrenalin"
            return
        target = self.adr_target()
        exe = self.game_exe(target["id"])
        appid = None
        try:
            c = sqlite3.connect(f"file:{core.DB_PATH}?mode=ro", uri=True, timeout=2)
            r = c.execute("SELECT appid FROM games WHERE id=?", (target["id"],)).fetchone()
            c.close()
            appid = r[0] if r else None
        except sqlite3.Error:
            pass
        try:
            err = ab.Blb().set(exe, prop, ap.ADLProfiles.raw(new), appid, expect=raw)
        except Exception as e:
            err = f"couldn't read Adrenalin's file ({e})"
        if err:
            self.note = err
            log(f"adrenalin write {prop}: {err}")
            return
        self._adl.dll.ADL2_ApplicationProfiles_User_Load(self._adl.ctx)  # driver re-reads the profiles
        self.adr_dirty = True
        self._aprof_at = 0
        log(f"adrenalin profile {target['id']}: {prop} {v} -> {new}")
        twin = self.LIVE_TWIN.get(prop)
        if self.play and twin:  # running game: make it take effect now as well
            if twin == "vsync":
                self.amd.set_3d("vsync", value=int(new))
            else:
                self.amd.set_3d(twin, enabled=new == "1")
            self.s3d = self.amd.read_3d()
        self.note = "Saved in Adrenalin" + (" · fully applies next launch" if self.play else "")

    def restart_adrenalin(self):
        """Adrenalin keeps its own copy of the profiles and rewrites the whole file when it saves, so
        after an edit it is restarted (through the Adrenalin watchdog) to load the new values."""
        self.adr_dirty = False
        vbs = Path.home() / "Documents" / "Atmosphere-Scripts" / "Restart-Adrenalin.vbs"
        if vbs.exists():
            import subprocess
            subprocess.Popen(["wscript.exe", str(vbs)], creationflags=0x08000000)
            log("Adrenalin restarted to load the edited profiles")

    def draw_side(self):
        """AMD settings panel, on the side opposite the stats panel (left by default)."""
        try:
            rows = self.rows()
            self.sel = min(self.sel, len(rows) - 1)
            t = self.adr_target()
            name = self.games.get(t)["name"] if t.get("id") else "No game played yet"
            sub = ("As set in Adrenalin · changes save to Adrenalin and fully apply next launch" if self.play else
                   "Last game played · as set in Adrenalin · changes save to Adrenalin")
            im, hits = render_settings(self.painter, rows, self.sel, name, False, self.note, sub)
            wa, m = self.work_area(), self.painter.S(24)
            right = self.cfg.get("overlay_position", "right") == "left"
            x = wa.right - im.width - m if right else wa.left + m
            y = wa.top + max(m, (wa.bottom - wa.top - im.height) // 2)
            self.hits = hits
            self.side.show(im, x, y, float(self.cfg.get("overlay_opacity", 0.92) or 0.92))
        except Exception as e:
            log(f"side: {e!r}")

    def draw_counter(self):
        """The pinned stats strip (GPU read once a second, only if a GPU stat is pinned)."""
        try:
            pins = self.pins()
            data = {"fps": self.fps_now, "fps_hist": self.fps_hist, "cpu": psutil.cpu_percent()}
            if {"gpu_usage", "gpu_temp", "gpu_hotspot", "gpu_clock", "gpu_power", "gpu_fan", "vram"} & set(pins):
                data["gpu"] = self.amd.gpu()
            if "rec" in pins:
                if time.time() - self.hud_rec_at > 2:
                    self.hud_rec, self.hud_rec_at = recording_status(), time.time()
                data["rec"] = self.hud_rec
            if "ach" in pins and self.play:
                data["game"] = self.games.get(self.play)
            im = render_pins(self.painter, pins, data)
            wa, m = self.work_area(), self.painter.S(12)
            vert, _, horiz = str(self.cfg.get("overlay_fps_counter_position", "top-left")).partition("-")
            x = wa.left + m if horiz == "left" else wa.right - im.width - m
            y = wa.top + m if vert == "top" else wa.bottom - im.height - m
            self.counter.show(im, x, y, 1.0)
            # a game that re-asserts TOPMOST buries the strip; lift it back only when that happens (re-asserting
            # every update can minimize fullscreen games) and at most every 3 s so we never fight a game over it
            now = time.time()
            if now - self.counter_raised_at >= 3 and self.counter.covered():
                self.counter_raised_at = now
                self.counter.raise_top()
                self.counter_raises += 1
                if self.counter_raises <= 5 or self.counter_raises % 50 == 0:
                    log(f"pinned strip was covered, raised it back on top (#{self.counter_raises})")
        except Exception as e:
            log(f"counter: {e!r}")


if __name__ == "__main__":
    log("overlay started")
    while True:
        app = None
        try:
            app = App()
            app.run()
        except Exception as e:  # never die silently in the background; rebuild and carry on
            log(f"crashed, restarting: {e!r}")
            if app:
                app.close()  # don't leave the old panel / haze frozen over the game
            user32.UnregisterHotKey(None, 1)
            try:  # never leave the game's controller held neutral
                import mmap
                mm = mmap.mmap(-1, 16, tagname="Local\\ABridgePadLink")
                mm[2] = 0
            except Exception:
                pass
            time.sleep(3)
