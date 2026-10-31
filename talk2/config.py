import json
import os

CONFIG_DIR = os.path.expanduser("~/.config/talk2")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")
DATA_DIR = os.path.expanduser("~/.local/share/talk2")
MODELS_DIR = os.path.join(DATA_DIR, "models")
TOOLS_DIR = os.path.join(DATA_DIR, "tools")

DEFAULTS = {
    # Wake word
    "wake_word": "hey_jarvis_v0.1",   # openWakeWord model name, or a path to a custom .onnx
    "wake_threshold": 0.5,           # 0..1; raise if it triggers by itself, lower if it misses you
    "interrupt_with_wake_word": True,  # say the wake word while Claude talks to cut it off
    "stop_words": True,              # say "stop" or "shut up" while Claude talks to end the reply
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
    "new_conversation_after_min": 15,
    "extra_instructions": "",
    # Voice
    "voice_engine": "kokoro",        # kokoro (natural) or piper (faster, more robotic)
    "voice": "af_heart",             # Kokoro voice, see `talk2 voices`
    "voice_speed": 1.05,
    "voice_model": "kokoro-v1.0.int8.onnx",  # twice as fast; kokoro-v1.0.onnx is the full model
    "voice_effect": "none",          # none, or robot for a deep metallic voice
    "piper_voice": "en_US-amy-medium",
    "volume": 0.9,
    "chimes": True,
    "speaker_device": "",
    "threads": 4,                    # CPU threads for speech models; lower if games stutter
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except ValueError as e:
        print(f"talk2: ignoring broken {CONFIG_PATH}: {e}")
    return cfg


def save(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: cfg[k] for k in DEFAULTS}, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def ensure_file():
    if not os.path.exists(CONFIG_PATH):
        save(dict(DEFAULTS))
