"""What's going on right now, gathered only when you ask something.

Everything here is a few file reads (no polling), so it costs nothing while
you're just playing. Each part fails quietly; missing info is left out.
"""

import glob
import json
import os
import re
import subprocess
import time

from .log import log

STEAM_DIRS = [os.path.expanduser(p) for p in (
    "~/.local/share/Steam", "~/.steam/steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")]
# Steam's own processes carry these ids; they aren't games.
NOT_GAMES = {"0", "769", "228980", "250820", "1070560", "1391110", "1628350", "1493710"}


def _read(path, binary=False):
    try:
        with open(path, "rb" if binary else "r") as f:
            return f.read()
    except OSError:
        return None


# -- Steam library ---------------------------------------------------------
_library = {"t": 0.0, "games": {}}


def steam_library():
    """{appid: name} for installed Steam apps (cached for 5 minutes)."""
    if time.time() - _library["t"] < 300:
        return _library["games"]
    roots = set()
    for d in STEAM_DIRS:
        roots.add(os.path.join(d, "steamapps"))
        vdf = _read(os.path.join(d, "steamapps", "libraryfolders.vdf")) or ""
        for p in re.findall(r'"path"\s+"([^"]+)"', vdf):
            roots.add(os.path.join(p.replace("\\\\", "\\"), "steamapps"))
    games = {}
    for root in roots:
        for acf in glob.glob(os.path.join(root, "appmanifest_*.acf")):
            text = _read(acf) or ""
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            name = re.search(r'"name"\s+"([^"]+)"', text)
            if appid and name and appid.group(1) not in NOT_GAMES:
                n = name.group(1)
                if not re.search(r"(Proton|Steam Linux Runtime|Steamworks Common|SteamVR)", n):
                    games[appid.group(1)] = n
    _library.update(t=time.time(), games=games)
    return games


def running_game():
    """(appid, name) of the game Steam is running, or None.

    Steam starts every game with SteamAppId (or SteamGameId) in its
    environment, Proton games included, so a scan of /proc finds it.
    """
    me = os.getuid()
    found = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != me:
                continue
        except OSError:
            continue
        env = _read(f"/proc/{pid}/environ", binary=True)
        if not env or b"SteamAppId=" not in env and b"SteamGameId=" not in env:
            continue
        for kv in env.split(b"\0"):
            if kv.startswith((b"SteamAppId=", b"SteamGameId=")):
                appid = kv.split(b"=", 1)[1].decode(errors="ignore")
                if appid.isdigit() and appid not in NOT_GAMES:
                    found[appid] = found.get(appid, 0) + 1
    if not found:
        return None
    appid = max(found, key=found.get)
    return appid, steam_library().get(appid) or f"Steam app {appid}"


# -- headset state -----------------------------------------------------------
def battery():
    for d in glob.glob("/sys/class/power_supply/*"):
        if (_read(os.path.join(d, "type")) or "").strip() != "Battery":
            continue
        cap = (_read(os.path.join(d, "capacity")) or "").strip()
        status = (_read(os.path.join(d, "status")) or "").strip().lower()
        if not cap.isdigit():
            continue
        info = {"percent": int(cap), "status": status}
        # Time left from the current draw, when the driver reports it.
        try:
            p_uw = _num(d, "power_now") or ((_num(d, "current_now") or 0) * (_num(d, "voltage_now") or 0) / 1e6)
            e_uwh = _num(d, "energy_now") or ((_num(d, "charge_now") or 0) * (_num(d, "voltage_now") or 0) / 1e6)
            if p_uw and e_uwh and status == "discharging":
                info["minutes_left"] = int(e_uwh / p_uw * 60)
                info["watts"] = round(p_uw / 1e6, 1)
        except (TypeError, ZeroDivisionError):
            pass
        return info
    return None


def _num(d, name):
    v = (_read(os.path.join(d, name)) or "").strip()
    return abs(float(v)) if v.lstrip("-").isdigit() else None


def volume():
    try:
        out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
                             capture_output=True, text=True, timeout=1).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Volume:\s*([\d.]+)", out)
    return {"percent": round(float(m.group(1)) * 100), "muted": "MUTED" in out} if m else None


def desktop_mode():
    return _pgrep("plasmashell") is not None


def _pgrep(name):
    try:
        out = subprocess.run(["pgrep", "-n", "-x", "-u", str(os.getuid()), name],
                             capture_output=True, text=True, timeout=1).stdout.strip()
        return int(out) if out else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


_rv = {"obj": None, "fail_until": 0.0}


def passthrough():
    """'on', 'off', or None if Steam's debug port isn't reachable."""
    if time.time() < _rv["fail_until"]:
        return None
    try:
        from .roomview import RoomView
        _rv["obj"] = _rv["obj"] or RoomView()
        return _rv["obj"].state()
    except Exception:
        _rv["fail_until"] = time.time() + 60   # don't retry on every question
        return None


def room_view():
    from .roomview import RoomView
    _rv["obj"] = _rv["obj"] or RoomView()
    _rv["fail_until"] = 0.0
    return _rv["obj"]


# -- the line sent with each question ----------------------------------------
def summary(extra=None):
    t = time.time()
    parts = [f"Local time: {time.strftime('%A %B %-d, %-I:%M %p')}"]
    try:
        g = running_game()
        parts.append(f"Playing: {g[1]} (Steam app {g[0]})" if g else "No game running")
    except Exception as e:
        log(f"game check failed: {e}")
    b = battery()
    if b:
        s = f"Battery {b['percent']}% {b['status']}"
        if "minutes_left" in b:
            s += f", about {b['minutes_left'] // 60}h{b['minutes_left'] % 60:02d}m left"
        parts.append(s)
    v = volume()
    if v:
        parts.append(f"Volume {v['percent']}%" + (" (muted)" if v["muted"] else ""))
    p = passthrough()
    if p:
        parts.append(f"Passthrough {p}")
    parts.append("Desktop Mode" if desktop_mode() else "Game Mode")
    parts += extra or []
    ms = (time.time() - t) * 1000
    if ms > 150:
        log(f"context took {ms:.0f} ms")
    return "; ".join(parts)


def status_dict():
    g = running_game()
    return {"time": time.strftime("%Y-%m-%d %H:%M"), "game": g and {"appid": g[0], "name": g[1]},
            "battery": battery(), "volume": volume(), "passthrough": passthrough(),
            "desktop_mode": desktop_mode()}


if __name__ == "__main__":
    print(summary())
    print(json.dumps(status_dict(), indent=2))
