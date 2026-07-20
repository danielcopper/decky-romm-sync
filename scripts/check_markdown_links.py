#!/usr/bin/env python3
"""Local markdown link-integrity gate.

The docs are a graph of cross-references — a page links to a sibling page and
to a specific heading on it (``[text](../foo.md#some-heading)``). Nothing ties
the link to its target at build time: a renamed file, a moved page, or a
reworded heading only surfaces as a dead link a reader hits at runtime (or, on
the published MkDocs site, a silent 404 / scroll-to-nowhere). Routing links rot
silently; this check mechanizes the sweep.

It walks every tracked ``.md`` file and, for each **local** inline link, asserts
two things: the target file exists, and — when the link carries a ``#anchor``
into a ``.md`` file — that anchor exists in the target's heading/attr-list anchor
set (slugified exactly the way this repo's MkDocs ``toc`` extension does).

What it guarantees (and what it deliberately does not):

  * Every local inline ``[text](target)`` link resolves to an existing file,
    resolved relative to the LINKING file's directory. A pure-anchor link
    (``[text](#foo)``) targets the linking file itself.
  * A ``#anchor`` into a ``.md`` target matches a real anchor: an explicit
    ``attr_list`` ``{#custom-id}`` id, or — for a heading with no explicit id —
    the heading slug (python-markdown ``toc`` DEFAULT slugify, inline markdown
    stripped first, ``_1``/``_2`` duplicate suffixing). An explicit id REPLACES
    the heading's auto-slug rather than adding to it, so the auto-slug of a
    ``{#id}``-carrying heading is correctly treated as a dead anchor.
  * It does NOT check external links (``http://``, ``https://``, ``mailto:``,
    ``tel:``, protocol-relative ``//``) — there is no network in CI — nor bare
    ``<...>`` autolinks, images (``![...]``), reference-style links, or
    line-number fragments. Only file existence + anchor membership are checked.
  * Heuristic limits (a guardrail, not a parser): inline links are matched
    per-line, so a link whose ``(...)`` destination wraps across lines is not
    seen; link text containing a literal ``]`` truncates the match; fenced code
    blocks and inline code spans are skipped so example links inside them are
    not checked. These favour false-negatives (a missed link) over false
    positives (a good link flagged), so the gate never fails a real doc wrongly.

``EXEMPT`` holds tracked ``.md`` paths deliberately kept out of the scan. It is
empty today; an entry here is a conscious "this file's links are intentionally
not checked" decision — never a lever to silence a real drift. A genuine broken
link is a finding to triage, not to exempt.

The file source is ``git ls-files '*.md'`` (auto-excludes ``.venv/``,
``node_modules/``, ``site/``, ``_vendor/``, ``.worktrees/`` — anything untracked
or gitignored). ``CHANGELOG.md`` is additionally skipped (release-bot generated;
``deno fmt`` excludes it too).

Exit 0 when every local link resolves, 1 (one ``path -> link  (reason)`` line
per discrepancy) otherwise.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Tracked ``.md`` paths (repo-relative, POSIX) deliberately excluded from the
# scan. Empty today; add an entry only as a conscious decision (see the module
# docstring). Do NOT add a path here to silence a real broken link — a broken
# link is a finding to triage.
EXEMPT: frozenset[str] = frozenset()

# Release-bot generated; not hand-maintained, so its links are not our contract.
# ``deno fmt`` / markdownlint exclude it too. Matched by basename.
_SKIP_BASENAMES: frozenset[str] = frozenset({"CHANGELOG.md"})

# Destination prefixes that mark an external / non-local link we never resolve.
_EXTERNAL_PREFIXES: tuple[str, ...] = ("http://", "https://", "mailto:", "tel:", "//")

# Inline link ``[text](dest)`` NOT preceded by ``!`` (which would make it an
# image). Captures the raw destination content, title included.
_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]*)\)")

# ATX heading, mirroring python-markdown's ``HashHeaderProcessor`` regex, which
# differs from CommonMark at BOTH edges:
#   * no whitespace is required after the hashes — a wrapped prose line beginning
#     ``#1478 reported ...`` really does render as a heading and really does
#     claim an anchor + a dedup slot;
#   * no leading indent is allowed — the hashes must sit at column 0, so an
#     indented continuation line (``  #1032 (after ...)`` inside a list item) is
#     paragraph text, NOT a heading.
# Matching both edges is what keeps this gate faithful to the engine.
_HEADING_RE = re.compile(r"^(#{1,6})(.*?)\s*$")

# A fenced code-block delimiter line (```` ``` ```` or ``~~~``, 3+ of one char).
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

# Inline code span on a single line (``...``), stripped before link matching.
_CODE_SPAN_RE = re.compile(r"`[^`]*`")

# A trailing attr-list block on a heading line: ``## Title {#id .cls}``.
_ATTR_BLOCK_TAIL_RE = re.compile(r"\s*\{[^}]*\}\s*$")

# An ``attr_list`` id token (``{#custom-id}`` / ``{: #id .cls}``) — the ``#``
# followed by the id characters.
_ATTR_ID_RE = re.compile(r"#([-\w]+)")
_ATTR_BLOCK_RE = re.compile(r"\{([^}]*)\}")

# python-markdown ``toc`` duplicate-slug suffix bookkeeping (mirrors
# ``markdown.extensions.toc``: ``IDCOUNT_RE`` + ``'%s_%d'`` underscore suffix).
_IDCOUNT_RE = re.compile(r"^(.*)_(\d+)$")

# Inline-markdown strippers applied to heading text before slugifying, so the
# slug matches python-markdown's rendered (tags-removed) heading text.
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_INLINE_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REF_LINK_RE = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")

# Underscore emphasis, mirroring python-markdown's SMART_EMPHASIS rule: ``_``/
# ``__`` opens emphasis only when NOT flanked by a word char on the outside.
# This is why a naive ``replace("_", "")`` is wrong — an intra-word underscore
# (``file_path``) is literal text the engine KEEPS, and ``_`` survives slugify
# as a word char, so mis-stripping it corrupts the slug in both directions.
_STRONG_UNDERSCORE_RE = re.compile(r"(?<!\w)__(?!_)(.+?)(?<!_)__(?!\w)")
_EM_UNDERSCORE_RE = re.compile(r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)")

# A whole code span including its delimiters; group 2 is the literal content.
# Emphasis rules do NOT apply inside a code span — `` `__init__.py` `` keeps its
# underscores (slug ``__init__py``), so the content is passed through verbatim.
_CODE_SPAN_FULL_RE = re.compile(r"(`+)(.+?)\1")


def slugify(value: str, sep: str = "-") -> str:
    """Slugify *value* exactly as python-markdown's DEFAULT ``toc`` slug function.

    NFKD-normalise, drop non-ASCII, strip every char that is not a word char /
    whitespace / hyphen, lowercase, then collapse any run of separators and
    whitespace to a single *sep*. The collapse is the point: MkDocs (unlike
    GitHub) squashes ``foo / bar`` down to ``foo-bar``, not ``foo--bar``.
    """
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    return re.sub(rf"[{re.escape(sep)}\s]+", sep, value)


def _unique_slug(slug: str, used: set[str]) -> str:
    """Return *slug* made unique against *used*, python-markdown ``toc`` style.

    Mirrors ``markdown.extensions.toc.unique``: an already-seen (or empty) slug
    gets an ``_1`` suffix, and a repeat of that gets ``_2``, ``_3``, … The
    resolved slug is registered in *used*. NOTE the suffix is an UNDERSCORE
    (``dup_1``), not a hyphen — this matches the installed python-markdown, not
    GitHub's ``dup-1``.
    """
    while slug in used or not slug:
        match = _IDCOUNT_RE.match(slug)
        slug = f"{match.group(1)}_{int(match.group(2)) + 1}" if match else f"{slug}_1"
    used.add(slug)
    return slug


def _strip_inline_markdown(text: str) -> str:
    """Strip inline markdown from heading text so its slug matches the rendered text.

    Mirrors what python-markdown's ``toc`` sees after rendering the heading and
    taking its text content: code spans keep their content verbatim (emphasis and
    link syntax are literal in there), while everything outside them has images
    dropped, links unwrapped to their text, and emphasis markers removed.
    """
    out: list[str] = []
    pos = 0
    for span in _CODE_SPAN_FULL_RE.finditer(text):
        out.append(_strip_outside_code(text[pos : span.start()]))
        out.append(span.group(2))
        pos = span.end()
    out.append(_strip_outside_code(text[pos:]))
    return "".join(out)


def _strip_outside_code(text: str) -> str:
    """Strip images, links and emphasis from a non-code-span run of heading text."""
    text = _IMG_RE.sub("", text)
    text = _INLINE_LINK_RE.sub(r"\1", text)
    text = _REF_LINK_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("*", "")
    text = _STRONG_UNDERSCORE_RE.sub(r"\1", text)
    return _EM_UNDERSCORE_RE.sub(r"\1", text)


def anchor_set(text: str) -> set[str]:
    """Collect every anchor a ``#fragment`` could target in a markdown document.

    Mirrors ``TocTreeprocessor``'s two passes over the rendered tree, both
    scanned outside fenced code blocks:

      * Pass 1 — every explicit ``attr_list`` ``{#id}`` (on a heading OR any
        other element) claims its anchor verbatim and seeds the dedup pool. The
        engine pre-scans existing ids before assigning any auto-slug, so a
        later ``{#dup}`` pushes an earlier auto-slugged ``## Dup`` to ``dup_1``.
        Explicit ids are never themselves deduped (two ``{#same}`` stay
        ``same``).
      * Pass 2 — each heading WITHOUT an explicit id gets its slugified text,
        deduped against that pool, so the 2nd ``# Dup`` registers as ``dup_1``.

    A heading that carries an explicit ``{#id}`` therefore contributes ONLY that
    id: attr_list REPLACES the auto-slug, so the auto-slug is a dead anchor and
    registering it would silently accept a broken link.
    """
    lines = _non_fenced_lines(text)

    explicit: set[str] = set()
    for line in lines:
        for block in _ATTR_BLOCK_RE.findall(line):
            explicit.update(_ATTR_ID_RE.findall(block))

    anchors: set[str] = set(explicit)
    used_slugs: set[str] = set(explicit)
    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading is None:
            continue
        raw = re.sub(r"\s+#+$", "", heading.group(2))  # strip ATX closing ``##``
        attr_tail = _ATTR_BLOCK_TAIL_RE.search(raw)
        if attr_tail is not None and _ATTR_ID_RE.search(attr_tail.group(0)):
            continue  # explicit id replaces this heading's auto-slug
        raw = _ATTR_BLOCK_TAIL_RE.sub("", raw)  # strip a trailing ``{.cls}`` block
        anchors.add(_unique_slug(slugify(_strip_inline_markdown(raw)), used_slugs))
    return anchors


def _non_fenced_lines(text: str) -> list[str]:
    """Return *text*'s lines with fenced-code-block regions removed.

    A ```` ``` ```` / ``~~~`` line toggles fence state; the closing fence must
    use the same character as the opener. Fence delimiter lines and everything
    between them are dropped.
    """
    result: list[str] = []
    in_fence = False
    fence_char = ""
    for line in text.splitlines():
        fence = _FENCE_RE.match(line)
        if fence is not None:
            marker_char = fence.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_char = marker_char
            elif marker_char == fence_char:
                in_fence = False
            continue
        if not in_fence:
            result.append(line)
    return result


def _destination(raw: str) -> str:
    """Extract the destination from a link's ``(...)`` body, dropping an optional title.

    Unwraps an angle-bracket ``<dest>`` destination; otherwise the destination is
    everything up to the first whitespace (a markdown title — ``"..."`` / ``'...'``
    / ``(...)`` — follows the whitespace and is discarded).
    """
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("<"):
        end = raw.find(">")
        return raw[1:end] if end != -1 else raw
    return raw.split()[0]


def _iter_destinations(text: str) -> list[str]:
    """Yield each inline link's destination, skipping fenced/inline code and images."""
    destinations: list[str] = []
    for line in _non_fenced_lines(text):
        line = _CODE_SPAN_RE.sub("", line)
        for match in _LINK_RE.finditer(line):
            dest = _destination(match.group(1))
            if dest:
                destinations.append(dest)
    return destinations


def _is_external(dest: str) -> bool:
    """Return True for a destination we never resolve locally (external / autolink)."""
    return dest.startswith(_EXTERNAL_PREFIXES) or dest.startswith("<")


@dataclass(frozen=True)
class BrokenLink:
    """One broken local link: its source file, the link as written, and why."""

    source_rel: str
    link: str
    reason: str

    def render(self) -> str:
        return f"{self.source_rel} -> {self.link}  ({self.reason})"


def check_markdown_file(
    path: Path,
    repo_root: Path,
    anchor_cache: dict[Path, set[str]],
) -> list[BrokenLink]:
    """Return every broken local link in the markdown file at *path*.

    *anchor_cache* memoises target anchor sets across the whole run (a page is
    linked from many others); keyed by the resolved absolute target path.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    source_rel = _rel(path, repo_root)
    broken: list[BrokenLink] = []
    for dest in _iter_destinations(text):
        if _is_external(dest):
            continue
        file_part, _, anchor = dest.partition("#")
        target = (path.parent / file_part).resolve() if file_part else path.resolve()
        if file_part and not target.is_file():
            broken.append(BrokenLink(source_rel, dest, "target file not found"))
            continue
        if not anchor or target.suffix.lower() != ".md":
            continue
        anchors = anchor_cache.get(target)
        if anchors is None:
            anchors = _anchor_set_for(target)
            anchor_cache[target] = anchors
        if anchor not in anchors:
            broken.append(BrokenLink(source_rel, dest, f"anchor '#{anchor}' not found in {target.name}"))
    return broken


def _anchor_set_for(path: Path) -> set[str]:
    """Read *path* and return its anchor set (empty if unreadable)."""
    try:
        return anchor_set(path.read_text(encoding="utf-8"))
    except OSError:
        return set()


def _rel(path: Path, repo_root: Path) -> str:
    """Repo-relative POSIX path for display, falling back to the absolute path."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def tracked_markdown_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Every tracked ``.md`` file to scan: ``git ls-files '*.md'`` minus the skips.

    ``git ls-files`` already drops untracked/gitignored trees (``.venv/``,
    ``node_modules/``, ``site/``, ``_vendor/``, ``.worktrees/``). We additionally
    drop ``CHANGELOG.md`` and any ``EXEMPT`` path.
    """
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "*.md"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    files: list[Path] = []
    for line in result.stdout.splitlines():
        rel = line.strip()
        if not rel or rel in EXEMPT or os.path.basename(rel) in _SKIP_BASENAMES:
            continue
        files.append(repo_root / rel)
    return files


def collect_broken_links(files: list[Path], repo_root: Path = REPO_ROOT) -> list[BrokenLink]:
    """Scan *files* and return every broken local link, in file+source order."""
    anchor_cache: dict[Path, set[str]] = {}
    broken: list[BrokenLink] = []
    for path in files:
        broken.extend(check_markdown_file(path, repo_root, anchor_cache))
    return broken


def main(argv: list[str]) -> int:
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0
    files = tracked_markdown_files(REPO_ROOT)
    broken = collect_broken_links(files, REPO_ROOT)
    if broken:
        for finding in broken:
            print(finding.render())
        print()
        print(
            "ERROR: broken local markdown link(s). A link's target file must exist "
            "(resolved relative to the linking file) and any '#anchor' must match a "
            "real heading slug or attr-list id in the target. Fix the link or the "
            "target — do not add the file to EXEMPT to silence a real drift."
        )
        return 1
    print(f"OK: all local markdown links resolve ({len(files)} files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
