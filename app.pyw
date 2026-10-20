"""Local Game Genesys window: library, per-game playtime and achievements, activity and settings.
The UI is ui\\index.html in an Edge WebView2 window (pywebview); images are served by a
small local web server bound to 127.0.0.1.
"""
import ctypes
import datetime as dt
import http.server
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import webview  # noqa: E402

import core  # noqa: E402

log = core.log
UI = core.HERE / "ui"
STARTUP_LNK = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup\Local Game Genesys (background).lnk"
PYTHONW = core.HERE / "venv" / "Scripts" / "pythonw.exe"


# ------------------------------------------------------------------ local file server

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/file":
            p = Path(urllib.parse.parse_qs(u.query).get("p", [""])[0])
            if not self.allowed(p):
                return self.send_error(403)
        else:
            rel = urllib.parse.unquote(u.path.lstrip("/")) or "index.html"
            if rel.startswith("cache/"):
                p = (core.CACHE / rel[6:]).resolve()
                if core.CACHE.resolve() not in p.parents:
                    return self.send_error(403)
            else:
                p = (UI / rel).resolve()
                if UI.resolve() not in p.parents:
                    return self.send_error(403)
        if not p.is_file():
            return self.send_error(404)
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(p))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if p.suffix in (".html", ".js", ".css") else "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def allowed(p):
        """Icons that live inside a game install (steam_settings) or the cache."""
        try:
            p = p.resolve()
        except OSError:
            return False
        if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".ico"):
            return False
        roots = [core.CACHE.resolve()] + [Path(r["install_dir"]).resolve() for r in
                                           core.db().execute("SELECT install_dir FROM games WHERE install_dir IS NOT NULL")]
        return any(r == p or r in p.parents for r in roots)


def start_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


# ------------------------------------------------------------------ helpers

def img_url(p):
    if not p:
        return None
    if p.startswith("http"):
        return p
    if not os.path.isabs(p):
        return "/cache/" + p.replace("\\", "/")
    return "/file?p=" + urllib.parse.quote(p)


def unlock_found(g):
    """True if at least one place this game's unlocks are recorded exists."""
    if g["source"] in ("steam", "steam-history"):
        return True
    return bool(core.progress_sources(g["appid"], core.game_hints(g)))


def day_key(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def tracker_running():
    live = core.read_live()
    return bool(live)


def start_tracker():
    if tracker_running():
        return True
    subprocess.Popen([str(PYTHONW), str(core.HERE / "tracker.pyw")], cwd=str(core.HERE),
                     creationflags=0x08000000 | 0x00000200)  # no window, new process group
    return True


def make_shortcut(lnk, target, args, icon, workdir):
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
          "$s.TargetPath=$env:TGT;$s.Arguments=$env:ARGS;$s.WorkingDirectory=$env:WD;"
          "$s.IconLocation=$env:ICO;$s.Save()")
    env = dict(os.environ, LNK=str(lnk), TGT=str(target), ARGS=args, WD=str(workdir), ICO=str(icon))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, creationflags=0x08000000, check=False)


# ------------------------------------------------------------------ API for the page

class Api:
    def __init__(self):
        self._window = None
        self._task = {"running": False, "message": "", "done_at": 0, "error": None}

    # -------- library
    def library(self):
        c = core.db()
        played = core.played_seconds()
        live = core.read_live()
        playing = {p["id"]: p for p in live.get("playing", [])}
        ach = {r["game_id"]: (r["n"], r["u"]) for r in c.execute(
            "SELECT a.game_id, COUNT(*) n, COUNT(u.api) u FROM achievements a "
            "LEFT JOIN unlocks u ON u.game_id=a.game_id AND lower(u.api)=lower(a.api) GROUP BY a.game_id")}
        games = []
        now = time.time()
        for g in c.execute("SELECT * FROM games ORDER BY COALESCE(custom_name,name) COLLATE NOCASE"):
            secs, last, n = played.get(g["id"], (0, None, 0))
            if g["id"] in playing:
                secs += now - playing[g["id"]]["since"]
                last = now
            last = last or g["ext_last_played"]
            total, unl = ach.get(g["id"], (0, 0))
            played_s = secs + (g["baseline_seconds"] or 0)
            missing = bool(total and not unl and g["installed"] and g["source"] != "steam" and played_s > 0 and
                           (not unlock_found(g) or played_s >= 3600 or core.emu_missing_list(core.game_hints(g))))
            games.append({
                "id": g["id"], "name": core.display_name(g), "appid": g["appid"], "installed": bool(g["installed"]),
                "hidden": bool(g["hidden"]), "cover": img_url(g["custom_cover"] or g["cover"]), "hero": img_url(g["hero"]),
                "tracked": secs, "baseline": g["baseline_seconds"] or 0, "played": secs + (g["baseline_seconds"] or 0),
                "last_played": last, "sessions": n, "ach_total": total, "ach_unlocked": unl,
                "playing": g["id"] in playing, "added_at": g["added_at"], "unlock_missing": missing,
            })
        return {"games": games, "live": live, "tracker": bool(live)}

    def game(self, gid):
        c = core.db()
        g = c.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()
        if not g:
            return None
        live = core.read_live()
        now = time.time()
        sessions = [dict(r) for r in c.execute(
            "SELECT id, start, end, exe FROM sessions WHERE game_id=? ORDER BY start DESC", (gid,))]
        for p in live.get("playing", []):
            if p["id"] == gid and sessions and sessions[0]["start"] >= p["since"] - 1:
                sessions[0]["end"] = now
                sessions[0]["live"] = True
        tracked = sum(s["end"] - s["start"] for s in sessions)
        daily = {}
        for s in sessions:
            k = day_key(s["start"])
            daily[k] = daily.get(k, 0) + (s["end"] - s["start"])
        rows = c.execute(
            "SELECT a.*, u.unlocked_at, u.source FROM achievements a LEFT JOIN unlocks u "
            "ON u.game_id=a.game_id AND lower(u.api)=lower(a.api) WHERE a.game_id=? ORDER BY a.sort", (gid,)).fetchall()
        achievements = [{
            "api": r["api"], "name": r["name"], "description": r["description"], "hidden": bool(r["hidden"]),
            "icon": img_url(r["icon"]) or r["icon_url"], "icon_gray": r["icon_gray_url"], "pct": r["global_pct"],
            "unlocked_at": r["unlocked_at"], "source": r["source"], "sort": r["sort"],
        } for r in rows]
        known = {a["api"].lower() for a in achievements}
        orphans = [dict(r) for r in c.execute("SELECT api, unlocked_at, source FROM unlocks WHERE game_id=?", (gid,))
                   if r["api"].lower() not in known]
        hints = core.game_hints(g)
        sources = [{"label": l, "path": str(f), "manual": l == "Added by you"}
                   for l, f, _ in core.progress_sources(g["appid"], hints)]
        found = bool(sources) or g["source"] in ("steam", "steam-history")
        emu_fix = [str(d) for d in core.emu_missing_list(hints)] if achievements else []
        expected = [str(d / "achievements.json") for _, d in core.emulator_save_dirs(g["appid"], hints)]
        if g["appid"]:
            sp = core.steam_path()
            if sp:
                sources += [{"label": "Steam", "path": str(f)} for f in
                            (sp / "appcache" / "stats").glob(f"UserGameStats_*_{g['appid']}.bin")]
            if g["source"] in ("steam", "steam-history"):
                sources.append({"label": "Steam profile", "path": "steamcommunity.com (public profile)"})
        return {
            "id": gid, "name": core.display_name(g), "original_name": g["name"], "install_dir": g["install_dir"],
            "installed": bool(g["installed"]), "hidden": bool(g["hidden"]), "source": g["source"],
            "appid": g["appid"], "appid_source": g["appid_source"], "appid_locked": bool(g["appid_locked"]),
            "cover": img_url(g["custom_cover"] or g["cover"]), "hero": img_url(g["hero"]),
            "schema_source": g["schema_source"], "schema_error": g["schema_error"], "schema_at": g["schema_at"],
            "tracked": tracked, "baseline": g["baseline_seconds"] or 0, "baseline_source": g["baseline_source"],
            "sessions": sessions[:200], "session_count": len(sessions), "daily": daily,
            "achievements": achievements, "orphans": orphans, "sources": sources,
            "playing": any(p["id"] == gid for p in live.get("playing", [])), "added_at": g["added_at"],
            "ext_last_played": g["ext_last_played"], "unlock_found": found, "expected": expected[:6], "emu_fix": emu_fix, "source_is_steam": g["source"] in ("steam", "steam-history"),
        }

    def activity(self, days=30):
        c = core.db()
        since = time.time() - days * 86400
        names = {r["id"]: core.display_name(r) for r in c.execute("SELECT id, name, custom_name FROM games")}
        covers = {r["id"]: img_url(r["custom_cover"] or r["cover"]) for r in c.execute("SELECT id, cover, custom_cover FROM games")}
        daily, per_game, count = {}, {}, 0
        for s in c.execute("SELECT game_id, start, end FROM sessions WHERE end>=?", (since,)):
            d = s["end"] - max(s["start"], since)
            k = day_key(max(s["start"], since))
            daily.setdefault(k, {})
            daily[k][s["game_id"]] = daily[k].get(s["game_id"], 0) + d
            per_game[s["game_id"]] = per_game.get(s["game_id"], 0) + d
            count += 1
        unlocks = [{
            "game_id": r["game_id"], "game": names.get(r["game_id"], "?"), "name": r["name"] or r["api"],
            "description": r["description"], "icon": img_url(r["icon"]) or r["icon_url"], "time": r["unlocked_at"],
            "pct": r["global_pct"], "source": r["source"],
        } for r in c.execute(
            "SELECT u.*, a.name, a.description, a.icon, a.icon_url, a.global_pct FROM unlocks u LEFT JOIN achievements a "
            "ON a.game_id=u.game_id AND lower(a.api)=lower(u.api) ORDER BY u.unlocked_at DESC LIMIT 60")]
        longest = [{"game": names.get(r["game_id"], "?"), "game_id": r["game_id"], "start": r["start"], "dur": r["d"]}
                   for r in c.execute("SELECT game_id, start, end-start d FROM sessions WHERE end>=? ORDER BY d DESC LIMIT 5", (since,))]
        return {"days": days, "daily": daily, "names": names, "covers": covers, "sessions": count,
                "top": sorted(([g, s] for g, s in per_game.items()), key=lambda x: -x[1]),
                "unlocks": unlocks, "longest": longest}

    # -------- background tasks
    def status(self):
        live = core.read_live()
        return {"task": self._task, "live": live, "tracker": bool(live)}

    def _run(self, label, fn):
        if self._task["running"]:
            return False

        def go():
            self._task.update(running=True, message=label, error=None)
            try:
                fn(lambda m: self._task.update(message=m))
            except Exception as e:
                log.error("%s failed:\n%s", label, traceback.format_exc())
                self._task["error"] = str(e)
            self._task.update(running=False, done_at=time.time())
            (core.DATA / "reload.flag").touch()

        threading.Thread(target=go, daemon=True).start()
        return True

    def refresh_all(self, force=False):
        return self._run("Refreshing library…", lambda say: core.full_refresh(force=force, progress=say))

    def refresh_game(self, gid):
        def fn(say):
            core.full_refresh(force=True, progress=say, only=gid)
        return self._run("Refreshing game…", fn)

    # -------- edits
    def steam_search(self, term):
        cfg = core.load_config()
        res = core.store_search(term, cfg["store_country"])
        if term.strip().isdigit():
            det = core.store_details(int(term))
            if det:
                res.insert(0, {"appid": int(term), "name": det.get("name"), "image": det.get("capsule_imagev5") or det.get("header_image")})
        return res[:12]

    def set_appid(self, gid, appid):
        c = core.db()
        appid = int(appid) if appid else None
        c.execute("UPDATE games SET appid=?, appid_source=?, appid_locked=1, cover=NULL, hero=NULL, schema_try_at=NULL,"
                  " baseline_source=NULL WHERE id=?", (appid, "set by you" if appid else None, gid))
        c.execute("DELETE FROM achievements WHERE game_id=?", (gid,))
        c.execute("DELETE FROM unlocks WHERE game_id=?", (gid,))
        c.execute("UPDATE games SET ach_synced=0 WHERE id=?", (gid,))
        return self.refresh_game(gid)

    def auto_appid(self, gid):
        core.db().execute("UPDATE games SET appid_locked=0, appid=NULL, appid_source=NULL WHERE id=?", (gid,))
        return self.refresh_game(gid)

    def rename(self, gid, name):
        core.db().execute("UPDATE games SET custom_name=? WHERE id=?", (name.strip() or None, gid))
        (core.DATA / "reload.flag").touch()
        return True

    def set_hidden(self, gid, hidden):
        core.db().execute("UPDATE games SET hidden=? WHERE id=?", (1 if hidden else 0, gid))
        (core.DATA / "reload.flag").touch()
        return True

    def delete_session(self, sid):
        core.db().execute("DELETE FROM sessions WHERE id=?", (sid,))
        return True

    def add_session(self, gid, start_iso, minutes):
        start = dt.datetime.fromisoformat(start_iso).timestamp()
        core.db().execute("INSERT INTO sessions(game_id,start,end,exe) VALUES(?,?,?,?)",
                          (gid, start, start + float(minutes) * 60, "added by hand"))
        return True

    def open_path(self, path):
        if not path:
            return False
        p = Path(path)
        if p.exists():
            subprocess.Popen(f'explorer /select,"{p}"' if p.is_file() else ["explorer", str(p)])
            return True
        return False

    def open_url(self, url):
        if url.startswith("https://"):
            os.startfile(url)

    def choose_cover(self, gid):
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, file_types=("Images (*.jpg;*.jpeg;*.png;*.webp)",))
        if not res:
            return None
        src = Path(res[0])
        dest = core.CACHE / "covers" / f"custom_{gid}{src.suffix.lower()}"
        shutil.copyfile(src, dest)
        core.db().execute("UPDATE games SET custom_cover=? WHERE id=?", (f"covers/{dest.name}", gid))
        return img_url(f"covers/{dest.name}") + f"?t={int(time.time())}"

    def reset_cover(self, gid):
        core.db().execute("UPDATE games SET custom_cover=NULL WHERE id=?", (gid,))
        return True

    def add_game_folder(self):
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not res:
            return False
        cfg = core.load_config()
        if res[0] not in cfg["manual_games"]:
            cfg["manual_games"].append(res[0])
            core.save_config(cfg)
        return self.refresh_all()

    def add_unlock_source(self, gid, folder=False):
        """Point a game at its achievement unlock file (or a folder that contains it)."""
        if folder:
            res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        else:
            res = self._window.create_file_dialog(
                webview.OPEN_DIALOG, file_types=("Achievement files (*.json;*.ini;*.txt)", "All files (*.*)"))
        if not res:
            return {"ok": False}
        f = core.find_unlock_file(res[0])
        if not f:
            return {"ok": False, "error": "No achievements.json / achievements.ini / user_stats.ini in that folder."}
        c = core.db()
        g = c.execute("SELECT extra_sources FROM games WHERE id=?", (gid,)).fetchone()
        extra = json.loads(g["extra_sources"] or "[]")
        if str(f) not in extra:
            extra.append(str(f))
        c.execute("UPDATE games SET extra_sources=? WHERE id=?", (json.dumps(extra), gid))
        new = core.sync_progress(gid)
        (core.DATA / "reload.flag").touch()
        return {"ok": True, "file": str(f), "new": len(new)}

    def fix_emu_list(self, gid):
        return core.write_emu_list(gid)

    def mark_achievement(self, gid, api, on, when_iso=None):
        if on:
            when = dt.datetime.fromisoformat(when_iso).timestamp() if when_iso else None
            core.mark_unlocked(gid, api, when)
        else:
            core.unmark_unlocked(gid, api)
        return True

    def remove_unlock_source(self, gid, path):
        c = core.db()
        g = c.execute("SELECT extra_sources FROM games WHERE id=?", (gid,)).fetchone()
        extra = [p for p in json.loads(g["extra_sources"] or "[]") if p != path]
        c.execute("UPDATE games SET extra_sources=? WHERE id=?", (json.dumps(extra), gid))
        c.execute("DELETE FROM unlocks WHERE game_id=? AND source='Added by you'", (gid,))
        core.sync_progress(gid)
        return True

    def remove_game(self, gid):
        """Forget a game added by hand (history is kept unless it's re-added)."""
        c = core.db()
        g = c.execute("SELECT install_dir FROM games WHERE id=?", (gid,)).fetchone()
        cfg = core.load_config()
        cfg["manual_games"] = [p for p in cfg["manual_games"] if core.game_id_for(p) != gid]
        core.save_config(cfg)
        c.execute("UPDATE games SET installed=0, hidden=1 WHERE id=?", (gid,))
        return bool(g)

    # -------- settings
    def settings(self):
        cfg = core.load_config()
        drives = [f"{d}" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{d}:\\")]
        return {"config": cfg, "drives": drives, "autostart": STARTUP_LNK.exists(), "tracker": tracker_running(),
                "data_dir": str(core.DATA), "log": str(core.LOG_PATH), "defaults": core.DEFAULT_CONFIG,
                "overlay_available": (core.HERE / "game_overlay.pyw").exists()}

    def test_overlay(self, opts=None):
        """Save the overlay settings and show the panel for 12 s (works without a game running)."""
        if opts:
            self.save_settings(opts)
        (core.DATA / "overlay_preview.flag").touch()
        return True

    def save_settings(self, new):
        cfg = core.load_config()
        for k in ("scan_drives", "exclude_folders", "manual_games", "notifications", "session_summary",
                  "welcome_popup", "welcome_message",
                  "steam_api_key", "store_country", "min_session_seconds"):
            if k in new:
                cfg[k] = new[k]
        for k in new:
            if k.startswith(("welcome_", "overlay_")) and k in core.DEFAULT_CONFIG:
                cfg[k] = new[k]
        cfg["min_session_seconds"] = max(0, int(cfg.get("min_session_seconds", 60)))
        core.save_config(cfg)
        (core.DATA / "reload.flag").touch()
        return True

    def set_autostart(self, on):
        if on:
            make_shortcut(STARTUP_LNK, PYTHONW, f'"{core.HERE / "tracker.pyw"}"', core.HERE / "ui" / "icon.ico", core.HERE)
        else:
            STARTUP_LNK.unlink(missing_ok=True)
        return STARTUP_LNK.exists()

    _preview_lock = threading.Lock()

    def test_welcome(self, opts=None):
        """Preview the game-launch pop-up with the (unsaved) values on the Settings page,
        using the most recently played game for the name and cover."""
        import overlay
        if not self._preview_lock.acquire(blocking=False):
            return False
        cfg = core.load_config()
        cfg.update(opts or {})
        game, cover = "Test Game", None
        try:
            r = core.db().execute("SELECT g.* FROM games g JOIN sessions s ON s.game_id=g.id "
                                  "ORDER BY s.start DESC LIMIT 1").fetchone()
            if r:
                game, cover = core.display_name(r), core.cover_file(r)
        except Exception:
            pass

        def run():
            try:
                overlay.show(**core.welcome_popup_args(cfg, game, cover))
            finally:
                self._preview_lock.release()
        threading.Thread(target=run, daemon=True).start()
        return True

    def pick_sound(self):
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, file_types=("Sound files (*.wav)",))
        return res[0] if res else None

    def start_tracker(self):
        return start_tracker()

    def restart_tracker(self):
        live = core.read_live()
        if live.get("pid"):
            try:
                import psutil
                psutil.Process(live["pid"]).terminate()
                time.sleep(1.5)
            except Exception:
                pass
        core.LIVE_PATH.unlink(missing_ok=True)
        return start_tracker()


def focus_existing():
    hwnd = ctypes.windll.user32.FindWindowW(None, "Local Game Genesys")
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)


def main():
    core.setup_logging("app")
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\LocalGameGenesysUI")
    if ctypes.windll.kernel32.GetLastError() == 183:
        focus_existing()
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LocalGameGenesys.App")
    except Exception:
        pass
    start_tracker()
    port = start_server()
    api = Api()
    page = next((a for a in sys.argv[1:] if a.startswith("#")), "")  # e.g. "#game=<id>", "#activity"
    win = webview.create_window("Local Game Genesys", f"http://127.0.0.1:{port}/index.html{page}", js_api=api,
                                width=1400, height=880, min_size=(1040, 660), background_color="#0c0e13")
    api._window = win
    webview.start(private_mode=False, storage_path=str(core.DATA / "webview"),
                  icon=str(core.HERE / "ui" / "icon.ico"), debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
