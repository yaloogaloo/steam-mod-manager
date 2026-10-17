"""BG3 LSPK v18 reader and meta.lsx UUID resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.bg3_pak import (
    COMPRESSION_LZ4,
    COMPRESSION_NONE,
    COMPRESSION_ZLIB,
    COMPRESSION_ZSTD,
    Bg3PakError,
    clear_bg3_metadata_cache,
    deployed_pak_paths_from_manifest,
    extract_meta_lsx,
    resolve_bg3_mod_metadata,
    zstd_available,
)
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry
from tests.helpers.bg3_lspk import meta_lsx_bytes, write_lspk_v18

UUID = "fb5f528d-4d48-4bf2-a668-2274d3cfba96"
DEP = "28ac9ce2-2aba-8cda-b3b5-6e922f71b6b8"


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_bg3_metadata_cache()
    yield
    clear_bg3_metadata_cache()


def _pak(
    tmp_path: Path,
    *,
    name: str = "5eSpells.pak",
    compression: int = COMPRESSION_NONE,
    uuid: str = UUID,
    xml: bytes | None = None,
    files: dict[str, bytes] | None = None,
) -> Path:
    payload = xml if xml is not None else meta_lsx_bytes(
        uuid=uuid, name="5eSpells", folder="5eSpells"
    )
    contents = files if files is not None else {f"Mods/5eSpells/meta.lsx": payload}
    return write_lspk_v18(tmp_path / name, contents, compression=compression)


def test_uncompressed_meta_lsx_uuid(tmp_path: Path) -> None:
    pak = _pak(tmp_path)
    meta = resolve_bg3_mod_metadata(pak)
    assert meta.uuid == UUID
    assert meta.name == "5eSpells"
    assert meta.folder == "5eSpells"
    assert meta.dependencies == (DEP,)
    assert meta.md5
    assert meta.publish_handle == "0"


def test_zlib_compressed_entry(tmp_path: Path) -> None:
    pak = _pak(tmp_path, name="zlib.pak", compression=COMPRESSION_ZLIB)
    assert resolve_bg3_mod_metadata(pak).uuid == UUID


def test_lz4_compressed_entry(tmp_path: Path) -> None:
    pak = _pak(tmp_path, name="lz4.pak", compression=COMPRESSION_LZ4)
    assert resolve_bg3_mod_metadata(pak).uuid == UUID


@pytest.mark.skipif(not zstd_available(), reason="zstd not installed")
def test_zstd_compressed_entry(tmp_path: Path) -> None:
    pak = _pak(tmp_path, name="zstd.pak", compression=COMPRESSION_ZSTD)
    assert resolve_bg3_mod_metadata(pak).uuid == UUID


def test_zstd_unavailable_is_structured_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.bg3_pak import LspkFileEntry, extract_lspk_entry, list_lspk_v18_entries
    from services import bg3_pak as mod

    monkeypatch.setattr(mod, "_ZSTD_DECOMPRESS", None)
    pak = _pak(tmp_path, compression=COMPRESSION_ZLIB)
    entry = list_lspk_v18_entries(pak)[0]
    zstd_entry = LspkFileEntry(
        name=entry.name,
        offset=entry.offset,
        archive_part=0,
        compression=COMPRESSION_ZSTD,
        size_on_disk=entry.size_on_disk,
        uncompressed_size=entry.uncompressed_size,
    )
    with pytest.raises(Bg3PakError) as exc:
        extract_lspk_entry(pak, zstd_entry)
    assert exc.value.code == "UnsupportedCompression"
    assert exc.value.fields.get("method") == COMPRESSION_ZSTD


def test_malformed_header(tmp_path: Path) -> None:
    pak = write_lspk_v18(
        tmp_path / "bad.pak",
        {"Mods/X/meta.lsx": meta_lsx_bytes(uuid=UUID)},
        corrupt_signature=True,
    )
    with pytest.raises(Bg3PakError) as exc:
        resolve_bg3_mod_metadata(pak)
    assert exc.value.code == "MalformedHeader"


def test_missing_meta_lsx(tmp_path: Path) -> None:
    pak = write_lspk_v18(
        tmp_path / "empty.pak",
        {"Mods/X/readme.txt": b"no meta here"},
    )
    with pytest.raises(Bg3PakError) as exc:
        resolve_bg3_mod_metadata(pak)
    assert exc.value.code == "MissingMetaLsx"


def test_invalid_xml(tmp_path: Path) -> None:
    pak = write_lspk_v18(
        tmp_path / "badxml.pak",
        {"Mods/X/meta.lsx": b"<save><not-closed>"},
    )
    with pytest.raises(Bg3PakError) as exc:
        resolve_bg3_mod_metadata(pak)
    assert exc.value.code == "InvalidMetaXml"


def test_missing_module_info(tmp_path: Path) -> None:
    xml = b"""<?xml version="1.0"?><save><region id="Config"><node id="root"/></region></save>"""
    pak = write_lspk_v18(tmp_path / "nomi.pak", {"Mods/X/meta.lsx": xml})
    with pytest.raises(Bg3PakError) as exc:
        resolve_bg3_mod_metadata(pak)
    assert exc.value.code == "MissingModuleInfo"


def test_missing_uuid(tmp_path: Path) -> None:
    xml = b"""<?xml version="1.0"?><save>
    <node id="ModuleInfo">
        <attribute id="Name" type="LSString" value="X"/>
    </node>
    </save>"""
    pak = write_lspk_v18(tmp_path / "nouuid.pak", {"Mods/X/meta.lsx": xml})
    with pytest.raises(Bg3PakError) as exc:
        resolve_bg3_mod_metadata(pak)
    assert exc.value.code == "MissingUUID"


def test_filename_without_uuid_still_reads_meta(tmp_path: Path) -> None:
    pak = _pak(tmp_path, name="5eSpells.pak")
    assert "fb5f528d" not in pak.name
    meta = resolve_bg3_mod_metadata(pak)
    assert meta.uuid == UUID


def test_cache_hit_same_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pak = _pak(tmp_path)
    first = resolve_bg3_mod_metadata(pak)
    calls = {"n": 0}
    real = extract_meta_lsx

    def wrapped(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr("services.bg3_pak.extract_meta_lsx", wrapped)
    second = resolve_bg3_mod_metadata(pak)
    assert second.uuid == first.uuid
    assert calls["n"] == 0


def test_cache_miss_when_mtime_or_size_changes(tmp_path: Path) -> None:
    pak = _pak(tmp_path)
    first = resolve_bg3_mod_metadata(pak)
    other_uuid = "7b8366bd-abc1-4f9f-ba9d-585549b4a750"
    write_lspk_v18(
        pak,
        {"Mods/Other/meta.lsx": meta_lsx_bytes(uuid=other_uuid, name="Other", folder="Other")},
    )
    second = resolve_bg3_mod_metadata(pak)
    assert first.uuid == UUID
    assert second.uuid == other_uuid


def test_missing_pak_is_unresolved(tmp_path: Path) -> None:
    missing = tmp_path / "gone.pak"
    with pytest.raises(Bg3PakError) as exc:
        resolve_bg3_mod_metadata(missing)
    assert exc.value.code == "MissingPak"


def test_multipart_entry_is_rejected(tmp_path: Path) -> None:
    pak = write_lspk_v18(
        tmp_path / "part.pak",
        {"Mods/X/meta.lsx": meta_lsx_bytes(uuid=UUID)},
        archive_part=1,
    )
    with pytest.raises(Bg3PakError) as exc:
        resolve_bg3_mod_metadata(pak)
    assert exc.value.code == "MultipartEntryUnsupported"


def test_unsupported_compression_method(tmp_path: Path) -> None:
    from services.bg3_pak import LspkFileEntry, extract_lspk_entry, list_lspk_v18_entries

    pak = _pak(tmp_path)
    entry = list_lspk_v18_entries(pak)[0]
    bad = LspkFileEntry(
        name=entry.name,
        offset=entry.offset,
        archive_part=0,
        compression=9,
        size_on_disk=entry.size_on_disk,
        uncompressed_size=entry.uncompressed_size,
    )
    with pytest.raises(Bg3PakError) as exc:
        extract_lspk_entry(pak, bad)
    assert exc.value.code == "UnsupportedCompression"
    assert exc.value.fields.get("method") == 9


def test_filename_uuid_is_not_used_as_identity(tmp_path: Path) -> None:
    fake = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    pak = _pak(
        tmp_path,
        name=f"WeightlessGold_{fake}.pak",
        uuid=UUID,
    )
    meta = resolve_bg3_mod_metadata(pak)
    assert meta.uuid == UUID
    assert meta.uuid != fake


def test_deploy_manifest_pak_paths_only() -> None:
    manifest = DeployManifest(
        mod_id="1",
        deploy_time="",
        deploy_type="pak_mod_path",
        files=[
            ManifestFileEntry(
                source="a.pak",
                target=r"F:\game\Mods\5eSpells.pak",
                type="pak",
            ),
            ManifestFileEntry(
                source="note.txt",
                target=r"F:\game\Mods\readme.txt",
                type="folder_copy",
            ),
        ],
    )
    paths = deployed_pak_paths_from_manifest(manifest)
    assert [p.name for p in paths] == ["5eSpells.pak"]


def test_resolver_does_not_accept_workspace_id_as_uuid(tmp_path: Path) -> None:
    pak = _pak(tmp_path)
    meta = resolve_bg3_mod_metadata(pak)
    assert meta.uuid != "125"
    assert "-" in meta.uuid
