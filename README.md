# talk2

Talk to Claude while wearing your Steam Frame. Say **"Hey Claude"**, ask your question, and hear the answer in a natural voice, without taking the headset off or leaving your game.

- **Hands-free.** The wake word runs on the headset all the time, using about 2% of one CPU core.
- **Knows what's going on.** Each question comes with the game you're playing, the time, battery and passthrough state, and Claude can look at what you're seeing when you ask about it.
- **Does things.** "Open the browser", "launch Beat Saber", "volume down", "passthrough on", "remind me in 20 minutes to stretch": done, then a quick "Done."
- **Shows things.** Lists, steps and recipes go on a text panel you can read and tick off.
- **Learns.** It notices when an answer missed (you re-asked, corrected it or cut it off) and gets better at what you actually ask for.
- **Natural voice.** Replies are spoken by [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), one of the most natural-sounding open voices, and the voice starts talking while Claude is still writing.
- **Uses your Claude plan.** It talks to [Claude Code](https://claude.com/claude-code) on the headset, signed in with your own account. No API key.
- **Keeps the conversation.** Follow-ups work without the wake word, and Claude remembers what you said earlier until you say "new conversation".
- **Private until you ask.** Your voice is turned into text on the headset; only the text of your question goes to Claude.

Listen to the voices: [docs/voice-samples.mp3](docs/voice-samples.mp3) (Heart, Bella, Michael, Fenrir, Emma, George).

## Install (from inside the headset)

1. Open a terminal on the Frame (desktop mode, or a terminal app from the library).
2. Paste this and press Enter:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/huntergcook/Talk2/main/install.sh | bash
   ```

3. When asked, sign in to Claude in the browser window that opens.

That's it. It takes a few minutes and about 1 GB, everything stays in your home folder, and talk2 starts on its own from then on. Run the same command again to update.

## Using it

| Say | What happens |
|---|---|
| "Hey Claude" (rising chime), then your question | Claude answers out loud, short |
| "Hey Claude, what's a good build for a stealth archer?" | One breath works too |
| A follow-up within 5 seconds of the reply | No wake word needed |
| Just start talking while it's talking | It stops at once and answers what you said instead. Handy when you already know what it's asking |
| "Stop" or "shut up" while it's talking | Ends the reply |
| "Hey Claude" while it's talking | Cuts it off and listens again |
| "Never mind" | Cancels |
| "New conversation" | Claude forgets the chat so far |

A falling chime means it heard you and is thinking. A low chime means it didn't catch anything. Three slow falling notes mean the conversation is over, and you'll need "Hey Claude" again.

If it was only noise or its own voice that made it pause, it picks up where it left off. talk2 removes its own voice from what it hears with PipeWire's echo canceller, used only for talk2; your mic in other apps is untouched.

### Make the wake word catch you every time

```sh
talk2 enroll
```

About a minute: you say "Hey Claude" eight times, a few other "hey ..." phrases, and then just talk for 20 seconds. talk2 trains a small model of your voice on top of the stock one. Every quieter or unsure "Hey Claude" is double-checked by speech recognition, so it can be sensitive without waking up on its own. Run it again any time (in a noisy game, say) and it learns from all your recordings.

### Things it can do

Instantly, without asking Claude:

- **Apps and games:** "open the browser / Discord / files / Steam store", "launch Half-Life Alyx", "play Beat Saber". In Game Mode, the browser opens in Steam's browser.
- **Sound:** "volume down", "louder", "volume 40", "mute", "pause the music", "skip this song".
- **Passthrough:** "passthrough on/off", "let me see my room". This works with or without SquintThru.
- **Timers:** "set a timer for 10 minutes", "remind me in half an hour to drink water", "how long is left?", "cancel the timer".
- **Quick facts:** "what time is it?", "how much battery?"

With Claude:

- **Questions about your game.** It knows what you're playing, so "how do I beat this boss?" just works.
- **"What's this?" / "Read this" / "Which way?"** It takes a screenshot of your view and looks. Check that screenshots work on your setup with `talk2 look-test`.
- **The panel.** Anything you'd rather read than hear goes on a text panel: steps, lists, recipes, checklists you can tick off. Ask for one ("put that on the panel", "give me a checklist for…"), or Claude uses it when an answer is too long to say. "Close the panel" hides it.
- **Remembering.** "Remember that my server is called Windrose." "Forget that."

It also speaks up when a timer is due and when the battery reaches 20, 10 and 5 percent.

Claude can search the web and read files on the headset. Running commands or changing files is off by default, since a misheard word shouldn't run anything; turn it on with `talk2 set allow_commands true`.

With `talk2 set make_tools true`, Claude writes small Python scripts for things like weather and sums, keeps them in `~/.local/share/talk2/tools`, and reuses them. It can only write inside that folder and only run scripts from it, but those scripts are ordinary programs that run as you, with no sandbox.

### How it learns

Each exchange is logged on the headset with how it went, judged from what you did next: you asked again soon after, corrected it ("no, I meant…"), cut it off, or none of those. When a conversation ends, Claude (Haiku, so it's quick and cheap) rewrites a short set of notes: what you like, what works, what to avoid, and shortcuts for things you ask often. For example, after you say "no, open Chromium" once, it remembers that "the browser" means Chromium. Every new session starts with those notes.

```sh
talk2 memory          # see what it's learned
talk2 memory clear    # start over
talk2 set learn false # turn learning off
```

Everything stays in `~/.local/share/talk2` (`memory.md` and `history.jsonl`).

## Discord and PC streaming

talk2 shares the mic; it never takes it over. When you stream from your PC over SteamVR, the PC (and Discord on it) keeps hearing you exactly as before, while talk2 listens for "Hey Claude" alongside it.

While another app is using the mic, talk2 changes one thing: it skips the follow-up window, so whatever you say to your friends after a reply isn't sent to Claude. It also won't let speech from the speakers (your friends' voices) cut into a reply; "stop" and "Hey Claude" still work. You need "Hey Claude" for each question. `talk2 set follow_up_when_shared true` turns follow-ups back on.

By default your friends hear you say "Hey Claude" and your question, like any other talking. To keep your questions to Claude out of Discord, run `talk2 set keep_questions_private true`. Then talk2 mutes the mic for the PC from the moment it hears "Hey Claude" until the reply ends, and always unmutes it afterwards, even if talk2 crashes or is stopped. `talk2 mic-users` shows which apps are using the mic.

## Settings

```sh
talk2 demo-voices                 # hear the six best voices
talk2 set voice am_michael        # pick one
talk2 set voice_speed 1.3         # talk even faster
talk2 set claude_model haiku      # fastest replies (sonnet is the default)
talk2 set wake_sensitivity 0.25  # if it wakes up by itself
talk2 set barge_in false          # if game sounds keep pausing it
talk2 settings                    # everything else
```

`talk2 help` lists every command. Settings live in `~/.config/talk2/settings.json`.

| Setting | Default | |
|---|---|---|
| `voice` | `af_heart` | Kokoro voice. `a`/`b` = American/British, `f`/`m` = female/male |
| `voice_speed` | `1.2` | 0.8 to 1.5 |
| `voice_model` | `kokoro-v1.0.int8.onnx` | Twice as fast as the full model, `kokoro-v1.0.onnx` |
| `voice_effect` | `none` | `robot` for a deep, metallic voice; try it with `am_onyx` |
| `wake_word` | `hey_claude` | `hey_jarvis_v0.1` also works |
| `wake_sensitivity` | `0.15` | Scores above this are double-checked by speech recognition; lower = catches quieter "Hey Claude"s |
| `wake_threshold` | `0.5` | Scores above this wake at once |
| `barge_in` | `true` | Talk over a reply to cut in |
| `echo_cancel` | `true` | Remove talk2's own voice from what it hears |
| `quick_commands` | `true` | Handle simple commands instantly, without Claude |
| `learn` | `true` | Learn from how answers went |
| `battery_warnings` | `true` | Speak up at 20, 10 and 5 percent |
| `screenshot_command` | | Custom capture for "what's this?"; `{out}` is the file to write |
| `follow_up_s` | `5` | Seconds to keep listening after a reply (0 = always need the wake word) |
| `end_silence_ms` | `800` | How long a pause ends your question |
| `speech_model` | `base.en` | Speech recognition: `tiny.en` (fastest) … `small.en` (most accurate) |
| `claude_model` | `sonnet` | `haiku`, `sonnet`, `opus`, or blank for your account default |
| `allow_commands` | `false` | Let Claude run commands and edit files on the headset |
| `make_tools` | `false` | Let Claude write and run Python scripts in its toolbox folder |
| `stop_words` | `true` | Say "stop" or "shut up" while Claude talks to end the reply |
| `follow_up_when_shared` | `false` | Allow follow-ups without the wake word while another app (like Discord over PC streaming) uses the mic |
| `keep_questions_private` | `false` | Mute the mic for other apps from "Hey Claude" until the reply ends |
| `threads` | `4` | CPU threads for speech; lower it if a game stutters |
| `chimes` | `true` | The little listening sounds |

## Troubleshooting

- **It doesn't wake up.** Run `talk2 enroll`. If it still misses you, run `talk2 mic-test`: the level bar should move when you talk. If it doesn't, the Frame's mic isn't the default input; pick it in the sound settings.
- **It wakes up by itself.** `talk2 set wake_sensitivity 0.25`, or `talk2 enroll` again so it knows your voice better.
- **It keeps pausing mid-reply.** Game sounds are being heard as you. Try `talk2 set barge_in false`; "stop" and "Hey Claude" still work.
- **"What's this?" can't see.** Run `talk2 look-test`. It uses Spectacle or grim if they're installed, and otherwise asks gamescope for a screenshot. If none of those work, set `screenshot_command` to anything that saves a PNG to `{out}`.
- **Friends on Discord can't hear you.** Run `talk2 unmute` (it un-mutes anything talk2 muted), then `talk2 mic-users`; the PC streaming app should be listed without "(muted)".
- **talk2 hears nothing while streaming from the PC.** If `talk2 log` says the mic sent only silence, the streaming app has the mic to itself; please open an issue with `talk2 mic-users` output.
- **"Claude isn't signed in."** Run `talk2 login`.
- **See what it's doing.** `talk2 log` shows what it heard, how long each step took, and any errors.

## How it works

```
mic ─► echo canceller ─► "Hey Claude" ─► record until you pause ─► Whisper ─► instant command? ─► Claude Code ─► Kokoro ─► speakers
          (PipeWire)       3 checks       loudness vs. room noise               │                 + talk2 tools
                                                                                 └─► done in ms
```

- **Wake word:** openWakeWord's mel-spectrogram and speech-embedding models with [gdiamos/hey-claude](https://huggingface.co/gdiamos/hey-claude), run directly with ONNX Runtime in 80 ms steps. A strong score wakes at once. A weak one, or one from your personal model (logistic regression on the same features, trained by `talk2 enroll`), is checked by Whisper on the last 1.8 s of audio.
- **Speech recognition:** faster-whisper `base.en` (int8) on the CPU, primed with the name of the game you're playing.
- **Claude:** one long-lived `claude -p --input-format stream-json --output-format stream-json` process, so there's no start-up delay per question and the conversation keeps its context. talk2's tools (`mcp_server.py`) are a small MCP server that Claude Code starts alongside it. Text streams back token by token; interrupts use Claude Code's control request.
- **Context:** the running game comes from `SteamAppId` in the game's environment plus your library's app manifests; battery from `/sys/class/power_supply`; volume from `wpctl`; passthrough from the SteamVR dashboard (SquintThru's Room View code). It's gathered only when you ask something.
- **Voice:** Kokoro 82M (ONNX). The first clause is cut early so the voice starts fast, and each sentence is generated while the previous one plays. When you talk over it, playback stops at once and unplayed sentences are kept, so it can resume if that was only noise or its own echo.
- **Panel:** a page served by talk2 on `127.0.0.1`, updated live, opened as a Chromium app window in Desktop Mode or in Steam's browser in Game Mode.
- **Service:** a systemd user service (`talk2.service`), niced below your game.

## Uninstall

```sh
talk2 uninstall
```

## Credits

[openWakeWord](https://github.com/dscripka/openWakeWord) (Apache 2.0), [hey-claude](https://huggingface.co/gdiamos/hey-claude) by gdiamos (Apache 2.0), [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT), [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) and [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (Apache 2.0 / MIT). Made by the author of [SquintThru](https://github.com/huntergcook/SquintThru).
