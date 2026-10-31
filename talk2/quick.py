"""Instant commands: common requests handled on the headset without asking
Claude, in a fraction of a second. Only whole, simple commands match, e.g.
"open the browser" but not "open the browser and look up the weather", which
goes to Claude.
"""

import re
import time

from . import actions, context
from .actions import ActionError

W = r"(?:please\s+)?"
END = r"\W*(?:please|now|for me|thanks)?\W*$"

NUMBERS = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
           "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "half an": 0.5, "a couple": 2, "a few": 3}
UNIT = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600}


def _num(s):
    s = s.strip().lower()
    try:
        return float(s)
    except ValueError:
        return NUMBERS.get(s)


def _duration(text):
    """'5 minutes', 'an hour and a half', '90 seconds', '1 hour 20 minutes' -> seconds."""
    total, found = 0.0, False
    for n, unit in re.findall(r"(\d+(?:\.\d+)?|[a-z]+(?: [a-z]+)?)\s+(second|sec|minute|min|hour)s?\b", text):
        v = _num(n.split()[-1] if _num(n) is None else n)
        if v is None:
            continue
        total += v * UNIT[unit]
        found = True
    if re.search(r"\band a half\b", text) and found:
        total += 1800 if "hour" in text else 30
    return total if found and total > 0 else None


def spoken_duration(secs):
    secs = int(round(secs))
    h, rem = divmod(secs, 3600)
    mins, s = divmod(rem, 60)
    plural = lambda n, w: f"{n} {w}{'s' if n != 1 else ''}"
    parts = ([plural(h, "hour")] if h else []) + ([plural(mins, "minute")] if mins else []) + \
        ([plural(s, "second")] if s and not h else [])
    return " ".join(parts) or "0 seconds"


def _with_timers(fn):
    def wrapped(a, m):
        if not a.timers:
            return None
        return fn(a, m)
    return wrapped


def h_open(a, m):
    what = m.group("what")
    if re.search(r"\b(and|then|to|with|search|look up|find)\b", what):
        return None   # more than just opening something: let Claude handle it
    if re.search(r"\b(panel|list)\b", what):
        return None
    if actions.find_game(what) and not actions.find_app(what):
        return actions.launch_game(what)
    return actions.open_app(what)


def h_launch(a, m):
    return actions.launch_game(m.group("what"))


def h_vol_rel(a, m):
    up = m.group("dir") in ("up", "louder", "raise", "increase")
    step = 10 if not m.group("lot") else 25
    return actions.set_volume(change=step if up else -step)


def h_vol_set(a, m):
    return actions.set_volume(percent=_num(m.group("n")))


def h_mute(a, m):
    return actions.set_volume(mute=m.group(1).lower() != "unmute")


def h_media(a, m):
    w = m.group("act").lower()
    act = {"pause": "pause", "stop": "pause", "play": "play", "resume": "play",
           "next": "next", "skip": "next", "previous": "previous", "go back": "previous", "back": "previous"}[w]
    return actions.media(act)


def h_passthrough(a, m):
    s = (m.group("state") or "toggle").lower()
    return actions.passthrough({"on": "on", "off": "off"}.get(s, "toggle"))


def h_see_room(a, m):
    return actions.passthrough("on")


def h_time(a, m):
    return time.strftime("It's %-I:%M.")


def h_battery(a, m):
    b = context.battery()
    if not b:
        return "I can't read the battery right now."
    s = f"{b['percent']} percent"
    if b["status"] == "charging":
        s += ", and charging"
    elif "minutes_left" in b:
        h, mins = divmod(b["minutes_left"], 60)
        s += f", about {h} hour{'s' if h != 1 else ''} {mins} left" if h else f", about {mins} minutes left"
    return s + "."


def h_hide_panel(a, m):
    a.api.panel.hide()
    return "Done."


@_with_timers
def h_timer(a, m):
    secs = _duration(m.group("dur"))
    if not secs:
        return None
    label = (m.groupdict().get("label") or "").strip()
    a.timers.add(secs, label)
    when = spoken_duration(secs)
    return f"Okay, {when}." if not label else f"Got it, I'll remind you in {when}."


@_with_timers
def h_cancel_timer(a, m):
    gone = a.timers.cancel("all" if m.group("all") else "")
    return "Cancelled." if gone else "There's no timer running."


@_with_timers
def h_timer_left(a, m):
    items = a.timers.list()
    if not items:
        return "No timers running."
    i = items[0]
    left = int(i["at"] - time.time())
    mn, s = divmod(max(0, left), 60)
    return (f"{mn} minute{'s' if mn != 1 else ''} and {s} seconds left" if mn else f"{s} seconds left") + \
        (f" on {i['label']}." if i["label"] else ".")


PATTERNS = [
    (rf"^{W}(?:open|start|launch|bring up|pull up)\s+(?P<what>.+?){END}", h_open),
    (rf"^{W}(?:play|launch|start)\s+(?:the game\s+)?(?P<what>.+?){END}", None),  # see handle()
    (rf"^{W}(?:turn|crank)\s+(?:the\s+|it\s+)?(?:volume\s+)?(?P<dir>up|down)(?P<lot>\s+a lot)?(?:\s+the volume)?{END}", h_vol_rel),
    (rf"^{W}(?P<dir>louder|quieter|softer)(?P<lot>\s+a lot)?{END}", lambda a, m: actions.set_volume(change=10 if m.group('dir') == 'louder' else -10)),
    (rf"^{W}(?:volume|turn the volume|set (?:the )?volume)\s+(?P<dir>up|down)(?P<lot>\s+a lot)?{END}", h_vol_rel),
    (rf"^{W}(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+)?(?P<n>\d+|[a-z]+)\s*(?:percent|%)?{END}", h_vol_set),
    (rf"^{W}(mute|unmute)(?:\s+(?:the\s+)?(?:volume|sound|audio))?{END}", h_mute),
    (rf"^{W}(?P<act>pause|resume|next|skip|previous|go back)(?:\s+(?:the\s+|this\s+)?(?:music|song|track|video|one|this))?{END}", h_media),
    (rf"^{W}(?:turn\s+)?(?:passthrough|pass through|room view)\s*(?P<state>on|off)?{END}", h_passthrough),
    (rf"^{W}turn\s+(?P<state>on|off)\s+(?:the\s+)?(?:passthrough|pass through|room view){END}", h_passthrough),
    (rf"^{W}(?:let me see|show me)\s+(?:my\s+|the\s+)?(?:room|surroundings|real world){END}", h_see_room),
    (rf"^\W*what(?:'s| is) the time{END}|^\W*what time is it{END}", h_time),
    (rf"^\W*(?:how much\s+)?battery(?:\s+(?:left|level|life))?\W*$|^\W*how(?:'s| is) (?:my|the) battery{END}|^\W*how much battery(?: do i have)?(?: left)?{END}", h_battery),
    (rf"^{W}(?:close|hide|clear)\s+(?:the\s+)?(?:panel|list){END}", h_hide_panel),
    (rf"^{W}(?:set|start)\s+(?:a\s+)?timer\s+(?:for\s+)?(?P<dur>.+?)(?:\s+(?:for|called|named)\s+(?P<label>.+?))?{END}", h_timer),
    (rf"^{W}(?:set\s+)?(?:a\s+)?(?P<dur>.+?)\s+timer(?:\s+(?:for|called)\s+(?P<label>.+?))?{END}", h_timer),
    (rf"^{W}remind me in\s+(?P<dur>.+?)\s+to\s+(?P<label>.+?){END}", h_timer),
    (rf"^{W}remind me to\s+(?P<label>.+?)\s+in\s+(?P<dur>.+?){END}", h_timer),
    (rf"^{W}(?:cancel|stop|clear)\s+(?P<all>all\s+)?(?:the\s+|my\s+)?(?:timers?|reminders?){END}", h_cancel_timer),
    (rf"^\W*how (?:much time|long)(?: is)? left(?: on (?:the|my) timer)?{END}", h_timer_left),
]
COMPILED = [(re.compile(p, re.I), h) for p, h in PATTERNS]


def handle(assistant, text):
    """Returns what to say if text was an instant command, else None."""
    t = text.strip()
    for rx, h in COMPILED:
        m = rx.match(t)
        if not m:
            continue
        if h is None:   # "play X": a game if X is one, otherwise music etc. -> Claude
            what = m.group("what")
            if actions.find_game(what):
                return actions.launch_game(what)
            continue
        try:
            out = h(assistant, m)
        except ActionError:
            return None   # let Claude try another way
        if out is not None:
            return out
    return None
