from __future__ import annotations

import errno
import os
import resource
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from adapters.descriptor_paths import (
    claim_source,
    ensure_directory,
    measure_tree,
    remove_claimed,
    rename_claimed,
)
from lib.errors import OperationAbortedError


def test_inside_root_symlink_replacement_is_not_removed(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "source.srm"
    source.write_bytes(b"sealed")
    claim = claim_source(str(source), str(safe))
    replacement = safe / "replacement.srm"
    replacement.write_bytes(b"not sealed")
    source.unlink()
    source.symlink_to(replacement)

    with pytest.raises(RuntimeError, match="identity changed"):
        remove_claimed(str(source), str(safe), claim)

    assert source.is_symlink()
    assert replacement.read_bytes() == b"not sealed"


def test_intermediate_symlink_never_authorizes_deletion_outside_root(tmp_path):
    safe = tmp_path / "safe"
    safe.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.srm"
    target.write_bytes(b"keep")
    (safe / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        claim_source(str(safe / "linked" / "target.srm"), str(safe))

    assert target.read_bytes() == b"keep"


def test_replacement_in_pre_rename_window_is_verified_and_rolled_back(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "source.srm"
    source.write_bytes(b"sealed")
    claim = claim_source(str(source), str(safe))
    module = __import__("adapters.descriptor_paths", fromlist=["rename_noreplace_at"])
    original = module.rename_noreplace_at
    replaced = False

    def replace_then_rename(source_fd, src, destination_fd, dst):
        nonlocal replaced
        if src == source.name and not replaced:
            replaced = True
            source.unlink()
            source.write_bytes(b"replacement")
        return original(source_fd, src, destination_fd, dst)

    monkeypatch.setattr(module, "rename_noreplace_at", replace_then_rename)
    with pytest.raises(RuntimeError, match="while it was claimed"):
        remove_claimed(str(source), str(safe), claim)

    assert source.read_bytes() == b"replacement"


def test_claimed_directory_revalidates_descendants_and_restores_root_on_change(tmp_path):
    safe = tmp_path / "safe"
    source = safe / "game"
    source.mkdir(parents=True)
    child = source / "disc.bin"
    child.write_bytes(b"sealed")
    claim = claim_source(str(source), str(safe))
    child.write_bytes(b"replacement")

    with pytest.raises(RuntimeError, match="subtree changed"):
        remove_claimed(str(source), str(safe), claim)

    assert source.is_dir()
    assert child.read_bytes() == b"replacement"


def test_regular_root_hash_rejects_held_fd_write_after_rename(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"sealed")
    claim = claim_source(str(source), str(safe))
    writer = os.open(source, os.O_WRONLY)
    original = __import__("adapters.descriptor_paths", fromlist=["_require_claimed_identity"])._require_claimed_identity
    changed = False

    def mutate_after_rename(path, current, expected):
        nonlocal changed
        original(path, current, expected)
        if not changed:
            changed = True
            os.pwrite(writer, b"change", 0)

    monkeypatch.setattr("adapters.descriptor_paths._require_claimed_identity", mutate_after_rename)
    try:
        with pytest.raises(RuntimeError, match=r"active writer.*retained"):
            remove_claimed(str(source), str(safe), claim)
    finally:
        os.close(writer)

    assert source.read_bytes() == b"change"


def test_directory_child_write_after_post_rename_inventory_is_restored(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    source = safe / "game"
    source.mkdir(parents=True)
    child = source / "disc.bin"
    child.write_bytes(b"sealed")
    claim = claim_source(str(source), str(safe))
    writer = os.open(child, os.O_WRONLY)
    module = __import__("adapters.descriptor_paths", fromlist=["_inventory_directory"])
    original = module._inventory_directory
    calls = 0

    def mutate_after_inventory(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            os.pwrite(writer, b"change", 0)
        return result

    monkeypatch.setattr("adapters.descriptor_paths._inventory_directory", mutate_after_inventory)
    try:
        with pytest.raises(RuntimeError, match=r"active writer.*retained"):
            remove_claimed(str(source), str(safe), claim)
    finally:
        os.close(writer)

    assert child.read_bytes() == b"change"


def test_claim_rejects_same_device_nested_mount_identity(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    mounted = safe / "game" / "mounted"
    mounted.mkdir(parents=True)
    (mounted / "outside.bin").write_bytes(b"keep")
    module = __import__("adapters.descriptor_paths", fromlist=["_mount_id"])
    original = module._mount_id

    def fake_mount_id(fd):
        target = os.readlink(f"/proc/self/fd/{fd}")
        return original(fd) + (1 if "/mounted" in target else 0)

    monkeypatch.setattr("adapters.descriptor_paths._mount_id", fake_mount_id)

    with pytest.raises(ValueError, match="mount boundary"):
        claim_source(str(safe / "game"), str(safe))

    assert (mounted / "outside.bin").read_bytes() == b"keep"


def test_remove_reports_post_unlink_fsync_uncertainty(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"save")
    claim = claim_source(str(source), str(safe))
    monkeypatch.setattr("adapters.descriptor_paths.os.fsync", lambda _fd: (_ for _ in ()).throw(OSError("EIO")))

    outcome = remove_claimed(str(source), str(safe), claim)

    assert outcome == {
        "success": False,
        "changed": True,
        "ambiguous": True,
        "message": "Source was removed but directory durability is uncertain: EIO",
    }
    assert not source.exists()


def test_rename_reports_post_move_fsync_uncertainty(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    destination = safe / "backup.srm"
    source.write_bytes(b"save")
    claim = claim_source(str(source), str(safe))
    monkeypatch.setattr("adapters.descriptor_paths.os.fsync", lambda _fd: (_ for _ in ()).throw(OSError("EIO")))

    outcome = rename_claimed(str(source), str(destination), str(safe), claim)

    assert outcome["success"] is False
    assert outcome["changed"] is True
    assert outcome["ambiguous"] is True
    assert destination.read_bytes() == b"save"


def test_rename_never_replaces_a_destination_created_after_the_absence_check(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    destination = safe / "backup.srm"
    source.write_bytes(b"current-save")
    claim = claim_source(str(source), str(safe))
    module = __import__("adapters.descriptor_paths", fromlist=["rename_noreplace_at"])
    original = module.rename_noreplace_at

    def race_destination(*args, **kwargs):
        destination.write_bytes(b"concurrent-backup")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "rename_noreplace_at", race_destination)

    with pytest.raises(FileExistsError, match="already exists"):
        rename_claimed(str(source), str(destination), str(safe), claim)

    assert source.read_bytes() == b"current-save"
    assert destination.read_bytes() == b"concurrent-backup"


def test_rename_reports_the_move_when_the_source_path_was_reoccupied(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    destination = safe / "backup.srm"
    source.write_bytes(b"current-save")
    claim = claim_source(str(source), str(safe))

    def fail_and_reoccupy(path, current, expected):
        del path, current, expected
        source.write_bytes(b"emulator-recreated")
        raise RuntimeError("injected post-rename validation failure")

    monkeypatch.setattr("adapters.descriptor_paths._require_claimed_identity", fail_and_reoccupy)

    outcome = rename_claimed(str(source), str(destination), str(safe), claim)

    assert outcome["success"] is False
    assert outcome["changed"] is True
    assert outcome["ambiguous"] is True
    assert str(destination) in outcome["message"]
    assert "re-occupied" in outcome["message"]
    assert destination.read_bytes() == b"current-save"
    assert source.read_bytes() == b"emulator-recreated"


def test_ensure_directory_refuses_a_same_device_nested_mount_component(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    mounted = safe / "backups" / "mounted"
    mounted.mkdir(parents=True)
    (mounted / "outside.bin").write_bytes(b"keep")
    module = __import__("adapters.descriptor_paths", fromlist=["_mount_id"])
    original = module._mount_id

    def fake_mount_id(fd):
        target = os.readlink(f"/proc/self/fd/{fd}")
        return original(fd) + (1 if "/mounted" in target else 0)

    monkeypatch.setattr("adapters.descriptor_paths._mount_id", fake_mount_id)

    with pytest.raises(ValueError, match="mount boundary"):
        ensure_directory(str(mounted / "created"), str(safe))

    assert not (mounted / "created").exists()
    assert (mounted / "outside.bin").read_bytes() == b"keep"


def test_measure_tree_refuses_to_cross_a_same_device_nested_mount(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    mounted = safe / "game" / "mounted"
    mounted.mkdir(parents=True)
    (safe / "game" / "disc.bin").write_bytes(b"1234")
    (mounted / "outside.bin").write_bytes(b"keep")
    module = __import__("adapters.descriptor_paths", fromlist=["_mount_id"])
    original = module._mount_id

    def fake_mount_id(fd):
        target = os.readlink(f"/proc/self/fd/{fd}")
        return original(fd) + (1 if "/mounted" in target else 0)

    assert measure_tree(str(safe / "game"), str(safe)) == 4 + len(b"keep")

    monkeypatch.setattr("adapters.descriptor_paths._mount_id", fake_mount_id)

    with pytest.raises(ValueError, match="mount boundary"):
        measure_tree(str(safe / "game"), str(safe))


def test_remove_reports_lease_release_failure_after_unlink_as_ambiguous(tmp_path, monkeypatch):
    safe = tmp_path / "safe"
    safe.mkdir()
    source = safe / "save.srm"
    source.write_bytes(b"save")
    claim = claim_source(str(source), str(safe))
    module = __import__("adapters.descriptor_paths", fromlist=["fcntl"])
    original = module.fcntl.fcntl

    def fail_unlock(fd, command, *args):
        if command == module.fcntl.F_SETLEASE and args == (module.fcntl.F_UNLCK,):
            raise OSError("injected lease release failure")
        return original(fd, command, *args)

    monkeypatch.setattr(module.fcntl, "fcntl", fail_unlock)

    outcome = remove_claimed(str(source), str(safe), claim)

    assert outcome == {
        "success": False,
        "changed": True,
        "ambiguous": True,
        "message": "Source was removed but writer-exclusion teardown is uncertain: injected lease release failure",
    }
    assert not source.exists()


class TestCooperativeAbort:
    """Claiming a large tree is interruptible — it is pre-commit work."""

    def test_claiming_a_file_stops_when_asked(self, tmp_path):
        source = tmp_path / "big.bin"
        source.write_bytes(b"x" * (3 * 1024 * 1024))

        with pytest.raises(OperationAbortedError):
            claim_source(str(source), str(tmp_path), lambda: True)

    def test_claiming_a_directory_stops_when_asked(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        for index in range(5):
            (tree / f"file{index}.bin").write_bytes(b"y" * 1024)

        with pytest.raises(OperationAbortedError):
            claim_source(str(tree), str(tmp_path), lambda: True)

    def test_no_poll_claims_the_whole_tree_exactly_as_before(self, tmp_path):
        source = tmp_path / "small.bin"
        source.write_bytes(b"z")

        claim = claim_source(str(source), str(tmp_path))

        assert claim["source_identity"]["exists"] is True
        assert claim["sha256"] is not None

    def test_a_poll_that_never_fires_claims_the_whole_tree(self, tmp_path):
        source = tmp_path / "small.bin"
        source.write_bytes(b"z")

        claim = claim_source(str(source), str(tmp_path), lambda: False)

        assert claim == claim_source(str(source), str(tmp_path))


class TestIdentityOnlyClaims:
    """The hash-free claim discipline a caller uses when it seals and consumes its own claim."""

    @staticmethod
    def _tree(root) -> None:
        (root / "sub").mkdir(parents=True)
        (root / "disc.bin").write_bytes(b"\x00" * 4096)
        (root / "sub" / "data.bin").write_bytes(b"\x01" * 4096)

    @staticmethod
    def _count_hashes(monkeypatch) -> list[int]:
        module = __import__("adapters.descriptor_paths", fromlist=["_sha256_fd"])
        original = module._sha256_fd
        calls = [0]

        def counted(fd, should_abort=None):
            calls[0] += 1
            return original(fd, should_abort)

        monkeypatch.setattr(module, "_sha256_fd", counted)
        return calls

    def test_self_claimed_removal_reads_no_content_at_all(self, tmp_path, monkeypatch):
        safe = tmp_path / "safe"
        source = safe / "game"
        self._tree(source)
        calls = self._count_hashes(monkeypatch)

        claim = claim_source(str(source), str(safe), digest=False)
        outcome = remove_claimed(str(source), str(safe), claim)

        assert calls == [0]
        assert outcome["success"] is True
        assert outcome["changed"] is True
        assert not source.exists()

    def test_content_bound_removal_of_the_same_tree_still_hashes(self, tmp_path, monkeypatch):
        """Non-vacuous counterpart: the counter does see the content-bound path."""
        safe = tmp_path / "safe"
        source = safe / "game"
        self._tree(source)
        calls = self._count_hashes(monkeypatch)

        claim = claim_source(str(source), str(safe))
        remove_claimed(str(source), str(safe), claim)

        assert calls[0] > 0
        assert not source.exists()

    def test_identity_only_claim_carries_no_hashes(self, tmp_path):
        safe = tmp_path / "safe"
        source = safe / "game"
        self._tree(source)

        claim = claim_source(str(source), str(safe), digest=False)

        assert claim["content_bound"] is False
        assert claim["sha256"] is None
        assert all("sha256" not in entry for entry in claim["entries"].values())

    def test_identity_only_claim_still_refuses_a_rewritten_child(self, tmp_path):
        safe = tmp_path / "safe"
        source = safe / "game"
        self._tree(source)
        claim = claim_source(str(source), str(safe), digest=False)
        (source / "disc.bin").write_bytes(b"\x02" * 4096)

        with pytest.raises(RuntimeError, match="subtree changed"):
            remove_claimed(str(source), str(safe), claim)

        assert (source / "disc.bin").read_bytes() == b"\x02" * 4096
        assert (source / "sub" / "data.bin").exists()

    def test_identity_only_claim_still_refuses_a_replaced_root(self, tmp_path):
        safe = tmp_path / "safe"
        safe.mkdir()
        source = safe / "game.z64"
        source.write_bytes(b"sealed")
        claim = claim_source(str(source), str(safe), digest=False)
        source.unlink()
        source.write_bytes(b"replacement")

        with pytest.raises(RuntimeError, match="identity changed"):
            remove_claimed(str(source), str(safe), claim)

        assert source.read_bytes() == b"replacement"

    def test_identity_only_claim_still_refuses_an_active_writer(self, tmp_path):
        safe = tmp_path / "safe"
        source = safe / "game"
        self._tree(source)
        claim = claim_source(str(source), str(safe), digest=False)
        writer = os.open(source / "disc.bin", os.O_WRONLY)
        try:
            with pytest.raises(RuntimeError, match=r"active writer.*retained"):
                remove_claimed(str(source), str(safe), claim)
        finally:
            os.close(writer)

        assert (source / "disc.bin").exists()
        assert (source / "sub" / "data.bin").exists()


class TestRemovalProgress:
    @staticmethod
    def _recorder() -> tuple[list[tuple[int, int]], Callable[[int, int], None]]:
        reported: list[tuple[int, int]] = []

        def record(removed: int, total: int) -> None:
            reported.append((removed, total))

        return reported, record

    def test_every_unlinked_file_is_reported_against_the_claim_total(self, tmp_path):
        safe = tmp_path / "safe"
        source = safe / "game"
        (source / "sub").mkdir(parents=True)
        (source / "a.bin").write_bytes(b"a")
        (source / "b.bin").write_bytes(b"b")
        (source / "sub" / "c.bin").write_bytes(b"c")
        reported, record = self._recorder()

        claim = claim_source(str(source), str(safe), digest=False)
        remove_claimed(str(source), str(safe), claim, record)

        assert reported == [(1, 3), (2, 3), (3, 3)]
        assert not source.exists()

    def test_a_single_file_removal_reports_one_of_one(self, tmp_path):
        safe = tmp_path / "safe"
        safe.mkdir()
        source = safe / "game.z64"
        source.write_bytes(b"rom")
        reported, record = self._recorder()

        claim = claim_source(str(source), str(safe), digest=False)
        remove_claimed(str(source), str(safe), claim, record)

        assert reported == [(1, 1)]

    def test_an_absent_source_reports_nothing(self, tmp_path):
        safe = tmp_path / "safe"
        safe.mkdir()
        source = safe / "gone.z64"
        reported, record = self._recorder()

        claim = claim_source(str(source), str(safe), digest=False)
        outcome = remove_claimed(str(source), str(safe), claim, record)

        assert reported == []
        assert outcome["changed"] is False


class TestPerUnlinkLeasing:
    """An identity-only directory leases each file for its own unlink, not the whole tree at once."""

    @staticmethod
    def _tree(root, count: int) -> None:
        root.mkdir(parents=True)
        for index in range(count):
            (root / f"f{index:05d}.bin").write_bytes(b"x")

    def test_a_tree_larger_than_the_descriptor_limit_is_removable(self, tmp_path):
        """The whole-tree hold made the largest dumps permanently un-uninstallable (#1664)."""
        safe = tmp_path / "safe"
        source = safe / "game"
        self._tree(source, 1200)
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
        try:
            claim = claim_source(str(source), str(safe), digest=False)
            outcome = remove_claimed(str(source), str(safe), claim)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

        assert outcome["success"] is True
        assert not source.exists()

    def test_the_content_bound_hold_still_refuses_such_a_tree_rather_than_half_removing_it(self, tmp_path):
        """Non-vacuous counterpart: whole-tree leasing is what the identity-only path opts out of."""
        safe = tmp_path / "safe"
        source = safe / "game"
        self._tree(source, 1200)
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        claim = claim_source(str(source), str(safe))
        resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))
        try:
            with pytest.raises(OSError) as excinfo:
                remove_claimed(str(source), str(safe), claim)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

        assert excinfo.value.errno == errno.EMFILE
        assert source.is_dir()
        assert len(os.listdir(source)) == 1200

    def test_a_writer_present_before_the_loop_refuses_with_nothing_removed(self, tmp_path):
        safe = tmp_path / "safe"
        source = safe / "game"
        (source / "sub").mkdir(parents=True)
        (source / "a.bin").write_bytes(b"a")
        (source / "b.bin").write_bytes(b"b")
        (source / "sub" / "c.bin").write_bytes(b"c")
        claim = claim_source(str(source), str(safe), digest=False)
        # The last file in walk order, so a pre-loop probe is the only thing that
        # can catch it before the earlier two are already gone.
        writer = os.open(source / "sub" / "c.bin", os.O_WRONLY)
        try:
            with pytest.raises(RuntimeError, match=r"active writer.*retained"):
                remove_claimed(str(source), str(safe), claim)
        finally:
            os.close(writer)

        assert (source / "a.bin").exists()
        assert (source / "b.bin").exists()
        assert (source / "sub" / "c.bin").exists()

    def test_a_writer_arriving_mid_loop_reports_the_partial_removal(self, tmp_path, monkeypatch):
        safe = tmp_path / "safe"
        source = safe / "game"
        source.mkdir(parents=True)
        for name in ("a.bin", "b.bin", "c.bin"):
            (source / name).write_bytes(b"x")
        claim = claim_source(str(source), str(safe), digest=False)
        module = __import__("adapters.descriptor_paths", fromlist=["_leased_regular"])
        original = module._leased_regular
        writers: list[int] = []
        leased: list[str] = []

        def open_a_writer_once_the_delete_loop_is_under_way(directory_fd, name, path):
            # The first three calls are the pre-unlink probe over a/b/c; the
            # delete loop then re-leases each, so call five is b.bin's unlink.
            leased.append(name)
            if len(leased) == 5 and not writers:
                writers.append(os.open(name, os.O_WRONLY, dir_fd=directory_fd))
            return original(directory_fd, name, path)

        monkeypatch.setattr(module, "_leased_regular", open_a_writer_once_the_delete_loop_is_under_way)
        try:
            outcome = remove_claimed(str(source), str(safe), claim)
        finally:
            for fd in writers:
                os.close(fd)

        assert outcome["success"] is False
        assert outcome["changed"] is True
        assert outcome["ambiguous"] is True
        assert "1 of 3 files were removed" in outcome["message"]
        # A stopped removal leaves the remainder under the staging name rather
        # than back at the source path — the outcome is the record of that.
        staged = [entry for entry in os.listdir(safe) if entry.startswith(".game.romm-prune-")]
        assert len(staged) == 1
        assert sorted(os.listdir(safe / staged[0])) == ["b.bin", "c.bin"]
        assert not source.exists()

    def test_a_stopped_removal_that_touched_no_file_says_so(self, tmp_path, monkeypatch):
        safe = tmp_path / "safe"
        source = safe / "game"
        source.mkdir(parents=True)
        (source / "a.bin").write_bytes(b"a")
        claim = claim_source(str(source), str(safe), digest=False)
        module = __import__("adapters.descriptor_paths", fromlist=["_require_file_claim"])
        original = module._require_file_claim
        calls = [0]

        def fail_once_the_probe_is_done(*args, **kwargs):
            calls[0] += 1
            if calls[0] > 0 and kwargs.get("claimed") is False:
                raise RuntimeError("simulated pre-unlink refusal")
            return original(*args, **kwargs)

        monkeypatch.setattr(module, "_require_file_claim", fail_once_the_probe_is_done)
        outcome = remove_claimed(str(source), str(safe), claim)

        assert outcome["success"] is False
        assert outcome["ambiguous"] is True
        assert "no file was removed" in outcome["message"]
        assert os.listdir(safe) != []
