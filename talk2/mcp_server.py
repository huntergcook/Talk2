"""talk2's tools for Claude, as an MCP server on stdin/stdout.

Claude Code starts this alongside its session (see claude.py). Headset
actions run here directly; the panel and timers go to the talk2 service
over its local API, since that's the process that shows and speaks them.
"""

import base64
import json
import os
import sys
import urllib.request

from . import actions, context
from .actions import ActionError

API = f"http://127.0.0.1:{os.environ.get('TALK2_API_PORT', '0')}"
TOKEN = os.environ.get("TALK2_API_TOKEN", "")


def api(path, body=None):
    req = urllib.request.Request(API + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Talk2-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except OSError as e:
        raise ActionError("talk2's service isn't reachable.") from e


def _s(desc, **props):
    return {"type": "object", "properties": props, "additionalProperties": False, **desc}


STR = {"type": "string"}
NUM = {"type": "number"}

TOOLS = [
    ("open_app", "Open an app on the headset by name: browser, Chromium, Firefox, terminal, files, "
     "Discord, Spotify, settings, Steam store/library/friends... Optional args, e.g. a URL for a browser.",
     _s({"required": ["name"]}, name=STR, args={"type": "array", "items": STR})),
    ("open_url", "Open a web page in the browser (Steam's browser in Game Mode).",
     _s({"required": ["url"]}, url=STR)),
    ("launch_game", "Start an installed Steam game by (approximate) name.",
     _s({"required": ["name"]}, name=STR)),
    ("quit_game", "Close the running game. Only when the user clearly asks to quit or close it.", _s({})),
    ("list_games", "List installed Steam games, optionally filtered by a word.", _s({}, query=STR)),
    ("set_volume", "Set headset volume: percent (0-100) or change (+/- points), and/or mute (true/false/\"toggle\").",
     _s({}, percent=NUM, change=NUM, mute={"type": ["boolean", "string"]})),
    ("media", "Control music/video players: play, pause, toggle, next, previous, stop.",
     _s({"required": ["action"]}, action={"type": "string", "enum": ["play", "pause", "toggle", "next", "previous", "stop"]})),
    ("passthrough", "Turn the Steam Frame's passthrough (Room View: see your real surroundings) on, off or toggle.",
     _s({"required": ["state"]}, state={"type": "string", "enum": ["on", "off", "toggle"]})),
    ("look", "See what the user is looking at right now: returns a screenshot of their view. Use it when they "
     "refer to something on screen: 'what is this', 'what does this say', 'which way do I go', 'read this'.", _s({})),
    ("show_panel", "Show text on a panel in the headset. Use for anything worth reading rather than hearing: "
     "lists, steps, instructions, recipes, builds, comparisons, or when asked to show/write something down. "
     "Markdown: headings, bullets, numbered steps, '- [ ] item' checkboxes, bold, tables. Replaces the current panel.",
     _s({"required": ["title", "markdown"]}, title=STR, markdown=STR)),
    ("hide_panel", "Close the text panel.", _s({})),
    ("set_timer", "Start a timer or reminder; talk2 says the label out loud when it's due. Give seconds.",
     _s({"required": ["seconds"]}, seconds=NUM, label=STR)),
    ("list_timers", "List running timers and reminders.", _s({})),
    ("cancel_timer", "Cancel a timer by label or id; 'all' cancels every one; empty cancels the soonest.",
     _s({}, which=STR)),
    ("remember", "Save a lasting fact or preference about the user (their setup, likes, names, how they want "
     "things done) to your notes, which you'll see in every future session. One short sentence.",
     _s({"required": ["fact"]}, fact=STR)),
    ("forget", "Remove notes mentioning a word or phrase, when the user asks you to forget something.",
     _s({"required": ["phrase"]}, phrase=STR)),
    ("status", "Headset status: game running, battery and time left, volume, passthrough, mode, now playing.", _s({})),
]


def call(name, args):
    """Returns MCP content blocks."""
    text = lambda s: [{"type": "text", "text": s}]
    if name == "open_app":
        return text(actions.open_app(args["name"], args.get("args") or ()))
    if name == "open_url":
        return text(actions.open_url(args["url"]))
    if name == "launch_game":
        return text(actions.launch_game(args["name"]))
    if name == "quit_game":
        return text(actions.quit_game())
    if name == "list_games":
        games = actions.list_games(args.get("query", ""))
        return text("\n".join(games) if games else "No matching games installed.")
    if name == "set_volume":
        return text(actions.set_volume(args.get("percent"), args.get("change"), args.get("mute")))
    if name == "media":
        return text(actions.media(args["action"]))
    if name == "passthrough":
        return text(actions.passthrough(args["state"]))
    if name == "look":
        path = actions.look()
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        mime = "image/jpeg" if path.endswith(".jpg") else "image/png"
        return [{"type": "image", "data": data, "mimeType": mime},
                {"type": "text", "text": f"Screenshot of the user's current view ({path})."}]
    if name == "show_panel":
        r = api("/api/panel", {"title": args["title"], "markdown": args["markdown"]})
        return text("Panel shown." if r.get("ok") else "Couldn't show the panel.")
    if name == "hide_panel":
        api("/api/panel/hide", {})
        return text("Panel closed.")
    if name == "set_timer":
        r = api("/api/timer", {"seconds": args["seconds"], "label": args.get("label", "")})
        from .api import describe_timer
        return text("Set: " + describe_timer(r["timer"]))
    if name == "list_timers":
        from .api import describe_timer
        items = api("/api/timers")["timers"]
        return text("\n".join(describe_timer(i) for i in items) or "No timers running.")
    if name == "cancel_timer":
        gone = api("/api/timer/cancel", {"which": args.get("which", "")})["cancelled"]
        return text(f"Cancelled {len(gone)}: " + ", ".join(i["label"] or i["id"] for i in gone) if gone else "No matching timer.")
    if name == "remember":
        from . import memory
        memory.remember(args["fact"])
        return text("Saved.")
    if name == "forget":
        from . import memory
        n = memory.forget(args["phrase"])
        return text(f"Removed {n} note(s)." if n else "Nothing matched.")
    if name == "status":
        s = context.status_dict()
        s["now_playing"] = actions.now_playing()
        return text(json.dumps(s))
    raise ActionError(f"Unknown tool {name}.")


def main():
    out = sys.stdout
    sys.stdout = sys.stderr   # nothing else may write to the protocol stream

    def send(obj):
        out.write(json.dumps(obj) + "\n")
        out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue   # notifications (initialized, cancelled) need no reply
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "talk2", "version": "0.2"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}})
        elif method == "tools/call":
            try:
                content = call(params.get("name"), params.get("arguments") or {})
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": content}})
            except ActionError as e:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": str(e)}], "isError": True}})
            except Exception as e:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}})


if __name__ == "__main__":
    main()
