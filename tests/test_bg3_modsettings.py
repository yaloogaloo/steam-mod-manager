"""Non-destructive Patch 8 modsettings.lsx projector."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.bg3_modsettings import (
    GUSTAVX_UUID,
    Bg3ModsettingsError,
    MissingInsertion,
    ModuleShortDescValues,
    parse_bg3_modsettings,
    project_bg3_modsettings,
    short_desc_from_metadata,
)
from services.bg3_pak import resolve_bg3_mod_metadata
from tests.helpers.bg3_lspk import meta_lsx_bytes, write_lspk_v18

UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"
UUID_UNMANAGED = "396c5966-09b0-40a1-af3f-93a5e9ce71c0"
UUID_EXTRA = "cccccccc-cccc-cccc-cccc-ccccccccccc3"


def _short(
    uuid: str,
    *,
    folder: str,
    name: str,
    md5: str = "",
    handle: str = "0",
    version: str = "36028797018963968",
) -> str:
    return (
        '                        <node id="ModuleShortDesc">\n'
        f'                            <attribute id="Folder" type="LSString" value="{folder}"/>\n'
        f'                            <attribute id="MD5" type="LSString" value="{md5}"/>\n'
        f'                            <attribute id="Name" type="LSString" value="{name}"/>\n'
        f'                            <attribute id="PublishHandle" type="uint64" value="{handle}"/>\n'
        f'                            <attribute id="UUID" type="guid" value="{uuid}"/>\n'
        f'                            <attribute id="Version64" type="int64" value="{version}"/>\n'
        "                        </node>"
    )


def _lsx(*nodes: str) -> str:
    body = "\n".join(nodes)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<save>\n"
        '    <version major="4" minor="8" revision="0" build="700"/>\n'
        '    <region id="ModuleSettings">\n'
        '        <node id="root">\n'
        "            <children>\n"
        '                <node id="Mods">\n'
        "                    <children>\n"
        f"{body}\n"
        "                    </children>\n"
        "                </node>\n"
        "            </children>\n"
        "        </node>\n"
        "    </region>\n"
        "</save>\n"
    )


def _base_doc() -> str:
    return _lsx(
        _short(GUSTAVX_UUID, folder="GustavX", name="GustavX"),
        _short(UUID_A, folder="ModA", name="A"),
        _short(
            UUID_UNMANAGED,
            folder="CommunityLibrary",
            name="CommunityLibrary",
            version="1",
        ),
        _short(
            UUID_UNMANAGED,
            folder="CommunityLibrary",
            name="CommunityLibrary",
            version="2",
        ),
        _short(UUID_B, folder="ModB", name="B"),
    )


def _write(tmp_path: Path, text: str, name: str = "modsettings.lsx") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _project(path: Path, order: list[str], **kwargs):
    dest = kwargs.pop("output_path", path.parent / "out.lsx")
    return project_bg3_modsettings(
        path,
        dest,
        managed_uuid_order=order,
        **kwargs,
    )


def test_gustavx_stays_first(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    result = _project(src, [UUID_B, UUID_A])
    _, nodes = parse_bg3_modsettings(result.output_text)
    assert nodes[0].uuid == GUSTAVX_UUID
    assert [n.uuid for n in nodes[1:] if n.uuid in {UUID_A, UUID_B}] == [UUID_B, UUID_A]


def test_a_then_b_and_b_then_a(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    ab = _project(src, [UUID_A, UUID_B], output_path=tmp_path / "ab.lsx")
    ba = _project(src, [UUID_B, UUID_A], output_path=tmp_path / "ba.lsx")
    _, nodes_ab = parse_bg3_modsettings(ab.output_text)
    _, nodes_ba = parse_bg3_modsettings(ba.output_text)
    managed_ab = [n.uuid for n in nodes_ab if n.uuid in {UUID_A, UUID_B}]
    managed_ba = [n.uuid for n in nodes_ba if n.uuid in {UUID_A, UUID_B}]
    assert managed_ab == [UUID_A, UUID_B]
    assert managed_ba == [UUID_B, UUID_A]


def test_unmanaged_and_duplicate_unmanaged_preserved(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    result = _project(src, [UUID_B, UUID_A])
    _, nodes = parse_bg3_modsettings(result.output_text)
    unmanaged = [n for n in nodes if n.uuid == UUID_UNMANAGED]
    assert len(unmanaged) == 2
    assert unmanaged[0].attributes["Version64"] == "1"
    assert unmanaged[1].attributes["Version64"] == "2"
    assert result.unmanaged_uuids == (UUID_UNMANAGED, UUID_UNMANAGED)


def test_managed_duplicate_fails_closed(tmp_path: Path) -> None:
    text = _lsx(
        _short(GUSTAVX_UUID, folder="GustavX", name="GustavX"),
        _short(UUID_A, folder="ModA", name="A"),
        _short(UUID_A, folder="ModA2", name="A2"),
    )
    src = _write(tmp_path, text)
    dest = tmp_path / "out.lsx"
    with pytest.raises(Bg3ModsettingsError) as exc:
        project_bg3_modsettings(src, dest, managed_uuid_order=[UUID_A])
    assert exc.value.code == "DuplicateManagedUUID"
    assert exc.value.fields.get("uuid") == UUID_A
    assert exc.value.fields.get("count") == 2
    assert not dest.exists()


def test_missing_managed_node_creation(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    values = ModuleShortDescValues(
        uuid=UUID_EXTRA,
        folder="ModC",
        name="C",
        md5="abc",
        publish_handle="0",
        version64="9",
    )
    with pytest.raises(Bg3ModsettingsError) as exc:
        _project(src, [UUID_A, UUID_B, UUID_EXTRA])
    assert exc.value.code == "ProjectionPolicyRequired"

    result = _project(
        src,
        [UUID_A, UUID_B, UUID_EXTRA],
        create_missing=True,
        missing_insertion=MissingInsertion.AFTER_LAST_MANAGED_SLOT,
        missing_nodes={UUID_EXTRA: values},
    )
    _, nodes = parse_bg3_modsettings(result.output_text)
    managed = [n.uuid for n in nodes if n.uuid in {UUID_A, UUID_B, UUID_EXTRA}]
    assert managed == [UUID_A, UUID_B, UUID_EXTRA]
    assert result.created_uuids == (UUID_EXTRA,)
    created = next(n for n in nodes if n.uuid == UUID_EXTRA)
    assert created.attributes["Folder"] == "ModC"
    assert created.attributes["MD5"] == "abc"


def test_missing_node_from_meta_lsx_resolver(tmp_path: Path) -> None:
    pak = write_lspk_v18(
        tmp_path / "5eSpells.pak",
        {
            "Mods/5eSpells/meta.lsx": meta_lsx_bytes(
                uuid=UUID_EXTRA, name="5eSpells", folder="5eSpells"
            )
        },
    )
    meta = resolve_bg3_mod_metadata(pak)
    src = _write(tmp_path, _base_doc())
    result = _project(
        src,
        [UUID_A, UUID_B, UUID_EXTRA],
        create_missing=True,
        missing_insertion=MissingInsertion.AFTER_LAST_MANAGED_SLOT,
        missing_nodes={UUID_EXTRA: short_desc_from_metadata(meta)},
    )
    _, nodes = parse_bg3_modsettings(result.output_text)
    created = next(n for n in nodes if n.uuid == UUID_EXTRA)
    assert created.attributes["Name"] == "5eSpells"
    assert created.attributes["Folder"] == "5eSpells"


def test_stale_default_does_not_delete(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    result = _project(src, [UUID_A], active_managed_uuids=[UUID_A])
    _, nodes = parse_bg3_modsettings(result.output_text)
    assert UUID_B in [n.uuid for n in nodes]
    assert UUID_B in result.unmanaged_uuids


def test_stale_explicit_remove(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    result = _project(
        src,
        [UUID_A],
        active_managed_uuids=[UUID_A],
        remove_uuids=[UUID_B],
    )
    _, nodes = parse_bg3_modsettings(result.output_text)
    assert UUID_B not in [n.uuid for n in nodes]
    assert result.removed_uuids == (UUID_B,)
    assert UUID_UNMANAGED in [n.uuid for n in nodes]


def test_schema_remains_patch8_and_no_modorder(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    result = _project(src, [UUID_B, UUID_A])
    assert '<version major="4" minor="8" revision="0" build="700"/>' in result.output_text
    assert "ModOrder" not in result.output_text
    assert '<region id="ModuleSettings">' in result.output_text


def test_unrelated_region_preserved(tmp_path: Path) -> None:
    extra = _base_doc().replace(
        "</region>\n</save>",
        '        <node id="CustomKeepMe">\n'
        '            <attribute id="X" type="LSString" value="1"/>\n'
        "        </node>\n"
        "    </region>\n"
        "</save>",
    )
    src = _write(tmp_path, extra)
    result = _project(src, [UUID_A, UUID_B])
    assert 'node id="CustomKeepMe"' in result.output_text
    assert 'value="1"' in result.output_text


def test_untouched_unmanaged_attributes_preserved(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    before = parse_bg3_modsettings(src.read_text(encoding="utf-8"))[1]
    result = _project(src, [UUID_B, UUID_A])
    after = parse_bg3_modsettings(result.output_text)[1]
    before_u = [n.attributes for n in before if n.uuid == UUID_UNMANAGED]
    after_u = [n.attributes for n in after if n.uuid == UUID_UNMANAGED]
    assert before_u == after_u


def test_idempotent_second_pass_skips_write(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    first = _project(src, [UUID_A, UUID_B], output_path=tmp_path / "once.lsx")
    assert first.written is True
    second = project_bg3_modsettings(
        tmp_path / "once.lsx",
        tmp_path / "once.lsx",
        managed_uuid_order=[UUID_A, UUID_B],
    )
    assert second.skipped_idempotent is True
    assert second.written is False


def test_malformed_lsx_does_not_write(tmp_path: Path) -> None:
    src = _write(tmp_path, "<not xml")
    dest = tmp_path / "out.lsx"
    with pytest.raises(Bg3ModsettingsError) as exc:
        project_bg3_modsettings(src, dest, managed_uuid_order=[UUID_A])
    assert exc.value.code in {"MalformedLsx", "UnsupportedSchema"}
    assert not dest.exists()


def test_serialization_validation_failure_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write(tmp_path, _base_doc())
    dest = tmp_path / "out.lsx"

    def boom(*_args, **_kwargs):
        raise Bg3ModsettingsError("SerializationInvalid", "forced")

    monkeypatch.setattr("services.bg3_modsettings._validate_projection", boom)
    with pytest.raises(Bg3ModsettingsError) as exc:
        project_bg3_modsettings(src, dest, managed_uuid_order=[UUID_A, UUID_B])
    assert exc.value.code == "SerializationInvalid"
    assert not dest.exists()


def test_atomic_replace_failure_leaves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write(tmp_path, _base_doc())
    dest = tmp_path / "target.lsx"
    dest.write_text("ORIGINAL", encoding="utf-8")
    original = dest.read_bytes()

    def fail_replace(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("services.bg3_modsettings.os.replace", fail_replace)
    with pytest.raises(OSError):
        project_bg3_modsettings(src, dest, managed_uuid_order=[UUID_B, UUID_A])
    assert dest.read_bytes() == original


def test_gustavx_missing_does_not_invent(tmp_path: Path) -> None:
    text = _lsx(_short(UUID_A, folder="ModA", name="A"))
    src = _write(tmp_path, text)
    dest = tmp_path / "out.lsx"
    with pytest.raises(Bg3ModsettingsError) as exc:
        project_bg3_modsettings(src, dest, managed_uuid_order=[UUID_A])
    assert exc.value.code == "GustavXMissing"
    assert not dest.exists()


def test_gustavx_not_accepted_in_canonical_order(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    with pytest.raises(Bg3ModsettingsError) as exc:
        _project(src, [GUSTAVX_UUID, UUID_A])
    assert exc.value.code == "GustavXNotManaged"


def test_modorder_source_is_rejected(tmp_path: Path) -> None:
    text = _base_doc().replace(
        '<node id="Mods">',
        '<node id="ModOrder"><children></children></node>\n                <node id="Mods">',
    )
    src = _write(tmp_path, text)
    dest = tmp_path / "out.lsx"
    with pytest.raises(Bg3ModsettingsError) as exc:
        project_bg3_modsettings(src, dest, managed_uuid_order=[UUID_A, UUID_B])
    assert exc.value.code == "UnexpectedModOrder"
    assert not dest.exists()


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    src = _write(tmp_path, _base_doc())
    dest = tmp_path / "out.lsx"
    result = project_bg3_modsettings(
        src,
        dest,
        managed_uuid_order=[UUID_B, UUID_A],
        dry_run=True,
    )
    assert result.written is False
    assert not dest.exists()
    assert UUID_B in result.output_text
