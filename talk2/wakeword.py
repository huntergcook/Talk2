"""Wake word detection with openWakeWord ONNX models, run directly.

This follows openWakeWord's streaming feature pipeline (mel spectrogram ->
Google speech embedding -> per-word classifier) without its Python package,
whose Linux ARM dependencies don't install cleanly on SteamOS.
Audio is 16 kHz mono int16, fed in 80 ms (1280 sample) chunks.

"Hey Claude" uses three layers so it catches you every time without waking
on its own:
  1. the openWakeWord model, at a sensitive threshold;
  2. an optional personal model trained on your voice (`talk2 enroll`);
  3. a speech-recognition check of weak hits ("did that say Claude?").
"""

import os
import re
import time
from collections import deque

import numpy as np

from .log import log

CHUNK = 1280            # 80 ms at 16 kHz
MEL_CONTEXT = 160 * 3   # extra samples the mel model needs at the chunk's start
MEL_WINDOW = 76         # mel frames per embedding
EMB_WINDOW = 16         # embeddings per classifier input
RATE = 16000


def _session(path):
    import onnxruntime as rt
    opts = rt.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    return rt.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])


class WakeWord:
    """The openWakeWord pipeline. frames() yields (score, features) every 80 ms."""

    def __init__(self, models_dir, word="hey_claude", threshold=0.5, patience=1):
        self.mel = _session(os.path.join(models_dir, "melspectrogram.onnx"))
        self.emb = _session(os.path.join(models_dir, "embedding_model.onnx"))
        path = word if os.path.isabs(word) else os.path.join(models_dir, word + ".onnx")
        self.clf = _session(path)
        self.clf_input = self.clf.get_inputs()[0].name
        self.threshold = threshold
        self.patience = max(1, int(patience))
        self.reset()

    def reset(self):
        self.raw = np.zeros(CHUNK + MEL_CONTEXT, dtype=np.int16)
        self.pending = np.empty(0, dtype=np.int16)
        self.mels = np.ones((MEL_WINDOW, 32), dtype=np.float32)
        self.embs = np.zeros((0, 96), dtype=np.float32)
        self.hits = 0
        self.warmup = 5  # openWakeWord ignores the first few scores
        self.last_score = 0.0

    def _melspec(self, samples):
        x = samples.astype(np.float32)[None, :]
        out = self.mel.run(None, {"input": x})[0]
        return np.squeeze(out).reshape(-1, 32) / 10.0 + 2.0

    def _embedding(self):
        window = self.mels[-MEL_WINDOW:][None, :, :, None].astype(np.float32)
        return self.emb.run(None, {"input_1": window})[0].reshape(1, 96)

    def frames(self, chunk):
        """Feed int16 audio of any length; yields (score, features[16, 96]) per 80 ms."""
        audio = np.concatenate([self.pending, np.asarray(chunk, dtype=np.int16)])
        while len(audio) >= CHUNK:
            block, audio = audio[:CHUNK], audio[CHUNK:]
            self.raw = np.concatenate([self.raw, block])[-(CHUNK + MEL_CONTEXT):]
            self.mels = np.vstack([self.mels, self._melspec(self.raw)])[-(MEL_WINDOW + 20):]
            self.embs = np.vstack([self.embs, self._embedding()])[-EMB_WINDOW:]
            if len(self.embs) < EMB_WINDOW:
                continue
            feats = self.embs.copy()
            score = float(self.clf.run(None, {self.clf_input: feats[None]})[0].ravel()[0])
            if self.warmup > 0:
                self.warmup -= 1
                score = 0.0
            self.last_score = score
            yield score, feats
        self.pending = audio

    def process(self, chunk):
        """Simple single-threshold detection (used by tests and mic-test)."""
        fired = False
        for score, _ in self.frames(chunk):
            self.hits = self.hits + 1 if score >= self.threshold else 0
            if self.hits >= self.patience:
                fired = True
                self.hits = 0
                self.embs = self.embs[:0]
        return fired


class PersonalModel:
    """Logistic regression on the 16x96 speech features, trained on your voice."""

    def __init__(self, mean, std, w, b):
        self.mean, self.std, self.w, self.b = mean, std, w, float(b)

    @classmethod
    def load(cls, path):
        d = np.load(path)
        return cls(d["mean"], d["std"], d["w"], d["b"])

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        np.savez(path, mean=self.mean, std=self.std, w=self.w, b=self.b)

    def __call__(self, feats):
        x = (feats.reshape(-1) - self.mean) / self.std
        return float(1 / (1 + np.exp(-np.clip(x @ self.w + self.b, -30, 30))))

    @classmethod
    def train(cls, pos, neg, l2=1.0, steps=600, lr=0.2):
        """pos, neg: arrays of shape [n, 16, 96]. Class-balanced, L2-regularised."""
        X = np.concatenate([pos, neg]).reshape(len(pos) + len(neg), -1).astype(np.float64)
        y = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))])
        mean, std = X.mean(0), X.std(0) + 1e-3
        X = (X - mean) / std
        wts = np.where(y == 1, 0.5 / len(pos), 0.5 / len(neg))
        w, b = np.zeros(X.shape[1]), 0.0
        for _ in range(steps):
            p = 1 / (1 + np.exp(-np.clip(X @ w + b, -30, 30)))
            g = (p - y) * wts
            w -= lr * (X.T @ g + l2 * w / len(y))
            b -= lr * g.sum()
        return cls(mean.astype(np.float32), std.astype(np.float32), w.astype(np.float32), b)


WAKE_WORDS = re.compile(
    r"\b(claude|claud|clawed|cloud|clod|clyde|klaud|klod|clode|claws?|clause|clot|clued|cloe|chloe|claudia)\b",
    re.I)


class WakeDetector:
    """Combines the base model, your personal model and a speech-recognition check.

    Strong hits fire at once. Weak hits are held for up to ~0.3 s while the
    score peaks, then the last ~1.8 s of audio is transcribed and must contain
    "Claude" (or a common mishearing of it).
    """

    def __init__(self, ww, confirm=None, personal=None, low=0.15, high=0.5,
                 personal_low=0.5, personal_high=0.85):
        self.ww = ww
        self.confirm = confirm          # fn(int16 audio) -> bool, or None
        self.personal = personal        # PersonalModel or None
        self.low, self.high = low, high
        self.personal_low, self.personal_high = personal_low, personal_high
        self.ring = deque(maxlen=int(2.0 * RATE / CHUNK) + 1)
        self.reset()

    def reset(self):
        self.ww.reset()
        self.ring.clear()
        self.cooldown_until = 0.0
        self.pending = None   # [frames_waited, best_combined]
        self.last = {"base": 0.0, "personal": None}
        self.near_miss = 0.0

    def _audio(self, seconds=1.8):
        chunks = list(self.ring)
        audio = np.concatenate(chunks) if chunks else np.zeros(0, np.int16)
        return audio[-int(seconds * RATE):]

    def process(self, chunk, now=None):
        """Feed mic audio; returns True when the wake word is detected."""
        now = time.monotonic() if now is None else now
        self.ring.append(np.asarray(chunk, dtype=np.int16))
        fired = False
        for base, feats in self.ww.frames(chunk):
            p = self.personal(feats) if self.personal is not None else None
            self.last = {"base": base, "personal": p}
            if fired or now < self.cooldown_until:
                continue
            # Your personal model alone can't skip the speech check: it may like any "hey ...".
            strong = base >= self.high or (p is not None and p >= self.personal_high and base >= 0.25)
            weak = base >= self.low or (p is not None and p >= self.personal_low)
            if strong:
                fired = True
                continue
            if weak:
                level = max(base / self.low, (p or 0) / self.personal_low)
                if self.pending is None:
                    self.pending = [0, level]
                else:
                    self.pending[0] += 1
                    # Keep waiting while the score is still climbing, up to 4 frames.
                    if level > self.pending[1] and self.pending[0] < 4:
                        self.pending[1] = level
                        continue
                if self.pending[0] < 2:
                    continue
            elif self.pending is None:
                if 0.04 <= base < self.low:
                    self.near_miss = max(self.near_miss, base)
                continue
            # Decide on the held weak hit.
            self.pending = None
            ok = self.confirm(self._audio()) if self.confirm else self.last["base"] >= self.low
            if ok:
                fired = True
            else:
                self.cooldown_until = now + 0.8  # same utterance: don't re-check every frame
        if fired:
            self.ww.embs = self.ww.embs[:0]   # one utterance can't fire twice
            self.pending = None
            self.cooldown_until = now + 1.0
        return fired
