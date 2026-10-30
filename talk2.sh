#!/usr/bin/env bash
# The `talk2` command. Installed to ~/.local/bin/talk2.
set -euo pipefail
DATA="$HOME/.local/share/talk2"
PY="$DATA/venv/bin/python"
export PYTHONPATH="$DATA/app"
export PATH="$HOME/.local/bin:$PATH"
svc() { systemctl --user "$@" talk2.service; }
py() { "$PY" -m talk2 "$@"; }

case "${1:-help}" in
  status)   svc status --no-pager ;;
  log|logs) journalctl --user -u talk2.service -n 60 -f ;;
  start|stop|restart) svc "$1"; echo "talk2: $1 done" ;;
  login)    claude auth login ;;
  update)   curl -fsSL "https://raw.githubusercontent.com/${TALK2_REPO:-huntergcook/talk2}/${TALK2_REF:-main}/install.sh" | bash ;;
  set)      shift; py set "$@" && svc restart && echo "(restarted talk2)" ;;
  edit)     "${EDITOR:-nano}" "$HOME/.config/talk2/settings.json" && svc restart ;;
  say|ask|voices|demo-voices|settings)
            cmd="$1"; shift
            # The service would hear and answer the demo through the speakers.
            if [ "$cmd" != settings ] && svc is-active --quiet; then paused=1; svc stop; else paused=0; fi
            py "$cmd" "$@" || true
            if [ "$paused" = 1 ]; then svc start; fi ;;
  mic-test)
            svc is-active --quiet && { paused=1; svc stop; } || paused=0
            py mic-test || true
            if [ "$paused" = 1 ]; then svc start; fi ;;
  uninstall)
            svc disable --now 2>/dev/null || true
            rm -f "$HOME/.config/systemd/user/talk2.service" "$HOME/.local/bin/talk2"
            systemctl --user daemon-reload
            rm -rf "$DATA"
            echo "talk2 removed. Settings kept in ~/.config/talk2 (delete it if you like)." ;;
  *) cat <<'EOF'
talk2: voice assistant for the Steam Frame

  status | log | start | stop | restart
  set KEY VALUE        change a setting and restart, e.g. talk2 set voice am_michael
  settings             show all settings      edit   open settings.json
  demo-voices          hear six voices        voices list every voice
  say "text"           speak text             ask "question"  ask Claude without the mic
  mic-test             live mic level and wake word score
  login                sign in to Claude      update  install the latest version
  uninstall

Handy settings:
  voice             af_heart, af_bella, am_michael, am_fenrir, bf_emma, bm_george ...
  voice_speed       1.05 (0.8 to 1.4)
  wake_threshold    0.5; raise if it wakes by itself, lower if it misses you
  claude_model      sonnet, haiku (fastest), opus
  allow_commands    false; true lets Claude run commands on the headset
  follow_up_s       5; seconds to keep listening after a reply
  threads           4; lower if games stutter
EOF
  ;;
esac
