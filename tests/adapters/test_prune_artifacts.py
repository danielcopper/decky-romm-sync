from pathlib import Path

from adapters.prune_artifacts import PruneArtifactAdapter


def test_discovers_and_removes_only_named_rom_cache_files(tmp_path):
    covers = tmp_path / "covers"
    artwork = tmp_path / "artwork"
    covers.mkdir()
    artwork.mkdir()
    expected = [covers / "7.png", covers / "7.cover-meta.json", artwork / "7_grid.png"]
    for path in expected:
        path.write_bytes(b"x")
    unrelated = artwork / "8_grid.png"
    unrelated.write_bytes(b"keep")
    adapter = PruneArtifactAdapter(runtime_dir=str(tmp_path))

    artifacts = adapter.recovery_artifacts([7])
    assert {Path(item["source_path"]) for item in artifacts} == set(expected)
    adapter.remove([7])
    assert all(not path.exists() for path in expected)
    assert unrelated.exists()
