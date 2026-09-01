"""A Qt settings file, read as text — the format four emulators keep settings in.

Azahar, DuckStation, PCSX2 and melonDS's Qt frontend all write ``QSettings``
files, and two questions read them: where saves and texture packs live
(:mod:`atlas.installations`) and which firmware a launch expects
(:mod:`atlas.firmware`). The reading lives here so both reach it without one
importing the other, the way :mod:`atlas.melonds` and
:mod:`atlas.yaml_scalars` do for their formats.

Pure text in, mapping out. What a value *means* is the emulator's, and stays
with the caller: an empty string is a real value in one emulator and "unset"
in another, and a ``\\default`` companion key changes which of two values wins
(Azahar's ``ReadSetting``). :func:`values` interprets nothing.

What does live here beside it is one *named* reading — :func:`from_chars_bool`
— because two of the four emulators share it byte for byte and neither owns a
module the other could import. It is named after the upstream function it
mirrors and carries its citation, so it reads as one emulator family's own
value grammar rather than as this format's. :func:`simpleini_value` is the
same kind of resident (#295): the case-insensitive (section, key) match two
emulators perform through ``CSimpleIniA`` and a third performs through its own
``IniFile``, needed by :mod:`atlas.installations`, :mod:`atlas.firmware` and
:mod:`atlas.duckstation` alike — and this is the one module all three already
read.
"""

from __future__ import annotations

from collections.abc import Mapping


def unescape_section(name: str) -> str:
    """QSettings' ``%XX`` section-name escaping, undone byte-wise.

    ``[Data%20Storage]`` is the group ``Data Storage``. The undo is byte-wise
    because the escape encodes bytes, not characters, and a pair that is not
    valid hexadecimal is left as written rather than guessed at.
    """
    out = bytearray()
    index = 0
    encoded = name.encode("utf-8")
    while index < len(encoded):
        if encoded[index : index + 1] == b"%" and index + 2 < len(encoded) + 1:
            hex_pair = encoded[index + 1 : index + 3]
            try:
                out.append(int(hex_pair, 16))
                index += 3
                continue
            except ValueError:
                pass
        out.append(encoded[index])
        index += 1
    return out.decode("utf-8", errors="replace")


_FROM_CHARS_TRUE = ("true", "yes", "on", "1", "enabled")
_FROM_CHARS_FALSE = ("false", "no", "off", "0", "disabled")


def _path_append(dst: list[str], src: str) -> None:
    """``PathAppendString``, the POSIX half — one function in two emulators.

    (common/FileSystem.cpp:98-139 at PCSX2 v2.6.3; file_system.cpp:100-140 at
    stenzek/duckstation@64655818e.) Copies *src* onto *dst* collapsing every
    run of separators to one. The state that decides it is ``last_separator``,
    seeded from what *dst* already ends with (:104 / :105) — which is the
    whole mechanism behind :func:`path_combine` swallowing an absolute value's
    leading separator. The ``_WIN32`` arms (backslash folding, the UNC special
    case) are deliberately not ported: atlas resolves Linux machines, and
    porting a branch no read of one can reach would be code nothing proves.
    Outside those arms the two upstreams differ by nothing but whitespace —
    the UNC test, inline in PCSX2 and an ``IsUNCPath`` helper in DuckStation,
    is their one textual difference, and it sits in the arm the port omits.
    """
    last_separator = bool(dst) and dst[-1] == "/"
    for char in src:
        if char != "/":
            last_separator = False
            dst.append(char)
        elif not last_separator:
            last_separator = True
            dst.append("/")


def path_combine(base: str, name: str) -> str:
    """``Path::Combine``, ported faithfully — the same function in two emulators.

    (common/FileSystem.cpp:847-862 at PCSX2 v2.6.3; file_system.cpp:859-874 at
    stenzek/duckstation@64655818e — token-identical outside the unported
    ``_WIN32`` arm, see :func:`_path_append`, so one port serves both.) A
    resident here for the reason :func:`from_chars_bool` is: every configured
    *file name* either emulator composes goes through this combine — PCSX2's
    memory-card image (``FullpathToMcd``, Pcsx2Config.cpp:2065-2068, read by
    :mod:`atlas.installations`), PCSX2's BIOS image (``FullpathToBios``,
    :2057-2062, read by :mod:`atlas.firmware`) and DuckStation's region BIOS
    image (``GetBIOSImage``, bios.cpp:350, read by :mod:`atlas.firmware` too)
    — and neither module can import the other.

    It is this rather than :func:`os.path.join` because the two disagree on
    exactly the value issues #312 and #320 were about — and, on the relative
    side, on the degenerate spellings #325 was about: the collapse and the
    final strip mean ``memcards//sub/`` composes to ``<dir>/memcards/sub``,
    where ``os.path.join`` preserves both the run and the tail. The combine appends
    *base*, strips its trailing separators, appends **one** separator (:856 /
    :868), then appends *name* through :func:`_path_append` — which therefore
    enters with ``last_separator`` already true and swallows the leading
    separator of an absolute *name* at the ``continue`` on :128-129 / :129-130.
    So an absolute file name lands **below** the directory instead of replacing
    it, where ``os.path.join`` would let it replace it silently.

    That this is deliberate rather than incidental is visible two hundred lines
    away: ``LoadPathFromSettings`` *does* test ``Path::IsAbsolute``
    (Pcsx2Config.cpp:2275), because a ``[Folders]`` setting is where PCSX2
    wants an absolute value to win. ``Path::Combine`` has no such test — so in
    one PCSX2 configuration file an absolute value is a path when it names a
    directory and is not one when it names a file. DuckStation keeps the same
    asymmetry: its folder reader tests it (``LoadPathFromSettings``,
    settings.cpp:1955-1962) and its shared-card getter tests it
    (settings.cpp:1785-1797), while the BIOS-image combine tests nothing.

    Nothing here normalises, because upstream resolves no ``.`` or ``..``
    component either. A name of ``"../x"`` composes literally to
    ``<dir>/../x``, and where that lands is the kernel's answer: reading it
    lexically would make the resolved path and the opened file two different
    places wherever a parent is a symlink.
    """
    ret: list[str] = []
    _path_append(ret, base)
    while ret and ret[-1] == "/":
        ret.pop()
    ret.append("/")
    _path_append(ret, name)
    while ret and ret[-1] == "/":
        ret.pop()
    return "".join(ret)


def from_chars_bool(value: str | None) -> bool | None:
    """``StringUtil::FromChars<bool>`` as PCSX2 and DuckStation apply it to an ini value.

    Both emulators read a boolean setting the same way and through the same
    code shape: ``INISettingsInterface::GetBoolValue`` takes the raw string
    SimpleIni hands back and passes it to ``StringUtil::FromChars<bool>``,
    keeping the caller's default where that returns nothing
    (INISettingsInterface.cpp:198-210 and StringUtil.h:178-197 at PCSX2 v2.6.3;
    ini_settings_interface.cpp:155-167 and string_util.h:180-197 at
    stenzek/duckstation@64655818e). ``None`` here is that nothing: the key
    said something the emulator could not read as a boolean, so its compiled
    default governs — which is not the same fact as an absent key, and the
    caller keeps them apart.

    The comparison is the surprising part and it is upstream's, not an
    approximation of it: ``Strncasecmp(literal, str.data(), str.length())``
    compares only as many characters as the *value* has, so the test is a
    case-insensitive **prefix** match against those literals. ``t``, ``tr``
    and ``TRUE`` all read as true; ``o`` reads as true rather than as a prefix
    of ``off``, because the true list is tested first; and an empty value
    compares zero characters, which matches, so it reads as true as well.
    Mirroring that exactly is the difference between saying what the emulator
    does and saying what a reasonable ini reader would do.
    """
    if value is None:
        return None
    for literal in _FROM_CHARS_TRUE:
        if literal.startswith(value.casefold()):
            return True
    for literal in _FROM_CHARS_FALSE:
        if literal.startswith(value.casefold()):
            return False
    return None


def ascii_locase(text: str) -> str:
    """SI_GenericNoCase's lowering: ``A-Z`` only, nothing else folds.

    Python's ``casefold`` folds more than ASCII; the emulators' comparators
    do not (SimpleIni.h:2916-2931 at PCSX2 v2.6.3, the same generic class at
    DuckStation's pin; Dolphin's ``Common::ToLower`` is
    ``std::tolower(ch, std::locale::classic())``, StringUtil.h:306-308 at
    2603a, which folds the same 26 characters), and mirroring the fold
    exactly is the difference between reading the file the way the emulator
    does and the way a reasonable ini reader would.
    """
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text)


def simpleini_value(
    values: Mapping[tuple[str, str], str], section: str, key: str
) -> tuple[str | None, str]:
    """The value ``CSimpleIniA`` hands back for (section, key), and the spelling that carried it.

    Both emulators that keep their folders in an ini read it through
    ``CSimpleIniA``, whose comparator is ASCII case-insensitive on Linux
    (PCSX2 v2.6.3: INISettingsInterface.h:66, SimpleIni.h:2882-2887 define
    SI_NO_CONVERSION, :3629-3634 pick SI_GenericNoCase, :3642-3643 the
    typedef; the same chain at stenzek/duckstation@64655818e,
    ini_settings_interface.h:65 and its vendored SimpleIni.h:3593-3607). A
    file carrying two case-spellings of one key collapses them into one entry
    with the last occurrence winning (AddEntry assigns into the found key,
    SimpleIni.h:2042-2150) — mirrored here by taking the last matching entry
    in file order, exact for any file that spells each variant at most once.
    The spelling rides back for the reading's own sentence, because the
    shipped RetroDECK ini spells PCSX2's key another way than the source
    reads it, which is the trap issue #225 turned on.

    Dolphin.ini is not a Qt settings file and not SimpleIni, but its own
    reader matches the same way, so the same mirror serves it (#295): keys
    live in a ``CaseInsensitiveLess`` map (IniFile.h:64 at dolphin 2603a),
    sections are found by ``CaseInsensitiveEquals`` (IniFile.cpp:130-146,
    case-variant headers merged at :289), a duplicate key's last value wins
    (``insert_or_assign``, :47-49 from the parse at :308), and the config
    layer the values land in keys them by ``strcasecmp`` on section and key
    (BaseConfigLoader.cpp:144-181, Layer.h:56, ConfigInfo.cpp:18-29) — the
    identical chain at PrimeHack's pins (shiiion/dolphin@81bfb96
    IniFile.h:89, @53f53e0 IniFile.h:64, both ConfigInfo.cpp via strcasecmp).
    """
    found: str | None = None
    spelled = key
    lowered = (ascii_locase(section), ascii_locase(key))
    for (stated_section, stated_key), value in values.items():
        if (ascii_locase(stated_section), ascii_locase(stated_key)) == lowered:
            found = value
            spelled = stated_key
    return found, spelled


def values(text: str) -> dict[tuple[str, str], str]:
    """A Qt settings file as ``(section, key) -> raw value`` — read, not interpreted."""
    parsed: dict[tuple[str, str], str] = {}
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = unescape_section(line[1:-1])
            continue
        key, separator, value = line.partition("=")
        if separator:
            parsed[(section, key.strip())] = value.strip()
    return parsed
