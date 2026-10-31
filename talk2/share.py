"""Notice other apps using the mic, and optionally mute them for a moment.

When SteamVR streams to a PC, the headset mic is also being captured for the
PC (that's how Discord on the PC hears you). PipeWire lets both apps read the
mic at once; this module just tells talk2 when that's happening, so it can
avoid treating your Discord conversation as a follow-up question, and, if you
turn it on, keep your questions to Claude out of Discord.
"""

import json
import os
import subprocess
import time

from .audio import APP_NAME
from .log import log

MUTED_FILE = os.path.expanduser("~/.local/share/talk2/muted-streams.json")


def _pactl_json(*args):
    out = subprocess.run(["pactl", "-f", "json", *args], capture_output=True,
                         text=True, timeout=3)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "pactl failed")
    return json.loads(out.stdout or "[]")


def other_mic_streams(source_outputs=None, sources=None, own_pids=()):
    """Capture streams from real microphones that belong to other apps.

    Returns [(stream_index, app_name, muted)]. Monitor sources (recording
    what's playing, e.g. a screen recorder) aren't mics and are ignored.
    """
    if source_outputs is None:
        source_outputs = _pactl_json("list", "source-outputs")
    if sources is None:
        sources = _pactl_json("list", "sources")
    monitors = {s.get("index") for s in sources
                if str(s.get("name", "")).endswith(".monitor")
                or s.get("monitor_of_sink") not in (None, "", "n/a")}
    own = {str(p) for p in own_pids}
    found = []
    for so in source_outputs:
        props = so.get("properties", {}) or {}
        name = props.get("application.name") or props.get("node.name") or "unknown app"
        if name == APP_NAME or props.get("application.process.id") in own:
            continue
        if str(props.get("node.virtual")).lower() == "true":
            continue  # SteamOS's own mic filters (eq_capture, ns_capture), not an app
        if so.get("source") in monitors or so.get("corked"):
            continue  # not a mic, or paused and not hearing anything
        found.append((so.get("index"), name, bool(so.get("mute"))))
    return found


class MicSharing:
    def __init__(self, own_pids=lambda: ()):
        self.own_pids = own_pids
        self.cache = (0.0, [])
        self.warned = False
        self.last_names = None

    def others(self):
        """Other apps using the mic right now (cached for 2 s)."""
        t, val = self.cache
        if time.time() - t < 2:
            return val
        try:
            val = other_mic_streams(own_pids=self.own_pids())
        except Exception as e:
            if not self.warned:
                log(f"can't see other mic users ({e}); assuming the mic isn't shared")
                self.warned = True
            val = []
        names = sorted({n for _, n, _ in val})
        if names != self.last_names:
            log(f"mic also used by: {', '.join(names)}" if names else "mic not shared with other apps")
            self.last_names = names
        self.cache = (time.time(), val)
        return val

    def shared(self):
        """Is another app hearing the mic right now?"""
        return any(not muted for _, _, muted in self.others())

    # -- muting other apps while you talk to Claude ---------------------------
    def mute_others(self):
        """Mute other apps' mic streams. Returns the ones we muted."""
        muted = []
        for idx, name, already in self.others():
            if already:
                continue  # leave streams the user muted alone, and don't unmute them later
            if subprocess.run(["pactl", "set-source-output-mute", str(idx), "1"],
                              capture_output=True, timeout=3).returncode == 0:
                muted.append(idx)
        if muted:
            self._remember(muted)
            log(f"muted mic for other apps: {muted}")
        return muted

    def unmute(self, idxs):
        for idx in idxs:
            subprocess.run(["pactl", "set-source-output-mute", str(idx), "0"],
                           capture_output=True, timeout=3)
        if idxs:
            log(f"unmuted mic for other apps: {idxs}")
        self._remember([])

    def restore_after_crash(self):
        """If talk2 died mid-question, un-mute whatever it had muted."""
        try:
            with open(MUTED_FILE) as f:
                idxs = json.load(f)
        except (OSError, ValueError):
            return
        if idxs:
            log("restoring mic for other apps after an unclean exit")
            self.unmute(idxs)

    @staticmethod
    def _remember(idxs):
        try:
            os.makedirs(os.path.dirname(MUTED_FILE), exist_ok=True)
            with open(MUTED_FILE, "w") as f:
                json.dump(idxs, f)
        except OSError:
            pass
