"""`talk2 enroll`: teach the wake word your voice.

Records you saying "Hey Claude" a few times plus some ordinary talking, then
trains a small personal model on openWakeWord's speech features. Recordings
are kept, so each enroll adds to the last (do one in a noisy game later).
"""

import glob
import os
import time

import numpy as np

from . import config
from .log import log
from .wakeword import RATE, PersonalModel, WakeWord

ENROLL_DIR = os.path.join(config.DATA_DIR, "enroll")
PERSONAL_PATH = os.path.join(config.MODELS_DIR, "wakeword", "personal.npz")

PROMPTS = ["Say it like you normally would.", "Again, normally.", "One more, normally.",
           "Now a little quieter.", "Quiet again.", "Now a bit faster, like you're busy.",
           "Faster again.", "Last one, any way you like."]


def _frames(ww, audio):
    """All (end_time_s, base_score, features) for audio fed in 80 ms steps."""
    ww.reset()
    out = []
    pos = 0
    for i in range(0, len(audio) - 1279, 1280):
        for score, feats in ww.frames(audio[i:i + 1280]):
            pos = (i + 1280) / RATE
            out.append((pos, score, feats))
    return out


def _augment(clip, noise, rng):
    """Louder, quieter and noisier versions of a clip."""
    versions = [clip]
    for gain in (0.5, 1.6):
        versions.append(np.clip(clip.astype(np.float32) * gain, -32768, 32767).astype(np.int16))
    loud_noise = rng.normal(0, max(200.0, float(np.std(noise)) * 3), len(clip))
    versions.append(np.clip(clip + loud_noise, -32768, 32767).astype(np.int16))
    return versions


def build_dataset(ww, positives, negative_audio, noise, rng=None):
    """Returns (pos_feats, neg_feats). positives: list of int16 clips, trimmed to the speech."""
    rng = rng or np.random.default_rng(0)
    noise = noise if len(noise) >= RATE else np.zeros(RATE, np.int16)
    pad = noise[:RATE]          # 1 s of your room before each clip
    tail = noise[:int(0.6 * RATE)]
    pos, neg = [], []
    for clip in positives:
        for v in _augment(clip, noise, rng):
            audio = np.concatenate([pad, v, tail])
            end = (len(pad) + len(v)) / RATE - 0.15      # roughly where "Claude" ends
            for t, _, f in _frames(ww, audio):
                if end - 0.1 <= t <= end + 0.45:
                    pos.append(f)
                elif t < end - 0.6:
                    neg.append(f)                         # just "hey", or room noise
    for t, _, f in _frames(ww, np.concatenate([noise, negative_audio, noise])):
        neg.append(f)
    return np.array(pos), np.array(neg)


def train(ww, positives, negative_audio, noise):
    pos, neg = build_dataset(ww, positives, negative_audio, noise)
    model = PersonalModel.train(pos, neg)
    # How well does it do on what it learned from?
    hits = 0
    for clip in positives:
        audio = np.concatenate([noise[:RATE], clip, noise[:int(0.6 * RATE)]])
        if max(model(f) for _, _, f in _frames(ww, audio)) >= 0.5:
            hits += 1
    neg_scores = [model(f) for _, _, f in _frames(ww, negative_audio)] or [0.0]
    return model, hits, float(max(neg_scores)), len(pos), len(neg)


def saved_recordings():
    pos = [np.load(p) for p in sorted(glob.glob(os.path.join(ENROLL_DIR, "pos-*.npy")))]
    neg = [np.load(p) for p in sorted(glob.glob(os.path.join(ENROLL_DIR, "neg-*.npy")))]
    noise = [np.load(p) for p in sorted(glob.glob(os.path.join(ENROLL_DIR, "noise-*.npy")))]
    return pos, neg, noise


def run(cfg):
    from .assistant import Endpointer
    from .audio import CHIME_DONE, CHIME_LISTEN, Microphone, Speaker
    from .tts import load_voice

    os.makedirs(ENROLL_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    voice = load_voice(cfg, config.MODELS_DIR)
    sp = Speaker(cfg["speaker_device"], cfg["volume"])
    mic = Microphone(cfg["mic_device"])
    mic.start()
    ep = Endpointer(end_silence_ms=500, max_s=4)

    def say(text):
        print(text)
        sp.play(*voice.synth(text))
        sp.wait()
        time.sleep(0.15)
        mic.flush()

    def record_for(seconds):
        chunks, end = [], time.time() + seconds
        while time.time() < end:
            c = mic.read(timeout=1.0)
            if c is not None:
                chunks.append(c)
        return np.concatenate(chunks) if chunks else np.zeros(0, np.int16)

    say("Let's learn your voice. First, stay quiet for two seconds.")
    noise = record_for(2.0)
    if not len(noise):
        say("I'm not getting any sound from the microphone. Try talk2 mic-test.")
        return 1
    for c in np.array_split(noise, max(1, len(noise) // 1280)):
        ep.observe_idle(c)
    np.save(os.path.join(ENROLL_DIR, f"noise-{stamp}.npy"), noise)

    say("Now, after each beep, say Hey Claude.")
    got = 0
    for i, prompt in enumerate(PROMPTS):
        say(prompt)
        sp.play(CHIME_LISTEN, 24000)
        sp.wait()
        mic.flush()
        clip = ep.record(lambda: mic.read(timeout=2.0), start_timeout_s=4)
        if clip is None:
            say("I didn't hear that one, let's keep going.")
            continue
        sp.play(CHIME_DONE, 24000)
        np.save(os.path.join(ENROLL_DIR, f"pos-{stamp}-{i}.npy"), clip)
        got += 1
    if got < 4:
        say("I didn't catch enough of those. Let's try again later.")
        return 1

    say("Now three things that start with hey, but aren't my name. Like hey guys, or hey what's up.")
    for i in range(3):
        sp.play(CHIME_LISTEN, 24000)
        sp.wait()
        mic.flush()
        clip = ep.record(lambda: mic.read(timeout=2.0), start_timeout_s=4)
        if clip is not None:
            sp.play(CHIME_DONE, 24000)
            np.save(os.path.join(ENROLL_DIR, f"neg-{stamp}-hey{i}.npy"), clip)

    say("Great. Last part: just talk normally for about twenty seconds, about anything at all. "
        "Go ahead after the beep.")
    sp.play(CHIME_LISTEN, 24000)
    sp.wait()
    mic.flush()
    talk = record_for(20.0)
    sp.play(CHIME_DONE, 24000)
    np.save(os.path.join(ENROLL_DIR, f"neg-{stamp}.npy"), talk)
    mic.stop()

    print("Training...")
    pos, neg, noises = saved_recordings()
    ww = WakeWord(os.path.join(config.MODELS_DIR, "wakeword"), cfg["wake_word"])
    model, hits, worst_neg, npos, nneg = train(ww, pos, np.concatenate(neg), np.concatenate(noises))
    model.save(PERSONAL_PATH)
    log(f"personal wake model: {npos} positive / {nneg} negative windows; "
        f"caught {hits}/{len(pos)}; highest score while just talking {worst_neg:.2f}")
    msg = f"All set. I recognised {hits} of your {len(pos)} recordings."
    if worst_neg >= 0.5:
        msg += " I might wake up a bit easily, so tell me if that happens."
    say(msg)
    return 0
