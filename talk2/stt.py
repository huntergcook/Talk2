"""Speech-to-text with faster-whisper, fully on the headset."""

import os
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

    def transcribe(self, pcm16):
        audio = np.asarray(pcm16, dtype=np.float32) / 32768.0
        t = time.time()
        segments, _ = self.model.transcribe(
            audio, language="en", beam_size=1, vad_filter=False,
            condition_on_previous_text=False, without_timestamps=True,
            initial_prompt="Hey Claude, quick question.")
        text = " ".join(s.text.strip() for s in segments
                        if s.no_speech_prob < 0.8).strip()
        log(f"heard {len(audio) / 16000:.1f}s in {time.time() - t:.2f}s: {text!r}")
        return clean(text)


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
