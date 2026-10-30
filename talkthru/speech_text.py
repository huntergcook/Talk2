"""Turn streamed reply text into speakable sentences as early as possible."""

import re

_SENT_END = re.compile(r'([.!?]+["\')\]]*)(\s+)')
_CLAUSE = re.compile(r'([,;:—–])(\s+)')
# A period that doesn't end a sentence: list numbers and common abbreviations.
_NOT_AN_END = re.compile(r'(?:^|\n)\s*\d+\.$|\b(?:e\.g|i\.e|etc|vs|Mr|Mrs|Ms|Dr|St|approx|No)\.$', re.I)


class SentenceStream:
    """Feed text deltas, get back sentences ready to speak.

    The first piece is cut early (at a comma once it's long enough) so the
    voice starts sooner; later pieces are whole sentences.
    """

    def __init__(self, first_min=24, first_max=90, max_len=240):
        self.buf = ""
        self.in_code = False
        self.emitted = 0
        self.first_min, self.first_max, self.max_len = first_min, first_max, max_len

    def feed(self, text):
        self.buf += text
        out = []
        while True:
            piece = self._next()
            if piece is None:
                break
            piece = speakable(piece)
            if piece:
                out.append(piece)
                self.emitted += 1
        return out

    def flush(self):
        piece, self.buf = self.buf, ""
        piece = speakable(piece) if not self.in_code else ""
        return [piece] if piece else []

    def _next(self):
        b = self.buf
        # Paragraph or line breaks end a piece too.
        nl = b.find("\n")
        m = None
        for cand in _SENT_END.finditer(b):
            if not _NOT_AN_END.search(b[:cand.end(1)]):
                m = cand
                break
        cut = None
        if m and (nl < 0 or m.end(1) <= nl):
            cut = m.end(1), m.end()
        elif nl >= 0:
            cut = nl, nl + 1
        if cut is None:
            limit = self.first_max if self.emitted == 0 else self.max_len
            if len(b) > limit or (self.emitted == 0 and len(b) > self.first_min):
                for c in reversed(list(_CLAUSE.finditer(b))):
                    if c.end(1) >= self.first_min or len(b) > limit:
                        cut = c.end(1), c.end()
                        break
            if cut is None and len(b) > limit:
                sp = b.rfind(" ", 0, limit)
                if sp > 0:
                    cut = sp, sp + 1
            if cut is None:
                return None
        piece, self.buf = b[:cut[0]], b[cut[1]:]
        stripped = piece.strip()
        if stripped.startswith("```"):
            self.in_code = not self.in_code
            return " I've left out some code. " if self.in_code else ""
        if self.in_code:
            return ""
        return piece


_MD = [
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),      # [text](url) -> text
    (re.compile(r"https?://\S+"), "a link"),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"(\*\*|__|\*|_)(\S.*?\S|\S)\1"), r"\2"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),
    (re.compile(r"^\s*([-*+•]|\d+[.)])\s+", re.M), ""),
    (re.compile(r"^\s*>\s?", re.M), ""),
    (re.compile(r"\|"), ", "),
    (re.compile(r"[*#_~]+"), ""),
    (re.compile(r"\s*&\s*"), " and "),
    (re.compile(r"\s+"), " "),
]

# Most emoji and pictographs; Kokoro would otherwise read them oddly.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def speakable(text):
    t = _EMOJI.sub("", text)
    for pat, rep in _MD:
        t = pat.sub(rep, t)
    t = t.strip()
    return t if re.search(r"[A-Za-z0-9]", t) else ""
