---
name: romm-siblings-cannot-identify-discs
description: RomM has ONE relation between related roms (`sibling_roms`) and it cannot distinguish "Disc 2 of the same release" from "the European release". Never implement multi-disc grouping (#1554) on `sibling_group_key` alone — that silently merges regional variants into one game. Any grouping needs a disc discriminator the server does not provide. Upstream half is version-stamped and must be re-verified.
type: project
---

# Sibling ≠ disc — never group multi-disc on `sibling_group_key` alone

**Durable rule:** before building any multi-disc grouping feature, first establish whether the server can distinguish a
**disc** sibling from a **regional/revision** sibling. If it cannot, grouping requires a discriminator the client has to
invent, and that decision needs to be made deliberately — not assumed.

**Why:** we already store `sibling_group_key` and `is_main_sibling` on `roms` (from the version-picker work,
#1295/#1297). So the obvious implementation of [#1554](https://github.com/danielcopper/decky-romm-sync/issues/1554) —
"group the siblings, download them into one folder, one save" — is a two-line reach from code we already have, and it is
**wrong**: the same relation that links `Game (Disc 1)` / `Game (Disc 2)` also links `Game (USA)` / `Game (Europe)`.
Grouping on it merges separate regional releases into a single game with a single save. That is a data-losing mistake
that looks entirely reasonable from inside our codebase, because nothing on our side reveals that the relation is
overloaded.

The only signal that separates the two cases is the `(Disc N)` text in the filename. That is a filename heuristic, not
server data, and it is not sufficient on its own — `Game (USA) (Disc 1)` and `Game (Europe) (Disc 1)` are siblings too,
so a correct grouping has to reconstruct release identity (region, revision, version) from flattened tags before
deciding which siblings belong to the same release. Argosy does the regex grouping and then needs a fuzzy substring
match in its save layer to cope with the fallout; Grout does not attempt it at all.

**How to apply:** treat "which roms are discs of one game" as an **open design question**, not a lookup. If #1554 or a
successor is picked up, the shape of the discriminator is the first thing to settle, and the fallback position (declare
the per-disc library shape unsupported, point at the folder-per-game shape that RomM's docs recommend and that every
client handles correctly) is legitimate.

## Upstream half — VERIFY BEFORE RELYING ON THIS

Observed on **RomM 5.0.0, 2026-07-28**. Upstream implementation detail; re-check before acting on it. Recorded because
establishing it took a full source sweep.

RomM has **no multi-disc concept anywhere** — no disc-number column, no disc relation, no grouping logic. Every `disc`
occurrence in the backend is either disc-_image_ hashing (PSP/RVZ) or audio track metadata. There is exactly one
relation, and disc siblings and regional siblings are indistinguishable within it.

Re-verify against a RomM checkout:

- `backend/alembic/versions/0069_sibling_roms_fs_name.py:53-72` — the `sibling_roms` **view**: same `platform_id`, and
  either a shared metadata id (igdb/moby/ss/launchbox/ra/hasheous/tgdb) **or** equal `fs_name_no_tags`
- `backend/handler/filesystem/roms_handler.py:181` — `parse_tags`: every parenthesised/bracketed chunk becomes a tag;
  anything not recognised as region/language/version/revision lands in `other_tags`, and is stripped from
  `fs_name_no_tags`. `(Disc 1)` and `(USA)` go through the identical path.
- Empirically confirmable against a live server: a rom with `fs_name` `"<Game> (USA)"` reports `fs_name_no_tags`
  `"<Game>"`.

Related: [[romm-injects-m3u-keep-es-de-gate]].
