# TalkThru

Talk to Claude while wearing your Steam Frame. Say **"Hey Jarvis"**, ask your question, and hear the answer in a natural voice, without taking the headset off or leaving your game.

- **Hands-free.** The wake word runs on the headset all the time, using about 2% of one CPU core.
- **Natural voice.** Replies are spoken by [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), one of the most natural-sounding open voices, and the voice starts talking while Claude is still writing.
- **Uses your Claude plan.** It talks to [Claude Code](https://claude.com/claude-code) on the headset, signed in with your own account. No API key.
- **Keeps the conversation.** Follow-ups work without the wake word, and Claude remembers what you said earlier until you say "new conversation".
- **Private until you ask.** Your voice is turned into text on the headset; only the text of your question goes to Claude.

Listen to the voices: [docs/voice-samples.mp3](docs/voice-samples.mp3) (Heart, Bella, Michael, Fenrir, Emma, George).

## Install (from inside the headset)

1. Open a terminal on the Frame (desktop mode, or a terminal app from the library).
2. Paste this and press Enter:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/huntergcook/talk2/main/install.sh | bash
   ```

3. When asked, sign in to Claude in the browser window that opens.

That's it. It takes a few minutes and about 700 MB, everything stays in your home folder, and TalkThru starts on its own from then on. Run the same command again to update.

## Using it

| Say | What happens |
|---|---|
| "Hey Jarvis" (rising chime), then your question | Claude answers out loud |
| "Hey Jarvis, what's a good build for a stealth archer?" | One breath works too |
| A follow-up within 5 seconds of the reply | No wake word needed |
| "Hey Jarvis" while Claude is talking | Cuts it off and listens again |
| "Never mind" | Cancels |
| "New conversation" | Claude forgets the chat so far |

A falling chime means it heard you and is thinking. A low chime means it didn't catch anything.

Claude can search the web and read files on the headset. Running commands or changing files is off by default, since a misheard word shouldn't run anything; turn it on with `talkthru set allow_commands true`.

## Settings

```sh
talkthru demo-voices                 # hear the six best voices
talkthru set voice am_michael        # pick one
talkthru set voice_speed 1.15        # talk faster
talkthru set claude_model haiku      # fastest replies (sonnet is the default)
talkthru set wake_threshold 0.6      # if it wakes up by itself
talkthru settings                    # everything else
```

`talkthru help` lists every command. Settings live in `~/.config/talkthru/settings.json`.

| Setting | Default | |
|---|---|---|
| `voice` | `af_heart` | Kokoro voice. `a`/`b` = American/British, `f`/`m` = female/male |
| `voice_speed` | `1.05` | 0.8 to 1.4 |
| `wake_threshold` | `0.5` | Higher = fewer false wakes, lower = easier to wake |
| `follow_up_s` | `5` | Seconds to keep listening after a reply (0 = always need the wake word) |
| `end_silence_ms` | `800` | How long a pause ends your question |
| `speech_model` | `base.en` | Speech recognition: `tiny.en` (fastest) … `small.en` (most accurate) |
| `claude_model` | `sonnet` | `haiku`, `sonnet`, `opus`, or blank for your account default |
| `allow_commands` | `false` | Let Claude run commands and edit files on the headset |
| `threads` | `4` | CPU threads for speech; lower it if a game stutters |
| `chimes` | `true` | The little listening sounds |

## Troubleshooting

- **It doesn't wake up.** Run `talkthru mic-test` and say "Hey Jarvis". The level bar should move when you talk and the score should jump above 0.5. If the bar doesn't move, the Frame's mic isn't the default input; pick it in the sound settings.
- **It wakes up by itself.** `talkthru set wake_threshold 0.65`.
- **"Claude isn't signed in."** Run `talkthru login`.
- **See what it's doing.** `talkthru log` shows what it heard, how long each step took, and any errors.

## How it works

```
mic (PipeWire, 16 kHz) ─► wake word ─► record until you pause ─► Whisper ─► Claude Code ─► Kokoro ─► speakers
   openWakeWord ONNX models   loudness vs. room noise   faster-whisper   stream-json   sentence by sentence
```

- **Wake word:** openWakeWord's mel-spectrogram, speech-embedding and "hey jarvis" models, run directly with ONNX Runtime in 80 ms steps.
- **Speech recognition:** faster-whisper `base.en` (int8) on the CPU.
- **Claude:** one long-lived `claude -p --input-format stream-json --output-format stream-json` process, so there's no start-up delay per question and the conversation keeps its context. Text streams back token by token; interrupts use Claude Code's control request.
- **Voice:** Kokoro 82M (ONNX). The first clause is cut early so the voice starts fast, and each sentence is generated while the previous one plays.
- **Service:** a systemd user service (`talkthru.service`), niced below your game.

## Uninstall

```sh
talkthru uninstall
```

## Credits

[openWakeWord](https://github.com/dscripka/openWakeWord) (Apache 2.0), [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT), [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) and [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (Apache 2.0 / MIT). Made by the author of [SquintThru](https://github.com/huntergcook/SquintThru).
