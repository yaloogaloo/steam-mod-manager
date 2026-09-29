"""Non-destructive Patch 8 ``modsettings.lsx`` projector.

Parses an existing LSX and only reorders ``Mods/children/ModuleShortDesc``.
Never generates a document from scratch. Never creates ``ModOrder``.

Canonical index 0 (first SMM Mod after GustavX) is the lowest overlay
priority. The last SMM Mod is the highest overlay priority (later list
entry wins on conflicting resources).
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape

from services.bg3_pak import BG3ModMetadata

GUSTAVX_UUID = "cb555efe-2d9e-131f-8195-a89329d218ea"
PATCH8_VERSION = {"major": "4", "minor": "8", "revision": "0", "build": "700"}

_ATTR_RE = re.compile(
    r'<attribute\s+id="([^"]+)"\s+type="([^"]*)"\s+value="([^"]*)"\s*/>',
    re.IGNORECASE,
)
_VERSION_RE = re.compile(
    r"<version\s+([^>]*?)\s*/>",
    re.IGNORECASE | re.DOTALL,
)
_VERSION_FIELD_RE = re.compile(r'([A-Za-z]+)="([^"]*)"')


class Bg3ModsettingsError(Exception):
    """Structured projector failure. On error, no output file is written."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = str(code)
        self.fields = dict(fields)

    def __str__(self) -> str:
        extra = " ".join(f"{k}={v}" for k, v in self.fields.items())
        if extra:
            return f"{self.code}: {self.args[0]} ({extra})"
        return f"{self.code}: {self.args[0]}"


class MissingInsertion(str, Enum):
    """Where to put ModuleShortDesc nodes that are not yet in the file.

    POLICY_REQUIRED is the v1 default: refuse to invent a relative position
    against unmanaged nodes. AFTER_LAST_MANAGED_SLOT expands immediately
    after the last existing managed node (or after GustavX when there are
    no managed slots) and shifts following unmanaged nodes down. It does
    not skip trailing unmanaged entries to reach EOF.
    """

    POLICY_REQUIRED = "policy_required"
    AFTER_LAST_MANAGED_SLOT = "after_last_managed_slot"


@dataclass(frozen=True)
class ModuleShortDescValues:
    """Values used only when creating a missing managed node from meta.lsx."""

    uuid: str
    folder: str
    name: str
    md5: str = ""
    publish_handle: str = "0"
    version64: str = "0"


@dataclass(frozen=True)
class ParsedShortDesc:
    raw: str
    uuid: str
    attributes: dict[str, str]


@dataclass
class ProjectionResult:
    written: bool
    output_text: str
    managed_uuid_order: tuple[str, ...]
    unmanaged_uuids: tuple[str, ...]
    created_uuids: tuple[str, ...] = ()
    removed_uuids: tuple[str, ...] = ()
    skipped_idempotent: bool = False


def short_desc_from_metadata(meta: BG3ModMetadata) -> ModuleShortDescValues:
    """Build a Patch 8 ModuleShortDesc from resolved meta.lsx fields. No invented UUID."""
    uuid = str(meta.uuid or "").strip().lower()
    if not uuid:
        raise Bg3ModsettingsError("MissingNodeMetadata", "meta.lsx UUID is empty")
    return ModuleShortDescValues(
        uuid=uuid,
        folder=str(meta.folder or ""),
        name=str(meta.name or ""),
        md5=str(meta.md5 or ""),
        publish_handle=str(meta.publish_handle or "").strip() or "0",
        version64=str(meta.version64 or "").strip() or "0",
    )


def parse_bg3_modsettings(text: str) -> tuple[dict[str, str], list[ParsedShortDesc]]:
    """Return Patch 8 version attrs and the Mods/ModuleShortDesc list."""
    version = _parse_version(text)
    _assert_patch8(version)
    if _has_mod_order(text):
        raise Bg3ModsettingsError(
            "UnexpectedModOrder",
            "Patch 8 projector refuses files that already contain ModOrder",
        )
    _mods_span, inner_start, inner_end = _mods_children_span(text)
    del _mods_span
    nodes = [item for item in _split_short_descs(text[inner_start:inner_end]) if item is not None]
    if not nodes:
        raise Bg3ModsettingsError("EmptyMods", "Mods/children has no ModuleShortDesc")
    return version, nodes


def project_bg3_modsettings(
    source_path: str | Path,
    output_path: str | Path | None = None,
    *,
    managed_uuid_order: Sequence[str],
    active_managed_uuids: Sequence[str] | None = None,
    create_missing: bool = False,
    missing_insertion: MissingInsertion | str = MissingInsertion.POLICY_REQUIRED,
    remove_uuids: Sequence[str] | None = None,
    missing_nodes: Mapping[str, ModuleShortDescValues] | None = None,
    dry_run: bool = False,
) -> ProjectionResult:
    """Project canonical managed UUID order onto an existing Patch 8 LSX.

    Canonical ``managed_uuid_order[0]`` is the first SMM Mod after GustavX
    (lowest overlay priority). The last entry is highest overlay priority.
    """
    source = Path(source_path)
    try:
        original = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise Bg3ModsettingsError("UnreadableSource", f"cannot read {source}") from exc
    except UnicodeDecodeError as exc:
        raise Bg3ModsettingsError("MalformedLsx", "source is not UTF-8 XML") from exc

    if isinstance(missing_insertion, MissingInsertion):
        insertion = missing_insertion
    else:
        insertion = MissingInsertion(str(missing_insertion))
    active = _unique_uuid_list(
        active_managed_uuids if active_managed_uuids is not None else managed_uuid_order
    )
    active_set = set(active)
    order = _unique_uuid_list(managed_uuid_order)
    _assert_order_is_active(order, active_set)
    remove_set = set(_unique_uuid_list(remove_uuids or ()))
    overlap = remove_set & active_set
    if overlap:
        raise Bg3ModsettingsError(
            "RemoveConflictsActive",
            "remove_uuids intersects active_managed_uuids",
            uuids=",".join(sorted(overlap)),
        )

    try:
        version, nodes = parse_bg3_modsettings(original)
    except Bg3ModsettingsError:
        raise
    except Exception as exc:
        raise Bg3ModsettingsError("MalformedLsx", f"cannot parse modsettings.lsx: {exc}") from exc
    del version

    _assert_gustavx_first(nodes)

    kept: list[ParsedShortDesc] = []
    removed: list[str] = []
    for node in nodes:
        if node.uuid in remove_set:
            removed.append(node.uuid)
            continue
        kept.append(node)

    managed_indices = [
        i for i, node in enumerate(kept) if i > 0 and node.uuid in active_set
    ]
    _assert_no_managed_duplicates(kept, managed_indices)

    file_managed = {kept[i].uuid: kept[i] for i in managed_indices}
    missing = [uuid for uuid in order if uuid not in file_managed]
    created: list[str] = []
    new_raw_by_uuid = dict(file_managed)

    if missing:
        if not create_missing or insertion is MissingInsertion.POLICY_REQUIRED:
            raise Bg3ModsettingsError(
                "ProjectionPolicyRequired",
                "missing managed ModuleShortDesc requires an explicit create policy",
                missing=",".join(missing),
                insertion=insertion.value,
            )
        if insertion is not MissingInsertion.AFTER_LAST_MANAGED_SLOT:
            raise Bg3ModsettingsError(
                "ProjectionPolicyRequired",
                "unsupported missing-node insertion policy",
                insertion=insertion.value,
            )
        catalog = missing_nodes or {}
        for uuid in missing:
            values = catalog.get(uuid) or catalog.get(uuid.lower())
            if values is None:
                raise Bg3ModsettingsError(
                    "MissingNodeMetadata",
                    "cannot create ModuleShortDesc without meta.lsx values",
                    uuid=uuid,
                )
            created_node = ParsedShortDesc(
                raw=_format_short_desc(values),
                uuid=_norm_uuid(values.uuid) or uuid,
                attributes=_values_as_attrs(values),
            )
            if created_node.uuid != uuid:
                raise Bg3ModsettingsError(
                    "MissingNodeMetadata",
                    "created ModuleShortDesc UUID does not match canonical uuid",
                    uuid=uuid,
                )
            new_raw_by_uuid[uuid] = created_node
            created.append(uuid)

    projected = _apply_stable_slots(
        kept,
        order=order,
        managed_indices=managed_indices,
        raw_by_uuid=new_raw_by_uuid,
        insertion=insertion,
        created_uuids=created,
    )
    output = _splice_mods_children(original, projected)
    _validate_projection(
        output,
        managed_order=order,
        expected_unmanaged=_unmanaged_uuids(kept, active_set),
        original_unmanaged_attrs=_unmanaged_attr_map(kept, active_set),
    )
    skipped = output == original
    result = ProjectionResult(
        written=False,
        output_text=output,
        managed_uuid_order=tuple(order),
        unmanaged_uuids=_unmanaged_uuids(projected, active_set),
        created_uuids=tuple(created),
        removed_uuids=tuple(removed),
        skipped_idempotent=skipped,
    )
    if dry_run:
        return result
    dest = Path(output_path) if output_path is not None else source
    if skipped and dest.resolve() == source.resolve():
        return result
    _atomic_write_text(dest, output)
    result.written = True
    return result


def _assert_patch8(version: dict[str, str]) -> None:
    for key, expected in PATCH8_VERSION.items():
        if version.get(key) != expected:
            raise Bg3ModsettingsError(
                "UnsupportedSchema",
                "modsettings.lsx is not Patch 8 version 4.8.0.700",
                **version,
            )


def _parse_version(text: str) -> dict[str, str]:
    match = _VERSION_RE.search(text)
    if match is None:
        raise Bg3ModsettingsError("UnsupportedSchema", "missing <version> element")
    fields = dict(_VERSION_FIELD_RE.findall(match.group(1)))
    return {key: str(fields.get(key, "")) for key in PATCH8_VERSION}


def _has_mod_order(text: str) -> bool:
    return bool(re.search(r'<node\s+id="ModOrder"', text))


def _assert_gustavx_first(nodes: Sequence[ParsedShortDesc]) -> None:
    if not nodes:
        raise Bg3ModsettingsError("GustavXMissing", "Mods/children is empty")
    if nodes[0].uuid != GUSTAVX_UUID:
        if any(node.uuid == GUSTAVX_UUID for node in nodes):
            raise Bg3ModsettingsError(
                "GustavXNotFirst",
                "GustavX is present but is not Mods/children[0]",
                uuid=nodes[0].uuid,
            )
        raise Bg3ModsettingsError(
            "GustavXMissing",
            "GustavX ModuleShortDesc is missing; refusing to invent it",
        )


def _assert_no_managed_duplicates(
    nodes: Sequence[ParsedShortDesc], managed_indices: Sequence[int]
) -> None:
    counts: dict[str, int] = {}
    for index in managed_indices:
        uuid = nodes[index].uuid
        counts[uuid] = counts.get(uuid, 0) + 1
    dupes = {uuid: count for uuid, count in counts.items() if count > 1}
    if dupes:
        uuid, count = next(iter(dupes.items()))
        raise Bg3ModsettingsError(
            "DuplicateManagedUUID",
            "the same managed UUID appears more than once",
            uuid=uuid,
            count=count,
        )


def _assert_order_is_active(order: Sequence[str], active_set: set[str]) -> None:
    unknown = [uuid for uuid in order if uuid not in active_set]
    if unknown:
        raise Bg3ModsettingsError(
            "OrderUuidNotActive",
            "managed_uuid_order contains a UUID outside active_managed_uuids",
            uuid=unknown[0],
        )
    missing = [uuid for uuid in _unique_uuid_list(active_set) if uuid not in set(order)]
    if missing:
        raise Bg3ModsettingsError(
            "IncompleteManagedOrder",
            "active_managed_uuids is missing from managed_uuid_order",
            uuid=missing[0],
        )


def _apply_stable_slots(
    nodes: list[ParsedShortDesc],
    *,
    order: Sequence[str],
    managed_indices: Sequence[int],
    raw_by_uuid: Mapping[str, ParsedShortDesc],
    insertion: MissingInsertion,
    created_uuids: Sequence[str],
) -> list[ParsedShortDesc]:
    result = list(nodes)
    slot_count = len(managed_indices)
    fill = list(order[:slot_count])
    for slot, uuid in zip(managed_indices, fill):
        result[slot] = raw_by_uuid[uuid]
    extras = list(order[slot_count:])
    if not extras:
        return result
    if insertion is MissingInsertion.POLICY_REQUIRED:
        raise Bg3ModsettingsError(
            "ProjectionPolicyRequired",
            "extra managed nodes need an explicit insertion policy",
            extras=",".join(extras),
        )
    insert_at = (managed_indices[-1] + 1) if managed_indices else 1
    extra_nodes = [raw_by_uuid[uuid] for uuid in extras]
    result[insert_at:insert_at] = extra_nodes
    del created_uuids
    return result


def _unmanaged_uuids(
    nodes: Sequence[ParsedShortDesc], active_set: set[str]
) -> tuple[str, ...]:
    out: list[str] = []
    for index, node in enumerate(nodes):
        if index == 0:
            continue
        if node.uuid not in active_set:
            out.append(node.uuid)
    return tuple(out)


def _unmanaged_attr_map(
    nodes: Sequence[ParsedShortDesc], active_set: set[str]
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for index, node in enumerate(nodes):
        if index == 0:
            continue
        if node.uuid not in active_set:
            out.append(dict(node.attributes))
    return out


def _validate_projection(
    text: str,
    *,
    managed_order: Sequence[str],
    expected_unmanaged: Sequence[str],
    original_unmanaged_attrs: Sequence[Mapping[str, str]],
) -> None:
    if _has_mod_order(text):
        raise Bg3ModsettingsError(
            "SerializationInvalid",
            "serializer created a ModOrder node",
        )
    version, nodes = parse_bg3_modsettings(text)
    _assert_patch8(version)
    _assert_gustavx_first(nodes)
    active_set = set(managed_order)
    got_managed = [node.uuid for node in nodes[1:] if node.uuid in active_set]
    if got_managed != list(managed_order):
        raise Bg3ModsettingsError(
            "SerializationInvalid",
            "managed UUID sequence does not match canonical order",
        )
    got_unmanaged = _unmanaged_uuids(nodes, active_set)
    if got_unmanaged != tuple(expected_unmanaged):
        raise Bg3ModsettingsError(
            "SerializationInvalid",
            "unmanaged ModuleShortDesc sequence changed",
        )
    got_attrs = _unmanaged_attr_map(nodes, active_set)
    if list(got_attrs) != [dict(item) for item in original_unmanaged_attrs]:
        raise Bg3ModsettingsError(
            "SerializationInvalid",
            "unmanaged ModuleShortDesc attributes were rewritten",
        )


def _mods_children_span(text: str) -> tuple[tuple[int, int], int, int]:
    mods = _find_node_span(text, "Mods")
    if mods is None:
        raise Bg3ModsettingsError("MalformedLsx", "missing node id=\"Mods\"")
    start, end = mods
    inner = text[start:end]
    rel = inner.find("<children>")
    if rel < 0:
        rel = inner.find("<children ")
    if rel < 0:
        raise Bg3ModsettingsError("MalformedLsx", "Mods node has no <children>")
    open_end = inner.find(">", rel)
    if open_end < 0:
        raise Bg3ModsettingsError("MalformedLsx", "truncated Mods <children>")
    close_rel = _find_matching_close(inner, rel, open_tag="children")
    inner_start = start + open_end + 1
    inner_end = start + close_rel
    return (start, end), inner_start, inner_end


def _splice_mods_children(original: str, nodes: Sequence[ParsedShortDesc]) -> str:
    _span, inner_start, inner_end = _mods_children_span(original)
    inner = original[inner_start:inner_end]
    _parsed, seps = _split_short_descs_with_seps(inner)
    if not seps:
        seps = ["\n                        ", "\n                    "]
    while len(seps) < len(nodes) + 1:
        mid = seps[-2] if len(seps) >= 2 else "\n                        "
        seps.insert(-1, mid)
    while len(seps) > len(nodes) + 1:
        del seps[-2]
    parts: list[str] = [seps[0]]
    for index, node in enumerate(nodes):
        parts.append(node.raw)
        parts.append(seps[index + 1])
    return original[:inner_start] + "".join(parts) + original[inner_end:]


def _split_short_descs(inner: str) -> list[ParsedShortDesc]:
    nodes, _seps = _split_short_descs_with_seps(inner)
    return nodes


def _split_short_descs_with_seps(
    inner: str,
) -> tuple[list[ParsedShortDesc], list[str]]:
    nodes: list[ParsedShortDesc] = []
    seps: list[str] = []
    cursor = 0
    for start, end in _module_short_desc_spans(inner):
        seps.append(inner[cursor:start])
        raw = inner[start:end]
        nodes.append(_parse_short_desc(raw))
        cursor = end
    seps.append(inner[cursor:])
    return nodes, seps


def _module_short_desc_spans(inner: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        span = _find_node_span(inner, "ModuleShortDesc", start=pos)
        if span is None:
            break
        spans.append(span)
        pos = span[1]
    return spans


def _find_node_span(
    text: str, node_id: str, *, start: int = 0
) -> tuple[int, int] | None:
    needle = f'<node id="{node_id}"'
    idx = text.find(needle, start)
    if idx < 0:
        return None
    gt = text.find(">", idx)
    if gt < 0:
        raise Bg3ModsettingsError("MalformedLsx", f"truncated node id={node_id}")
    if text[gt - 1] == "/":
        return idx, gt + 1
    close = _find_matching_close(text, idx, open_tag="node")
    end = text.find(">", close)
    if end < 0:
        raise Bg3ModsettingsError("MalformedLsx", f"truncated close of node id={node_id}")
    return idx, end + 1


def _find_matching_close(text: str, open_pos: int, *, open_tag: str) -> int:
    """Return the index of the matching ``</open_tag>`` for the node at *open_pos*."""
    open_pat = f"<{open_tag}"
    close_pat = f"</{open_tag}>"
    depth = 0
    pos = open_pos
    length = len(text)
    while pos < length:
        nxt_open = text.find(open_pat, pos)
        nxt_close = text.find(close_pat, pos)
        if nxt_close < 0:
            raise Bg3ModsettingsError(
                "MalformedLsx",
                f"unclosed <{open_tag}>",
            )
        if nxt_open >= 0 and nxt_open < nxt_close:
            after = nxt_open + len(open_pat)
            if after < length and text[after] not in " >/\r\n\t":
                pos = after
                continue
            depth += 1
            pos = after
            continue
        depth -= 1
        if depth == 0:
            return nxt_close
        pos = nxt_close + len(close_pat)
    raise Bg3ModsettingsError("MalformedLsx", f"unclosed <{open_tag}>")


def _parse_short_desc(raw: str) -> ParsedShortDesc:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw):
        attrs[match.group(1)] = _unescape_attr(match.group(3))
    uuid = _norm_uuid(attrs.get("UUID", ""))
    if not uuid:
        raise Bg3ModsettingsError(
            "MalformedLsx",
            "ModuleShortDesc is missing UUID",
        )
    return ParsedShortDesc(raw=raw, uuid=uuid, attributes=attrs)


def _format_short_desc(values: ModuleShortDescValues) -> str:
    uuid = _norm_uuid(values.uuid)
    if not uuid:
        raise Bg3ModsettingsError("MissingNodeMetadata", "refusing to invent a UUID")
    handle = str(values.publish_handle or "").strip() or "0"
    version64 = str(values.version64 or "").strip() or "0"
    md5 = str(values.md5 or "")
    folder = str(values.folder or "")
    name = str(values.name or "")
    return (
        '<node id="ModuleShortDesc">\n'
        f'                            <attribute id="Folder" type="LSString" value="{_esc(folder)}"/>\n'
        f'                            <attribute id="MD5" type="LSString" value="{_esc(md5)}"/>\n'
        f'                            <attribute id="Name" type="LSString" value="{_esc(name)}"/>\n'
        f'                            <attribute id="PublishHandle" type="uint64" value="{_esc(handle)}"/>\n'
        f'                            <attribute id="UUID" type="guid" value="{_esc(uuid)}"/>\n'
        f'                            <attribute id="Version64" type="int64" value="{_esc(version64)}"/>\n'
        "                        </node>"
    )


def _values_as_attrs(values: ModuleShortDescValues) -> dict[str, str]:
    return {
        "Folder": str(values.folder or ""),
        "MD5": str(values.md5 or ""),
        "Name": str(values.name or ""),
        "PublishHandle": str(values.publish_handle or "").strip() or "0",
        "UUID": _norm_uuid(values.uuid),
        "Version64": str(values.version64 or "").strip() or "0",
    }


def _esc(value: str) -> str:
    return xml_escape(value, {'"': "&quot;", "'": "&apos;"})


def _unescape_attr(value: str) -> str:
    return (
        value.replace("&apos;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


def _unique_uuid_list(raw: Sequence[str] | set[str] | frozenset[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or ():
        uuid = _norm_uuid(item)
        if not uuid or uuid in seen:
            continue
        if uuid == GUSTAVX_UUID:
            raise Bg3ModsettingsError(
                "GustavXNotManaged",
                "GustavX must not enter SMM canonical order",
            )
        seen.add(uuid)
        out.append(uuid)
    return out


def _norm_uuid(raw: object) -> str:
    return str(raw or "").strip().lower()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=".bg3_modsettings_",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
