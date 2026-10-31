"""Speech-to-text with faster-whisper, fully on the headset."""

import os
import re
import threading
import time

import numpy as np

from .log import log


class Transcriber:
    def __init__(self, model="base.en", models_dir=None, threads=4):
        from faster_whisper import WhisperModel
        t = time.time()
        self.model = WhisperModel(model, device="cpu", compute_type="int8",
                                  cpu_threads=threads, download_root=models_dir)
        log(f"speech recognition ready ({model}, {time.time() - t:.1f}s)")
        self.lock = threading.Lock()   # the wake check, stop words and questions share the model
        # Words Whisper should expect: helps it spell game and app names right.
        self.vocab_prompt = "Hey Claude, quick question."

    def set_vocabulary(self, words):
        words = [w for w in words if w][:40]
        self.vocab_prompt = "Hey Claude, quick question" + (
            f" about {', '.join(words)}." if words else ".")

    def transcribe(self, pcm16):
        audio = np.asarray(pcm16, dtype=np.float32) / 32768.0
        t = time.time()
        with self.lock:
            segments, _ = self.model.transcribe(
                audio, language="en", beam_size=1, vad_filter=False,
                condition_on_previous_text=False, without_timestamps=True,
                initial_prompt=self.vocab_prompt)
            text = " ".join(s.text.strip() for s in segments
                            if s.no_speech_prob < 0.8).strip()
        log(f"heard {len(audio) / 16000:.1f}s in {time.time() - t:.2f}s: {text!r}")
        return clean(strip_wake_word(text))

    def says_wake_word(self, pcm16):
        """Second opinion on a weak wake-word hit: does this audio say "Claude"?"""
        from .wakeword import WAKE_WORDS
        audio = np.asarray(pcm16, dtype=np.float32) / 32768.0
        t = time.time()
        with self.lock:
            segments, _ = self.model.transcribe(
                audio, language="en", beam_size=1, vad_filter=False,
                condition_on_previous_text=False, without_timestamps=True,
                initial_prompt="Hey Claude.")
            text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6).strip()
        ok = bool(WAKE_WORDS.search(text))
        log(f"wake check {time.time() - t:.2f}s: {text!r} -> {'yes' if ok else 'no'}")
        return ok


_LEADING_WAKE = re.compile(r"^\W*((hey|hi|hay|okay|ok|a)\W+)?(claude|claud|cloud|clod|clyde|klaud)\b\W*", re.I)


def strip_wake_word(text):
    """"Hey Claude, what's the time" -> "what's the time"."""
    stripped = _LEADING_WAKE.sub("", text, count=1)
    return stripped[:1].upper() + stripped[1:] if stripped else stripped


# Whisper's stock outputs for silence or noise.
_HALLUCINATIONS = {"", "you", "thank you", "thanks for watching", "thank you for watching",
                   "bye", ".", "okay", "hmm", "uh", "um"}


def clean(text):
    t = text.strip().strip(".").strip()
    if t.lower() in _HALLUCINATIONS or t.startswith(("[", "(")) and t.endswith(("]", ")")):
        return ""
    return text.strip()


def default_models_dir():
    return os.path.expanduser("~/.local/share/talk2/models/whisper")
