import json
import os

CONFIG_DIR = os.path.expanduser("~/.config/talk2")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")
DATA_DIR = os.path.expanduser("~/.local/share/talk2")
MODELS_DIR = os.path.join(DATA_DIR, "models")
TOOLS_DIR = os.path.join(DATA_DIR, "tools")

DEFAULTS = {
    # Wake word
    "wake_word": "hey_claude",       # openWakeWord model name, or a path to a custom .onnx
    "wake_threshold": 0.5,           # a score this high wakes at once
    "wake_sensitivity": 0.15,        # a score this high is double-checked by speech recognition
    "wake_confirm": True,            # do that double-check (catches you every time, rarely misfires)
    "interrupt_with_wake_word": True,  # say the wake word while Claude talks to cut it off
    "stop_words": True,              # say "stop" or "shut up" while Claude talks to end the reply
    "barge_in": True,                # just talk over a reply to cut in (pauses at once, resumes if it was noise)
    "echo_cancel": True,             # remove talk2's own voice from what it hears (PipeWire, talk2 only)
    # Listening
    "mic_device": "",                # blank = system default (the Frame's mic)
    "end_silence_ms": 800,           # pause length that ends your question
    "max_question_s": 30,
    "follow_up_s": 5,                # after a reply, keep listening this long without the wake word
    # Sharing the mic (e.g. Discord on your PC over SteamVR streaming)
    "follow_up_when_shared": False,  # follow-ups without the wake word even while another app uses the mic
    "keep_questions_private": False, # mute the mic for other apps from the wake word until the reply ends
    "speech_model": "base.en",       # faster-whisper model: tiny.en, base.en, small.en, distil-small.en
    # Claude
    "claude_model": "sonnet",        # sonnet, haiku, opus, or a full model name; blank = your default
    "allow_commands": False,         # let Claude run commands and edit files on the headset
    "make_tools": False,             # let Claude write and run Python scripts in its toolbox folder
    "quick_commands": True,          # handle simple commands (volume, open X, timers) instantly, without Claude
    "learn": True,                   # learn from how answers went, kept in ~/.local/share/talk2/memory.md
    "memory_model": "haiku",         # model that updates those notes after each conversation
    "new_conversation_after_min": 15,
    "extra_instructions": "",
    # Voice
    "voice_engine": "kokoro",        # kokoro (natural) or piper (faster, more robotic)
    "voice": "af_heart",             # Kokoro voice, see `talk2 voices`
    "voice_speed": 1.2,
    "voice_model": "kokoro-v1.0.int8.onnx",  # twice as fast; kokoro-v1.0.onnx is the full model
    "voice_effect": "none",          # none, or robot for a deep metallic voice
    "piper_voice": "en_US-amy-medium",
    "volume": 0.9,
    "chimes": True,
    "speaker_device": "",
    "threads": 4,                    # CPU threads for speech models; lower if games stutter
    # Headset
    "battery_warnings": True,        # speak up at 20, 10 and 5 percent
    "screenshot_command": "",        # custom capture for the look tool; {out} is the file to write
}

VERSION = 2   # bump with a migration below when a default changes meaning


def _migrate(user):
    """Older installs saved every setting, so new defaults never reached them."""
    v = user.get("config_version", 1)
    if v < 2:
        if user.get("wake_word") == "hey_jarvis_v0.1":
            user.pop("wake_word")          # -> Hey Claude
        if user.get("voice_speed") == 1.05:
            user.pop("voice_speed")        # -> a bit quicker
    user["config_version"] = VERSION
    return user


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        if user.get("config_version", 1) < VERSION:
            user = _migrate(user)
            _write({k: v for k, v in user.items() if k in DEFAULTS and v != DEFAULTS[k]})
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except ValueError as e:
        print(f"talk2: ignoring broken {CONFIG_PATH}: {e}")
    return cfg


def _write(changed):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"config_version": VERSION, **changed}, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def save(cfg):
    """Writes only settings that differ from the defaults, so improved defaults reach you."""
    _write({k: cfg[k] for k in DEFAULTS if cfg[k] != DEFAULTS[k]})


def ensure_file():
    if not os.path.exists(CONFIG_PATH):
        save(dict(DEFAULTS))
