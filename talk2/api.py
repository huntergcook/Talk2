"""A small local server inside the talk2 service.

- /panel: the text panel page (lists, steps, notes), updated live
- /api/*: used by Claude's tools (a separate process) for the panel and timers

It listens on 127.0.0.1 only, checks the Host header (so web pages can't
reach it by DNS tricks), and the API needs a per-run token.
"""

import json
import os
import queue
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .log import log

HERE = os.path.dirname(os.path.abspath(__file__))
TIMERS_PATH = os.path.join(config.DATA_DIR, "timers.json")


class Panel:
    def __init__(self):
        self.lock = threading.Condition()
        self.version = 0
        self.content = {"title": "", "markdown": "", "visible": False}
        self.viewers = 0

    def show(self, title, markdown):
        with self.lock:
            self.version += 1
            self.content = {"title": title, "markdown": markdown, "visible": True,
                            "time": time.strftime("%-I:%M %p")}
            self.lock.notify_all()

    def hide(self):
        with self.lock:
            self.version += 1
            self.content = dict(self.content, visible=False)
            self.lock.notify_all()

    def wait(self, seen, timeout=15):
        with self.lock:
            if self.version == seen:
                self.lock.wait(timeout)
            return self.version, dict(self.content)


class Timers:
    """Timers and reminders. Due ones go to `due` for the assistant to speak."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = []
        self.due = queue.Queue()
        self._load()
        threading.Thread(target=self._run, daemon=True).start()

    def _load(self):
        try:
            with open(TIMERS_PATH) as f:
                self.items = json.load(f)
        except (OSError, ValueError):
            self.items = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(TIMERS_PATH), exist_ok=True)
            with open(TIMERS_PATH, "w") as f:
                json.dump(self.items, f)
        except OSError:
            pass

    def add(self, seconds, label=""):
        item = {"id": uuid.uuid4().hex[:6], "at": time.time() + float(seconds),
                "label": label.strip(), "set": time.time()}
        with self.lock:
            self.items.append(item)
            self._save()
        return item

    def cancel(self, which=""):
        with self.lock:
            if not self.items:
                return []
            w = which.strip().lower()
            if w in ("", "all", "everything"):
                gone = list(self.items) if w else [min(self.items, key=lambda i: i["at"])]
            else:
                gone = [i for i in self.items if i["id"] == w or (w and w in i["label"].lower())]
            self.items = [i for i in self.items if i not in gone]
            self._save()
        return gone

    def list(self):
        with self.lock:
            return sorted(self.items, key=lambda i: i["at"])

    def _run(self):
        while True:
            now = time.time()
            with self.lock:
                fired = [i for i in self.items if i["at"] <= now]
                if fired:
                    self.items = [i for i in self.items if i["at"] > now]
                    self._save()
            for i in fired:
                late = now - i["at"]
                self.due.put(dict(i, late=late))
            time.sleep(0.5)


def describe_timer(i, now=None):
    left = max(0, int(i["at"] - (now or time.time())))
    m, s = divmod(left, 60)
    h, m = divmod(m, 60)
    when = f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")
    return f"{i['label'] or 'timer'} ({i['id']}): {when} left, at {time.strftime('%-I:%M %p', time.localtime(i['at']))}"


class Api:
    def __init__(self, panel, timers, open_panel_window, port=0):
        self.panel, self.timers = panel, timers
        self.open_panel_window = open_panel_window
        self.token = secrets.token_urlsafe(16)
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _host_ok(self):
                host = self.headers.get("Host", "")
                if host not in (f"127.0.0.1:{api.port}", f"localhost:{api.port}"):
                    self.send_error(403)
                    return False
                return True

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._host_ok():
                    return
                path = self.path.split("?")[0]
                if path in ("/", "/panel"):
                    with open(os.path.join(HERE, "panel.html"), "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/panel/events":
                    self._events()
                elif path == "/api/timers" and self._token_ok():
                    self._json({"timers": api.timers.list()})
                else:
                    self.send_error(404)

            def _token_ok(self):
                if self.headers.get("X-Talk2-Token") != api.token:
                    self.send_error(401)
                    return False
                return True

            def _events(self):
                # Server-sent events: the page holds this open and re-renders on each update.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                api.panel.viewers += 1
                try:
                    seen = -1
                    while True:
                        ver, content = api.panel.wait(seen)
                        if ver == seen:
                            self.wfile.write(b": ping\n\n")   # notices closed windows
                        else:
                            seen = ver
                            self.wfile.write(b"data: " + json.dumps(content).encode() + b"\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    api.panel.viewers -= 1

            def do_POST(self):
                if not self._host_ok() or not self._token_ok():
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except ValueError:
                    return self.send_error(400)
                p = self.path
                if p == "/api/panel":
                    api.show_panel(str(body.get("title", "")), str(body.get("markdown", "")))
                    self._json({"ok": True, "viewers": api.panel.viewers})
                elif p == "/api/panel/hide":
                    api.panel.hide()
                    self._json({"ok": True})
                elif p == "/api/timer":
                    item = api.timers.add(float(body.get("seconds", 0)), str(body.get("label", "")))
                    self._json({"ok": True, "timer": item})
                elif p == "/api/timer/cancel":
                    self._json({"ok": True, "cancelled": api.timers.cancel(str(body.get("which", "")))})
                else:
                    self.send_error(404)

        ThreadingHTTPServer.daemon_threads = True
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        log(f"panel at http://127.0.0.1:{self.port}/panel")

    @property
    def panel_url(self):
        return f"http://127.0.0.1:{self.port}/panel"

    def show_panel(self, title, markdown):
        self.panel.show(title, markdown)
        if self.panel.viewers == 0:
            # Give an already-open page a moment to reconnect before opening another.
            def later():
                time.sleep(0.6)
                if self.panel.viewers == 0:
                    try:
                        self.open_panel_window(self.panel_url)
                    except Exception as e:
                        log(f"couldn't open the panel window: {e}")
            threading.Thread(target=later, daemon=True).start()
