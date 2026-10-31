"""python -m talk2 <command>

  run              start the voice assistant (the service runs this)
  say TEXT         speak TEXT with the current voice
  ask TEXT         send TEXT to Claude and speak the reply (no microphone)
  voices           list the Kokoro voices
  demo-voices      say a sample sentence in a few good voices
  set KEY VALUE    change a setting, e.g. `set voice am_michael`
  settings         show the current settings
  mic-test         show live mic level and wake word score for 15 seconds
  mic-users        list other apps using the mic
  unmute           un-mute other apps' mic streams talk2 muted (run on service stop)
"""

import json
import sys
import time

from . import config


def _voice(cfg):
    from .tts import load_voice
    return load_voice(cfg, config.MODELS_DIR)


def _speaker(cfg):
    from .audio import Speaker
    return Speaker(cfg["speaker_device"], cfg["volume"])


def main(argv):
    cfg = config.load()
    cmd = argv[0] if argv else "run"
    rest = argv[1:]

    if cmd == "run":
        config.ensure_file()
        from .assistant import Assistant
        Assistant(cfg).run()

    elif cmd == "say":
        v, sp = _voice(cfg), _speaker(cfg)
        t = time.time()
        audio, sr = v.synth(" ".join(rest) or "Hi! I'm ready when you are.")
        print(f"made {len(audio) / sr:.1f}s of speech in {time.time() - t:.2f}s")
        sp.play(audio, sr)
        sp.wait()

    elif cmd == "ask":
        from .claude import ClaudeSession
        from .speech_text import SentenceStream
        v, sp = _voice(cfg), _speaker(cfg)
        s = ClaudeSession(model=cfg["claude_model"], allow_commands=cfg["allow_commands"],
                          make_tools=cfg["make_tools"],
                          extra_prompt=cfg["extra_instructions"])
        ss = SentenceStream()
        for kind, val in s.ask(" ".join(rest)):
            if kind == "text":
                print(val, end="", flush=True)
                for sent in ss.feed(val):
                    sp.play(*v.synth(sent))
        for sent in ss.flush():
            sp.play(*v.synth(sent))
        print()
        sp.wait()
        s.stop()

    elif cmd == "voices":
        v = _voice(cfg)
        names = v.voices()
        print("\n".join(n for n in names if n[:2] in ("af", "am", "bf", "bm")) or "(Piper voice in use)")
        print("\nNames start with a=American or b=British, then f=female or m=male.")
        print("Good ones: af_heart, af_bella, am_michael, am_fenrir, bf_emma, bm_george")

    elif cmd == "demo-voices":
        sp = _speaker(cfg)
        for name in ["af_heart", "af_bella", "am_michael", "am_fenrir", "bf_emma", "bm_george"]:
            c = dict(cfg, voice=name, voice_engine="kokoro")
            v = _voice(c)
            print(name)
            sp.play(*v.synth(f"Hi, this is {name.split('_')[1].title()}. Want me to look anything up while you play?"))
            sp.wait()
            time.sleep(0.4)

    elif cmd == "set":
        if len(rest) != 2 or rest[0] not in config.DEFAULTS:
            print("usage: talk2 set KEY VALUE\nkeys: " + ", ".join(config.DEFAULTS))
            return 2
        key, raw = rest
        default = config.DEFAULTS[key]
        if isinstance(default, bool):
            value = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(default, (int, float)):
            value = type(default)(float(raw)) if isinstance(default, int) else float(raw)
        else:
            value = raw
        cfg[key] = value
        config.save(cfg)
        print(f"{key} = {json.dumps(value)}")

    elif cmd == "unmute":
        from .share import MicSharing
        MicSharing().restore_after_crash()

    elif cmd == "mic-users":
        from .share import other_mic_streams
        others = other_mic_streams()
        print("\n".join(f"{name}{' (muted)' if m else ''}" for _, name, m in others)
              or "No other app is using the mic.")

    elif cmd == "settings":
        print(json.dumps(cfg, indent=2))

    elif cmd == "mic-test":
        import os
        from .assistant import rms
        from .audio import Microphone
        from .wakeword import WakeWord
        mic = Microphone(cfg["mic_device"])
        ww = WakeWord(os.path.join(config.MODELS_DIR, "wakeword"), cfg["wake_word"], cfg["wake_threshold"])
        mic.start()
        print("Talk, then say the wake word. Ctrl+C to stop.")
        end = time.time() + 15
        while time.time() < end:
            c = mic.read()
            if c is None:
                print("no audio from the microphone")
                continue
            fired = ww.process(c)
            level = min(40, int(rms(c) / 100))
            print(f"\rlevel {'#' * level:<40} wake {ww.last_score:.2f} {'<- WAKE' if fired else '       '}", end="", flush=True)
        print()
        mic.stop()

    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
