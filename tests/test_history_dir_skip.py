"""Global skip of ``历史版本`` directories in Mod file scans."""

from __future__ import annotations

from pathlib import Path

from services.importers.local_scanner import (
    is_history_version_path,
    is_skipped_mod_path_part,
    is_under_skipped_mod_dir,
    scan_mod_directory,
)


def test_is_skipped_mod_path_part_history() -> None:
    assert is_skipped_mod_path_part("历史版本")
    assert is_under_skipped_mod_dir(("Main", "历史版本", "old.zip"))
    assert is_history_version_path(r"E:\mods\历史版本\a.zip")
    assert is_history_version_path("Optional/历史版本/nested.zip")
    assert not is_skipped_mod_path_part("Main")


def test_scan_mod_directory_skips_history_folder(tmp_path: Path) -> None:
    root = tmp_path / "mod"
    (root / "Main").mkdir(parents=True)
    (root / "历史版本").mkdir(parents=True)
    (root / "Main" / "current.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (root / "历史版本" / "old.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    nested = root / "Optional" / "历史版本"
    nested.mkdir(parents=True)
    (nested / "nested_old.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (root / "Optional").mkdir(parents=True, exist_ok=True)
    (root / "Optional" / "extra.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    names = {f.filename for f in scan_mod_directory(root).files}
    assert names == {"current.zip", "extra.zip"}
    assert "old.zip" not in names
    assert "nested_old.zip" not in names


def test_filter_out_history_version_entries_cache() -> None:
    from core.mod_platform import ModFileEntry
    from services.importers.local_scanner import (
        filter_out_history_version_entries,
        is_history_version_entry,
    )

    keep = ModFileEntry(id="1", filename="ok.zip", path="Main/ok.zip")
    drop = ModFileEntry(
        id="2", filename="old.zip", path="历史版本/old.zip", name="历史版本包"
    )
    assert is_history_version_entry(drop)
    assert not is_history_version_entry(keep)
    assert filter_out_history_version_entries([keep, drop]) == [keep]


def test_zip_extract_skips_history_members(tmp_path: Path) -> None:
    import zipfile

    from services.importers.archive import _extract_zip

    src = tmp_path / "pack.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("Main/a.pak", b"aaa")
        zf.writestr("历史版本/b.pak", b"bbb")
    dest = tmp_path / "out"
    dest.mkdir()
    _extract_zip(src, dest)
    assert (dest / "Main" / "a.pak").is_file()
    assert not (dest / "历史版本" / "b.pak").exists()
