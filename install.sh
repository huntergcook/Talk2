#!/usr/bin/env bash
# Installs TalkThru on the Steam Frame: a voice assistant that listens for
# "Hey Jarvis", sends what you say to Claude Code, and speaks the reply.
#
#   curl -fsSL https://raw.githubusercontent.com/huntergcook/talk2/main/install.sh | bash
#
# Everything goes in your home folder (SteamOS's system files stay untouched):
#   ~/.local/share/talkthru   app, Python environment and models (~700 MB)
#   ~/.config/talkthru        settings.json
#   ~/.local/bin/talkthru     the `talkthru` command
# Running it again updates TalkThru and keeps your settings.
set -euo pipefail

REPO="${TALKTHRU_REPO:-huntergcook/talk2}"
REF="${TALKTHRU_REF:-main}"
DATA="$HOME/.local/share/talkthru"
APP="$DATA/app"
VENV="$DATA/venv"
MODELS="$DATA/models"
BIN="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
DRY="${TALKTHRU_DRY_RUN:-}"   # set to 1 to test the script without downloading models
export PATH="$BIN:$PATH"

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*"; exit 1; }
fetch() { curl -fL --retry 3 --progress-bar -o "$2.part" "$1" && mv "$2.part" "$2"; }

[ "$(uname -s)" = Linux ] || die "TalkThru runs on the Steam Frame (Linux)."
command -v curl >/dev/null || die "curl is missing."
mkdir -p "$DATA" "$MODELS"/{wakeword,kokoro,whisper,piper} "$BIN" "$UNIT_DIR"

# --- 1. App code ------------------------------------------------------------
say "Downloading TalkThru ($REF)"
if [ -n "${TALKTHRU_SRC:-}" ]; then          # local checkout, for development
  SRC="$TALKTHRU_SRC"
else
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar -xz -C "$TMP"
  SRC="$(echo "$TMP"/*)"
fi
rm -rf "$APP.new"; mkdir -p "$APP.new"
cp -r "$SRC/talkthru" "$SRC/talkthru.sh" "$SRC/talkthru.service" "$APP.new/"
rm -rf "$APP"; mv "$APP.new" "$APP"

# --- 2. Python environment ----------------------------------------------------
if ! command -v uv >/dev/null; then
  say "Installing uv (Python package manager)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN" INSTALLER_NO_MODIFY_PATH=1 sh
fi
if [ ! -x "$VENV/bin/python" ]; then
  say "Creating the Python environment"
  uv venv --quiet --python 3.12 "$VENV"
fi
say "Installing speech recognition and voice (a few minutes the first time)"
if [ -z "$DRY" ]; then
  uv pip install --quiet --python "$VENV/bin/python" --upgrade \
    "numpy>=1.26" "onnxruntime>=1.20" "faster-whisper>=1.1" "kokoro-onnx>=0.4"
fi

# --- 3. Models ----------------------------------------------------------------
say "Downloading models"
OWW="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
KOK="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
get() { [ -s "$2" ] || { echo "  $(basename "$2")"; [ -n "$DRY" ] && : > "$2" || fetch "$1" "$2"; }; }
for f in melspectrogram.onnx embedding_model.onnx hey_jarvis_v0.1.onnx; do
  get "$OWW/$f" "$MODELS/wakeword/$f"
done
get "$KOK/kokoro-v1.0.onnx" "$MODELS/kokoro/kokoro-v1.0.onnx"
get "$KOK/voices-v1.0.bin"  "$MODELS/kokoro/voices-v1.0.bin"
SPEECH_MODEL="$(PYTHONPATH="$APP" "$VENV/bin/python" -c 'from talkthru import config; print(config.load()["speech_model"])')"
if [ -z "$DRY" ]; then
  echo "  whisper $SPEECH_MODEL"
  PYTHONPATH="$APP" "$VENV/bin/python" -c "
from faster_whisper import WhisperModel
WhisperModel('$SPEECH_MODEL', device='cpu', compute_type='int8', download_root='$MODELS/whisper')"
fi

# --- 4. Check the voice; fall back to Piper only if Kokoro can't run ------------
if [ -z "$DRY" ]; then
  say "Testing the voice"
  if ! PYTHONPATH="$APP" "$VENV/bin/python" -c "
from talkthru import config; from talkthru.tts import KokoroVoice; import time
v = KokoroVoice('$MODELS/kokoro'); t = time.time(); a, sr = v.synth('Testing one two three, this is a short sentence.')
print(f'  Kokoro made {len(a)/sr:.1f}s of speech in {time.time()-t:.2f}s')"; then
    warn "Kokoro didn't run here, so TalkThru will use the Piper voice instead."
    uv pip install --quiet --python "$VENV/bin/python" "piper-tts>=1.2"
    PV="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium"
    get "$PV/en_US-amy-medium.onnx" "$MODELS/piper/en_US-amy-medium.onnx"
    get "$PV/en_US-amy-medium.onnx.json" "$MODELS/piper/en_US-amy-medium.onnx.json"
    PYTHONPATH="$APP" "$VENV/bin/python" -m talkthru set voice_engine piper >/dev/null
  fi
fi

# --- 5. Claude Code -------------------------------------------------------------
if ! command -v claude >/dev/null; then
  say "Installing Claude Code"
  [ -n "$DRY" ] || curl -fsSL https://claude.ai/install.sh | bash
fi
if [ -z "$DRY" ] && ! claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
  say "Sign in to Claude (a browser window opens; use the account with your Claude plan)"
  if [ -t 0 ] || [ -e /dev/tty ]; then
    claude auth login </dev/tty || warn "Sign-in didn't finish. Run: talkthru login"
  else
    warn "Run 'talkthru login' to sign in."
  fi
fi

# --- 6. Command and background service ---------------------------------------------
say "Setting up the talkthru command and background service"
install -m 755 "$APP/talkthru.sh" "$BIN/talkthru"
install -m 644 "$APP/talkthru.service" "$UNIT_DIR/talkthru.service"
PYTHONPATH="$APP" "$VENV/bin/python" -c 'from talkthru import config; config.ensure_file()'
if [ -z "$DRY" ]; then
  systemctl --user daemon-reload
  systemctl --user enable talkthru.service >/dev/null 2>&1
  systemctl --user restart talkthru.service
fi

cat <<EOF

TalkThru is installed and running.

  Say "Hey Jarvis", then your question. A rising chime means it's listening.
  After a reply you can ask a follow-up without the wake word for a few seconds.
  Say "Hey Jarvis" while it talks to cut it off, or "new conversation" to start fresh.

  talkthru demo-voices     hear the voices, then: talkthru set voice am_michael
  talkthru mic-test        check the mic level and wake word
  talkthru log             see what it's hearing and doing
  talkthru help            everything else

EOF
