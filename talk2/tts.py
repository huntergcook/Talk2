"""Text-to-speech. Kokoro is the default because it sounds far more natural
than Piper; Piper is kept as a fallback for when Kokoro can't load."""

import os
import time

import numpy as np

from .log import log


class KokoroVoice:
    name = "kokoro"

    def __init__(self, models_dir, voice="af_heart", speed=1.0, threads=4,
                 model_file="kokoro-v1.0.int8.onnx"):
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


ROBOT_PITCH = 2 ** (4 / 12)   # 4 semitones down
ROBOT_COMB_S = 0.007          # the metal resonance
ROBOT_COMB_GAIN = 0.55
ROBOT_RING_HZ = 50            # the mechanical buzz
ROBOT_RING_MIX = 0.3


def robotize(audio, sr):
    """Deep, metallic robot voice. Same sample rate, ROBOT_PITCH times longer."""
    n = len(audio)
    if n < 2:
        return audio
    # Stretch rather than change the rate: the speaker reopens on a new rate,
    # which would cut off a chime.
    out = np.interp(np.arange(int(n * ROBOT_PITCH)) / ROBOT_PITCH, np.arange(n), audio)
    delay = int(ROBOT_COMB_S * sr)
    for i in range(delay, len(out), delay):
        block = out[i:i + delay]
        block += ROBOT_COMB_GAIN * out[i - delay:i - delay + len(block)]
    ring = np.sin(2 * np.pi * ROBOT_RING_HZ * np.arange(len(out)) / sr)
    out = (1 - ROBOT_RING_MIX) * out + ROBOT_RING_MIX * out * ring
    out = np.tanh(2 * out)
    return (0.95 * out / max(float(np.abs(out).max()), 1e-6)).astype(np.float32)


class RobotVoice:
    """Wraps a voice engine and runs its speech through robotize()."""

    def __init__(self, voice):
        self.v = voice
        self.name = voice.name
        if hasattr(voice, "speed"):
            voice.speed *= ROBOT_PITCH  # the stretch slows speech; keep the pace

    def voices(self):
        return self.v.voices()

    def synth(self, text):
        audio, sr = self.v.synth(text)
        return robotize(audio, sr), sr


def load_voice(cfg, models_dir):
    """Load the configured engine, falling back to the other one."""
    order = ["kokoro", "piper"] if cfg["voice_engine"] != "piper" else ["piper", "kokoro"]
    for engine in order:
        try:
            t = time.time()
            if engine == "kokoro":
                v = KokoroVoice(os.path.join(models_dir, "kokoro"), cfg["voice"],
                                cfg["voice_speed"], cfg["threads"], cfg["voice_model"])
            else:
                v = PiperVoice(os.path.join(models_dir, "piper"), cfg["piper_voice"])
            if cfg["voice_effect"] == "robot":
                v = RobotVoice(v)
            audio, sr = v.synth("Ready.")  # warm up so the first reply isn't slow
            log(f"voice ready ({engine}, {time.time() - t:.1f}s)")
            return v
        except Exception as e:
            log(f"{engine} voice unavailable: {e}")
    raise RuntimeError("No text-to-speech voice could be loaded. Run talk2 update to reinstall.")
