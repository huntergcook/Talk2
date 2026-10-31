"""Microphone capture and speaker playback through PipeWire's command-line tools.

Using parecord/pw-record as subprocesses avoids compiled audio libraries in
the virtualenv; SteamOS ships PipeWire with its PulseAudio compatibility layer.

The mic is always opened as an ordinary PipeWire stream, never as the raw ALSA
device, so it is shared: SteamVR streaming (and Discord on the PC through it)
keeps getting the same mic while talk2 listens.
"""

import queue
import shutil
import subprocess
import threading
import time

import numpy as np

from .log import log

MIC_RATE = 16000
MIC_CHUNK = 1280  # 80 ms


APP_NAME = "talk2"
_PW_PROPS = ["-P", "{ application.name = \"talk2\" }"]


def _record_cmds(device):
    # No arecord fallback: on a raw hw: device ALSA can take the mic exclusively.
    pulse_dev = [f"--device={device}"] if device else []
    pw_dev = ["--target", device] if device else []
    return [
        ["parecord", "--raw", "--format=s16le", f"--rate={MIC_RATE}", "--channels=1",
         "--latency-msec=40", f"--client-name={APP_NAME}", *pulse_dev],
        ["pw-record", "--raw", "--rate", str(MIC_RATE), "--channels", "1",
         "--format", "s16", "--latency", "40ms", *_PW_PROPS, *pw_dev, "-"],
    ]


def _play_cmds(rate, device):
    pulse_dev = [f"--device={device}"] if device else []
    pw_dev = ["--target", device] if device else []
    return [
        ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={rate}", "--channels=1",
         "--latency-msec=60", f"--client-name={APP_NAME}", *pulse_dev],
        ["pw-play", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16",
         "--latency", "60ms", *_PW_PROPS, *pw_dev, "-"],
    ]


def _first_available(cmds):
    return [c for c in cmds if shutil.which(c[0])]


class Microphone:
    """Reads 80 ms int16 chunks from the default (or named) source into a queue."""

    def __init__(self, device=""):
        self.device = device
        self.q = queue.Queue(maxsize=200)
        self.proc = None
        self.running = False

    def start(self):
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.running = False
        if self.proc:
            self.proc.kill()

    def _run(self):
        cmds = _first_available(_record_cmds(self.device))
        if not cmds:
            log("no recorder found (need parecord or pw-record from PipeWire)")
            return
        i = 0
        while self.running:
            cmd = cmds[i % len(cmds)]
            started = time.time()
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                log(f"microphone: {cmd[0]}")
                nbytes = MIC_CHUNK * 2
                silent = 0
                while self.running:
                    data = self.proc.stdout.read(nbytes)
                    if not data or len(data) < nbytes:
                        break
                    chunk = np.frombuffer(data, dtype=np.int16)
                    # Exact digital silence for a long stretch means we're
                    # getting no real audio (muted, or the wrong input).
                    silent = silent + 1 if not chunk.any() else 0
                    if silent == int(15 / 0.08):
                        log("the microphone has sent only silence for 15 s: is it muted, "
                            "or is another input the default? Try: talk2 mic-test")
                    try:
                        self.q.put_nowait(chunk)
                    except queue.Full:  # we fell behind; drop the oldest audio
                        self.q.get_nowait()
                        self.q.put_nowait(chunk)
                err = self.proc.stderr.read().decode(errors="replace").strip() if self.proc.poll() is not None else ""
                self.proc.kill()
                if err:
                    log(f"{cmd[0]}: {err[:200]}")
            except OSError as e:
                log(f"{cmd[0]} failed: {e}")
            if not self.running:
                break
            if time.time() - started < 2:
                i += 1  # died at once: try the next tool
            time.sleep(0.5)

    def pids(self):
        p = self.proc
        return [p.pid] if p and p.poll() is None else []

    def read(self, timeout=1.0):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return


class Speaker:
    """Plays mono float32 or int16 audio; stop() cuts it off immediately."""

    def __init__(self, device="", volume=1.0):
        self.device = device
        self.volume = volume
        self.lock = threading.Lock()
        self.proc = None
        self.rate = None
        self.ends_at = 0.0
        self.stopped = threading.Event()

    def _ensure(self, rate):
        if self.proc and self.proc.poll() is None and self.rate == rate:
            return True
        self._close()
        for cmd in _first_available(_play_cmds(rate, self.device)):
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.rate = rate
                return True
            except OSError:
                continue
        log("no player found (need pacat or pw-play from PipeWire)")
        return False

    def play(self, audio, rate):
        """Queue audio; returns once it's handed to the player (not when heard)."""
        if self.stopped.is_set():
            return
        if audio.dtype != np.int16:
            audio = (np.clip(audio * self.volume, -1, 1) * 32767).astype(np.int16)
        with self.lock:
            if not self._ensure(rate):
                return
            now = time.time()
            self.ends_at = max(self.ends_at, now) + len(audio) / rate
            try:
                # Write in small pieces so stop() can cut in between them.
                step = rate // 10
                for i in range(0, len(audio), step):
                    if self.stopped.is_set():
                        break
                    self.proc.stdin.write(audio[i:i + step].tobytes())
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                self._close()

    def wait(self, cancelled=lambda: False):
        """Block until queued audio has finished playing."""
        while time.time() < self.ends_at + 0.08:
            if cancelled() or self.stopped.is_set():
                return
            time.sleep(0.02)

    def playing(self):
        return time.time() < self.ends_at

    def stop(self):
        """Cut playback now. Call resume() before playing again."""
        self.stopped.set()
        with self.lock:
            self._close()
            self.ends_at = 0.0

    def resume(self):
        self.stopped.clear()

    def _close(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
            self.proc = None


def tone(freqs, ms=90, rate=24000, volume=0.25):
    """A short soft chime: one note per frequency, back to back."""
    out = []
    for f in freqs:
        n = int(rate * ms / 1000)
        t = np.arange(n) / rate
        env = np.minimum(1, np.minimum(t / 0.008, (n / rate - t) / 0.04))
        note = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t)
        out.append(note * env * volume / 1.25)
    return np.concatenate(out).astype(np.float32)


CHIME_LISTEN = tone([660, 990])      # rising: I'm listening
CHIME_DONE = tone([880, 660], ms=70)  # falling: got it
CHIME_CANCEL = tone([440, 330], ms=110)
CHIME_END = tone([660, 495, 330], ms=130)  # slow fall: the conversation is over


# -- echo cancellation -----------------------------------------------------------
EC_SOURCE = "talk2_mic"   # the mic with talk2's own voice removed
EC_SINK = "talk2_out"     # where talk2 plays, so the canceller knows what to remove


class EchoCancel:
    """PipeWire's WebRTC echo canceller, just for talk2.

    Loads module-echo-cancel with a private sink (talk2 plays into it) and a
    private source (talk2 listens to it). Everything else — your default mic,
    Discord, SteamVR streaming, game audio — keeps using the real devices, and
    the defaults are put back in case PipeWire switched them.
    """

    def __init__(self):
        self.module = None

    @staticmethod
    def _pactl(*args, timeout=4):
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=timeout)

    def start(self):
        if not shutil.which("pactl"):
            return False
        try:
            self._unload_stale()
            src = self._pactl("get-default-source").stdout.strip()
            sink = self._pactl("get-default-sink").stdout.strip()
            if not src or not sink:
                return False
            r = self._pactl("load-module", "module-echo-cancel", "aec_method=webrtc",
                            f"source_name={EC_SOURCE}", f"sink_name={EC_SINK}",
                            f"source_master={src}", f"sink_master={sink}",
                            "source_properties=device.description=talk2-echo-cancelled-mic",
                            "sink_properties=device.description=talk2-voice")
            if r.returncode != 0 or not r.stdout.strip().isdigit():
                log(f"echo cancellation unavailable: {(r.stderr or r.stdout).strip()[:200]}")
                return False
            self.module = r.stdout.strip()
            time.sleep(0.3)
            # Keep everyone else on the real devices.
            if self._pactl("get-default-source").stdout.strip() != src:
                self._pactl("set-default-source", src)
            if self._pactl("get-default-sink").stdout.strip() != sink:
                self._pactl("set-default-sink", sink)
            log(f"echo cancellation on (module {self.module}, mic {src})")
            return True
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"echo cancellation unavailable: {e}")
            return False

    def _unload_stale(self):
        """Remove a canceller left behind by a crashed talk2."""
        out = self._pactl("list", "short", "modules").stdout
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[1] == "module-echo-cancel" and EC_SOURCE in parts[2]:
                self._pactl("unload-module", parts[0])

    def stop(self):
        if self.module:
            try:
                self._pactl("unload-module", self.module)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self.module = None
