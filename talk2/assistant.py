"""The voice loop: wake word -> listen -> transcribe -> Claude -> speak.

While talk2 is talking you can:
- say "Hey Claude" to cut it off and ask something new,
- say "stop" / "shut up" to end the reply,
- or just start talking (barge-in): it pauses at once, listens to the whole
  thing, and answers that instead. If it was only noise or its own echo, it
  picks up where it left off.
"""

import collections
import os
import queue
import re
import signal
import sys
import threading
import time

import numpy as np

from . import actions, config, context, memory, quick
from .audio import (CHIME_CANCEL, CHIME_DONE, CHIME_END, CHIME_LISTEN, EC_SINK, EC_SOURCE,
                    MIC_CHUNK, MIC_RATE, EchoCancel, Microphone, Speaker, tone)
from .claude import ClaudeError, ClaudeSession
from .log import log
from .share import MicSharing
from .speech_text import SentenceStream

CHUNK_S = MIC_CHUNK / MIC_RATE

CANCEL = re.compile(r"^\W*(never ?mind|cancel|nothing|stop|shut up|forget it|no thanks?)\W*$", re.I)
# The whole utterance must be a stop command, so talking about stopping doesn't count.
STOP = re.compile(r"^\W*((please|hey|okay|ok|jarvis|claude)\W+)*"
                  r"(stop( talking| it| that)?|shut up|shut it|be quiet|quiet|hush|enough|that's enough|"
                  r"never ?mind|cancel)"
                  r"(\W+(please|now|jarvis|claude))*\W*$", re.I)
STOP_END_CHUNKS = 5    # 0.4 s of quiet ends a possible stop command
STOP_MAX_CHUNKS = 25   # 2 s; anything longer isn't one
RESET = re.compile(r"\b(new conversation|start over|start a new (chat|conversation)|fresh start|clear (the )?(chat|conversation))\b", re.I)
FILLERS = ["One sec.", "Let me look.", "Hang on."]
SLOW_TOOLS = ("WebSearch", "WebFetch", "look", "Bash")
CHIME_TIMER = tone([784, 988, 1175, 988, 1175], ms=110, volume=0.3)


def rms(chunk):
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2))) if len(chunk) else 0.0


def _words(s):
    return re.findall(r"[a-z']+", s.lower())


def sounds_like_echo(heard, spoken):
    """Is what the mic heard just talk2's own recent sentences coming back?

    An echo is a run of its own words in the same order. Short replies are
    treated as you, since answers reuse the question's words ("the short one").
    """
    import difflib
    h = _words(heard)
    if not h:
        return True
    said = _words(" ".join(spoken))
    if not said or len(h) <= 3:
        return False
    m = difflib.SequenceMatcher(None, h, said, autojunk=False)
    matched = sum(b.size for b in m.get_matching_blocks())
    return matched / len(h) >= 0.7


class Endpointer:
    """Decides when you've started and stopped talking from mic loudness.

    The headset mic sits close to your mouth, so your voice is much louder
    than the room; the threshold tracks the room's noise floor.
    """

    def __init__(self, end_silence_ms=800, max_s=30):
        self.noise = 120.0
        self.end_chunks = max(3, int(end_silence_ms / 1000 / CHUNK_S))
        self.max_chunks = int(max_s / CHUNK_S)

    def observe_idle(self, chunk):
        r = rms(chunk)
        # Rise slowly (speech shouldn't drag it up), fall quickly.
        a = 0.02 if r > self.noise else 0.2
        self.noise = max(30.0, (1 - a) * self.noise + a * r)

    def threshold(self):
        return max(self.noise * 3.0, 260.0)

    def record(self, read, start_timeout_s, preroll=()):
        """Returns int16 audio of one utterance, or None if nobody spoke."""
        thr = self.threshold()
        chunks = list(preroll)
        started = bool(preroll)
        voiced = silent = 0
        waited = 0
        start_chunks = int(start_timeout_s / CHUNK_S)
        while True:
            c = read()
            if c is None:
                return None
            loud = rms(c) > thr
            if not started:
                chunks = (chunks + [c])[-4:]  # keep ~0.3 s before speech starts
                waited += 1
                if loud:
                    voiced += 1
                    if voiced >= 2:
                        started, silent = True, 0
                else:
                    voiced = 0
                if not started and waited > start_chunks:
                    return None
                continue
            chunks.append(c)
            if loud:
                voiced += 1
                silent = 0
            else:
                silent += 1
            if silent >= self.end_chunks or len(chunks) >= self.max_chunks:
                break
        if voiced < 3 and not preroll:
            return None
        return np.concatenate(chunks[:len(chunks) - max(0, silent - 3)])


class Assistant:
    def __init__(self, cfg=None):
        self.cfg = cfg or config.load()
        c = self.cfg
        models = config.MODELS_DIR
        from .api import Api, Panel, Timers
        from .stt import Transcriber
        from .tts import load_voice
        from .wakeword import PersonalModel, WakeDetector, WakeWord
        self.stt = Transcriber(c["speech_model"], os.path.join(models, "whisper"), c["threads"])
        personal = None
        ppath = os.path.join(models, "wakeword", "personal.npz")
        if os.path.exists(ppath):
            try:
                personal = PersonalModel.load(ppath)
                log("using your personal wake word model")
            except Exception as e:
                log(f"personal wake model unreadable: {e}")
        self.wake = WakeDetector(
            WakeWord(os.path.join(models, "wakeword"), c["wake_word"]),
            confirm=self.stt.says_wake_word if c["wake_confirm"] else None,
            personal=personal, low=c["wake_sensitivity"], high=c["wake_threshold"])
        self.voice = load_voice(c, models)
        self.speaker = Speaker(c["speaker_device"], c["volume"])
        self.mic = Microphone(c["mic_device"])
        self.echo = EchoCancel()
        self.aec = False
        self.panel, self.timers = Panel(), Timers()
        self.api = Api(self.panel, self.timers, actions.open_panel_window)
        self.claude = ClaudeSession(model=c["claude_model"], allow_commands=c["allow_commands"],
                                    make_tools=c["make_tools"], extra_prompt=c["extra_instructions"],
                                    mcp_env={"TALK2_API_PORT": str(self.api.port),
                                             "TALK2_API_TOKEN": self.api.token})
        self.ep = Endpointer(c["end_silence_ms"], c["max_question_s"])
        self.share = MicSharing(own_pids=self.mic.pids)
        self.learner = memory.Learner(c)
        self.filler_i = 0
        self.last_battery_warn = 101

    # -- helpers -----------------------------------------------------------
    def chime(self, sound):
        if self.cfg["chimes"]:
            self.speaker.resume()
            self.speaker.play(sound, 24000)

    def say(self, text, wait=True):
        self.speaker.resume()
        audio, sr = self.voice.synth(text)
        self.speaker.play(audio, sr)
        if wait:
            self.speaker.wait()

    def read_mic(self):
        return self.mic.read(timeout=2.0)

    # -- main loop ---------------------------------------------------------
    def run(self):
        # Let `talk2 stop` run cleanup (un-muting other apps' mic) instead of dying mid-question.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        self.share.restore_after_crash()
        if self.cfg["echo_cancel"] and not self.cfg["mic_device"] and not self.cfg["speaker_device"]:
            self.aec = self.echo.start()
            if self.aec:
                self.mic.device, self.speaker.device = EC_SOURCE, EC_SINK
        self.mic.start()
        try:
            self.claude.start()
        except ClaudeError as e:
            log(str(e))
        log(f"listening for the wake word ({self.cfg['wake_word']})")
        try:
            while True:
                chunk = self.mic.read(timeout=1.0)
                self._maybe_reset_conversation()
                self._announce_due()
                if chunk is None:
                    continue
                self.ep.observe_idle(chunk)
                if self.wake.process(chunk):
                    log(f"wake word ({self.wake.last})")
                    try:
                        self.converse()
                    except Exception as e:  # keep the service alive no matter what
                        log(f"conversation error: {e!r}")
                        self.speaker.stop()
                    self.learner.end_conversation()
                    self.wake.reset()
                    self.mic.flush()
        finally:
            self.echo.stop()

    def _maybe_reset_conversation(self):
        idle_min = self.cfg["new_conversation_after_min"]
        s = self.claude
        if idle_min and s.proc and s.turns and not s.busy and time.time() - s.last_used > idle_min * 60:
            log("starting a fresh conversation after being idle")
            s.restart()   # also picks up what it learned since

    def _announce_due(self):
        """Say timers and reminders that came due, and warn about a low battery."""
        said = False
        while True:
            try:
                item = self.timers.due.get_nowait()
            except queue.Empty:
                break
            label = item["label"]
            if item.get("late", 0) > 120:
                msg = f"Sorry, I missed a reminder while I was off: {label}." if label else "You had a timer go off while I was off."
            elif label and label != "timer":
                msg = f"Hey, reminder: {label}." if not label.lower().endswith("timer") else f"Your {label}'s done."
            else:
                msg = "Hey, your timer's done."
            self.chime(CHIME_TIMER)
            self.say(msg)
            said = True
        if said:
            self.mic.flush()
        if int(time.time()) % 60 == 0 and self.cfg["battery_warnings"]:
            b = context.battery()
            if b and b["status"] == "discharging":
                for level in (20, 10, 5):
                    if b["percent"] <= level < self.last_battery_warn:
                        self.last_battery_warn = level
                        left = b.get("minutes_left")
                        self.say(f"Heads up, battery's at {b['percent']} percent" +
                                 (f", about {left} minutes left." if left else "."))
                        break
            elif b and b["status"] != "discharging":
                self.last_battery_warn = 101

    def converse(self):
        """One exchange, plus follow-ups. Other apps' mic access is untouched
        unless keep_questions_private is on, and then always restored."""
        muted = []
        if self.cfg["keep_questions_private"]:
            muted = self.share.mute_others()
        try:
            self._converse()
        finally:
            self.share.unmute(muted)

    def _follow_up_allowed(self):
        if not self.cfg["follow_up_s"]:
            return False
        if self.cfg["follow_up_when_shared"] or self.cfg["keep_questions_private"]:
            return True
        # While the mic is shared (e.g. Discord over SteamVR streaming), what you
        # say next is probably meant for your friends, not Claude.
        return not self.share.shared()

    def _barge_in_allowed(self):
        if not self.cfg["barge_in"]:
            return False
        # Friends' voices on Discord come out of the speakers too; don't let them interrupt.
        return self.cfg["keep_questions_private"] or not self.share.shared()

    def _converse(self):
        self.speaker.stop()
        g = None
        try:
            g = context.running_game()
        except Exception:
            pass
        self.stt.set_vocabulary([g[1]] if g else [])
        # No flush here: people often say the question in the same breath as
        # the wake word, and that audio is already queued.
        self.chime(CHIME_LISTEN)
        audio = self.ep.record(self.read_mic, start_timeout_s=5)
        replied = False
        text = None
        note = ""
        while True:
            # Once there's been a reply, every way out ends the "call".
            leaving = CHIME_END if replied else CHIME_CANCEL
            if text is None:
                if audio is None:
                    self.chime(leaving)
                    return
                self.chime(CHIME_DONE)
                text = self.stt.transcribe(audio)
            if not text or CANCEL.match(text):
                self.chime(leaving)
                return
            self.learner.before_question(text)
            if RESET.search(text) and len(text.split()) <= 6:
                self.learner.end_conversation()
                self.claude.restart()
                self.say("Okay, fresh start.")
                if replied:
                    self.chime(CHIME_END)
                return
            if self.cfg["quick_commands"]:
                t0 = time.time()
                try:
                    said = quick.handle(self, text)
                except Exception as e:
                    log(f"quick command failed: {e!r}")
                    said = None
                if said:
                    log(f"instant: {text!r} -> {said!r} ({(time.time() - t0) * 1000:.0f} ms)")
                    self.say(said)
                    self.learner.record(text, said, kind="instant", game=g and g[1])
                    replied = True
                    text, note = None, ""
                    if not self._follow_up_allowed():
                        self.chime(CHIME_END)
                        return
                    self.mic.flush()
                    audio = self.ep.record(self.read_mic, start_timeout_s=self.cfg["follow_up_s"])
                    continue
            kind, data = self.respond(text, note, game=g)
            replied = True
            text, note = None, ""
            if kind == "stopped":
                self.chime(CHIME_END)
                return
            if kind == "barge":
                text, note = data["text"], data["note"]
                continue
            if kind == "wake":
                self.chime(CHIME_LISTEN)
                audio = self.ep.record(self.read_mic, start_timeout_s=5)
                continue
            self.mic.flush()  # don't hear the end of our own reply as a follow-up
            if not self._follow_up_allowed():
                self.chime(CHIME_END)
                return
            audio = self.ep.record(self.read_mic, start_timeout_s=self.cfg["follow_up_s"])

    # -- one reply -----------------------------------------------------------
    def _context_line(self, game):
        extra = []
        if self.panel.content.get("visible"):
            extra.append(f"Panel showing: {self.panel.content.get('title') or 'untitled'}")
        timers = self.timers.list()
        if timers:
            from .api import describe_timer
            extra.append("Timers: " + "; ".join(describe_timer(i) for i in timers[:3]))
        try:
            return context.summary(extra)
        except Exception as e:
            log(f"context failed: {e}")
            return time.strftime("Local time: %A %-I:%M %p")

    def respond(self, text, note="", game=None):
        """Stream Claude's reply into speech, listening the whole time.

        Returns (kind, data): ("done", None), ("stopped", None) for a stop
        command, ("wake", None) for the wake word, or ("barge", {...}) when you
        talked over it.
        """
        cfg = self.cfg
        outcome = {}                     # set once: kind, data
        decided = threading.Event()
        cv = threading.Condition()
        pending = collections.deque()    # sentences not yet played
        timeline = []                    # (sentence, start, end) as played
        state = {"paused": False, "finished": False, "phrase": False, "spoken": [], "all": []}
        synth_cache = {}
        barge = self._barge_in_allowed()
        t0 = time.time()

        def decide(kind, data=None):
            with cv:
                if decided.is_set():
                    return
                outcome.update(kind=kind, data=data)
                decided.set()
                cv.notify_all()
            self.speaker.stop()

        def pause():
            with cv:
                if state["paused"]:
                    return
                state["paused"] = True
            t = time.time()
            self.speaker.stop()
            with cv:
                replay = [s for s, st, en in timeline if en > t]
                state["spoken"] += [s for s, st, en in timeline if en <= t]
                # The sentence it was in the middle of, for "you were cut off while saying".
                state["partial"] = next((s for s, st, en in timeline if st <= t < en), "")
                timeline.clear()
                pending.extendleft(reversed(replay))
            log(f"paused for you ({len(replay)} sentence(s) to resume)")

        def resume():
            self.speaker.resume()
            with cv:
                state["paused"] = False
                cv.notify_all()
            log("resuming")

        def speak_worker():
            started = False
            while True:
                with cv:
                    while not decided.is_set() and (state["paused"] or not pending) and not (
                            state["finished"] and not pending):
                        cv.wait(0.1)
                    if decided.is_set() or (state["finished"] and not pending):
                        return
                    s = pending.popleft()
                try:
                    audio, sr = synth_cache.get(s) or self.voice.synth(s)
                    synth_cache[s] = (audio, sr)
                except Exception as e:
                    log(f"voice failed on {s!r}: {e}")
                    continue
                with cv:
                    if decided.is_set():
                        return
                    if state["paused"]:
                        pending.appendleft(s)
                        continue
                    start = max(time.time(), self.speaker.ends_at)
                    timeline.append((s, start, start + len(audio) / sr))
                if not started:
                    log(f"voice started after {time.time() - t0:.1f}s")
                    started = True
                self.speaker.play(audio, sr)

        stop_listening = threading.Event()

        def listen_worker():
            """Wake word, stop words and talking over the reply."""
            self.wake.reset()
            base_thr = self.ep.threshold()
            echo = 0.0
            before = collections.deque(maxlen=4)
            phrase, quiet, onset = [], 0, 0
            end_chunks = self.ep.end_chunks if barge else STOP_END_CHUNKS
            max_chunks = int(15 / CHUNK_S) if barge else STOP_MAX_CHUNKS
            while not stop_listening.is_set() and not decided.is_set():
                c = self.mic.read(timeout=0.1)
                if c is None:
                    continue
                if cfg["interrupt_with_wake_word"] and self.wake.process(c):
                    log("interrupted by wake word")
                    decide("wake")
                    return
                if not (barge or cfg["stop_words"]):
                    continue
                level = rms(c)
                if not phrase:
                    playing = self.speaker.playing()
                    # How loud talk2's own voice is in the mic (low with echo cancellation).
                    if playing and level < base_thr * 4:
                        echo = 0.9 * echo + 0.1 * level
                    thr = base_thr * (1.4 if playing else 1.0)
                    if not self.aec and playing:
                        thr = max(thr, echo * 3.0)
                    before.append(c)
                    onset = onset + 1 if level > thr else 0
                    if onset >= 2:
                        phrase, quiet, onset = list(before), 0, 0
                        state["phrase"] = True
                        if barge:
                            pause()
                    continue
                phrase.append(c)
                quiet = 0 if level > base_thr else quiet + 1
                if quiet < end_chunks and len(phrase) < max_chunks:
                    continue
                audio = np.concatenate(phrase)
                phrase = []
                heard = self.stt.transcribe(audio) if len(audio) > 0.35 * MIC_RATE else ""
                with cv:
                    recent = state["spoken"][-3:] + [s for s, _, _ in timeline] + list(pending)[:2]
                state["phrase"] = False
                if STOP.match(heard) or (barge and CANCEL.match(heard)):
                    log("stopped by voice")
                    decide("stopped")
                    return
                if not heard or sounds_like_echo(heard, recent) or not barge:
                    if heard and barge:
                        log(f"ignored (echo or noise): {heard!r}")
                    if state["paused"]:
                        resume()
                    continue
                log(f"you cut in: {heard!r}")
                said = " ".join(state["spoken"] + ([state["partial"] + " ..."] if state.get("partial") else []))
                unsaid = " ".join(list(pending) + [s for s, _, _ in timeline])
                asked = "?" in (said + " " + unsaid)
                if said:
                    n = f"(You were cut off while saying: \"{said[-300:]}\". The user talked over you" + (
                        "; they probably already knew what you were asking.)" if asked else ".)")
                else:
                    n = "(The user cut in before you started talking.)"
                decide("barge", {"text": heard, "note": n, "asked": asked})
                return

        speaker_thread = threading.Thread(target=speak_worker, daemon=True)
        listener = threading.Thread(target=listen_worker, daemon=True)
        speaker_thread.start()
        self.mic.flush()
        listener.start()

        ss = SentenceStream()
        spoken_any = False
        reply_text, tools = [], []
        prompt = f"[{self._context_line(game)}]\n" + (note + "\n" if note else "") + text

        def add(s):
            with cv:
                pending.append(s)
                state["all"].append(s)
                cv.notify_all()

        try:
            for kind, val in self.claude.ask(prompt, cancelled=decided.is_set):
                if kind == "text":
                    reply_text.append(val)
                    for s in ss.feed(val):
                        if not spoken_any:
                            log(f"first words after {time.time() - t0:.1f}s")
                        spoken_any = True
                        add(s)
                elif kind == "tool":
                    tools.append(val)
                    if not spoken_any and any(k in val for k in SLOW_TOOLS):
                        add(FILLERS[self.filler_i % len(FILLERS)])
                        self.filler_i += 1
                        spoken_any = True
            if not decided.is_set():
                for s in ss.flush():
                    add(s)
        except ClaudeError as e:
            add(str(e))
        finally:
            with cv:
                state["finished"] = True
                cv.notify_all()

        # Wait for the reply to finish playing, or for you to cut in.
        while not decided.is_set():
            idle = (not speaker_thread.is_alive() and not self.speaker.playing()
                    and not state["paused"] and not (barge and state["phrase"]))
            if idle:
                break
            time.sleep(0.03)
        stop_listening.set()
        listener.join()          # only one reader of the mic at a time
        speaker_thread.join(1.0)
        self.wake.reset()
        self.speaker.resume()

        kind = outcome.get("kind", "done")
        learned = {"done": None, "stopped": "stopped", "wake": "cut off",
                   "barge": None if (outcome.get("data") or {}).get("asked") else "cut off"}[kind]
        self.learner.record(text, "".join(reply_text).strip(), tools, outcome=learned,
                            game=game and game[1])
        return kind, outcome.get("data")
