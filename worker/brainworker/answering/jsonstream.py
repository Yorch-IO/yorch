"""One string field of a JSON object, decoded as the object streams in.

`answer.SCHEMA` is `{suficiente, respuesta, citas, motivo}` and the model returns
it in JSON mode, so the bytes arriving from a streaming generation are a JSON
envelope rather than prose. Showing them raw would show the reader a brace and a
field name; waiting for the object to close would not be streaming at all. This
walks the envelope as it arrives and hands back only the decoded characters of
one named field.

Pure on purpose — no provider, no store, no I/O. It is the same testability
decision `lib/radial.ts`, `lib/force.ts` and `brainworker/auditversion.py`
embody: what is worth asserting is the decoding, and a test that needed a live
Vertex stream to check an escape sequence split across two chunks would run
rarely enough to be worth nothing. Every case below is reachable from a table of
strings.

Four things that are easy to get wrong and are therefore what the tests are
about:

- **The field name can appear inside an earlier string value.** A model writing
  `{"motivo": "falta la respuesta", "respuesta": "..."}` must not have the word
  inside `motivo` mistaken for the key. So this is a real (if small) scanner
  that knows whether it is inside a string, not a `find()`.
- **An escape can straddle a chunk boundary.** `\\n` can arrive as `\\` then `n`,
  and `\\uD83D` as `\\u`, `D8`, `3D`. State survives between `feed` calls.
- **A surrogate pair is two escapes and one character.** A lone high surrogate
  handed to the caller is a string that raises on `encode()`, so the high half
  is held until its partner arrives.
- **A chunk id can straddle a chunk boundary too.** `answer._clean` strips
  `chk_[0-9a-f]{24}` from the prose, and text released before the id is complete
  would show the reader an id that the finished answer does not contain. So a
  tail is withheld — see `HOLD_BACK`.
"""

from __future__ import annotations

#: Characters withheld from the caller until more arrive or the field closes.
#: A chunk id is `chk_` plus 24 hex digits — 28 characters — and `_clean` also
#: removes a bracketed *group* of them with the surrounding punctuation, so the
#: window has to cover more than one id's worth to avoid releasing the first
#: half of a citation the finished text will not have. 60 is two ids and change.
#:
#: The cost of the withholding is that the last few dozen characters of an
#: answer arrive at the end rather than as they are written. The cost of not
#: withholding is text that visibly changes after the fact, which reads as a
#: mistake rather than as a stream.
HOLD_BACK = 60

_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

_HIGH = range(0xD800, 0xDC00)
_LOW = range(0xDC00, 0xE000)

#: What an unpaired surrogate becomes. U+FFFD rather than the surrogate itself,
#: because a lone surrogate is a `str` Python will build and then refuse to
#: encode — the failure would land far away, in whatever wrote the text out.
_REPLACEMENT = "�"


class FieldStreamer:
    """Decoded text of one top-level string field, fed the raw JSON in pieces.

    >>> s = FieldStreamer("respuesta")
    >>> s.feed('{"suficiente": true, "respuesta": "hola')
    ''
    >>> s.feed(' mundo"}')
    'hola mundo'

    Nothing is released until `HOLD_BACK` characters are behind it, which is why
    the first `feed` above returns nothing. Set `hold_back=0` to disable that.
    """

    def __init__(self, field: str, *, hold_back: int = HOLD_BACK) -> None:
        self._field = field
        self._hold_back = hold_back

        # -- scanner state, all of it surviving between feeds --------------
        self._stack: list[str] = []
        self._in_string = False
        self._escape = False
        self._unicode: str | None = None
        self._expect_key = False
        self._current: list[str] = []
        self._pending_key: str | None = None
        self._capturing = False
        self._high: int | None = None

        self._buffer = ""
        self._released = 0
        self._closed = False

    # -- what the caller reads ---------------------------------------------

    @property
    def closed(self) -> bool:
        """True once the field's own string has ended.

        Not the same as the object being complete: everything after the field
        is still scanned, and still costs nothing.
        """
        return self._closed

    @property
    def text(self) -> str:
        """Everything decoded so far, released or not.

        The authoritative answer is still whatever `json.loads` makes of the
        whole envelope — this is what was *shown*, which is a different thing
        and is why both exist.
        """
        return self._buffer

    def feed(self, chunk: str) -> str:
        """Scan a piece of the envelope; return newly releasable characters."""
        if self._closed:
            # The field is done. Later fields are somebody else's business and
            # scanning them would only burn cycles on the largest one (`citas`).
            return ""
        for ch in chunk:
            self._step(ch)
            if self._closed:
                break
        return self._release()

    def finish(self) -> str:
        """Release the withheld tail. Call once, when the stream ends.

        A stream can end without the field closing — `MAX_TOKENS` truncates the
        envelope mid-string — and the characters already decoded are still the
        best draft there is. Withholding them for ever because a closing quote
        never arrived would lose the whole answer to a truncation.
        """
        self._closed = True
        return self._release()

    # -- the scanner --------------------------------------------------------

    def _step(self, ch: str) -> None:
        if self._in_string:
            self._string_char(ch)
            return

        if ch == '"':
            self._in_string = True
            self._current = []
            # A value string opening while the key that named it is ours is the
            # one string in the document worth decoding.
            # `_pending_key` is only ever set by a key at the top level (see
            # `_end_string`), so this needs to know one further thing: that what
            # is opening is the value and not another key.
            self._capturing = not self._expect_key and self._pending_key == self._field
            return
        if ch == "{":
            self._stack.append("{")
            self._expect_key = True
            self._pending_key = None
            return
        if ch == "[":
            self._stack.append("[")
            self._expect_key = False
            return
        if ch in "}]":
            if self._stack:
                self._stack.pop()
            self._expect_key = bool(self._stack) and self._stack[-1] == "{"
            self._pending_key = None
            return
        if ch == ",":
            self._expect_key = bool(self._stack) and self._stack[-1] == "{"
            self._pending_key = None
            return
        if ch == ":":
            self._expect_key = False
            return

    def _string_char(self, ch: str) -> None:
        if self._unicode is not None:
            self._unicode += ch
            if len(self._unicode) == 4:
                self._code_point(self._unicode)
                self._unicode = None
            return

        if self._escape:
            self._escape = False
            if ch == "u":
                self._unicode = ""
                return
            self._emit(_SIMPLE_ESCAPES.get(ch, ch))
            return

        if ch == "\\":
            self._escape = True
            return

        if ch == '"':
            self._end_string()
            return

        self._emit(ch)

    def _end_string(self) -> None:
        self._in_string = False
        value = "".join(self._current)
        self._current = []
        if self._capturing:
            self._flush_high()
            self._capturing = False
            self._closed = True
            return
        if self._expect_key and len(self._stack) == 1 and self._stack[0] == "{":
            self._pending_key = value

    def _code_point(self, digits: str) -> None:
        try:
            code = int(digits, 16)
        except ValueError:
            # Not hex. The document is malformed; showing the escape verbatim
            # beats dropping the rest of the answer over it.
            self._emit("\\u" + digits)
            return

        if code in _HIGH:
            self._flush_high()
            self._high = code
            return
        if code in _LOW and self._high is not None:
            high, self._high = self._high, None
            self._emit(chr(0x10000 + (high - 0xD800) * 0x400 + (code - 0xDC00)))
            return
        self._flush_high()
        self._emit(chr(code))

    def _flush_high(self) -> None:
        if self._high is not None:
            self._high = None
            self._emit(_REPLACEMENT)

    def _emit(self, text: str) -> None:
        # A pending high surrogate followed by anything but its partner is
        # unpaired, and the partner can only arrive as another `\u` escape.
        if self._high is not None and text != _REPLACEMENT:
            self._flush_high()
        if self._capturing:
            self._buffer += text
        else:
            self._current.append(text)

    # -- the hold-back ------------------------------------------------------

    def _release(self) -> str:
        end = len(self._buffer) if self._closed else len(self._buffer) - self._hold_back
        if end <= self._released:
            return ""
        out = self._buffer[self._released:end]
        self._released = end
        return out
