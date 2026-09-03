"""ElementTree's shape, on expat directly — the parser a frozen runtime keeps.

atlas reads local configuration as XML: ES-DE's catalogue, its settings file and
its gamelists, Cemu's ``settings.xml``, Vita3K's ``user.xml``. That takes a
parser which is present wherever a consumer runs a vendored copy, and
``xml.etree`` is not reliably that parser. It is a Python-source package over
the extension that does the work, and a frozen runtime ships only the modules
its build analysis reached: on Decky Loader's PyInstaller bundle ``import
xml.etree.ElementTree`` raises ``ModuleNotFoundError: No module named
'xml.etree'`` while the expat extension itself sits in the bundle's
``lib-dynload`` (issue #339). Vendoring atlas is a directory copy (DESIGN.md,
consumption) whose release manifest checksums every packaged file
(``docs/how-to-use.md``), so a consumer cannot patch that away — the assumption
was atlas's to drop.

Underneath, ElementTree parses with expat: its source reaches for
``xml.parsers.expat`` and falls back to ``pyexpat`` (``ElementTree.py:1514-1523``
at CPython 3.12, the import repeated below), and where the ``_elementtree``
accelerator shadows that source (``ElementTree.py:2080``) it is the same
extension underneath. So this module keeps the parser and drops the wrapper,
rebuilding exactly the surface the call sites in :mod:`atlas.esde` and
:mod:`atlas.installations` use: :func:`fromstring`, :class:`ParseError`, and an
:class:`Element` with ``tag``, ``text``, ``get``, ``find``, ``findall``,
``findtext``, ``iter`` and iteration over its direct children. Nothing beyond
that surface is here — no ``tail``, no namespace
handling, no path expressions, no serialization — and inside it the behavior is
ElementTree's, down to ``text`` being the character data before the first child
and ``ParseError`` deriving from ``SyntaxError``. Each behavior cited below to
``ElementTree.py`` is cited to that Python source, which is where it is written
down and which the accelerator mirrors.

Namespaces are the one omission a document could notice: a prefixed name arrives
as written (``ns:tag``) and an ``xmlns`` declaration stays an ordinary
attribute, where ElementTree would rewrite the name to ``{uri}tag``, take the
declaration out of the attributes, and refuse an undeclared prefix outright. No
configuration atlas reads declares a namespace, and every tag the callers ask
for is the one its file spells.

The security rationale of parsing this way carries over unchanged, because it
was expat's all along: the input is local config from the user's own machine
(not attacker-controlled in this threat model), entity-expansion limits are the
parser's — a rejection surfaces as :class:`ParseError`, i.e. an honestly skipped
file — and no ``ExternalEntityRefHandler`` is registered here, which is the only
way expat would ever fetch an external entity. ``dependencies = []`` is a design
contract (DESIGN.md, consumption), so ``defusedxml`` is not an option.
"""

from __future__ import annotations

from typing import Iterator

try:  # The two spellings of the same extension, in ElementTree's own order.
    from xml.parsers import expat
except ImportError:  # pragma: no cover - the wrapper-less runtime this module exists for
    import pyexpat as expat


class ParseError(SyntaxError):
    """XML expat refused.

    A ``SyntaxError`` subclass, as ElementTree's is (``ElementTree.py:107``), so
    a caller that catches the base class keeps catching this.
    """


class Element:
    """One element of a parsed document: tag, attributes, text, children.

    Constructed only by the parse below. ``text`` is the character data between
    the start tag and the first child element, ``None`` where there is none —
    ElementTree's meaning, and the one every call site reads it with.
    """

    __slots__ = ("tag", "text", "_attrib", "_children")

    def __init__(self, tag: str, attrib: dict[str, str], children: list[Element]) -> None:
        self.tag = tag
        self.text: str | None = None
        self._attrib = attrib
        self._children = children

    def __iter__(self) -> Iterator[Element]:
        """The direct children, in document order."""
        return iter(self._children)

    def get(self, key: str, default: str | None = None) -> str | None:
        """The value of attribute *key*, or *default* where it is not set."""
        return self._attrib.get(key, default)

    def find(self, tag: str) -> Element | None:
        """The first **direct child** named *tag*, or ``None``.

        A bare tag name, never a path: a grandchild of that name is not a match,
        exactly as ``find("tag")`` behaves in ElementTree. The catalogue read
        depends on it — ES-DE counts ``<loadExclusive/>`` only as a document
        child (``doc.child``, ``es-app/src/SystemData.cpp:884,898``, v3.4.1), so
        one nested deeper, which the frontend ignores, must stay unfound here.
        """
        for child in self._children:
            if child.tag == tag:
                return child
        return None

    def findall(self, tag: str) -> list[Element]:
        """Every direct child named *tag*, in document order."""
        return [child for child in self._children if child.tag == tag]

    def findtext(self, tag: str, default: str | None = None) -> str | None:
        """The text of the first direct child named *tag*, else *default*.

        A found child with no text answers ``""`` rather than *default*, as in
        ElementTree (``ElementPath.findtext``): an empty element and an absent
        one are different facts, and callers spell the second one themselves.
        """
        element = self.find(tag)
        if element is None:
            return default
        return element.text or ""

    def iter(self, tag: str) -> Iterator[Element]:
        """This element and every descendant named *tag*, in document order."""
        if self.tag == tag:
            yield self
        for child in self._children:
            yield from child.iter(tag)


class _Builder:
    """The tree, assembled from expat's event stream.

    ElementTree's ``TreeBuilder`` minus tails: character data is buffered and
    flushed onto the last element *started*, which is what makes ``text`` the
    run before the first child; after an end tag it is dropped, where
    ElementTree would keep it as that element's ``tail``.

    The open stack holds each open element's own child list, so a child is
    appended where it belongs without reaching into the element for it.
    """

    def __init__(self) -> None:
        self.root: Element | None = None
        self._open: list[list[Element]] = []
        self._data: list[str] = []
        self._last: Element | None = None
        self._after_end = False

    def start(self, tag: str, attrib: dict[str, str]) -> None:
        self._flush()
        children: list[Element] = []
        element = Element(tag, attrib, children)
        if self._open:
            self._open[-1].append(element)
        else:
            self.root = element
        self._open.append(children)
        self._last = element
        self._after_end = False

    def end(self, _tag: str) -> None:
        # expat names the tag it closed; the open stack already knows which
        # element that is, and well-formedness is expat's to enforce.
        self._flush()
        self._open.pop()
        self._after_end = True

    def data(self, text: str) -> None:
        self._data.append(text)

    def _flush(self) -> None:
        if self._data:
            if self._last is not None and not self._after_end:
                self._last.text = "".join(self._data)
            self._data = []


def fromstring(text: str) -> Element:
    """Parse a whole XML document from *text* and return its root element.

    *text* is ``str``, declaration and all: expat is told the input is UTF-8, so
    a declared encoding is overridden rather than obeyed — the same treatment
    ElementTree gives a string document, and what lets a config file's
    ``<?xml … encoding="…"?>`` line pass through unread.

    Anything expat refuses — malformed markup, an empty document, an undefined
    entity, an entity expansion beyond its limits — raises :class:`ParseError`.
    """
    parser = expat.ParserCreate()
    # One call per run of character data instead of one per buffer boundary;
    # ElementTree configures its parser the same way (``ElementTree.py:1549``).
    parser.buffer_text = True
    builder = _Builder()

    def refuse_skipped_entity(name: str, _is_parameter: bool) -> None:
        # A document pointing at a DTD nobody read can name entities expat
        # cannot resolve: it reports them here instead of failing. ElementTree
        # refuses such a document (``_default``, ``ElementTree.py:1649-1669``),
        # and so does this — a dropped reference would be a file read as saying
        # something it does not say.
        raise ParseError(
            f"undefined entity &{name};: line {parser.ErrorLineNumber}, "
            f"column {parser.ErrorColumnNumber}"
        )

    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    parser.SkippedEntityHandler = refuse_skipped_entity
    try:
        parser.Parse(text, True)
    except expat.ExpatError as error:
        raise ParseError(str(error)) from error
    root = builder.root
    # A document with no element at all is "no element found" to expat, so the
    # parse above has already raised wherever there is no root to return.
    assert root is not None
    return root
