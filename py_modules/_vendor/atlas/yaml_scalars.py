"""A deliberately small reader for the flat half of a YAML configuration.

Two of the standalone emulators RetroDECK ships keep the paths atlas answers
from in YAML — RPCS3's ``vfs.yml`` maps ``/dev_hdd0/`` to the directory its
save tree hangs off, Vita3K's ``config.yml`` carries ``pref-path`` — and atlas
ships zero runtime dependencies, so there is no YAML library to reach for.

This module reads the part of such a file that can be read *exactly*, in the
same spirit as the Cemu XML read and the melonDS legacy-INI scan: top-level
``key: value`` scalars, quoted or bare, with ``$(Name)`` substitution from
another key in the same file — which is how RPCS3 composes every device path
off ``$(EmulatorDir)``.

Everything else is refused rather than guessed, and the refusal is *named* so a
caller can tell "atlas did not read this" from "this is not set":

- a key whose value is a **nested block, a list or a multi-line scalar** is
  recorded in :attr:`YamlScalars.skipped` by name and not parsed. RPCS3's
  ``/dev_usb***/`` is one of those, and nothing atlas answers needs it, so the
  file still speaks while the reader stays honest about which key it passed
  over. Asking for a skipped key raises rather than answering a default.
- **anchors, aliases, tags and multiple documents** change meaning beyond the
  line they sit on — an alias can make a key elsewhere mean something this
  reader never saw — so they refuse the whole file, not one key.
- a **substitution cycle** refuses rather than looping.

What this is not: a YAML parser. It does not build a tree, it does not type
values (everything is the text as written), and it will refuse or skip a great
deal of legal YAML. That is the point — the alternative is a half-parser whose
mistakes look like answers.

Pure text in, value object out. No I/O — the machine seam supplies the text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# The refusal codes this reader answers with. They name what was found, not
# what the caller wanted, so a message can say which construct stopped the read.
# A *second* document in the stream. The opening ``---`` is harmless and common
# — Vita3K's config.yml carries one — but a second document may redefine every
# key the first one set, and which of the two an emulator reads is not a
# question this reader can answer from the text.
REFUSAL_SECOND_DOCUMENT = "second-document"
REFUSAL_ANCHOR = "anchor-or-alias"
REFUSAL_TAG = "tag"
REFUSAL_SUBSTITUTION_CYCLE = "substitution-cycle"
REFUSAL_SUBSTITUTION_UNKNOWN = "substitution-unknown"
# An indented line under no key at all, or a top-level line that is not a
# ``key: value`` pair. Either way the file is not the flat mapping this reader
# reads, and no line in it can be attributed to a key with confidence.
REFUSAL_NOT_A_FLAT_MAPPING = "not-a-flat-mapping"

# How deep a chain of ``$(Name)`` substitutions may go before the reader calls
# it a cycle. RPCS3's file is one level deep; the bound exists so a malformed
# file refuses instead of spinning.
_MAX_SUBSTITUTION_DEPTH = 8


@dataclass(frozen=True, slots=True)
class YamlScalars:
    """What the reader established, and what it deliberately did not.

    ``values`` holds the top-level scalars it read, substitutions resolved.
    ``skipped`` names the top-level keys whose value was a construct this
    reader does not read — the caller decides whether that matters for the key
    it wants. ``refusal`` is set exactly when the whole file was refused, and
    then ``values`` is empty: a file carrying an alias may mean something
    different from what its plain lines say, so no line from it is reported.
    """

    values: Mapping[str, str] = field(default_factory=dict)
    skipped: tuple[str, ...] = ()
    refusal: str | None = None

    def get(self, key: str) -> str | None:
        """The value at *key*, or ``None`` where the file states none.

        Raises :class:`KeyError` for a key this reader skipped: the file *does*
        state something there and the reader did not read it, which is not the
        same fact as an absent key and must never collapse into one.
        """
        if key in self.skipped:
            raise KeyError(
                f"{key!r} is stated as a construct this reader does not read "
                f"(nested block, list or multi-line scalar) — its value is unread, not absent"
            )
        return self.values.get(key)


def _unquote(value: str) -> str:
    """A scalar as written: quotes come off, everything else stays verbatim.

    Only the two quote forms are handled, and only when they wrap the whole
    scalar. No escape processing: a double-quoted YAML scalar may carry
    backslash escapes, and pretending otherwise would silently change a path.
    A value that opens a quote and does not close it is left verbatim, the way
    the melonDS and marker readers leave an unterminated quote — visibly odd
    rather than invented.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _strip_comment(value: str) -> str:
    """A bare scalar's trailing comment, cut the way YAML cuts it.

    ``#`` begins a comment only where whitespace precedes it, so a ``#`` inside
    a word — a path segment, a colour — stays part of the value. *Whitespace*
    is the whole of it, not a space: a tab before the ``#`` opens a comment
    just as well, and looking only for ``" #"`` left the comment in the value.
    """
    for index in range(1, len(value)):
        if value[index] == "#" and value[index - 1].isspace():
            return value[:index]
    return value


def _comment_after_quote(value: str) -> str:
    """A quoted scalar's trailing comment, cut only where the quote closed first.

    ``path: "/tmp/x" # note`` is a quoted path with a comment after it, and the
    quote-wrapping test alone does not see that: the value neither opens and
    closes with the same character nor is unterminated, so it came back with
    its quotes and the comment still attached — a path no emulator would ever
    write. The comment comes off only when a closing quote precedes it and
    whitespace precedes the ``#``, which is the same rule bare scalars follow;
    anything else between the closing quote and the end of the line is left
    verbatim rather than guessed at.
    """
    quote = value[0]
    end = value.find(quote, 1)
    if end == -1 or end == len(value) - 1:
        return value
    rest = value[end + 1 :]
    if rest[:1].isspace() and rest.lstrip().startswith("#"):
        return value[: end + 1]
    return value


def _scalar(raw: str) -> str:
    """One value as written — quoted scalars keep their content, bare ones lose comments."""
    stripped = raw.strip()
    if stripped[:1] in ("\"", "'"):
        return _unquote(_comment_after_quote(stripped))
    return _strip_comment(stripped).strip()


def _is_skipping_value(value: str) -> bool:
    """Does this value open a construct the reader records instead of reading?

    Four of them. A ``|`` or ``>`` opens a multi-line scalar; ``- `` opens a
    list; ``[`` and ``{`` open a flow collection — Vita3K writes ``[]`` for an
    empty module list, and reporting that as the two-character string ``"[]"``
    would be a value nothing configured. An empty value with indented lines
    under it is the fifth, and only the caller can see what follows.
    """
    return value in ("|", ">", "|-", ">-", "|+", ">+") or value.startswith(("- ", "[", "{"))


def _refusal_in(value: str) -> str | None:
    """The whole-file refusal this value triggers, if any."""
    if value.startswith(("&", "*")):
        return REFUSAL_ANCHOR
    if value.startswith("!"):
        return REFUSAL_TAG
    return None


def _split_key(line: str) -> tuple[str, str] | None:
    """``key: value`` split at the first colon-space, or a bare ``key:``.

    A key may itself contain a colon — RPCS3's ``/dev_usb***/`` does not, but
    ``$(EmulatorDir)`` sits left of one — so the split is on ``": "`` first and
    on a trailing colon second, which is what YAML's own plain-key rule comes
    down to for these files.
    """
    if line.endswith(":"):
        return line[:-1].strip(), ""
    marker = line.find(": ")
    if marker == -1:
        return None
    return line[:marker].strip(), line[marker + 2 :]


def _substitute(
    values: dict[str, str], fallbacks: Mapping[str, str]
) -> tuple[dict[str, str], str | None]:
    """Resolve ``$(Name)`` against the file's own keys, or refuse.

    RPCS3 writes ``$(EmulatorDir): /path/`` and then composes every device path
    off it — the defining key is spelled exactly like the reference, so the
    whole ``$(Name)`` token is what gets looked up, not the name inside it.

    A token the file leaves empty or does not define at all falls to
    *fallbacks*, which is where the caller puts what the emulator itself would
    use — RPCS3 takes its config directory when ``$(EmulatorDir)`` is empty
    (vfs_config.cpp:32-39). Without a fallback such a token is a refusal
    rather than an empty string: the emulator would resolve it and atlas
    cannot, so answering the unexpanded text would state a path nothing uses.
    """
    resolved: dict[str, str] = {}
    for key, value in values.items():
        current = value
        for _ in range(_MAX_SUBSTITUTION_DEPTH):
            expanded, refusal, found = _substitute_once(current, values, fallbacks)
            if refusal is not None:
                return {}, refusal
            if not found:
                break
            current = expanded
        else:
            return {}, REFUSAL_SUBSTITUTION_CYCLE
        resolved[key] = current
    return resolved, None


def _substitute_once(
    value: str, values: Mapping[str, str], fallbacks: Mapping[str, str]
) -> tuple[str, str | None, bool]:
    """Replace every token in *value* once — one link of the chain, not one token.

    The bound above counts links, and this is what makes a link a link: a value
    naming eight different keys is eight replacements at **depth one**, and
    nothing about it is a cycle. Substituting one token per bounded step
    counted the replacements instead, so such a value was refused as
    ``substitution-cycle`` — a refusal that named the wrong thing about a file
    that was perfectly resolvable.

    Returns ``(text, refusal, found)``; ``found`` is ``False`` when the value
    holds no complete ``$(…)`` token, which is what ends the chain. An
    unterminated ``$(`` is not a token and ends it too, leaving the text as
    written the way an unterminated quote is left as written.
    """
    out: list[str] = []
    index = 0
    found = False
    while True:
        start = value.find("$(", index)
        if start == -1:
            break
        end = value.find(")", start)
        if end == -1:
            break
        token = value[start : end + 1]
        replacement = values.get(token) or fallbacks.get(token)
        if replacement is None:
            return "", REFUSAL_SUBSTITUTION_UNKNOWN, False
        out.append(value[index:start])
        out.append(replacement)
        index = end + 1
        found = True
    out.append(value[index:])
    return "".join(out), None, found


@dataclass(frozen=True, slots=True)
class _KeyLine:
    """What one top-level ``key: value`` line contributes.

    Exactly one of the three says what happened: ``refusal`` stops the whole
    file, ``skip`` records the key as stated-but-unread, and otherwise
    ``value`` is the scalar to keep. ``pending`` marks a key whose meaning the
    following lines still decide — an empty value is a scalar until an
    indented line turns it into a block.
    """

    key: str = ""
    value: str | None = None
    skip: bool = False
    pending: bool = False
    refusal: str | None = None


def _classify(line: str) -> _KeyLine:
    """One top-level line, read the way this module reads: value, skip or refusal."""
    split = _split_key(line)
    if split is None:
        # Not a key at all — a bare scalar or a list item at the top level.
        # Nothing here can be attributed to a key.
        return _KeyLine(refusal=REFUSAL_NOT_A_FLAT_MAPPING)
    key, raw_value = split
    value = raw_value.strip()
    # The key side too: an anchor or a tag changes meaning beyond the line it
    # sits on wherever it sits, and checking only the value let `&anc key: v`
    # through as a key literally spelled "&anc key".
    refusal = _refusal_in(key) or _refusal_in(value)
    if refusal is not None:
        return _KeyLine(refusal=refusal)
    if _is_skipping_value(value):
        return _KeyLine(key=key, skip=True, pending=True)
    if value == "":
        # An empty scalar, or the head of a nested block — the lines that
        # follow decide, so it is recorded as empty and stays pending.
        return _KeyLine(key=key, value="", pending=True)
    return _KeyLine(key=key, value=_scalar(raw_value))


def _first_document(text: str) -> tuple[tuple[str, ...], str | None]:
    """The content lines of the first document, blanks and comments dropped.

    Document structure is settled here so the reading below sees nothing but
    lines that carry content: an opening ``---`` is just "here begins the
    document", a second one begins a document that may redefine everything
    above it, and ``...`` ends the one being read.
    """
    lines: list[str] = []
    opened = False
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith("---"):
            # A marker ends the document being read whenever one was being
            # read at all — because content has been seen (an implicit first
            # document), or because a marker already opened one. `---\n---\n`
            # is an empty first document and a second, and reading the
            # second's lines as the first's would answer from a document this
            # reader never established is the one in force.
            if lines or opened:
                return (), REFUSAL_SECOND_DOCUMENT
            opened = True
            continue
        if raw_line.startswith("..."):
            break
        lines.append(raw_line)
    return tuple(lines), None


def _absorb_indent(
    pending_key: str | None, values: dict[str, str], skipped: list[str]
) -> bool:
    """Take an indented line into the key above it — ``False`` if there is none.

    An indented line makes the key above it a nested block after all: it stops
    being a scalar and becomes one this reader names as unread. Indentation
    under no key at all is a file this reader cannot attribute lines from.
    """
    if pending_key is None:
        return False
    if values.pop(pending_key, None) is not None:
        skipped.append(pending_key)
    return True


def _absorb_key(
    outcome: _KeyLine, values: dict[str, str], skipped: list[str]
) -> str | None:
    """Record one key line, and return the key the following lines still decide.

    The twin of :func:`_absorb_indent`: that one takes a line into the key
    above it, this one takes the key itself. A pending key is one whose
    meaning is not settled by its own line — an empty value is a scalar until
    something indented follows it.
    """
    if outcome.skip:
        skipped.append(outcome.key)
    else:
        values[outcome.key] = outcome.value or ""
    return outcome.key if outcome.pending else None


def read_scalars(text: str, *, fallbacks: Mapping[str, str] | None = None) -> YamlScalars:
    """Read the flat top-level scalars of *text*, naming what was not read.

    *fallbacks* supplies what a ``$(Name)`` token means where the file leaves
    it empty or states it nowhere — the emulator's own rule, which only the
    caller knows.
    """
    lines, refusal = _first_document(text)
    if refusal is not None:
        return YamlScalars(refusal=refusal)
    values: dict[str, str] = {}
    skipped: list[str] = []
    pending_key: str | None = None
    for raw_line in lines:
        if raw_line[:1].isspace():
            if not _absorb_indent(pending_key, values, skipped):
                return YamlScalars(refusal=REFUSAL_NOT_A_FLAT_MAPPING)
            continue
        outcome = _classify(raw_line.rstrip())
        if outcome.refusal is not None:
            return YamlScalars(refusal=outcome.refusal)
        pending_key = _absorb_key(outcome, values, skipped)
    resolved, refusal = _substitute(values, fallbacks or {})
    if refusal is not None:
        return YamlScalars(refusal=refusal)
    return YamlScalars(values=resolved, skipped=tuple(skipped))
