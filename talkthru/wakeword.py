"""Wake word detection with openWakeWord's ONNX models, run directly.

This follows openWakeWord's streaming feature pipeline (mel spectrogram ->
Google speech embedding -> per-word classifier) without its Python package,
whose Linux ARM dependencies don't install cleanly on SteamOS.
Audio is 16 kHz mono int16, fed in 80 ms (1280 sample) chunks.
"""

import os

import numpy as np

CHUNK = 1280            # 80 ms at 16 kHz
MEL_CONTEXT = 160 * 3   # extra samples the mel model needs at the chunk's start
MEL_WINDOW = 76         # mel frames per embedding
EMB_WINDOW = 16         # embeddings per classifier input


def _session(path):
    import onnxruntime as rt
    opts = rt.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    return rt.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])


class WakeWord:
    def __init__(self, models_dir, word="hey_jarvis_v0.1", threshold=0.5, patience=1):
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

    def process(self, chunk):
        """Feed int16 audio of any length. Returns True when the wake word fires."""
        audio = np.concatenate([self.pending, np.asarray(chunk, dtype=np.int16)])
        fired = False
        while len(audio) >= CHUNK:
            block, audio = audio[:CHUNK], audio[CHUNK:]
            self.raw = np.concatenate([self.raw, block])[-(CHUNK + MEL_CONTEXT):]
            self.mels = np.vstack([self.mels, self._melspec(self.raw)])[-(MEL_WINDOW + 20):]
            self.embs = np.vstack([self.embs, self._embedding()])[-EMB_WINDOW:]
            if len(self.embs) < EMB_WINDOW:
                continue
            score = float(self.clf.run(None, {self.clf_input: self.embs[None]})[0].ravel()[0])
            if self.warmup > 0:
                self.warmup -= 1
                score = 0.0
            self.last_score = score
            self.hits = self.hits + 1 if score >= self.threshold else 0
            if self.hits >= self.patience:
                fired = True
                self.hits = 0
                # Clear history so one utterance can't fire twice.
                self.embs = self.embs[:0]
        self.pending = audio
        return fired
