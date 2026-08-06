# A removal's claim discipline follows the recovery bundle, and an identity-only tree leases per unlink

## Status

Accepted. Triggered by [#1664](https://github.com/danielcopper/decky-romm-sync/issues/1664) (uninstalling a large ROM
looks dead for ~25 minutes, then reports a spurious failure).

First record to own the descriptor-relative claim machinery. That apparatus arrived whole with
[#1577](https://github.com/danielcopper/decky-romm-sync/pull/1577) (recovery-backed removed-game cleanup) without a
decision record of its own; what it is remains current truth in
[removed-game-cleanup.md](../architecture/removed-game-cleanup.md), and this records why it applies where it does.

## Context

Before #1577, `RomRemovalService._delete_rom_files` was a path-guarded `remove_tree(rom_dir)` — or a bare unlink for a
single-file ROM — returning `None`. #1577 built the claim machinery for removed-game cleanup, where the authority to
delete comes from a server 404 and a descriptor-relative no-follow claim is what makes acting on that authority safe,
and routed the **ordinary uninstall** through the same path. Nothing recorded that as a decision; the uninstall simply
inherited a discipline designed for a different trust situation.

The cost is proportional to bytes, for every uninstall. A multi-file ROM is read four times — at `claim_source`, at the
post-rename inventory, while acquiring writer-exclusion leases, and once more immediately before each unlink. On the
reporting device a 31 GB install moved roughly 124 GB off an SD card at a measured 91 MB/s: **22 minutes 48 seconds**,
with no progress indication, during which repeat presses raced the first removal and produced spurious failure toasts.
Nothing about this is specific to 31 GB; it is simply invisible below a few gigabytes.

A second, sharper defect sits in the same machinery. Whole-tree writer exclusion holds one open descriptor per regular
file for the entire removal. The plugin process runs with a soft `RLIMIT_NOFILE` of 1024 (hard 524288), so any tree with
more files than that raises `EMFILE` during lease acquisition — and because the rollback then restores the tree intact,
those ROMs become **permanently un-uninstallable through the UI**. Six titles in a real library exceed the limit today,
at 1685, 2025, 2999, 3802, 4488 and 53864 files. Multi-thousand-file dumps are ordinary on some platforms, so this is
reached in practice rather than in theory.

What the hashes actually buy was measured against the code rather than assumed. In a cleanup run,
`RecoveryBundleAdapter._require_records_match_claims` refuses any artifact record whose `sha256` differs from the sealed
claim's per-file hash, and that record digest is taken over the **copied** bytes in the bundle. The hash is therefore
what proves _the copy in the bundle is the copy being deleted_. Where no bundle exists, the claim is sealed and consumed
inside one call against one filesystem state: re-reading the bytes compares them only against themselves, and exact
identity (device, inode, mode, size, mtime, ctime) plus the kernel writer exclusion — which cannot be established at all
while another process holds the file open for writing — already carry everything that comparison could.

## Decision

**The claim discipline follows the bundle, not the caller.**

| Source                                            | Claim                                                            |
| ------------------------------------------------- | ---------------------------------------------------------------- |
| Selected for a sealed bundle                      | Decoded from that held bundle, digest-bound                      |
| Not captured, but a bundle was sealed             | Sealed fresh at mutation time, content-bound — the bundle exists |
| Removed with recovery off (no bundle anywhere)    | Sealed fresh at mutation time, identity-only                     |
| Removed by a user-initiated uninstall (no bundle) | Sealed fresh at mutation time, identity-only                     |

An identity-only claim is `claim_source(..., digest=False)` and records itself as `content_bound: false`. It is also the
only claim permitted to adopt the debris of a removal interrupted between the staging rename and the last unlink, and
only where a surviving install row proves the path was that ROM's.

**An identity-only directory leases each file across its own unlink** rather than holding the whole tree. A
content-bound removal is untouched and keeps the whole-tree hold.

Everything else is unchanged under both disciplines: staging rename, mount checks, no-follow traversal, and
exact-identity revalidation under writer exclusion held across each unlink.

## Consequences

**What was given up, precisely.** All-or-nothing across the tree, on the identity-only path only. Per-unlink
authorization is unchanged — a file another process holds open for writing is never deleted. A whole-subtree pass still
runs before any unlink and takes each file's lease in turn, so a writer already holding any file in the tree, or any
identity drift, refuses with nothing deleted. Only a writer arriving **during** the unlink loop reaches a partial
removal. That partial is reported as partial and ambiguous, never as success, and its message names how far the removal
got ("3 of 331 files were removed", or that no file was removed), so a caller can tell an untouched source from a
half-removed one. The remainder stays under the staging name, which the next attempt reclaims.

**What this buys.** Measured on a 2.59 GiB tree of 331 files: content-bound reads 10.34 GiB in 13.66 s — exactly four
times the tree, confirming the four-pass diagnosis — where identity-only reads **0.00 GiB in 0.38 s**. Applying that
byte count at the 91 MB/s the reporting device recorded reproduces the observed 22:48, so the model is validated rather
than plausible. Trees larger than the descriptor limit become removable at all.

**What is not weakened.** The prune path keeps every hash it had, including the fresh claims it seals for sources a
bundle did not capture. Deletion authority is untouched: it is still, only, a fresh single-attempt exact-id 404 under
the pinned namespace.

## Alternatives considered

**Collapse the four read passes into one, keeping content binding everywhere.** Rejected on both axes at once. It still
costs roughly six minutes for a 31 GB uninstall, so it does not fix the reported defect; and the only way to reach one
pass is to stop re-hashing immediately before each unlink, which weakens the prune path's revalidate-immediately-before-
mutation rule — the one property that makes a claimed deletion safe. Optimising the number of passes was the wrong axis:
the question was never how often to hash, but whether these bytes have anything to be bound to.

**Discriminate by entry point** — uninstall identity-only, every cleanup removal content-bound. Rejected. It preserved a
contradiction rather than resolving one: `removed-game-cleanup.md` already stated that recovery-off sources take a claim
of their own rather than consuming a bundle-decoded one, while the code sealed them fully content-bound. It also adds a
second discriminator to maintain beside the claim map that is already there. Keying on the absence of a bundle covers
both no-bundle cases with one rule.

**Raise the process's soft `RLIMIT_NOFILE` toward the 524288 hard limit and keep whole-tree leasing.** Rejected: it puts
an adapter in the business of mutating a process-global limit, it holds tens of thousands of concurrent kernel leases
for a single uninstall, and it only moves the ceiling — there is always a larger dump.

**Leave the descriptor exhaustion for later.** Rejected: six titles in a real library exceed the limit today, one of
them fifty-fold, and the failure mode is a ROM that can never be uninstalled from the UI rather than a slow one.

## See also

- [removed-game-cleanup.md](../architecture/removed-game-cleanup.md) — current truth for the claim disciplines, the
  writer-exclusion split, and the cleanup machinery around them
- [ADR-0007](0007-rom-retention-identity-anchor.md) — why an uninstall drops only files and the install record
