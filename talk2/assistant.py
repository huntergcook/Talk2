"""The voice loop: wake word -> listen -> transcribe -> Claude -> speak."""

import os
import queue
import re
import signal
import sys
import threading
import time

import numpy as np

from . import config
from .audio import CHIME_CANCEL, CHIME_DONE, CHIME_LISTEN, MIC_CHUNK, MIC_RATE, Microphone, Speaker
from .claude import ClaudeError, ClaudeSession
from .log import log
from .share import MicSharing
from .speech_text import SentenceStream

CHUNK_S = MIC_CHUNK / MIC_RATE

CANCEL = re.compile(r"^\W*(never ?mind|cancel|nothing|stop|forget it|no thanks?)\W*$", re.I)
RESET = re.compile(r"\b(new conversation|start over|start a new (chat|conversation)|fresh start|clear (the )?(chat|conversation))\b", re.I)
FILLERS = ["One sec.", "Let me check.", "One moment."]


def rms(chunk):
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2))) if len(chunk) else 0.0


class Endpointer:
    """Decides when you've started and stopped talking from mic loudness.

    The headset mic sits close to your mouth, so your voice is much louder
    than the room; the threshold tracks the room's noise floor.
    """

    def __init__(self, end_silence_ms=800, max_s=30):
        self.noise = 120.0
        self.end_chunks = max(3, int(end_silence_ms / 1000 / CHUNK_S))
        self.max_chunks = int(max_s / CHUNK_S)

    def observe_idle(self, chunk):
        r = rms(chunk)
        # Rise slowly (speech shouldn't drag it up), fall quickly.
        a = 0.02 if r > self.noise else 0.2
        self.noise = max(30.0, (1 - a) * self.noise + a * r)

    def threshold(self):
        return max(self.noise * 3.0, 260.0)

    def record(self, read, start_timeout_s, preroll=()):
        """Returns int16 audio of one utterance, or None if nobody spoke."""
        thr = self.threshold()
        chunks = list(preroll)
        started = False
        voiced = silent = 0
        waited = 0
        start_chunks = int(start_timeout_s / CHUNK_S)
        while True:
            c = read()
            if c is None:
                return None
            loud = rms(c) > thr
            if not started:
                chunks = (chunks + [c])[-4:]  # keep ~0.3 s before speech starts
                waited += 1
                if loud:
                    voiced += 1
                    if voiced >= 2:
                        started, silent = True, 0
                else:
                    voiced = 0
                if not started and waited > start_chunks:
                    return None
                continue
            chunks.append(c)
            if loud:
                voiced += 1
                silent = 0
            else:
                silent += 1
            if silent >= self.end_chunks or len(chunks) >= self.max_chunks:
                break
        if voiced < 3:
            return None
        return np.concatenate(chunks[:len(chunks) - max(0, silent - 3)])


class Assistant:
    def __init__(self, cfg=None):
        self.cfg = cfg or config.load()
        c = self.cfg
        models = config.MODELS_DIR
        from .stt import Transcriber
        from .tts import load_voice
        from .wakeword import WakeWord
        self.wake = WakeWord(os.path.join(models, "wakeword"), c["wake_word"], c["wake_threshold"])
        self.stt = Transcriber(c["speech_model"], os.path.join(models, "whisper"), c["threads"])
        self.voice = load_voice(c, models)
        self.speaker = Speaker(c["speaker_device"], c["volume"])
        self.mic = Microphone(c["mic_device"])
        self.claude = ClaudeSession(model=c["claude_model"], allow_commands=c["allow_commands"],
                                    extra_prompt=c["extra_instructions"])
        self.ep = Endpointer(c["end_silence_ms"], c["max_question_s"])
        self.share = MicSharing(own_pids=self.mic.pids)
        self.filler_i = 0

    # -- helpers -----------------------------------------------------------
    def chime(self, sound):
        if self.cfg["chimes"]:
            self.speaker.resume()
            self.speaker.play(sound, 24000)

    def say(self, text, wait=True):
        self.speaker.resume()
        audio, sr = self.voice.synth(text)
        self.speaker.play(audio, sr)
        if wait:
            self.speaker.wait()

    def read_mic(self):
        return self.mic.read(timeout=2.0)

    # -- main loop ---------------------------------------------------------
    def run(self):
        # Let `talk2 stop` run cleanup (un-muting other apps' mic) instead of dying mid-question.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        self.share.restore_after_crash()
        self.mic.start()
        try:
            self.claude.start()
        except ClaudeError as e:
            log(str(e))
        log(f"listening for the wake word ({self.cfg['wake_word']})")
        while True:
            chunk = self.mic.read(timeout=1.0)
            self._maybe_reset_conversation()
            if chunk is None:
                continue
            self.ep.observe_idle(chunk)
            if self.wake.process(chunk):
                log(f"wake word ({self.wake.last_score:.2f})")
                try:
                    self.converse()
                except Exception as e:  # keep the service alive no matter what
                    log(f"conversation error: {e!r}")
                    self.speaker.stop()
                self.wake.reset()
                self.mic.flush()

    def _maybe_reset_conversation(self):
        idle_min = self.cfg["new_conversation_after_min"]
        s = self.claude
        if idle_min and s.proc and s.turns and not s.busy and time.time() - s.last_used > idle_min * 60:
            log("starting a fresh conversation after being idle")
            s.restart()

    def converse(self):
        """One exchange, plus follow-ups. Other apps' mic access is untouched
        unless keep_questions_private is on, and then always restored."""
        muted = []
        if self.cfg["keep_questions_private"]:
            muted = self.share.mute_others()
        try:
            self._converse()
        finally:
            self.share.unmute(muted)

    def _follow_up_allowed(self):
        if not self.cfg["follow_up_s"]:
            return False
        if self.cfg["follow_up_when_shared"] or self.cfg["keep_questions_private"]:
            return True
        # While the mic is shared (e.g. Discord over SteamVR streaming), what you
        # say next is probably meant for your friends, not Claude.
        return not self.share.shared()

    def _converse(self):
        self.speaker.stop()
        # No flush here: people often say the question in the same breath as
        # the wake word, and that audio is already queued.
        self.chime(CHIME_LISTEN)
        audio = self.ep.record(self.read_mic, start_timeout_s=5)
        while True:
            if audio is None:
                self.chime(CHIME_CANCEL)
                return
            self.chime(CHIME_DONE)
            text = self.stt.transcribe(audio)
            if not text or CANCEL.match(text):
                self.chime(CHIME_CANCEL)
                return
            if RESET.search(text) and len(text.split()) <= 6:
                self.claude.restart()
                self.say("Okay, fresh start.")
                return
            interrupted = self.respond(text)
            if not interrupted:
                self.mic.flush()  # don't hear the end of our own reply as a follow-up
            if interrupted:
                self.chime(CHIME_LISTEN)
                audio = self.ep.record(self.read_mic, start_timeout_s=5)
                continue
            if not self._follow_up_allowed():
                return
            audio = self.ep.record(self.read_mic, start_timeout_s=self.cfg["follow_up_s"])
            if audio is None:
                return  # no follow-up: back to waiting quietly

    def respond(self, text):
        """Stream Claude's reply into speech. Returns True if interrupted."""
        interrupted = threading.Event()
        done_streaming = threading.Event()
        sentences = queue.Queue()
        self.speaker.resume()

        def speak_worker():
            while True:
                s = sentences.get()
                if s is None or interrupted.is_set():
                    return
                try:
                    audio, sr = self.voice.synth(s)
                except Exception as e:
                    log(f"voice failed on {s!r}: {e}")
                    continue
                if not interrupted.is_set():
                    self.speaker.play(audio, sr)

        stop_barge_in = threading.Event()

        def barge_in_worker():
            self.wake.reset()
            self.mic.flush()
            while not stop_barge_in.is_set():
                c = self.mic.read(timeout=0.1)
                if c is not None and self.wake.process(c):
                    log("interrupted by wake word")
                    interrupted.set()
                    self.speaker.stop()
                    return

        speaker_thread = threading.Thread(target=speak_worker, daemon=True)
        speaker_thread.start()
        barge_in = threading.Thread(target=barge_in_worker, daemon=True)
        if self.cfg["interrupt_with_wake_word"]:
            barge_in.start()

        ss = SentenceStream()
        spoken_any = False
        t0 = time.time()
        stamped = f"(Local time: {time.strftime('%A %-I:%M %p')})\n{text}"
        try:
            for kind, val in self.claude.ask(stamped, cancelled=interrupted.is_set):
                if kind == "text":
                    for s in ss.feed(val):
                        if not spoken_any:
                            log(f"first words after {time.time() - t0:.1f}s")
                        spoken_any = True
                        sentences.put(s)
                elif kind == "tool" and not spoken_any:
                    sentences.put(FILLERS[self.filler_i % len(FILLERS)])
                    self.filler_i += 1
                    spoken_any = True
            for s in ss.flush():
                sentences.put(s)
        except ClaudeError as e:
            sentences.put(str(e))
        finally:
            sentences.put(None)
            done_streaming.set()
        while speaker_thread.is_alive() and not interrupted.is_set():
            speaker_thread.join(0.05)
        self.speaker.wait(cancelled=interrupted.is_set)
        stop_barge_in.set()
        if barge_in.is_alive():
            barge_in.join()  # only one reader of the mic at a time
        self.wake.reset()
        if interrupted.is_set():
            self.speaker.stop()
            self.speaker.resume()
        return interrupted.is_set()
