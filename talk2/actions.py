"""Things talk2 can do on the headset. Used by the instant commands and by
Claude's tools. Each returns a short result to speak or report, or raises
ActionError with a message worth saying out loud."""

import configparser
import difflib
import glob
import os
import re
import shlex
import shutil
import signal
import subprocess
import time

from . import config, context
from .log import log


class ActionError(Exception):
    pass


# -- running things in the right session ----------------------------------------
def session_env():
    """The Desktop Mode session's environment, or None in Game Mode.

    Desktop Mode runs nested with its own XDG_RUNTIME_DIR, display and D-Bus,
    which the background service can't see, so borrow plasmashell's.
    """
    pid = context._pgrep("plasmashell")
    if not pid:
        return None
    raw = context._read(f"/proc/{pid}/environ", binary=True)
    if not raw:
        return None
    env = {}
    for kv in raw.split(b"\0"):
        if b"=" in kv:
            k, v = kv.split(b"=", 1)
            env[k.decode(errors="ignore")] = v.decode(errors="ignore")
    return env or None


def spawn(cmd, env=None):
    """Start a program detached, in Desktop Mode's session when there is one."""
    env = env if env is not None else (session_env() or dict(os.environ))
    log(f"launch: {' '.join(cmd)}")
    try:
        subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        raise ActionError(f"I couldn't start {os.path.basename(cmd[0])}.") from e


def run(cmd, env=None, timeout=5):
    env = env if env is not None else (session_env() or dict(os.environ))
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ActionError(f"{os.path.basename(cmd[0])} didn't work.") from e


def steam_url(url):
    """Hand a steam:// URL to the running Steam client."""
    steam = shutil.which("steam") or "/usr/bin/steam"
    spawn([steam, url], env=dict(os.environ))


# -- apps ------------------------------------------------------------------------
APP_DIRS = [os.path.expanduser("~/.local/share/applications"),
            os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
            "/var/lib/flatpak/exports/share/applications",
            "/usr/local/share/applications", "/usr/share/applications"]

BROWSERS = ["org.chromium.Chromium", "chromium", "com.google.Chrome", "google-chrome",
            "org.mozilla.firefox", "firefox", "com.brave.Browser", "brave-browser",
            "com.microsoft.Edge", "microsoft-edge"]
ALIASES = {
    "browser": "@browser", "web browser": "@browser", "internet": "@browser", "the internet": "@browser",
    "web": "@browser", "chrome": "@browser", "google chrome": "@browser",
    "terminal": "org.kde.konsole", "console": "org.kde.konsole", "konsole": "org.kde.konsole",
    "files": "org.kde.dolphin", "file manager": "org.kde.dolphin", "file browser": "org.kde.dolphin",
    "text editor": "org.kde.kate", "notes": "org.kde.kate", "calculator": "org.kde.kcalc",
    "settings": "systemsettings", "system settings": "systemsettings",
    "discover": "org.kde.discover", "app store": "org.kde.discover", "software center": "org.kde.discover",
}
STEAM_PLACES = {
    "steam": "steam://open/main", "steam library": "steam://open/games", "library": "steam://open/games",
    "steam store": "steam://store", "store": "steam://store", "friends": "steam://open/friends",
    "steam friends": "steam://open/friends", "downloads": "steam://open/downloads",
    "steam settings": "steam://open/settings", "screenshots": "steam://open/screenshots",
}


def desktop_entries():
    """[(id, name, keywords, path)] for installed apps."""
    seen, out = set(), []
    for d in APP_DIRS:
        for path in glob.glob(os.path.join(d, "*.desktop")):
            app_id = os.path.basename(path)[:-8]
            if app_id in seen:
                continue
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(path, encoding="utf-8")
                e = cp["Desktop Entry"]
            except (configparser.Error, KeyError, UnicodeDecodeError):
                continue
            if e.get("NoDisplay", "").lower() == "true" or e.get("Type", "Application") != "Application":
                continue
            seen.add(app_id)
            words = " ".join([e.get("GenericName", ""), e.get("Keywords", "").replace(";", " "),
                              e.get("Categories", "").replace(";", " ")])
            out.append((app_id, e.get("Name", app_id), words.lower(), path))
    return out


def _norm(s):
    return re.sub(r"[^a-z0-9 ]+", "", s.lower().replace("-", " ").replace("_", " ")).strip()


def find_app(name):
    """Best matching desktop entry for a spoken name, or None."""
    want = _norm(name)
    want = re.sub(r"^(the|a|an|my)\s+", "", want)
    target = ALIASES.get(want)
    entries = desktop_entries()
    if target == "@browser":
        return default_browser(entries)
    if target:
        for e in entries:
            if e[0].lower() == target.lower() or e[0].lower().endswith(target.lower()):
                return e
    best, best_score = None, 0.0
    for e in entries:
        n = _norm(e[1])
        score = difflib.SequenceMatcher(None, want, n).ratio()
        if want == n:
            score = 2.0
        elif want and (n.startswith(want) or want in n.split()):
            score = max(score, 0.9)
        elif want and want in _norm(e[0]):
            score = max(score, 0.85)
        elif want and want in e[2].split():
            score = max(score, 0.7)
        if score > best_score:
            best, best_score = e, score
    return best if best_score >= 0.72 else None


def default_browser(entries=None):
    entries = entries or desktop_entries()
    env = session_env()
    if env:
        r = subprocess.run(["xdg-settings", "get", "default-web-browser"], env=env,
                           capture_output=True, text=True, timeout=3) if shutil.which("xdg-settings") else None
        want = r.stdout.strip()[:-8] if r and r.stdout.strip().endswith(".desktop") else ""
        for e in entries:
            if want and e[0] == want:
                return e
    for b in BROWSERS:
        for e in entries:
            if e[0].lower() == b.lower():
                return e
    for e in entries:
        if "webbrowser" in e[2].replace(" ", "") or "web browser" in e[2]:
            return e
    return None


def _exec_cmd(path, args=()):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.read(path, encoding="utf-8")
    raw = cp["Desktop Entry"].get("Exec", "")
    parts = []
    for tok in shlex.split(raw):
        if tok in ("%u", "%U", "%f", "%F"):
            parts.extend(args)
            args = ()
        elif re.fullmatch(r"%[a-zA-Z]", tok):
            continue
        else:
            parts.append(tok.replace("%%", "%"))
    return parts + list(args)


def open_app(name, args=()):
    """Open an app by spoken name. Steam pages work in Game Mode too."""
    key = re.sub(r"^(the|a|an|my)\s+", "", _norm(name))
    if key in STEAM_PLACES:
        steam_url(STEAM_PLACES[key])
        return f"Opened {key}."
    env = session_env()
    entry = find_app(name)
    if env is None:
        is_browser = ALIASES.get(key) == "@browser" or (
            entry and any(entry[0].lower() == b.lower() for b in BROWSERS))
        if is_browser:
            steam_url("steam://openurl/https://www.google.com")
            return "Opened Steam's browser."
        raise ActionError("Apps open in Desktop Mode, and you're in Game Mode right now.")
    if not entry:
        raise ActionError(f"I couldn't find an app called {name}.")
    spawn(_exec_cmd(entry[3], args), env=env)
    return f"Opened {entry[1]}."


def open_url(url):
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url):
        url = "https://" + url
    if not url.startswith(("http://", "https://", "steam://")):
        raise ActionError("I can only open web links.")
    if url.startswith("steam://"):
        steam_url(url)
        return "Opened it in Steam."
    env = session_env()
    if env is None:
        steam_url("steam://openurl/" + url)
        return "Opened it in Steam's browser."
    b = default_browser()
    spawn(_exec_cmd(b[3], [url]) if b else ["xdg-open", url], env=env)
    return "Opened it."


# -- games ---------------------------------------------------------------------
def find_game(name):
    games = context.steam_library()
    want = _norm(name)
    best, best_score = None, 0.0
    for appid, title in games.items():
        t = _norm(title)
        score = difflib.SequenceMatcher(None, want, t).ratio()
        if want == t:
            score = 2.0
        elif want and t.startswith(want):
            score = max(score, 0.92)
        elif want and want in t:
            score = max(score, 0.85)
        # Spoken initials: "half life alyx" vs "Half-Life: Alyx" already match; "hl alyx" won't.
        if score > best_score:
            best, best_score = (appid, title), score
    return best if best_score >= 0.7 else None


def launch_game(name):
    g = find_game(name)
    if not g:
        raise ActionError(f"I couldn't find {name} in your Steam library.")
    steam_url(f"steam://rungameid/{g[0]}")
    return f"Starting {g[1]}."


def quit_game():
    g = context.running_game()
    if not g:
        raise ActionError("No game is running.")
    pids = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        env = context._read(f"/proc/{pid}/environ", binary=True) or b""
        if f"SteamAppId={g[0]}\0".encode() in env + b"\0" or f"SteamGameId={g[0]}\0".encode() in env + b"\0":
            pids.append(int(pid))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    return f"Closing {g[1]}."


def list_games(query=""):
    games = sorted(context.steam_library().values())
    if query:
        q = _norm(query)
        games = [g for g in games if q in _norm(g)]
    return games


# -- audio and media ------------------------------------------------------------------
SINK = "@DEFAULT_AUDIO_SINK@"


def set_volume(percent=None, change=None, mute=None):
    if mute is not None:
        run(["wpctl", "set-mute", SINK, "toggle" if mute == "toggle" else ("1" if mute else "0")],
            env=dict(os.environ))
    if percent is not None:
        run(["wpctl", "set-volume", "-l", "1.0", SINK, f"{max(0, min(100, int(percent)))}%"],
            env=dict(os.environ))
    elif change is not None:
        c = int(change)
        run(["wpctl", "set-volume", "-l", "1.0", SINK, f"{abs(c)}%{'+' if c > 0 else '-'}"],
            env=dict(os.environ))
    v = context.volume()
    if not v:
        return "Done."
    return "Muted." if v["muted"] else f"Volume {v['percent']}."


def media(action):
    if not shutil.which("playerctl"):
        raise ActionError("Media controls need playerctl, which isn't installed.")
    cmd = {"play": "play", "pause": "pause", "toggle": "play-pause", "play_pause": "play-pause",
           "next": "next", "previous": "previous", "stop": "stop"}.get(action)
    if not cmd:
        raise ActionError("I can play, pause, skip or go back.")
    r = run(["playerctl", cmd])
    if r.returncode != 0:
        raise ActionError("Nothing is playing that I can control.")
    return "Done."


def now_playing():
    if not shutil.which("playerctl"):
        return None
    r = run(["playerctl", "metadata", "--format", "{{artist}} - {{title}} ({{status}})"])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


# -- passthrough ---------------------------------------------------------------
def passthrough(state="toggle"):
    try:
        rv = context.room_view()
        if state == "toggle":
            was = rv.flip(True)
            return "Passthrough on." if was == "off" else "Passthrough off."
        on = state in ("on", True, "true")
        rv.set(on, True)
        return "Passthrough on." if on else "Passthrough off."
    except Exception as e:
        raise ActionError("I couldn't reach Steam to switch passthrough. " + str(e)) from e


# -- seeing what you see ---------------------------------------------------------
SHOT_DIR = os.path.join(config.DATA_DIR, "screenshots")


def _newest(paths, since):
    files = [p for p in paths if os.path.getmtime(p) >= since]
    return max(files, key=os.path.getmtime) if files else None


def capture_screen(cfg=None):
    """Screenshot of what you're looking at. Returns a PNG/JPEG path."""
    cfg = cfg or config.load()
    os.makedirs(SHOT_DIR, exist_ok=True)
    out = os.path.join(SHOT_DIR, time.strftime("view-%Y%m%d-%H%M%S.png"))
    env = session_env()
    tried = []
    custom = cfg.get("screenshot_command", "")
    if custom:
        tried.append("screenshot_command")
        run(["sh", "-c", custom.replace("{out}", shlex.quote(out))], env=env or dict(os.environ), timeout=8)
        if os.path.exists(out):
            return out
    if env and shutil.which("spectacle"):
        tried.append("spectacle")
        run(["spectacle", "--background", "--nonotify", "--fullscreen", "--output", out], env=env, timeout=8)
        if os.path.exists(out):
            return out
    if env and shutil.which("grim"):
        tried.append("grim")
        run(["grim", out], env=env, timeout=8)
        if os.path.exists(out):
            return out
    if shutil.which("gamescopectl"):
        # Sees Desktop Mode too: its nested session is a window inside gamescope.
        tried.append("gamescopectl")
        # Desktop Mode's nested session moves XDG_RUNTIME_DIR, hiding gamescope's socket.
        genv = dict(os.environ, XDG_RUNTIME_DIR=f"/run/user/{os.getuid()}")
        genv.setdefault("GAMESCOPE_WAYLAND_DISPLAY", "gamescope-0")
        run(["gamescopectl", "screenshot", out], env=genv, timeout=5)
        for _ in range(20):
            time.sleep(0.1)
            if os.path.exists(out):
                time.sleep(0.2)   # let it finish writing
                return out
    if shutil.which("xprop"):
        # Game Mode: gamescope saves a screenshot when this root window property is set.
        tried.append("gamescope")
        since = time.time() - 0.5
        genv = dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":0"))
        run(["xprop", "-root", "-f", "GAMESCOPECTRL_REQUEST_SCREENSHOT", "32c",
             "-set", "GAMESCOPECTRL_REQUEST_SCREENSHOT", "1"], env=genv, timeout=3)
        for _ in range(20):
            time.sleep(0.1)
            f = _newest(glob.glob("/tmp/gamescope_*.png"), since)
            if f:
                time.sleep(0.2)   # let it finish writing
                return f
    raise ActionError("I couldn't take a screenshot here." + (f" Tried {', '.join(tried)}." if tried else ""))


def shrink(path, max_side=1280):
    """JPEG copy no larger than max_side, to keep image tokens and upload small."""
    out = os.path.splitext(path)[0] + "-small.jpg"
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            im.save(out, "JPEG", quality=80)
        return out
    except Exception:
        pass
    if shutil.which("ffmpeg"):
        r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-vf",
                            f"scale='min({max_side},iw)':-2", "-q:v", "4", out],
                           capture_output=True, timeout=10)
        if r.returncode == 0 and os.path.exists(out):
            return out
    return path


def look(cfg=None):
    shot = capture_screen(cfg)
    small = shrink(shot)
    # Keep only the newest few.
    for old in sorted(glob.glob(os.path.join(SHOT_DIR, "view-*")))[:-6]:
        try:
            os.remove(old)
        except OSError:
            pass
    return small


# -- the text panel window -------------------------------------------------------
CHROMIUMS = ("chromium", "chrome", "brave", "edge", "vivaldi")


def open_panel_window(url):
    """Open the panel page as its own app window (Desktop Mode) or in Steam's browser."""
    env = session_env()
    if env is None:
        steam_url("steam://openurl/" + url)
        return
    b = default_browser()
    if b and any(c in b[0].lower() for c in CHROMIUMS):
        flags = [f"--app={url}", "--window-size=900,1100", "--class=talk2-panel"]
        if b[3].startswith(("/var/lib/flatpak", os.path.expanduser("~/.local/share/flatpak"))):
            spawn(["flatpak", "run", b[0], *flags], env=env)
        else:
            exe = _exec_cmd(b[3])
            spawn([exe[0], *flags], env=env)
        return
    spawn(_exec_cmd(b[3], [url]) if b else ["xdg-open", url], env=env)
