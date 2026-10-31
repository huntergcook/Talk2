"""Learning from use.

Every exchange is logged with how it went, judged from what you did next:
- "repeated": you asked the same thing again soon after
- "corrected": your next words started with "no", "I meant", "that's wrong"...
- "cut off": you stopped it or talked over it to say something else
- "ok": none of those

When a conversation ends, Claude reads that log and its current notes and
rewrites the notes: what you like, what worked, what to avoid, and shortcuts
for things you ask often. The notes are given to Claude at the start of
every session. Everything stays on the headset in ~/.local/share/talk2.
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import config
from .log import log

MEMORY_PATH = os.path.join(config.DATA_DIR, "memory.md")
HISTORY_PATH = os.path.join(config.DATA_DIR, "history.jsonl")
MAX_MEMORY_CHARS = 6000

CORRECTION = re.compile(
    r"^\W*(no\b|nope|not (that|this|what)|that'?s (not|wrong)|wrong|i (meant|said|asked)|"
    r"you (misheard|got it wrong|didn'?t)|actually,?\s+i|i didn'?t (say|ask|mean))", re.I)

DISTILL_PROMPT = """You maintain the notes a voice assistant keeps about its user, so it gets
better the more it's used. The assistant runs in the user's Steam Frame VR headset and talks to
them by voice while they play. Below are the current notes and a log of the latest conversation.
Each exchange has an outcome judged from what the user did next:
  ok = no repeat or correction (likely a good answer)
  repeated = they asked the same thing again soon after (the answer missed)
  corrected = their next words corrected it ("no, I meant...")
  cut off = they stopped it or talked over it (too long, wrong, or they already knew)
  stopped = they told it to stop talking

Rewrite the notes. Keep what generalizes:
- About the user: stated preferences, their setup, the games they play, people or names they use.
- What works: answer styles and approaches that went well, especially for their frequent requests.
- What to avoid: what got repeated, corrected or cut off, and what to do instead.
- Shortcuts: requests they make often and exactly how to handle them (which tool, which app,
  which settings), so next time it's one step.
Drop anything one-off, stale or contradicted by newer evidence. Merge duplicates. Never record
passwords, keys, tokens, payment details or anything they asked to keep private. Don't guess
about the user beyond the evidence.

Reply with ONLY the new notes in Markdown, under 350 words, using those four headings.

## Current notes
{memory}

## Latest conversation
{log}"""


def load():
    try:
        with open(MEMORY_PATH) as f:
            return f.read()[:MAX_MEMORY_CHARS].strip()
    except OSError:
        return ""


def save(text):
    os.makedirs(os.path.dirname(MEMORY_PATH), exist_ok=True)
    tmp = MEMORY_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(text.strip()[:MAX_MEMORY_CHARS] + "\n")
    os.replace(tmp, MEMORY_PATH)


def remember(fact):
    """Add one line under 'About the user' right away (the `remember` tool)."""
    fact = " ".join(fact.split())
    text = load()
    if fact.lower() in text.lower():
        return
    if "## About the user" not in text:
        text = "## About the user\n" + text
    text = text.replace("## About the user", f"## About the user\n- {fact}", 1)
    save(text)


def forget(phrase):
    """Remove every line mentioning phrase. Returns how many went."""
    p = phrase.lower().strip()
    lines = load().splitlines()
    keep = [l for l in lines if not (p and p in l.lower() and not l.startswith("#"))]
    save("\n".join(keep))
    return len(lines) - len(keep)


def prompt_section():
    text = load()
    if not text:
        return ""
    return ("What you've learned about this user from past conversations (use it; if they say "
            "something that contradicts it, go with what they say now):\n" + text)


def _similar(a, b):
    norm = lambda s: re.sub(r"[^a-z0-9 ]+", "", s.lower())
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


class Learner:
    """Tracks one conversation's exchanges and their outcomes."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.session = []
        self.lock = threading.Lock()

    def before_question(self, text):
        """Judge the previous exchange from how this question starts."""
        with self.lock:
            if not self.session:
                return
            prev = self.session[-1]
            if prev["outcome"] not in (None, "ok"):
                return
            if CORRECTION.match(text):
                prev["outcome"] = "corrected"
            elif time.time() - prev["t"] < 120 and _similar(text, prev["q"]) >= 0.72:
                prev["outcome"] = "repeated"

    def record(self, question, reply, tools=(), outcome=None, game=None, kind="claude"):
        with self.lock:
            self.session.append({"t": time.time(), "q": question, "a": reply[:600], "tools": list(tools),
                                 "outcome": outcome, "game": game, "kind": kind})

    def mark_last(self, outcome):
        with self.lock:
            if self.session and self.session[-1]["outcome"] in (None, "ok"):
                self.session[-1]["outcome"] = outcome

    def end_conversation(self):
        """Save the log and, in the background, have Claude update its notes."""
        with self.lock:
            session, self.session = self.session, []
        if not session:
            return
        for e in session:
            e["outcome"] = e["outcome"] or "ok"
        try:
            os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
            with open(HISTORY_PATH, "a") as f:
                for e in session:
                    f.write(json.dumps(e) + "\n")
        except OSError as e:
            log(f"couldn't save history: {e}")
        if self.cfg.get("learn", True):
            threading.Thread(target=self._distill, args=(session,), daemon=True).start()

    def _distill(self, session):
        lines = []
        for e in session:
            tools = f" [tools: {', '.join(e['tools'])}]" if e["tools"] else ""
            game = f" (playing {e['game']})" if e.get("game") else ""
            lines.append(f"- User{game}: {e['q']}\n  Assistant{tools}: {e['a']}\n  Outcome: {e['outcome']}")
        prompt = DISTILL_PROMPT.format(memory=load() or "(none yet)", log="\n".join(lines))
        claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")}
        t = time.time()
        try:
            r = subprocess.run([claude, "-p", "--model", self.cfg.get("memory_model") or "haiku",
                                "--tools", "", "--strict-mcp-config", "--output-format", "text"],
                               input=prompt, capture_output=True, text=True, timeout=120,
                               env=env, cwd=config.DATA_DIR)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"memory update failed: {e}")
            return
        new = r.stdout.strip()
        if r.returncode != 0 or "##" not in new or len(new) < 40:
            log(f"memory update skipped (exit {r.returncode}): {(r.stderr or new)[:200]}")
            return
        save(new)
        log(f"memory updated in {time.time() - t:.1f}s ({len(new)} chars)")
