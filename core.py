"""Local Game Genesys (GameTracker) core: game discovery on the scan drives, playtime database, achievement
schemas (downloaded when missing) and achievement progress from Steam and the common
Steam emulators (GSE/Goldberg, CODEX, RUNE, EMPRESS, OnlineFix, Tenoke, local JSON).

Used by tracker.pyw (background service) and app.pyw (window).
"""
import concurrent.futures as cf
import difflib
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import struct
import threading
import time
import winreg
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = DATA / "cache"
DB_PATH = DATA / "tracker.db"
CONFIG_PATH = HERE / "config.json"
LIVE_PATH = DATA / "live.json"
LOG_PATH = DATA / "gametracker.log"
for _d in (DATA, CACHE / "covers", CACHE / "icons"):
    _d.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("gametracker")


def default_drives():
    """Every fixed drive except the Windows drive (its top-level folders aren't games)."""
    import ctypes
    sysdrive = os.environ.get("SystemDrive", "C:")[0].upper()
    return [d for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if d != sysdrive and ctypes.windll.kernel32.GetDriveTypeW(f"{d}:\\") == 3]

DEFAULT_CONFIG = {
    "scan_drives": default_drives(),
    "exclude_folders": [],
    "extra_game_folders": [],
    # single game installs added by hand (anywhere, any drive)
    "manual_games": [],
    # processes under a game folder whose exe name contains one of these never count as playing
    "exclude_patterns": [
        "crash", "report", "setup", "install", "redist", "vc_redist", "dxsetup", "unins",
        "easyanticheat", "beservice", "battleye", "updater", "cefprocess", "qtwebengine",
        "unitycrashhandler", "dxwebsetup", "dotnet", "touchup", "cleanup",
    ],
    "poll_seconds": 3,
    "min_session_seconds": 60,
    "notifications": True,
    # silent achievement-style pop-up when a game starts; {game} is replaced by the game name
    "welcome_popup": True,
    "welcome_message": "WELCOME TO THE GAME, {user}",
    "welcome_header": "NOW PLAYING",
    "welcome_show_game": True,
    "welcome_image": "cover",          # cover | logo
    "welcome_color": "#107C10",
    "welcome_position": "top-center",  # top|bottom - left|center|right
    "welcome_size": 1.2,               # 1.0 = achievement pop-up size
    "welcome_seconds": 11,             # fully open, plus ~2 s sliding in and out
    "welcome_sound": "none",           # none | achievement | achievement-360 | path to a .wav
    "welcome_cooldown_minutes": 5,     # don't greet the same game again within this
    # in-game overlay panel (game_overlay.pyw), opened with the controller Guide/PS button or a hotkey
    "overlay_enabled": True,
    "overlay_guide_button": True,
    "overlay_hotkey": "Alt+G",         # Win+G stays reserved by Windows even without Game Bar; empty = none
    "overlay_position": "right",       # left | center | right
    "overlay_size": 1.0,
    "overlay_opacity": 0.92,
    "overlay_haze": 0.45,              # dark see-through veil over the game while open; 0 = off
    "overlay_color": "#107C10",
    "overlay_auto_hide": 0,            # seconds; 0 = stays until opened again
    "overlay_show_fps": True,          # FPS + frame-time graph (AMD driver FPS)
    "overlay_show_gpu": True,          # AMD GPU usage, clocks, temps, power, VRAM
    "overlay_show_system": True,       # CPU and RAM
    "overlay_show_recording": True,    # GameRecorder / OBS status
    "overlay_show_achievements": True,
    "overlay_show_amd_settings": True,  # left panel: AMD settings, remembered per game
    "overlay_fps_counter": False,      # old setting: small FPS counter (now = overlay_pins ["fps"])
    "overlay_fps_counter_position": "top-left",  # where the pinned stats strip sits
    "overlay_pins": [],                # stats pinned on screen while playing (fps, frametime, gpu_usage, vram, ...)
    "steam_api_key": "",
    "store_country": "us",
}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GameTracker/1.0"}
HTTP = requests.Session()
HTTP.headers.update(UA)


def setup_logging(name):
    from logging.handlers import RotatingFileHandler
    h = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter(f"%(asctime)s {name} %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
    except FileNotFoundError:
        save_config(cfg)
    except Exception as e:
        log.error("config.json unreadable, using defaults: %s", e)
    return cfg


WELCOME_SOUNDS = {"none": None, "achievement": "achievement.wav", "achievement-360": "achievement-360.wav"}


def cover_file(row):
    """Local cover image for a games row (custom cover first), or None."""
    for v in (row["custom_cover"], row["cover"]):
        if v:
            p = Path(v) if Path(v).is_absolute() else CACHE / v
            if p.exists():
                return str(p)
    return None


def welcome_popup_args(cfg, game, cover=None):
    """overlay.show() keyword arguments for the game-launch pop-up, built from the welcome_* settings."""
    d = DEFAULT_CONFIG

    def num(k, lo, hi):
        try:
            return min(hi, max(lo, float(cfg.get(k, d[k]))))
        except (TypeError, ValueError):
            return d[k]

    msg = str(cfg.get("welcome_message") or "").strip() or d["welcome_message"]
    user = os.environ.get("USERNAME", "")
    fill = lambda t: t.replace("{game}", game).replace("{user}", user)  # noqa: E731
    snd = str(cfg.get("welcome_sound") or "none")
    sound = (HERE / "sounds" / WELCOME_SOUNDS[snd] if WELCOME_SOUNDS[snd] else None) if snd in WELCOME_SOUNDS else Path(snd)
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(cfg.get("welcome_color") or ""))
    return {
        "name": fill(msg),
        "game": game if cfg.get("welcome_show_game", True) else "",
        "icon": cover if cfg.get("welcome_image", "cover") == "cover" else None,
        "pct": None,
        "sound": sound,
        "head": fill(str(cfg.get("welcome_header", d["welcome_header"]) or "").strip()),
        "hold": num("welcome_seconds", 2, 60),
        "scale": num("welcome_size", 0.6, 2.5),
        "accent": tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else None,
        "position": str(cfg.get("welcome_position") or "top-center"),
    }


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


# ------------------------------------------------------------------ database

SCHEMA = """
CREATE TABLE IF NOT EXISTS games(
  id TEXT PRIMARY KEY, name TEXT, custom_name TEXT, install_dir TEXT, source TEXT,
  appid INTEGER, appid_source TEXT, appid_locked INTEGER DEFAULT 0,
  installed INTEGER DEFAULT 1, hidden INTEGER DEFAULT 0, added_at REAL,
  baseline_seconds INTEGER DEFAULT 0, baseline_source TEXT,
  hints TEXT, cover TEXT, hero TEXT, custom_cover TEXT,
  schema_source TEXT, schema_at REAL, schema_error TEXT, schema_try_at REAL,
  ach_synced INTEGER DEFAULT 0, ext_last_played REAL, extra_sources TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT, start REAL, end REAL, exe TEXT);
CREATE INDEX IF NOT EXISTS ix_sessions_game ON sessions(game_id, start);
CREATE TABLE IF NOT EXISTS achievements(
  game_id TEXT, api TEXT, name TEXT, description TEXT, hidden INTEGER DEFAULT 0,
  icon_url TEXT, icon_gray_url TEXT, icon TEXT, global_pct REAL, sort INTEGER,
  PRIMARY KEY(game_id, api));
CREATE TABLE IF NOT EXISTS unlocks(
  game_id TEXT, api TEXT, unlocked_at REAL, source TEXT, seen_at REAL,
  PRIMARY KEY(game_id, api));
"""

_local = threading.local()


def db():
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=15000")
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(games)")}
        if "ext_last_played" not in cols:
            c.execute("ALTER TABLE games ADD COLUMN ext_last_played REAL")
        if "extra_sources" not in cols:
            c.execute("ALTER TABLE games ADD COLUMN extra_sources TEXT")
        _local.conn = c
    return c


def game_id_for(path):
    return hashlib.sha1(str(path).lower().rstrip("\\").encode()).hexdigest()[:12]


# ------------------------------------------------------------------ Steam files

def steam_path():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return Path(winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\"))
    except OSError:
        p = Path(r"C:\Program Files (x86)\Steam")
        return p if p.exists() else None


def parse_text_vdf(text):
    root, stack, key = {}, [], None
    cur = root
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        if m.group(2) == "{":
            d = {}
            cur[key if key is not None else ""] = d
            stack.append(cur)
            cur, key = d, None
        elif m.group(2) == "}":
            cur = stack.pop() if stack else root
            key = None
        else:
            v = m.group(1).replace("\\\\", "\\")
            if key is None:
                key = v
            else:
                cur[key] = v
                key = None
    return root


def iget(d, *keys):
    """Case-insensitive nested dict get."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        low = {kk.lower(): vv for kk, vv in d.items()}
        d = low.get(k.lower())
    return d


def parse_binary_vdf(b, i=0):
    out = {}
    while i < len(b):
        t = b[i]
        i += 1
        if t in (0x08, 0x0B):
            return out, i
        j = b.index(0, i)
        k = b[i:j].decode("utf-8", "replace")
        i = j + 1
        if t == 0:
            v, i = parse_binary_vdf(b, i)
        elif t == 1:
            j = b.index(0, i)
            v = b[i:j].decode("utf-8", "replace")
            i = j + 1
        elif t in (2, 4, 6):
            v = struct.unpack_from("<i", b, i)[0]
            i += 4
        elif t == 3:
            v = struct.unpack_from("<f", b, i)[0]
            i += 4
        elif t == 7:
            v = struct.unpack_from("<Q", b, i)[0]
            i += 8
        elif t == 10:
            v = struct.unpack_from("<q", b, i)[0]
            i += 8
        else:
            raise ValueError(f"binary vdf type {t} at {i}")
        out[k] = v
    return out, i


def steam_libraries():
    sp = steam_path()
    if not sp:
        return []
    vdf = sp / "steamapps" / "libraryfolders.vdf"
    libs = [sp]
    try:
        for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="ignore")):
            p = Path(m.group(1).replace("\\\\", "\\"))
            if p not in libs:
                libs.append(p)
    except OSError:
        pass
    return libs


def steam_accounts():
    sp = steam_path()
    d = sp / "userdata" if sp else None
    return [p.name for p in d.iterdir() if p.name.isdigit() and p.name != "0"] if d and d.exists() else []


def steam_playtimes():
    """{appid: (minutes, last_played)} from every local Steam account."""
    out = {}
    sp = steam_path()
    for acct in steam_accounts():
        f = sp / "userdata" / acct / "config" / "localconfig.vdf"
        try:
            v = parse_text_vdf(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        apps = iget(v, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps") or {}
        for appid, a in apps.items():
            if appid.isdigit() and isinstance(a, dict):
                mins = int(iget(a, "Playtime") or 0)
                if mins:
                    prev = out.get(int(appid), (0, 0))
                    out[int(appid)] = (prev[0] + mins, max(prev[1], int(iget(a, "LastPlayed") or 0)))
    return out


# ------------------------------------------------------------------ discovery

GAME_FILES = {"unityplayer.dll", "steam_api64.dll", "steam_api.dll", "steam_appid.txt", "steam_emu.ini",
              "eossdk-win64-shipping.dll", "galaxy64.dll", "galaxy.dll", "bink2w64.dll", "start_protected_game.exe",
              "cemu.exe", "rpcs3.exe", "ryujinx.exe", "yuzu.exe", "xenia.exe", "xenia_canary.exe", "pcsx2-qt.exe",
              "dolphin.exe", "duckstation-qt-x64-releaseltcg.exe", "ppssppwindows64.exe", "tenoke.ini"}
EMULATOR_DIRS = ("cemu", "rpcs3", "ryujinx", "yuzu", "xenia", "pcsx2", "dolphin", "duckstation", "ppsspp")
REDIST_DIRS = {"redist", "_redist", "_commonredist", "redistributables", "directx", "vcredist", "support"}
GAME_DIRS = {"easyanticheat", "battleye", "_commonredist"}
SKIP_TOP = {"$recycle.bin", "system volume information", "program files", "program files (x86)", "windows",
            "windowsapps", "wpsystem", "wudownloadcache", "recovery", "_commonredist", "steamlibrary",
            "xboxgames", "games", "epic games", "gog games", "programdata", "users", "msdownld.tmp"}
LIBRARY_DIRS = ("Games", "XboxGames", "Epic Games", "GOG Games", "Program Files\\EA Games")
NOT_GAMES = {"steamworks shared", "steam controller configs", "steamvr"}


def _subdirs(p):
    try:
        return [Path(e.path) for e in os.scandir(p)
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(("$", "."))]
    except OSError:
        return []


def looks_like_game(folder, max_depth=4, max_dirs=400):
    if (folder / "Engine" / "Binaries").is_dir():
        return True
    stack, seen = [(folder, 0)], 0
    while stack and seen < max_dirs:
        d, depth = stack.pop()
        seen += 1
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            n = e.name.lower()
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if n in GAME_DIRS:
                    return True
                if depth < max_depth and n not in ("venv", ".git", "node_modules", "__pycache__"):
                    stack.append((Path(e.path), depth + 1))
            elif n in GAME_FILES or n.endswith("-shipping.exe") or (n.startswith("goggame-") and n.endswith(".info")):
                return True
    return False


def _install_name(d):
    """Repack wrappers (E:\\SomeRepack\\The Game) are named after their single inner game folder."""
    for _ in range(2):
        kids = [k for k in _subdirs(d) if k.name.lower() not in REDIST_DIRS]
        if len(kids) != 1 or kids[0].name.lower().startswith(EMULATOR_DIRS) or not looks_like_game(kids[0]):
            break
        d = kids[0]
    return d.name


def discover_games(cfg):
    """[{install_dir, name, source, appid}] for everything on the scan drives."""
    drives = [d.upper().rstrip(":\\") for d in cfg["scan_drives"]]
    on_drive = lambda p: str(p)[:1].upper() in drives
    excl = {str(Path(p)).lower().rstrip("\\") for p in cfg["exclude_folders"]}
    found, covered = {}, []

    def add(path, name, source, appid=None):
        key = str(path).lower().rstrip("\\")
        if key in excl or key in found:
            return
        found[key] = {"install_dir": str(path), "name": name, "source": source, "appid": appid}

    for p in cfg.get("manual_games", []):
        if Path(p).is_dir():
            add(Path(p), _install_name(Path(p)), "manual")
            covered.append(str(Path(p)).lower() + "\\")

    # Steam libraries: every appmanifest is one game
    for lib in steam_libraries():
        if not on_drive(lib):
            continue
        sa = lib / "steamapps"
        covered.append(str(sa / "common").lower() + "\\")
        for acf in sa.glob("appmanifest_*.acf"):
            try:
                st = iget(parse_text_vdf(acf.read_text(encoding="utf-8", errors="ignore")), "AppState") or {}
            except OSError:
                continue
            inst = iget(st, "installdir")
            if inst and (sa / "common" / inst).is_dir() and inst.lower() not in NOT_GAMES:
                add(sa / "common" / inst, iget(st, "name") or inst, "steam", int(iget(st, "appid") or 0) or None)

    # Epic manifests
    man = Path(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests")
    for item in man.glob("*.item") if man.exists() else []:
        try:
            j = json.loads(item.read_text(encoding="utf-8", errors="ignore"))
            loc = j.get("InstallLocation")
            if loc and on_drive(loc) and Path(loc).is_dir():
                add(Path(loc), j.get("DisplayName") or Path(loc).name, "epic")
                covered.append(str(Path(loc)).lower() + "\\")
        except Exception:
            pass

    # library-style folders: each subfolder is a game
    lib_roots = [Path(f"{d}:\\{s}") for d in drives for s in LIBRARY_DIRS] + [Path(p) for p in cfg["extra_game_folders"]]
    for root in lib_roots:
        if root.is_dir():
            covered.append(str(root).lower() + "\\")
            for g in _subdirs(root):
                if g.name.lower() not in NOT_GAMES and looks_like_game(g):
                    add(g, _install_name(g), "folder")

    def usable(p):
        s = str(p).lower()
        return s not in excl and not any((s + "\\").startswith(c) for c in covered)

    # loose installs: X:\Game or X:\Wrapper\Game
    for drive in drives:
        top = Path(f"{drive}:\\")
        for d in _subdirs(top) if top.exists() else []:
            if d.name.lower() in SKIP_TOP or not usable(d):
                continue
            if looks_like_game(d):
                add(d, _install_name(d), "drive")
                continue
            for sub in _subdirs(d):
                if usable(sub) and looks_like_game(sub):
                    add(sub, _install_name(sub), "drive")
    return list(found.values())


# ------------------------------------------------------------------ install inspection

APPID_FILES = {"steam_appid.txt", "steam_emu.ini", "tenoke.ini", "cream_api.ini", "onlinefix.ini", "codex.ini",
               "steam_api.ini", "steam_api64.ini", "ali213.ini", "cpy.ini", "hlm.ini", "ds.ini", "rune.ini",
               "smartsteamemu.ini", "valve.ini", "configs.app.ini", "steamconfig.ini", "emp.ini", "empress.ini"}
SPACEWAR = {480, 0}


def inspect_install(path, max_dirs=900, max_depth=6):
    """Breadth-first look through an install for appid hints and local achievement files."""
    hints = {"appids": [], "tenoke": [], "steam_settings": [], "json": []}
    queue, seen = [(Path(path), 0)], 0
    while queue and seen < max_dirs:
        d, depth = queue.pop(0)
        seen += 1
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            n = e.name.lower()
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if n == "steam_settings":
                    hints["steam_settings"].append(e.path)
                    f = Path(e.path) / "steam_appid.txt"
                    _appid_from_file(f, hints, depth)
                elif n == "steamdata" and (Path(e.path) / "user_stats.ini").exists():
                    hints["tenoke"].append(str(Path(e.path) / "user_stats.ini"))
                if depth < max_depth and n not in REDIST_DIRS and n not in ("__pycache__", ".git", "shadercache"):
                    queue.append((Path(e.path), depth + 1))
            elif n in APPID_FILES:
                _appid_from_file(Path(e.path), hints, depth)
            elif n == "achievements.json" and "steam_settings" not in e.path.lower():
                hints["json"].append(e.path)
    return hints


def _appid_from_file(f, hints, depth):
    try:
        t = f.read_text(encoding="utf-8", errors="ignore")[:20000]
    except OSError:
        return
    if f.name.lower().endswith(".txt"):
        m = re.match(r"\s*(\d{2,9})", t)
        ids = [m.group(1)] if m else []
    else:
        t = re.sub(r"(?m)^\s*[#;].*$", "", t)
        ids = re.findall(r"(?im)^\s*(?:appid|app_id|id|realappid|gameid)\s*=\s*\"?(\d{2,9})", t)
    for i in ids:
        i = int(i)
        if i not in SPACEWAR and not any(a["appid"] == i for a in hints["appids"]):
            hints["appids"].append({"appid": i, "file": str(f), "depth": depth})


# ------------------------------------------------------------------ Steam store / web

def norm_name(s):
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[®™©]", "", s)
    s = re.sub(r"\b(ankergames|gamedrive|steamrip|fitgirl|dodi|repack|elamigos|goldberg|gog|codex|rune|tenoke|empress)\b", " ", s)
    s = re.sub(r"\b(org|com|net)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def store_search(term, cc="us"):
    try:
        r = HTTP.get("https://store.steampowered.com/api/storesearch/",
                     params={"term": term, "cc": cc, "l": "english"}, timeout=15)
        r.raise_for_status()
        return [{"appid": it["id"], "name": it["name"], "image": it.get("tiny_image")}
                for it in r.json().get("items", []) if it.get("type") == "app"]
    except Exception as e:
        log.warning("store search %r failed: %s", term, e)
        return []


def best_store_match(name, cc="us"):
    want = norm_name(name)
    if not want:
        return None
    results = store_search(want, cc) or store_search(name, cc)
    best, score = None, 0.0
    for r in results:
        got = norm_name(r["name"])
        s = difflib.SequenceMatcher(None, want, got).ratio()
        if got == want:
            s = 2.0
        elif want in got or got in want:
            s = max(s, 0.85 - 0.01 * abs(len(got) - len(want)))
        if any(x in got for x in ("soundtrack", " dlc", "season pass", " pack", "artbook")):
            s -= 0.3
        if s > score:
            best, score = r, s
    return best if score >= 0.72 else None


def store_details(appid):
    try:
        r = HTTP.get("https://store.steampowered.com/api/appdetails",
                     params={"appids": appid, "l": "english"}, timeout=15)
        d = r.json().get(str(appid), {})
        return d.get("data") if d.get("success") else None
    except Exception:
        return None


def download(url, dest, min_bytes=500):
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size >= min_bytes:
        return True
    try:
        r = HTTP.get(url, timeout=20)
        if r.status_code != 200 or len(r.content) < min_bytes or "image" not in r.headers.get("content-type", "image"):
            return False
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(r.content)
        os.replace(tmp, dest)
        return True
    except Exception:
        return False


def fetch_art(appid):
    """(cover, hero) local paths relative to CACHE, downloaded from the Steam CDN."""
    cover = hero = None
    d = CACHE / "covers"
    try:  # current store assets (newer apps keep their art at hashed paths)
        r = HTTP.get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/", timeout=15, params={"input_json": json.dumps(
            {"ids": [{"appid": appid}], "context": {"language": "english", "country_code": "US"},
             "data_request": {"include_assets": True}})})
        assets = r.json()["response"]["store_items"][0].get("assets") or {}
        fmt = "https://shared.akamai.steamstatic.com/store_item_assets/" + assets.get("asset_url_format", "")
        for key in ("library_capsule_2x", "library_capsule"):
            if assets.get(key) and download(fmt.replace("${FILENAME}", assets[key]), d / f"{appid}_cover.jpg", 5000):
                cover = f"covers/{appid}_cover.jpg"
                break
        for key in ("library_hero_2x", "library_hero"):
            if assets.get(key) and download(fmt.replace("${FILENAME}", assets[key]), d / f"{appid}_hero.jpg", 5000):
                hero = f"covers/{appid}_hero.jpg"
                break
        if cover and hero:
            return cover, hero
    except Exception as e:
        log.warning("store assets %s: %s", appid, e)
    bases = ["https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{a}/{f}",
             "https://cdn.akamai.steamstatic.com/steam/apps/{a}/{f}"]
    for f in ("library_600x900_2x.jpg", "library_600x900.jpg"):
        if any(download(b.format(a=appid, f=f), d / f"{appid}_cover.jpg", 5000) for b in bases):
            cover = f"covers/{appid}_cover.jpg"
            break
    if any(download(b.format(a=appid, f="library_hero.jpg"), d / f"{appid}_hero.jpg", 5000) for b in bases):
        hero = f"covers/{appid}_hero.jpg"
    if not cover or not hero:
        det = store_details(appid) or {}
        if not hero and det.get("background_raw") and download(det["background_raw"], d / f"{appid}_hero.jpg", 5000):
            hero = f"covers/{appid}_hero.jpg"
        if not cover and det.get("header_image") and download(det["header_image"], d / f"{appid}_header.jpg", 2000):
            cover = f"covers/{appid}_header.jpg"
    return cover, hero


# ------------------------------------------------------------------ achievement schemas

ICON_BASE = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon}"


def _english(v):
    if isinstance(v, dict):
        return v.get("english") or next((x for x in v.values() if isinstance(x, str)), "")
    return v or ""


def schema_from_steam_settings(folder):
    f = Path(folder) / "achievements.json"
    if not f.exists():
        return None
    try:
        items = json.loads(f.read_text(encoding="utf-8-sig", errors="ignore"))
    except Exception:
        return None
    out = []
    for i, a in enumerate(items if isinstance(items, list) else []):
        icon = a.get("icon") or ""
        gray = a.get("icon_gray") or a.get("icongray") or ""
        local = lambda p: str(Path(folder) / p) if p and not p.startswith("http") and (Path(folder) / p).exists() else None
        desc = _english(a.get("description"))
        if desc.strip().lower() in ("no description available", "no description", "hidden achievement"):
            desc = ""
        out.append({"api": a.get("name"), "name": _english(a.get("displayName") or a.get("display_name")) or a.get("name"),
                    "description": desc, "hidden": int(str(a.get("hidden", 0)) in ("1", "True", "true")),
                    "icon_url": icon if icon.startswith("http") else None, "icon_gray_url": gray if gray.startswith("http") else None,
                    "local_icon": local(icon), "sort": i})
    return [a for a in out if a["api"]] or None


def schema_from_steam_cache(appid):
    """Achievements from Steam's own appcache\\stats schema (also gives the stat/bit layout)."""
    sp = steam_path()
    f = sp / "appcache" / "stats" / f"UserGameStatsSchema_{appid}.bin" if sp else None
    if not f or not f.exists():
        return None, {}
    try:
        root = parse_binary_vdf(f.read_bytes())[0]
    except Exception as e:
        log.warning("schema cache %s unreadable: %s", appid, e)
        return None, {}
    root = next(iter(root.values()), {})
    out, bits = [], {}
    for sid, st in (root.get("stats") or {}).items():
        if not isinstance(st, dict) or "bits" not in st:
            continue
        for bit, a in st["bits"].items():
            disp = a.get("display") or {}
            api = a.get("name")
            if not api:
                continue
            icon, gray = disp.get("icon"), disp.get("icon_gray")
            out.append({"api": api, "name": _english(disp.get("name")) or api, "description": _english(disp.get("desc")),
                        "hidden": int(str(disp.get("hidden", 0)) in ("1", "true")),
                        "icon_url": ICON_BASE.format(appid=appid, icon=icon) if icon else None,
                        "icon_gray_url": ICON_BASE.format(appid=appid, icon=gray) if gray else None,
                        "sort": int(sid) * 100 + int(bit)})
            bits[(sid, int(bit))] = api
    return out or None, bits


def schema_from_web_api(appid, key):
    r = HTTP.get("https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/",
                 params={"appid": appid, "key": key, "l": "english"}, timeout=20)
    r.raise_for_status()
    ach = (((r.json().get("game") or {}).get("availableGameStats") or {}).get("achievements")) or []
    return [{"api": a["name"], "name": a.get("displayName") or a["name"], "description": a.get("description", ""),
             "hidden": int(a.get("hidden", 0)), "icon_url": a.get("icon"), "icon_gray_url": a.get("icongray"), "sort": i}
            for i, a in enumerate(ach)] or None


def schema_from_public_pages(appid):
    """No-key route: SteamHunters (api name, name, description) + Steam Community page (icons)."""
    r = HTTP.get(f"https://steamhunters.com/api/apps/{appid}/achievements", timeout=25)
    if r.status_code != 200:
        return None
    items = r.json()
    if not isinstance(items, list) or not items:
        return None
    icons = {}
    try:
        page = HTTP.get(f"https://steamcommunity.com/stats/{appid}/achievements/?l=english", timeout=25).text
        for m in re.finditer(r'<img src="([^"]+)"[^>]*>\s*</div>\s*<div class="achieveTxtHolder">.*?<h3>(.*?)</h3>\s*<h5>(.*?)</h5>',
                             page, re.S):
            key = (html.unescape(m.group(2)).strip().lower(), html.unescape(m.group(3)).strip().lower())
            icons.setdefault(key, m.group(1))
            icons.setdefault((key[0], None), m.group(1))
    except Exception as e:
        log.warning("community page %s: %s", appid, e)
    out = []
    for i, a in enumerate(sorted(items, key=lambda x: x.get("achievementId", 0))):
        name, desc = a.get("name") or a["apiName"], a.get("description") or ""
        icon = icons.get((name.strip().lower(), desc.strip().lower())) or icons.get((name.strip().lower(), None))
        out.append({"api": a["apiName"], "name": name, "description": desc, "hidden": 0,
                    "icon_url": icon, "icon_gray_url": None, "global_pct": a.get("steamPercentage"), "sort": i})
    return out


def global_percentages(appid):
    try:
        r = HTTP.get("https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/",
                     params={"gameid": appid}, timeout=15)
        return {a["name"].lower(): float(a["percent"]) for a in r.json()["achievementpercentages"]["achievements"]}
    except Exception:
        return {}


def schema_from_custom_json(f):
    """Game-made achievement files like {"achievements":[{"id","name","description","unlocked"}]}."""
    try:
        j = json.loads(Path(f).read_text(encoding="utf-8-sig", errors="ignore"))
    except Exception:
        return None
    items = j.get("achievements") if isinstance(j, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict) or "name" not in items[0]:
        return None
    return [{"api": str(a.get("id", a.get("name"))), "name": a.get("name"), "description": a.get("description", ""),
             "hidden": int(bool(a.get("hidden"))), "icon_url": None, "icon_gray_url": None, "sort": i}
            for i, a in enumerate(items)]


# ------------------------------------------------------------------ achievement progress

APPDATA = Path(os.environ.get("APPDATA", ""))
PUBLIC = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", ""))


def _ts(v):
    try:
        v = float(v)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _read_text(f):
    try:
        return Path(f).read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        try:
            return Path(f).read_text(encoding="utf-16", errors="ignore")
        except OSError:
            return ""


def progress_goldberg_json(f):
    """GSE / Goldberg / EMPRESS: {"API": {"earned": true, "earned_time": 123}}."""
    try:
        j = json.loads(_read_text(f) or "{}")
    except Exception:
        return {}
    out = {}
    if isinstance(j, dict):
        for k, v in j.items():
            if isinstance(v, dict) and (v.get("earned") in (True, 1, "true") or v.get("achieved") in (True, 1)):
                out[k] = _ts(v.get("earned_time") or v.get("time"))
    elif isinstance(j, list):
        for v in j:
            if isinstance(v, dict) and v.get("earned"):
                out[v.get("name")] = _ts(v.get("earned_time"))
    return out


def _ini_sections(text):
    secs, cur = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = secs.setdefault(line[1:-1], {})
        elif cur is not None and "=" in line and not line.startswith((";", "#")):
            k, v = line.split("=", 1)
            cur[k.strip().lower()] = v.strip().strip('"')
    return secs


def progress_codex_ini(f):
    """CODEX / RUNE / OnlineFix / generic: [API] Achieved=1 UnlockTime=123."""
    out = {}
    for sec, kv in _ini_sections(_read_text(f)).items():
        if sec.lower() in ("steamachievements", "achievements", "stats"):
            continue
        ach = kv.get("achieved", kv.get("unlocked", kv.get("state", "")))
        if str(ach).lower() in ("1", "true", "yes"):
            out[sec] = _ts(kv.get("unlocktime") or kv.get("timestamp") or kv.get("time"))
    return out


def progress_tenoke(f):
    out = {}
    text = _read_text(f)
    block = text.split("[ACHIEVEMENTS]", 1)[-1].split("\n[", 1)[0] if "[ACHIEVEMENTS]" in text else text
    for m in re.finditer(r'"?([^"\s=]+)"?\s*=\s*\{([^}]*)\}', block):
        body = m.group(2)
        if re.search(r"unlocked\s*=\s*true", body, re.I):
            t = re.search(r"time\s*=\s*(\d+)", body)
            out[m.group(1)] = _ts(t.group(1)) if t else None
    return out


def progress_custom_json(f):
    try:
        j = json.loads(_read_text(f))
    except Exception:
        return {}
    out = {}
    for a in (j.get("achievements") if isinstance(j, dict) else []) or []:
        if isinstance(a, dict) and (a.get("unlocked") or a.get("earned") or a.get("achieved")):
            out[str(a.get("id", a.get("name")))] = _ts(a.get("unlocked_time") or a.get("time") or a.get("unlockTime"))
    return out


def progress_steam(appid, bits):
    """Real Steam unlocks from appcache\\stats\\UserGameStats_<account>_<appid>.bin."""
    sp = steam_path()
    out = {}
    if not sp or not bits:
        return out
    for f in (sp / "appcache" / "stats").glob(f"UserGameStats_*_{appid}.bin"):
        try:
            root = parse_binary_vdf(f.read_bytes())[0]
        except Exception:
            continue
        root = next(iter(root.values()), {})
        for sid, st in root.items():
            if not isinstance(st, dict) or "data" not in st:
                continue
            data = st["data"] & 0xFFFFFFFF
            times = st.get("AchievementTimes") or {}
            for bit in range(32):
                if data >> bit & 1 and (sid, bit) in bits:
                    out[bits[(sid, bit)]] = _ts(times.get(str(bit)))
    return out


def progress_steam_profile(appid):
    """Unlocks from the public Steam profile (steamcommunity.com XML) of every local Steam account.
    Covers Steam games whose appcache stats Steam hasn't written yet."""
    out = {}
    for acct in steam_accounts():
        sid = 76561197960265728 + int(acct)
        try:
            r = HTTP.get(f"https://steamcommunity.com/profiles/{sid}/stats/{appid}/achievements/?xml=1&l=english", timeout=20)
        except Exception:
            continue
        if r.status_code != 200 or "<achievements>" not in r.text:
            continue
        for m in re.finditer(r'<achievement closed="1">(.*?)</achievement>', r.text, re.S):
            api = re.search(r"<apiname><!\[CDATA\[(.*?)\]\]>", m.group(1))
            ts = re.search(r"<unlockTimestamp>(\d+)", m.group(1))
            if api:
                out[api.group(1)] = _ts(ts.group(1)) if ts else None
    return out


def game_hints(row):
    """Install-scan hints plus the unlock files the user pointed to by hand."""
    hints = json.loads(row["hints"] or "{}")
    try:
        hints["extra"] = json.loads(row["extra_sources"] or "[]")
    except (IndexError, KeyError, ValueError):
        hints["extra"] = []
    return hints


def emulator_save_dirs(appid, hints):
    r"""[(label, folder)] where a Goldberg/GSE-style emulator keeps <appid>\achievements.json:
    the standard AppData folders, plus whatever local_save_path / saves_folder_name / local_save.txt
    a repack configured next to its steam_api dll (e.g. ...\Win64\_Saved Games\<appid>)."""
    ids = {str(appid)} if appid else set()
    ids |= {str(a["appid"]) for a in hints.get("appids", [])}
    roots = [("GSE", APPDATA / "GSE Saves"), ("Goldberg", APPDATA / "Goldberg SteamEmu Saves"),
             ("Goldberg", LOCALAPPDATA / "Goldberg SteamEmu Saves")]
    for d in hints.get("steam_settings", []):
        d = Path(d)
        dll_dir = d.parent
        secs = {k.lower(): v for k, v in _ini_sections(_read_text(d / "configs.user.ini")).items()}
        saves = secs.get("user::saves", {})
        if saves.get("local_save_path"):
            roots.append(("GSE (game folder)", dll_dir / os.path.expandvars(saves["local_save_path"])))
        if saves.get("saves_folder_name"):
            roots.append(("GSE", APPDATA / saves["saves_folder_name"]))
        for f in (d / "local_save.txt", dll_dir / "local_save.txt"):  # older Goldberg builds
            if f.exists():
                roots.append(("Goldberg (game folder)", dll_dir / (_read_text(f).strip() or "SAVE")))
    out, seen = [], set()
    for label, r in roots:
        for a in ids:
            p = r / a
            key = os.path.normcase(os.path.abspath(p))
            if key not in seen:
                seen.add(key)
                out.append((label, p))
    return out


def emu_missing_list(hints):
    """GSE/Goldberg steam_settings folders that have no achievements.json. Those emulators only save
    unlocks for achievements on that list, so such a copy silently throws every unlock away."""
    out = []
    for d in hints.get("steam_settings", []):
        d = Path(d)
        is_gse = any((d / n).exists() for n in ("configs.user.ini", "configs.app.ini", "configs.main.ini", "steam_appid.txt"))
        if is_gse and not (d / "achievements.json").exists():
            out.append(d)
    return out


def write_emu_list(gid):
    """Give a GSE/Goldberg copy the achievement list it is missing (built from the downloaded list),
    so the emulator starts saving unlocks. Adds steam_settings\achievements.json; changes nothing else."""
    c = db()
    g = c.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
    rows = c.execute("SELECT * FROM achievements WHERE game_id=? ORDER BY sort", (gid,)).fetchall()
    if not g or not rows:
        return []
    items = [{"name": r["api"], "displayName": r["name"], "display_name": r["name"],
              "description": r["description"] or "", "hidden": int(r["hidden"] or 0),
              "icon": r["icon_url"] or "", "icon_gray": r["icon_gray_url"] or r["icon_url"] or ""} for r in rows]
    written = []
    for d in emu_missing_list(game_hints(g)):
        f = d / "achievements.json"
        f.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(str(f))
        log.info("wrote emulator achievement list %s (%d achievements)", f, len(items))
    return written


def reader_for(f):
    """Pick the parser for an unlock file the user located by hand."""
    f = Path(f)
    if f.suffix.lower() == ".json":
        return progress_custom_json if schema_from_custom_json(f) else progress_goldberg_json
    text = _read_text(f)
    if "[ACHIEVEMENTS]" in text and re.search(r"unlocked\s*=", text, re.I):
        return progress_tenoke
    return progress_codex_ini


def find_unlock_file(path):
    """A file, or a folder picked by hand: the first achievements file inside it."""
    p = Path(path)
    if p.is_file():
        return p
    for pat in ("achievements.json", "achievements.ini", "user_stats.ini", "*/achievements.json",
                "*/achievements.ini", "*/*/achievements.json", "*/*/achievements.ini", "**/user_stats.ini",
                "**/achievements.json", "**/achievements.ini"):
        hit = next(iter(sorted(p.glob(pat))), None)
        if hit:
            return hit
    return None


# ------------------------------------------------------------------ game save readers
# For copies whose crack never records achievements: read the game's OWN save and credit only
# achievements the save proves (a mission/act that is recorded as completed). Never guesses.

def _save_strings(f):
    try:
        b = Path(f).read_bytes()
    except OSError:
        return []
    return [m.group().decode() for m in re.finditer(rb"[\x20-\x7e]{4,}", b)]


def _first_mtime(pattern_dir, glob):
    times = [p.stat().st_mtime for p in Path(pattern_dir).glob(glob)] if Path(pattern_dir).is_dir() else []
    return min(times) if times else None


HALO_SAVE = LOCALAPPDATA / "Meteorite" / "Saved" / "SaveGames" / "Progress.sav"
HALO_FILMS = LOCALAPPDATA / "Meteorite" / "Saved" / "BlamData" / "autosave"
# mission code in Progress.sav -> (achievement api, code of the next mission, used to date the unlock)
HALO_MISSIONS = {"a15": ("CAMPAIGN_0_1", "a30"), "a10": ("CAMPAIGN_0_1", "a30"), "a30": ("CAMPAIGN_0_2", "a50"),
                 "a50": ("CAMPAIGN_0_3", "b30"), "b30": ("CAMPAIGN_0_4", "b40"), "b40": ("CAMPAIGN_0_5", "c10"),
                 "c10": ("CAMPAIGN_0_6", "c20"), "c20": ("CAMPAIGN_0_7", "c40"), "c40": ("CAMPAIGN_0_8", "d20"),
                 "d20": ("CAMPAIGN_0_9", "d40"), "d40": ("CAMPAIGN_0_10", None)}


def progress_halo_save(f):
    """Halo: Campaign Evolved Progress.sav holds tags like Blam.Progress.Mission.Completion.Heroic.a30."""
    done = {}
    for t in _save_strings(f):
        m = re.search(r"Mission\.Completion\.(Easy|Normal|Heroic|Legendary)\.([a-d]\d\d)\b", t)
        if m:
            done.setdefault(m.group(2), set()).add(m.group(1))
    out = {}
    for code in done:
        if code not in HALO_MISSIONS:
            log.info("halo save: unknown mission code %s", code)
            continue
        api, nxt = HALO_MISSIONS[code]
        # unlock time ~ first autosave film of the following mission, else the save's own time
        t = _first_mtime(HALO_FILMS, f"asq_campaign_{nxt}_*.film") if nxt else None
        out[api] = t or Path(f).stat().st_mtime
    return out


GEARS_SAVES = LOCALAPPDATA / "Microsoft" / "Gears of War E-Day" / "Saves"


def progress_gears_eday_save(f):
    """Gears of War: E-Day. A chapter of Act N+1 in the campaign save means Act N is complete, so every
    'Unlock during Act N' story achievement has unlocked. Only whole acts count (no per-chapter guessing)."""
    acts = set()
    for sf in Path(f).parent.glob("Gears*Campaign*.sav"):
        for t in _save_strings(sf):
            for m in re.finditer(r"(?<![A-Za-z0-9])A(\d)C\d{4}(?!\d)", t):
                acts.add(int(m.group(1)))
    if not acts:
        return {}
    done_acts = {a for a in range(1, max(acts))}
    c = db()
    out = {}
    for r in c.execute("SELECT a.api, a.description FROM achievements a JOIN games g ON g.id=a.game_id WHERE g.appid=3010850"):
        m = re.search(r"during Act (\d) of the Campaign", r["description"] or "")
        if m and int(m.group(1)) in done_acts:
            out[r["api"]] = None
    return out


def save_reader_sources(appid):
    if appid == 2806050 and HALO_SAVE.exists():
        return [("Halo save (missions completed)", HALO_SAVE, progress_halo_save)]
    if appid == 3010850 and GEARS_SAVES.is_dir():
        return [("Gears save (acts completed)", f, progress_gears_eday_save)
                for f in GEARS_SAVES.glob("*/GearsGlobalCampaign.sav")]
    return []


def progress_sources(appid, hints):
    """[(label, path, reader)] that exist for this game."""
    srcs = []
    for label, d in emulator_save_dirs(appid, hints):
        f = d / "achievements.json"
        if f.exists():
            srcs.append((label, f, progress_goldberg_json))
    if appid:
        a = str(appid)
        for label, base in (("EMPRESS", APPDATA / "EMPRESS"), ("EMPRESS", PUBLIC / "Documents" / "EMPRESS")):
            if (base / a).is_dir():
                for f in list((base / a).glob("**/achievements.json"))[:3]:
                    srcs.append((label, f, progress_goldberg_json))
        for label, base in (("CODEX", PUBLIC / "Documents" / "Steam" / "CODEX"), ("CODEX", APPDATA / "Steam" / "CODEX"),
                            ("RUNE", PUBLIC / "Documents" / "Steam" / "RUNE"), ("RUNE", APPDATA / "Steam" / "RUNE"),
                            ("ALI213", PUBLIC / "Documents" / "Steam" / "ALI213"), ("3DM", PUBLIC / "Documents" / "3DMGAME")):
            for name in ("achievements.ini", "stats/achievements.ini", "Stats/Achievements.ini"):
                f = base / a / name
                if f.exists():
                    srcs.append((label, f, progress_codex_ini))
                    break
        for base in (PUBLIC / "Documents" / "OnlineFix", APPDATA / "OnlineFix"):
            f = base / a / "Stats" / "Achievements.ini"
            if f.exists():
                srcs.append(("OnlineFix", f, progress_codex_ini))
    for f in hints.get("tenoke", []):
        srcs.append(("Tenoke", Path(f), progress_tenoke))
    for d in hints.get("steam_settings", []):
        if (Path(d) / "achievements_unlocked.json").exists():
            srcs.append(("Goldberg (local)", Path(d) / "achievements_unlocked.json", progress_goldberg_json))
    have = {os.path.normcase(os.path.abspath(f)) for _, f, _ in srcs}
    for f in hints.get("json", []):
        if os.path.normcase(os.path.abspath(f)) in have:
            continue
        if schema_from_custom_json(f):
            srcs.append(("Game file", Path(f), progress_custom_json))
        elif Path(f).parent.name.isdigit():
            srcs.append(("Goldberg (local)", Path(f), progress_goldberg_json))
    for f in hints.get("extra", []):
        if Path(f).exists() and os.path.normcase(os.path.abspath(f)) not in have:
            srcs.append(("Added by you", Path(f), reader_for(f)))
    srcs += save_reader_sources(appid)
    return srcs


# ------------------------------------------------------------------ sync logic

_sync_lock = threading.RLock()


def display_name(row):
    return row["custom_name"] or row["name"]


def scan_and_register(cfg=None):
    """Discover games on the scan drives, add new ones, mark vanished ones uninstalled."""
    cfg = cfg or load_config()
    c = db()
    found = discover_games(cfg)
    now = time.time()
    ids = set()
    with _sync_lock:
        for g in found:
            gid = game_id_for(g["install_dir"])
            ids.add(gid)
            hints = inspect_install(g["install_dir"])
            row = c.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
            if row is None:
                c.execute("INSERT INTO games(id,name,install_dir,source,appid,appid_source,added_at,hints) VALUES(?,?,?,?,?,?,?,?)",
                          (gid, g["name"], g["install_dir"], g["source"], g["appid"],
                           "steam manifest" if g["appid"] else None, now, json.dumps(hints)))
                log.info("new game: %s (%s)", g["name"], g["install_dir"])
            else:
                c.execute("UPDATE games SET installed=1, hints=?, name=?, source=? WHERE id=?",
                          (json.dumps(hints), g["name"], g["source"], gid))
                if g["appid"] and not row["appid_locked"] and row["appid"] != g["appid"]:
                    c.execute("UPDATE games SET appid=?, appid_source='steam manifest' WHERE id=?", (g["appid"], gid))
        for r in c.execute("SELECT id, install_dir FROM games WHERE installed=1").fetchall():
            if r["id"] not in ids and not Path(r["install_dir"]).exists():
                c.execute("UPDATE games SET installed=0 WHERE id=?", (r["id"],))
    return len(found)


# ------------------------------------------------------------------ older history (not installed)

HISTORY_SKIP_APPIDS = {0, 480, 2462, 1182480, 2104320}  # Steam/Spacewar, bogus AMD id, TestApp, ToS Gamepad Tester
NOT_GAME_EXES = ("lm studio", "vlc", "heaven", "unigine", "obs64", "chrome", "msedge", "discord", "steamwebhelper")
WRAPPER_RE = re.compile(r"gamedrive|ankergames|steamrip|fitgirl|dodi|repack|^new folder|^\d{4,}[-.]|~|part\d", re.I)


def merge_history_into(gid, appid):
    """A game reinstalled on the scan drives takes over the sessions and unlocks of its history entry."""
    c = db()
    for h in c.execute("SELECT id FROM games WHERE appid=? AND id<>? AND source IN ('history','steam-history')",
                       (appid, gid)).fetchall():
        with _sync_lock:
            c.execute("UPDATE sessions SET game_id=? WHERE game_id=?", (gid, h["id"]))
            c.execute("INSERT OR IGNORE INTO unlocks SELECT ?, api, unlocked_at, source, seen_at FROM unlocks WHERE game_id=?",
                      (gid, h["id"]))
            for t in ("unlocks", "achievements"):
                c.execute(f"DELETE FROM {t} WHERE game_id=?", (h["id"],))
            c.execute("DELETE FROM games WHERE id=?", (h["id"],))
        log.info("history entry %s merged into reinstalled game %s", h["id"], gid)


def _emu_unlock_count(appid):
    n = 0
    for _, f, reader in progress_sources(appid, {}):
        try:
            n = max(n, len(reader(f)))
        except Exception:
            pass
    return n


def _emu_playtime(appid):
    best = 0
    for _, d in emulator_save_dirs(appid, {}):
        try:
            best = max(best, int(float(_read_text(d / "playtime.txt").strip() or 0)))
        except ValueError:
            pass
    return best


def history_candidates():
    """{appid: steam_owned} for every game this PC has a record of: Steam playtime and stats,
    every emulator save folder, and AMD's session log."""
    ids = {}
    steam = steam_playtimes()
    for a, (mins, _) in steam.items():
        ids[a] = True
    sp = steam_path()
    if sp:
        for f in (sp / "appcache" / "stats").glob("UserGameStats_*_*.bin"):
            ids.setdefault(int(f.stem.split("_")[-1]), True)
    bases = [APPDATA / "GSE Saves", APPDATA / "Goldberg SteamEmu Saves", LOCALAPPDATA / "Goldberg SteamEmu Saves",
             APPDATA / "EMPRESS", PUBLIC / "Documents" / "EMPRESS", PUBLIC / "Documents" / "Steam" / "CODEX",
             APPDATA / "Steam" / "CODEX", PUBLIC / "Documents" / "Steam" / "RUNE", APPDATA / "Steam" / "RUNE",
             PUBLIC / "Documents" / "OnlineFix", APPDATA / "OnlineFix"]
    for b in bases:
        if b.is_dir():
            for d in b.iterdir():
                if d.name.isdigit():
                    ids.setdefault(int(d.name), False)
    for a in [x for x in ids if x in HISTORY_SKIP_APPIDS]:
        del ids[a]
    return ids


def _amd_rows():
    if not AMD_DB.exists():
        return []
    import shutil
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "gametracker_rgstats.db"
    try:
        shutil.copyfile(AMD_DB, tmp)
        src = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        rows = src.execute("SELECT g.ExePath, g.Title, g.SteamID, COUNT(s.GameID) FROM InstalledGames g "
                           "LEFT JOIN GameSessions s ON s.GameID=g.GameID GROUP BY g.GameID").fetchall()
        src.close()
        return rows
    except Exception as e:
        log.warning("AMD history unreadable: %s", e)
        return []


def _guess_install(exe):
    """D:\\Game-Repack~XY\\Some Game\\Game.exe -> (D:\\Game-Repack~XY, 'Some Game')."""
    p = Path(exe)
    parts = p.parts[1:-1]
    if not parts:
        return None, None
    top = Path(p.parts[0]) / parts[0]
    name = next((x for x in parts if not WRAPPER_RE.search(x)), None) or p.stem
    return top, re.sub(r"\s+", " ", name.replace("_", " ")).strip()


def import_history(say=None):
    """Add every game with past playtime or unlocks that is not installed on the scan drives, as
    'not installed' entries (installed=0). Empty records (no time, no unlocks) are skipped."""
    say = say or (lambda m: None)
    c = db()
    cfg = load_config()
    have_appids = {r["appid"] for r in c.execute("SELECT appid FROM games WHERE appid IS NOT NULL")}
    steam = steam_playtimes()
    now = time.time()
    added = 0
    amd = _amd_rows()
    amd_steam = {sid: n for _, _, sid, n in amd if sid and sid > 0 and n}
    for appid, owned in sorted(history_candidates().items()):
        if appid in have_appids:
            continue
        mins = steam.get(appid, (0, 0))[0]
        _, bits = schema_from_steam_cache(appid)
        unl = max(_emu_unlock_count(appid), len(progress_steam(appid, bits)))
        if not (mins or unl or _emu_playtime(appid) or amd_steam.get(appid)):
            continue
        det = store_details(appid) or {}
        name = det.get("name")
        if not name:
            s_, _ = None, None
            f = steam_path() / "appcache" / "stats" / f"UserGameStatsSchema_{appid}.bin" if steam_path() else None
            if f and f.exists():
                try:
                    name = next(iter(parse_binary_vdf(f.read_bytes())[0].values()), {}).get("gamename")
                except Exception:
                    pass
        if not name or det.get("type") in ("dlc", "music", "video", "advertising"):
            log.info("history: skipping appid %s (no store name / not a game)", appid)
            continue
        say(f"Older history: {name}")
        c.execute("INSERT OR IGNORE INTO games(id,name,install_dir,source,appid,appid_source,appid_locked,installed,added_at,hints)"
                  " VALUES(?,?,NULL,?,?,?,1,0,?,'{}')",
                  (f"app-{appid}", name, "steam-history" if owned else "history", appid, "history record", now))
        have_appids.add(appid)
        added += 1
    # games AMD saw that have no Steam id and aren't installed any more (e.g. Epic / repacks deleted since)
    for exe, title, sid, n in amd:
        if not n or not exe or (sid and sid > 0) or any(x in exe.lower() for x in NOT_GAME_EXES):
            continue
        if exe.lower().startswith(("c:\\program files", "c:\\windows")) or "\\appdata\\" in exe.lower():
            continue
        top, name = _guess_install(exe)
        if not top or Path(exe).exists():
            continue  # still installed: the scan handles it
        low = str(top).lower().rstrip("\\") + "\\"
        if any(low.startswith(r["install_dir"].lower().rstrip("\\") + "\\") or r["install_dir"].lower().startswith(low)
               for r in c.execute("SELECT install_dir FROM games WHERE install_dir IS NOT NULL")):
            continue
        match = best_store_match(name, cfg["store_country"])
        appid = match["appid"] if match else None
        if appid and appid in have_appids:
            continue
        gid = game_id_for(top)
        c.execute("INSERT OR IGNORE INTO games(id,name,install_dir,source,appid,appid_source,appid_locked,installed,added_at,hints)"
                  " VALUES(?,?,?,?,?,?,1,0,?,'{}')",
                  (gid, match["name"] if match else name, str(top), "history", appid,
                   f"Steam store search ({match['name']})" if match else None, now))
        if appid:
            have_appids.add(appid)
        added += 1
        say(f"Older history: {match['name'] if match else name}")
    if added:
        log.info("history: added %d not-installed games", added)
    return added


def resolve_appid(gid, cfg=None):
    """Find the Steam AppID from install files, else by searching the Steam store by name."""
    cfg = cfg or load_config()
    c = db()
    g = c.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
    if not g or g["appid_locked"] or (g["appid"] and g["appid_source"] == "steam manifest"):
        return g["appid"] if g else None
    hints = json.loads(g["hints"] or "{}")
    cands = sorted(hints.get("appids", []), key=lambda a: a["depth"])
    appid, src = None, None
    if len(cands) == 1 or (cands and len({a["appid"] for a in cands}) == 1):
        appid, src = cands[0]["appid"], "file: " + Path(cands[0]["file"]).name
    else:
        match = best_store_match(g["name"], cfg["store_country"])
        if cands:
            ids = [a["appid"] for a in cands]
            pick = match["appid"] if match and match["appid"] in ids else ids[0]
            appid, src = pick, "file: " + Path(next(a["file"] for a in cands if a["appid"] == pick)).name
        elif match:
            appid, src = match["appid"], f"Steam store search ({match['name']})"
        elif cfg["store_country"] != "in":
            match = best_store_match(g["name"], "in")
            if match:
                appid, src = match["appid"], f"Steam store search ({match['name']})"
    if appid != g["appid"] or src != g["appid_source"]:
        c.execute("UPDATE games SET appid=?, appid_source=? WHERE id=?", (appid, src, gid))
        log.info("appid %s -> %s (%s)", g["name"], appid, src)
    if appid and g["installed"]:
        merge_history_into(gid, appid)
    return appid


def refresh_schema(gid, cfg=None, force=False):
    """Download/collect the achievement list for a game. Returns (count, source or error)."""
    cfg = cfg or load_config()
    c = db()
    g = c.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
    if not g:
        return 0, "unknown game"
    appid = g["appid"]
    hints = json.loads(g["hints"] or "{}")
    schema, source, errors = None, None, []

    for d in hints.get("steam_settings", []):
        s = schema_from_steam_settings(d)
        if s:
            # steam_settings folders of a bundle (MGS1/MGS4) belong to different appids
            sid = Path(d) / "steam_appid.txt"
            if not appid or not sid.exists() or _read_text(sid).strip().startswith(str(appid)):
                schema, source = s, "game files (steam_settings)"
                break
    if not schema and appid:
        s, _ = schema_from_steam_cache(appid)
        if s:
            schema, source = s, "Steam client cache"
    if not schema and appid and cfg.get("steam_api_key"):
        try:
            schema = schema_from_web_api(appid, cfg["steam_api_key"])
            source = "Steam Web API" if schema else None
        except Exception as e:
            errors.append(f"Steam Web API: {e}")
    if not schema and appid:
        try:
            schema = schema_from_public_pages(appid)
            source = "SteamHunters + Steam Community" if schema else None
        except Exception as e:
            errors.append(f"SteamHunters: {e}")
    if not schema:
        for f in hints.get("json", []):
            s = schema_from_custom_json(f)
            if s:
                schema, source = s, "game file (" + Path(f).name + ")"
                break

    now = time.time()
    if not schema:
        err = "; ".join(errors) or ("no Steam AppID found" if not appid else "this game has no achievements on Steam")
        c.execute("UPDATE games SET schema_try_at=?, schema_error=? WHERE id=?", (now, err, gid))
        return 0, err

    # local lists (steam_settings, Steam cache) often lack descriptions or icons: fill the gaps from the web
    if appid and any(not a["description"] or not (a.get("icon_url") or a.get("local_icon")) for a in schema) \
            and "SteamHunters" not in source:
        try:
            web = {w["api"].lower(): w for w in schema_from_public_pages(appid) or []}
            for a in schema:
                w = web.get(a["api"].lower())
                if w:
                    a["description"] = a["description"] or w["description"]
                    if not (a.get("icon_url") or a.get("local_icon")):
                        a["icon_url"] = w["icon_url"]
                    a.setdefault("global_pct", w.get("global_pct"))
            if web:
                source += " + SteamHunters"
        except Exception as e:
            log.warning("filling gaps for %s: %s", appid, e)

    pct = global_percentages(appid) if appid else {}
    with _sync_lock:
        c.execute("BEGIN")
        old = {r["api"].lower(): r["icon"] for r in c.execute("SELECT api, icon FROM achievements WHERE game_id=?", (gid,))}
        c.execute("DELETE FROM achievements WHERE game_id=?", (gid,))
        for a in schema:
            gp = pct.get(a["api"].lower(), a.get("global_pct"))
            icon = a.get("local_icon") or old.get(a["api"].lower())
            c.execute("INSERT OR REPLACE INTO achievements(game_id,api,name,description,hidden,icon_url,icon_gray_url,icon,global_pct,sort)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (gid, a["api"], a["name"], a["description"], a["hidden"], a.get("icon_url"),
                       a.get("icon_gray_url"), icon, gp, a["sort"]))
        c.execute("UPDATE games SET schema_source=?, schema_at=?, schema_error=NULL, schema_try_at=? WHERE id=?",
                  (source, now, now, gid))
        c.execute("COMMIT")
    log.info("schema %s: %d achievements from %s", g["name"], len(schema), source)
    return len(schema), source


def cache_icons(gid):
    """Download achievement icons so the app and notifications work offline."""
    c = db()
    rows = c.execute("SELECT api, icon_url FROM achievements WHERE game_id=? AND icon IS NULL AND icon_url IS NOT NULL",
                     (gid,)).fetchall()
    if not rows:
        return 0
    folder = CACHE / "icons" / gid
    folder.mkdir(parents=True, exist_ok=True)

    def one(r):
        name = hashlib.sha1(r["icon_url"].encode()).hexdigest()[:16] + ".jpg"
        return (r["api"], str(folder / name)) if download(r["icon_url"], folder / name, 200) else None

    with cf.ThreadPoolExecutor(8) as ex:
        done = [x for x in ex.map(one, rows) if x]
    with _sync_lock:
        for api, path in done:
            c.execute("UPDATE achievements SET icon=? WHERE game_id=? AND api=?", (path, gid, api))
    return len(done)


def refresh_art(gid):
    c = db()
    g = c.execute("SELECT appid, cover, hero FROM games WHERE id=?", (gid,)).fetchone()
    if not g or not g["appid"]:
        return
    cover, hero = fetch_art(g["appid"])
    c.execute("UPDATE games SET cover=COALESCE(?,cover), hero=COALESCE(?,hero) WHERE id=?", (cover, hero, gid))


AMD_LABEL = "AMD Adrenalin history"
AMD_DB = LOCALAPPDATA / "AMD" / "CN" / "RGStats.db"


def import_amd_history():
    """Past sessions from AMD Adrenalin's own game log (RGStats.db: every game launch since the driver
    was installed). Matched by exe path inside a game folder, or by Steam AppID. Sessions overlapping
    one already recorded are skipped, so this is safe to repeat: it also fills gaps when the tracker wasn't running."""
    if not AMD_DB.exists():
        return 0
    import datetime as _dt
    import shutil
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "gametracker_rgstats.db"
    try:
        shutil.copyfile(AMD_DB, tmp)  # Adrenalin keeps it open; read a copy
        src = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        rows = src.execute("SELECT g.ExePath, g.SteamID, s.Start, s.End FROM GameSessions s JOIN InstalledGames g "
                           "ON g.GameID=s.GameID WHERE s.IsRunning=0").fetchall()
        src.close()
    except Exception as e:
        log.warning("AMD history unreadable: %s", e)
        return 0
    c = db()
    games = c.execute("SELECT id, install_dir, appid FROM games").fetchall()
    by_dir = sorted(((g["install_dir"].lower().rstrip("\\") + "\\", g["id"]) for g in games if g["install_dir"]),
                    key=lambda x: -len(x[0]))
    by_appid = {g["appid"]: g["id"] for g in games if g["appid"]}
    min_s = load_config().get("min_session_seconds", 60)
    added = 0
    with _sync_lock:
        for exe, steamid, start, end in rows:
            gid = None
            low = (exe or "").lower()
            if low:
                gid = next((i for d, i in by_dir if low.startswith(d)), None)
            if gid is None and steamid and steamid > 0:
                gid = by_appid.get(steamid)
            if gid is None:
                continue
            try:
                t0 = _dt.datetime.fromisoformat(start.replace(" ", "T")).timestamp()
                t1 = _dt.datetime.fromisoformat(end.replace(" ", "T")).timestamp()
            except ValueError:
                continue
            if t1 - t0 < min_s or t1 - t0 > 3 * 86400:
                continue
            if c.execute("SELECT 1 FROM sessions WHERE game_id=? AND start < ? AND end > ?", (gid, t1 - 5, t0 + 5)).fetchone():
                continue
            c.execute("INSERT INTO sessions(game_id,start,end,exe) VALUES(?,?,?,?)", (gid, t0, t1, AMD_LABEL))
            added += 1
    if added:
        log.info("imported %d sessions from AMD Adrenalin history", added)
    return added


def import_baselines():
    """Playtime recorded before any session history exists (Steam / emulator playtime.txt), minus what
    the imported AMD sessions already cover. Frozen once GameTracker records its own sessions for a game
    (later Steam/emulator time would double count)."""
    c = db()
    steam = steam_playtimes()
    for appid, (_, last) in steam.items():
        if last:
            c.execute("UPDATE games SET ext_last_played=? WHERE appid=?", (last, appid))
    for g in c.execute("SELECT * FROM games WHERE baseline_source IS NULL OR NOT EXISTS "
                       "(SELECT 1 FROM sessions s WHERE s.game_id=games.id AND s.exe IS NOT ?)", (AMD_LABEL,)).fetchall():
        secs, src = 0, None
        if g["appid"] in steam:
            secs, src = steam[g["appid"]][0] * 60, "Steam"
        for label, d in emulator_save_dirs(g["appid"], game_hints(g)):
            try:
                v = int(float(_read_text(d / "playtime.txt").strip() or 0))
            except ValueError:
                v = 0
            if v > secs:
                secs, src = v, label.replace(" (game folder)", "") + " emulator"
        amd = c.execute("SELECT COALESCE(SUM(end-start),0) FROM sessions WHERE game_id=? AND exe=?",
                        (g["id"], AMD_LABEL)).fetchone()[0]
        c.execute("UPDATE games SET baseline_seconds=?, baseline_source=? WHERE id=?",
                  (max(0, int(secs - amd)), src or "none", g["id"]))


MANUAL_LABEL = "Marked by you"


def mark_unlocked(gid, api, when=None):
    with _sync_lock:
        db().execute("INSERT OR IGNORE INTO unlocks(game_id,api,unlocked_at,source,seen_at) VALUES(?,?,?,?,?)",
                     (gid, api, when or time.time(), MANUAL_LABEL, time.time()))


def unmark_unlocked(gid, api):
    with _sync_lock:
        db().execute("DELETE FROM unlocks WHERE game_id=? AND api=? AND source=?", (gid, api, MANUAL_LABEL))


def sync_progress(gid, notify=None, online=False):
    """Read every progress source for a game and record new unlocks. Returns list of new unlock dicts."""
    c = db()
    g = c.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
    if not g:
        return []
    hints = game_hints(g)
    schema = {r["api"].lower(): r for r in c.execute("SELECT * FROM achievements WHERE game_id=? ORDER BY sort", (gid,))}
    found = {}  # api(lower) -> (time, source)

    def merge(res, label):
        for api, t in res.items():
            if api is None:
                continue
            k = str(api).lower()
            if k not in schema and k.isdigit() and schema:  # CODEX numeric sections = schema order
                idx = int(k)
                order = list(schema)
                k = order[idx] if idx < len(order) else k
            prev = found.get(k)
            if not prev or (t and (not prev[0] or t < prev[0])):
                found[k] = (t, label)

    if g["appid"]:
        _, bits = schema_from_steam_cache(g["appid"])
        merge(progress_steam(g["appid"], bits), "Steam")
        if online and g["source"] in ("steam", "steam-history"):
            merge(progress_steam_profile(g["appid"]), "Steam profile")
    for label, f, reader in progress_sources(g["appid"], hints):
        try:
            merge(reader(f), label)
        except Exception as e:
            log.warning("progress %s %s: %s", label, f, e)

    now = time.time()
    new = []
    with _sync_lock:
        have = {r["api"].lower() for r in c.execute("SELECT api FROM unlocks WHERE game_id=?", (gid,))}
        for k, (t, label) in found.items():
            api = schema[k]["api"] if k in schema else k
            if k not in have:
                c.execute("INSERT OR REPLACE INTO unlocks(game_id,api,unlocked_at,source,seen_at) VALUES(?,?,?,?,?)",
                          (gid, api, t or now, label, now))
                new.append({"api": api, "time": t or now, "source": label,
                            "name": schema[k]["name"] if k in schema else api,
                            "description": schema[k]["description"] if k in schema else "",
                            "icon": schema[k]["icon"] if k in schema else None,
                            "global_pct": schema[k]["global_pct"] if k in schema else None})
            elif t:
                c.execute("UPDATE unlocks SET unlocked_at=MIN(unlocked_at, ?) WHERE game_id=? AND lower(api)=?", (t, gid, k))
            if k in have:
                c.execute("UPDATE unlocks SET source=? WHERE game_id=? AND lower(api)=? AND source=?",
                          (label, gid, k, MANUAL_LABEL))
        first = not g["ach_synced"]
        if first:
            c.execute("UPDATE games SET ach_synced=1 WHERE id=?", (gid,))
    if new and notify and not first:
        for n in new:
            notify(display_name(g), n)
    return new


def watched_files(gid):
    """Paths whose change means achievements may have changed (for cheap polling)."""
    c = db()
    g = c.execute("SELECT appid, hints, extra_sources FROM games WHERE id=?", (gid,)).fetchone()
    if not g:
        return []
    hints = game_hints(g)
    files = [f for _, f, _ in progress_sources(g["appid"], hints)]
    if g["appid"] == 3010850 and GEARS_SAVES.is_dir():
        files += list(GEARS_SAVES.glob("*/GearsCampaign*.sav"))
    # save folders that don't exist yet appear on the first unlock
    files += [d / "achievements.json" for _, d in emulator_save_dirs(g["appid"], hints)]
    sp = steam_path()
    if sp and g["appid"]:
        files += list((sp / "appcache" / "stats").glob(f"UserGameStats_*_{g['appid']}.bin"))
    # emulator folders that don't exist yet appear on the first unlock
    if g["appid"]:
        files += [PUBLIC / "Documents" / "Steam" / "CODEX" / str(g["appid"]), PUBLIC / "Documents" / "OnlineFix" / str(g["appid"])]
    return files


def files_signature(paths):
    sig = []
    for p in paths:
        try:
            st = os.stat(p)
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), 0, 0))
    return tuple(sig)


def full_refresh(cfg=None, force=False, progress=None, only=None):
    """Rescan drives, resolve AppIDs, download missing achievement lists, art and icons, sync unlocks."""
    cfg = cfg or load_config()
    say = progress or (lambda m: None)
    say("Scanning drives for games…")
    if only is None:
        scan_and_register(cfg)
    c = db()
    if only is None:
        say("Importing older play history…")
        import_history(say)
    rows = c.execute("SELECT * FROM games WHERE (installed=1 OR source IN ('history','steam-history'))"
                     + (" AND id=?" if only else ""),
                     (only,) if only else ()).fetchall()
    now = time.time()
    for g in rows:
        name = display_name(g)
        say(f"{name}: finding Steam AppID…")
        resolve_appid(g["id"], cfg)
        g = c.execute("SELECT * FROM games WHERE id=?", (g["id"],)).fetchone()
        count = c.execute("SELECT COUNT(*) FROM achievements WHERE game_id=?", (g["id"],)).fetchone()[0]
        stale = not g["schema_at"] or now - g["schema_at"] > 7 * 86400
        retry_ok = not g["schema_try_at"] or now - g["schema_try_at"] > 6 * 3600
        if force or (count == 0 and retry_ok) or (stale and retry_ok):
            say(f"{name}: downloading achievement list…")
            refresh_schema(g["id"], cfg)
        if g["appid"] and (force or not g["cover"] or not g["hero"]) and not g["custom_cover"]:
            say(f"{name}: downloading artwork…")
            refresh_art(g["id"])
        say(f"{name}: downloading achievement icons…")
        cache_icons(g["id"])
        sync_progress(g["id"], online=True)
    import_amd_history()
    import_baselines()
    say("Done")


# ------------------------------------------------------------------ queries for the UI

def played_seconds(gid=None):
    c = db()
    q = "SELECT game_id, SUM(end-start) s, MAX(end) last, COUNT(*) n FROM sessions GROUP BY game_id"
    return {r["game_id"]: (r["s"] or 0, r["last"], r["n"]) for r in c.execute(q)}


def read_live():
    try:
        j = json.loads(LIVE_PATH.read_text(encoding="utf-8"))
        if time.time() - j.get("heartbeat", 0) < 30:
            return j
    except Exception:
        pass
    return {}
