"""Text-to-speech. Kokoro is the default because it sounds far more natural
than Piper; Piper is kept as a fallback for when Kokoro can't load."""

import os
import time

import numpy as np

from .log import log


class KokoroVoice:
    name = "kokoro"

    def __init__(self, models_dir, voice="af_heart", speed=1.0, threads=4,
                 model_file="kokoro-v1.0.onnx"):
        import onnxruntime as rt
        from kokoro_onnx import Kokoro
        model = os.path.join(models_dir, model_file)
        voices = os.path.join(models_dir, "voices-v1.0.bin")
        opts = rt.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        sess = rt.InferenceSession(model, sess_options=opts, providers=["CPUExecutionProvider"])
        if hasattr(Kokoro, "from_session"):
            self.k = Kokoro.from_session(sess, voices)
        else:
            self.k = Kokoro(model, voices)
        self.voice = voice if voice in self.k.get_voices() else "af_heart"
        self.lang = "en-gb" if self.voice[:1] == "b" else "en-us"
        self.speed = speed

    def voices(self):
        return self.k.get_voices()

    def synth(self, text):
        audio, sr = self.k.create(text, voice=self.voice, speed=self.speed, lang=self.lang)
        return np.asarray(audio, dtype=np.float32), sr


class PiperVoice:
    name = "piper"

    def __init__(self, models_dir, voice="en_US-amy-medium", **_):
        from piper import PiperVoice as PV
        self.v = PV.load(os.path.join(models_dir, voice + ".onnx"))

    def voices(self):
        return []

    def synth(self, text):
        if hasattr(self.v, "synthesize_stream_raw"):  # piper-tts 1.2
            raw = b"".join(self.v.synthesize_stream_raw(text))
            return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768, self.v.config.sample_rate
        chunks = list(self.v.synthesize(text))  # piper-tts 1.3+
        audio = np.concatenate([c.audio_float_array for c in chunks]) if chunks else np.zeros(1, np.float32)
        return audio.astype(np.float32), chunks[0].sample_rate if chunks else 22050


def load_voice(cfg, models_dir):
    """Load the configured engine, falling back to the other one."""
    order = ["kokoro", "piper"] if cfg["voice_engine"] != "piper" else ["piper", "kokoro"]
    for engine in order:
        try:
            t = time.time()
            if engine == "kokoro":
                v = KokoroVoice(os.path.join(models_dir, "kokoro"), cfg["voice"],
                                cfg["voice_speed"], cfg["threads"])
            else:
                v = PiperVoice(os.path.join(models_dir, "piper"), cfg["piper_voice"])
            audio, sr = v.synth("Ready.")  # warm up so the first reply isn't slow
            log(f"voice ready ({engine}, {time.time() - t:.1f}s)")
            return v
        except Exception as e:
            log(f"{engine} voice unavailable: {e}")
    raise RuntimeError("No text-to-speech voice could be loaded. Run talk2 update to reinstall.")
