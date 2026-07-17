"""Cover-fingerprint compare kernel — which bound ROMs' server covers changed.

The single decision point for the ``cover_source`` fingerprint compare (#1386):
given a fetch's raw ROM dicts and the bound-row registry projection, split the
ROMs into fingerprint-changed covers (re-download + in-session tile refresh)
and NULL-fingerprint candidates (the pre-fingerprint upgrade path, whose
adopt-vs-download fork needs a cache-file existence check the caller performs).
Both the apply-path invalidation pass and the preview's cover-work count run
this kernel, so the number the preview shows is by construction the set the
apply pass refreshes. Pure compute — no I/O, no state; anything that reads the
DB, checks files, or downloads belongs in the calling service.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class CoverRefreshScan(NamedTuple):
    """The kernel's split of a fetch against the registry's fingerprints.

    ``changed`` holds ``(rom_id, app_id, fresh_source)`` for every bound ROM
    whose stored fingerprint differs from the fresh cover source — the covers
    the invalidation pass re-downloads and the preview counts.
    ``null_fingerprint`` holds ``(rom_id, fresh_source)`` for bound ROMs with
    no stored fingerprint — adopted without a download when a cache file
    exists, else left for the apply path (a caller-side I/O fork).
    """

    changed: list[tuple[int, int, str]]
    null_fingerprint: list[tuple[int, str]]


def fresh_cover_source(rom: dict[str, Any]) -> str | None:
    """Return *rom*'s fresh cover source string, or ``None`` when it has no cover.

    The fingerprint is the full RomM cover source (``path_cover_large`` else
    ``path_cover_small``, the embedded ``?ts=…`` cache-buster included),
    compared as an opaque string. An empty string counts as no cover.
    """
    return rom.get("path_cover_large") or rom.get("path_cover_small") or None


def _strip_cover_ts(source: str) -> str:
    """Return *source* with its ``ts`` query parameter removed.

    Splits off the query string and drops the ``ts=…`` cache-buster RomM embeds
    (the value that a server-side rescan re-stamps without touching the file),
    preserving any other query parameters and their order. Pure string algebra —
    no URL parsing library — so it stays dependency-free and never rewrites the
    path portion (the compare is exact-string equality).
    """
    base, sep, query = source.partition("?")
    if not sep:
        return base
    kept = [param for param in query.split("&") if param != "ts" and not param.startswith("ts=")]
    return base + ("?" + "&".join(kept) if kept else "")


def cover_ts_only_change(stored_source: str | None, fresh_source: str | None) -> bool:
    """True iff the cover source changed by ONLY its ``?ts=`` cache-buster (#1454).

    The revalidation trigger's pure part: both sources are present, they differ
    (a fingerprint change was already detected), and stripping the ``ts`` query
    parameter from each yields equal, non-empty bases — i.e. the same underlying
    asset path with only the rescan-restamped timestamp differing. A path change
    beyond ``ts`` (a different rom path, or a ``url_cover``↔``path_cover`` switch
    from #1450) is NOT ts-only and must take the regular download path.
    """
    if not stored_source or not fresh_source or stored_source == fresh_source:
        return False
    stripped = _strip_cover_ts(stored_source)
    return bool(stripped) and stripped == _strip_cover_ts(fresh_source)


def scan_cover_refresh_candidates(
    all_roms: list[dict[str, Any]],
    registry: dict[str, dict[str, Any]],
) -> CoverRefreshScan:
    """Split *all_roms* against *registry* fingerprints into changed / NULL buckets.

    *registry* is the bound-row projection keyed by ``str(rom_id)`` (each entry
    carrying ``app_id`` and ``cover_source``). A ROM with no fresh cover, no
    ``id``, no registry entry (unbound rows are never projected), or an
    unchanged fingerprint is skipped. Order follows *all_roms*; a duplicate
    ``rom_id`` is counted once (first occurrence wins).
    """
    changed: list[tuple[int, int, str]] = []
    null_fingerprint: list[tuple[int, str]] = []
    seen: set[int] = set()
    for rom in all_roms:
        fresh = fresh_cover_source(rom)
        if not fresh or "id" not in rom:
            continue
        rom_id = int(rom["id"])
        if rom_id in seen:
            continue
        seen.add(rom_id)
        entry = registry.get(str(rom_id))
        if entry is None or entry.get("app_id") is None:
            continue
        stored = entry.get("cover_source")
        if stored == fresh:
            continue
        if stored is None:
            null_fingerprint.append((rom_id, fresh))
            continue
        changed.append((rom_id, int(entry["app_id"]), fresh))
    return CoverRefreshScan(changed=changed, null_fingerprint=null_fingerprint)


def count_cover_refreshes(
    all_roms: list[dict[str, Any]],
    registry: dict[str, dict[str, Any]],
) -> int:
    """Count the bound ROMs whose server cover changed — the preview's cover-work number.

    Exactly ``len(scan_cover_refresh_candidates(...).changed)``: the same
    predicate the apply-path invalidation pass refreshes by, so the preview's
    count and the run's refresh set can never diverge. NULL-fingerprint rows
    are not counted — their adopt-vs-download fork depends on a cache-file
    check the preview must not perform (the preview is side-effect-free and
    the adopt is invisible to the user).
    """
    return len(scan_cover_refresh_candidates(all_roms, registry).changed)
