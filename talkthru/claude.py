"""A long-lived Claude Code session that streams replies as text.

Claude Code runs once in stream-json mode and stays open, so each question
skips the CLI's start-up time and the conversation keeps its context.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid

from .log import log

VOICE_PROMPT = """You are being used as a voice assistant inside a Steam Frame VR headset.
The user is speaking to you while wearing the headset, often while playing a game, and
everything you write is read aloud by a text-to-speech voice.

- Answer conversationally, like a friend talking, in a sentence or three unless the user
  asks for more. Lead with the answer.
- Write only speakable prose: no markdown, lists, headings, tables, emoji, code blocks or URLs.
  Spell out symbols and units the way you would say them.
- The question comes from speech recognition and may contain misheard words. Use the
  most likely meaning; ask a short question back only if you really cannot tell.
- If you use a tool, don't narrate it; just give the result.
- The headset runs SteamOS (Linux)."""

READ_ONLY_TOOLS = ["WebSearch", "WebFetch", "Read", "Glob", "Grep"]


class ClaudeError(Exception):
    pass


class ClaudeSession:
    """Talks to one `claude -p` process over stream-json stdin/stdout."""

    def __init__(self, claude_bin="claude", model="", allow_commands=False,
                 extra_prompt="", cwd=None):
        found = shutil.which(claude_bin)
        if not found and claude_bin == "claude":
            found = os.path.expanduser("~/.local/bin/claude")  # native installer's spot
        self.claude_bin = found or claude_bin
        self.model = model
        self.allow_commands = allow_commands
        self.extra_prompt = extra_prompt
        self.cwd = cwd or os.path.expanduser("~")
        self.proc = None
        self.events = queue.Queue()
        self.lock = threading.Lock()
        self.last_used = 0.0
        self.busy = False
        self.turns = 0

    # -- process -----------------------------------------------------------
    def _command(self):
        tools = list(READ_ONLY_TOOLS)
        if self.allow_commands:
            tools += ["Bash", "Write", "Edit"]
        prompt = VOICE_PROMPT
        if self.extra_prompt:
            prompt += "\n\n" + self.extra_prompt
        cmd = [self.claude_bin, "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--verbose", "--include-partial-messages",
               "--append-system-prompt", prompt,
               "--tools", ",".join(tools),
               "--allowedTools", ",".join(tools)]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            if not self.claude_bin or not os.path.exists(self.claude_bin):
                raise ClaudeError("Claude Code isn't installed. Run talkthru setup.")
            env = dict(os.environ)
            # A parent Claude Code session must not leak into ours.
            for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID"):
                env.pop(k, None)
            log(f"starting Claude Code ({self.model or 'default model'})")
            self.events = queue.Queue()
            self.turns = 0
            self.proc = subprocess.Popen(
                self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=self.cwd, env=env, text=True, bufsize=1)
            threading.Thread(target=self._read_stdout, args=(self.proc,), daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()
            self.last_used = time.time()

    def stop(self):
        with self.lock:
            p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(3)
            except Exception:
                p.kill()

    def restart(self):
        self.stop()
        self.start()

    def _read_stdout(self, proc):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.events.put(json.loads(line))
            except ValueError:
                log(f"claude: {line[:200]}")
        self.events.put({"type": "_exit", "code": proc.wait()})

    def _read_stderr(self, proc):
        for line in proc.stderr:
            if line.strip():
                log(f"claude stderr: {line.strip()[:300]}")

    def _send(self, obj):
        if not self.proc or self.proc.poll() is not None:
            raise ClaudeError("Claude Code stopped")
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    # -- conversation ------------------------------------------------------
    def interrupt(self):
        """Ask Claude Code to stop the current reply."""
        if self.busy:
            try:
                self._send({"type": "control_request",
                            "request_id": f"req_{uuid.uuid4().hex[:12]}",
                            "request": {"subtype": "interrupt"}})
            except Exception as e:
                log(f"interrupt failed: {e}")

    def ask(self, text, cancelled=lambda: False, timeout=180):
        """Send one user turn; yield ("text", str) and ("tool", name) events.

        Ends when Claude Code reports the turn's result. Raises ClaudeError on
        failures the user should hear about.
        """
        self.start()
        # Drop anything left over from an earlier, abandoned turn.
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        self.busy = True
        self.turns += 1
        self.last_used = time.time()
        try:
            self._send({"type": "user", "message": {"role": "user", "content": text}})
            deadline = time.time() + timeout
            got_text = False
            while True:
                if cancelled():
                    self.interrupt()
                    self._drain_until_result(10)
                    return
                try:
                    ev = self.events.get(timeout=0.1)
                except queue.Empty:
                    if time.time() > deadline:
                        self.interrupt()
                        raise ClaudeError("Claude took too long to answer.")
                    continue
                t = ev.get("type")
                if t == "_exit":
                    raise ClaudeError(self._exit_message(ev.get("code")))
                if t == "stream_event":
                    se = ev.get("event", {})
                    if ev.get("parent_tool_use_id"):
                        continue  # a subagent's output, not the reply
                    if se.get("type") == "content_block_delta":
                        d = se.get("delta", {})
                        if d.get("type") == "text_delta" and d.get("text"):
                            got_text = True
                            yield ("text", d["text"])
                    elif se.get("type") == "content_block_start":
                        b = se.get("content_block", {})
                        if b.get("type") in ("tool_use", "server_tool_use"):
                            yield ("tool", b.get("name", ""))
                        elif b.get("type") == "text" and got_text:
                            yield ("text", "\n")  # keep text blocks apart
                elif t == "result":
                    if ev.get("is_error") and not got_text:
                        raise ClaudeError(self._result_message(ev))
                    return
        finally:
            self.busy = False
            self.last_used = time.time()

    def _drain_until_result(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                ev = self.events.get(timeout=0.2)
            except queue.Empty:
                continue
            if ev.get("type") in ("result", "_exit"):
                return

    @staticmethod
    def _result_message(ev):
        msg = str(ev.get("result") or ev.get("subtype") or "")
        low = msg.lower()
        if "login" in low or "log in" in low or "authenticat" in low or "api key" in low:
            return "Claude isn't signed in. Run talkthru login in a terminal."
        if "rate" in low and "limit" in low or "usage limit" in low:
            return "You've hit your Claude usage limit for now."
        log(f"claude error result: {msg[:300]}")
        return "Sorry, something went wrong talking to Claude."

    @staticmethod
    def _exit_message(code):
        log(f"Claude Code exited with code {code}")
        return "Claude stopped unexpectedly. Try again in a moment."
