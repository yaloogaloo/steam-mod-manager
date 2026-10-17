"""Synthetic LSPK v18 writers for tests. Never used on live game paks."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from services.bg3_pak import (
    COMPRESSION_LZ4,
    COMPRESSION_NONE,
    COMPRESSION_ZLIB,
    COMPRESSION_ZSTD,
    FILE_ENTRY_SIZE,
    HEADER_SIZE,
    NAME_FIELD_SIZE,
    lz4_compress_literals,
    zstd_available,
)

META_LSX_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<save>
    <version major="4" minor="0" revision="0" build="0"/>
    <region id="Config">
        <node id="root">
            <children>
                <node id="Dependencies">
                    <children>
                        <node id="ModuleShortDesc">
                            <attribute id="Folder" type="LSString" value="GustavDev"/>
                            <attribute id="MD5" type="LSString" value=""/>
                            <attribute id="Name" type="LSString" value="GustavDev"/>
                            <attribute id="UUID" type="FixedString" value="{dep_uuid}"/>
                            <attribute id="Version64" type="int64" value="1"/>
                        </node>
                    </children>
                </node>
                <node id="ModuleInfo">
                    <attribute id="Author" type="LSString" value="Test"/>
                    <attribute id="Folder" type="LSString" value="{folder}"/>
                    <attribute id="MD5" type="LSString" value="{md5}"/>
                    <attribute id="Name" type="LSString" value="{name}"/>
                    <attribute id="PublishHandle" type="uint64" value="{publish}"/>
                    <attribute id="Type" type="FixedString" value="Mod"/>
                    <attribute id="UUID" type="FixedString" value="{uuid}"/>
                    <attribute id="Version64" type="int64" value="{version64}"/>
                    <children>
                        <node id="Scripts"/>
                    </children>
                </node>
            </children>
        </node>
    </region>
</save>
"""

GUSTAVDEV_UUID = "28ac9ce2-2aba-8cda-b3b5-6e922f71b6b8"


def meta_lsx_bytes(
    *,
    uuid: str,
    name: str = "TestMod",
    folder: str = "TestMod",
    md5: str = "d41d8cd98f00b204e9800998ecf8427e",
    publish: str = "0",
    version64: str = "36028797018963968",
    dep_uuid: str = GUSTAVDEV_UUID,
) -> bytes:
    return META_LSX_TEMPLATE.format(
        uuid=uuid,
        name=name,
        folder=folder,
        md5=md5,
        publish=publish,
        version64=version64,
        dep_uuid=dep_uuid,
    ).encode("utf-8")


def write_lspk_v18(
    path: Path,
    files: dict[str, bytes],
    *,
    compression: int = COMPRESSION_NONE,
    version: int = 18,
    corrupt_signature: bool = False,
    solid: bool = False,
    archive_part: int = 0,
) -> Path:
    """Write a tiny LSPK v18 pak. Test-only."""
    entries: list[tuple[str, int, int, int, bytes]] = []
    cursor = HEADER_SIZE
    for name, raw in files.items():
        payload, flags, uncompressed = _compress_payload(raw, compression)
        entries.append((name.replace("\\", "/"), cursor, flags, uncompressed, payload))
        cursor += len(payload)

    table = bytearray()
    for name, offset, flags, uncompressed, payload in entries:
        raw_name = name.encode("utf-8")[: NAME_FIELD_SIZE - 1]
        table.extend(raw_name.ljust(NAME_FIELD_SIZE, b"\x00"))
        off1 = offset & 0xFFFFFFFF
        off2 = (offset >> 32) & 0xFFFF
        table.extend(
            struct.pack(
                "<IHBBII",
                off1,
                off2,
                archive_part,
                flags,
                len(payload),
                uncompressed,
            )
        )
    if len(table) != FILE_ENTRY_SIZE * len(entries):
        raise RuntimeError("file entry packing mismatch")
    compressed_list = lz4_compress_literals(bytes(table))
    listing = struct.pack("<II", len(entries), len(compressed_list)) + compressed_list
    file_list_offset = cursor
    file_list_size = len(listing)

    flags_byte = 0x04 if solid else 0
    signature = b"XXXX" if corrupt_signature else b"LSPK"
    header = bytearray(HEADER_SIZE)
    header[0:4] = signature
    struct.pack_into("<I", header, 4, version)
    struct.pack_into("<Q", header, 8, file_list_offset)
    struct.pack_into("<I", header, 16, file_list_size)
    header[20] = flags_byte
    header[21] = 0
    struct.pack_into("<H", header, 38, 1)

    blob = bytearray()
    blob.extend(header)
    for _name, _offset, _flags, _uncompressed, payload in entries:
        blob.extend(payload)
    blob.extend(listing)
    path.write_bytes(bytes(blob))
    return path


def _compress_payload(raw: bytes, method: int) -> tuple[bytes, int, int]:
    level_flag = 0x20
    if method == COMPRESSION_NONE:
        return raw, COMPRESSION_NONE, 0
    if method == COMPRESSION_ZLIB:
        return zlib.compress(raw), COMPRESSION_ZLIB | level_flag, len(raw)
    if method == COMPRESSION_LZ4:
        return lz4_compress_literals(raw), COMPRESSION_LZ4 | level_flag, len(raw)
    if method == COMPRESSION_ZSTD:
        if not zstd_available():
            raise RuntimeError("zstd is not available")
        try:
            from compression.zstd import compress as zstd_compress
        except ImportError:
            import zstandard

            zstd_compress = zstandard.ZstdCompressor().compress
        return zstd_compress(raw), COMPRESSION_ZSTD | level_flag, len(raw)
    raise RuntimeError(f"unsupported test compression {method}")
