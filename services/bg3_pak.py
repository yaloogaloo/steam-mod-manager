"""Read-only Baldur's Gate 3 LSPK v18 reader and meta.lsx UUID resolver.

Does not write ``.pak`` files. UUID is taken only from ``meta.lsx`` /
``ModuleInfo`` / ``attribute id="UUID"``. Pak filename, ``workspace_id``,
``internal_id``, and ``mod_id`` are never used as the resolved identity.
"""

from __future__ import annotations

import os
import struct
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from xml.etree.ElementTree import Element

from services.deploy_rules.manifest import DeployManifest

LSPK_SIGNATURE = b"LSPK"
LSPK_VERSION = 18
FILE_ENTRY_SIZE = 272
NAME_FIELD_SIZE = 256
HEADER_SIZE = 40
PACKAGE_FLAG_SOLID = 0x04

COMPRESSION_NONE = 0
COMPRESSION_ZLIB = 1
COMPRESSION_LZ4 = 2
COMPRESSION_ZSTD = 3

_MAX_FILE_LIST_BYTES = 32 * 1024 * 1024
_MAX_META_BYTES = 8 * 1024 * 1024

_ZSTD_DECOMPRESS = None
try:
    from compression.zstd import decompress as _ZSTD_DECOMPRESS  # type: ignore[attr-defined]
except ImportError:
    try:
        import zstandard as _zstandard

        def _ZSTD_DECOMPRESS(data: bytes) -> bytes:  # type: ignore[misc]
            return _zstandard.ZstdDecompressor().decompress(data)
    except ImportError:
        try:
            import zstd as _zstd_mod

            def _ZSTD_DECOMPRESS(data: bytes) -> bytes:  # type: ignore[misc]
                return _zstd_mod.decompress(data)
        except ImportError:
            _ZSTD_DECOMPRESS = None


class Bg3PakError(Exception):
    """Structured pak / meta.lsx failure. Never a silent wrong UUID."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = str(code)
        self.fields = dict(fields)

    def __str__(self) -> str:
        extra = " ".join(f"{k}={v}" for k, v in self.fields.items())
        if extra:
            return f"{self.code}: {self.args[0]} ({extra})"
        return f"{self.code}: {self.args[0]}"


@dataclass(frozen=True)
class LspkFileEntry:
    name: str
    offset: int
    archive_part: int
    compression: int
    size_on_disk: int
    uncompressed_size: int


@dataclass(frozen=True)
class BG3ModMetadata:
    uuid: str
    name: str = ""
    folder: str = ""
    version64: str = ""
    md5: str = ""
    publish_handle: str = ""
    dependencies: tuple[str, ...] = ()
    pak_path: str = ""
    meta_lsx_name: str = ""


@dataclass(frozen=True)
class _PakIdentity:
    path: str
    size: int
    mtime_ns: int


_METADATA_CACHE: dict[_PakIdentity, BG3ModMetadata] = {}


def clear_bg3_metadata_cache() -> None:
    _METADATA_CACHE.clear()


def zstd_available() -> bool:
    return _ZSTD_DECOMPRESS is not None


def deployed_pak_paths_from_manifest(manifest: DeployManifest) -> list[Path]:
    """Explicit pak targets from a Deploy manifest. No library walk.

    ``internal_id → UUID`` is: caller already selected this Mod's manifest,
    then :func:`resolve_bg3_mod_metadata` on each returned path.
    """
    out: list[Path] = []
    seen: set[str] = set()
    for entry in list(getattr(manifest, "files", None) or ()):
        kind = str(getattr(entry, "type", "") or "").strip().lower()
        target = str(getattr(entry, "target", "") or "").strip()
        if kind and kind != "pak":
            continue
        if not target.lower().endswith(".pak"):
            continue
        key = os.path.normcase(os.path.normpath(target))
        if key in seen:
            continue
        seen.add(key)
        out.append(Path(target))
    return out


def resolve_bg3_mod_metadata(
    pak_path: str | Path,
    *,
    use_cache: bool = True,
) -> BG3ModMetadata:
    """Read ``ModuleInfo.UUID`` from ``meta.lsx`` inside *pak_path*."""
    path = Path(pak_path)
    identity = _pak_identity(path)
    if use_cache and identity in _METADATA_CACHE:
        return _METADATA_CACHE[identity]
    raw, entry_name = extract_meta_lsx(path)
    meta = parse_meta_lsx(raw, pak_path=str(path), meta_lsx_name=entry_name)
    if use_cache:
        _METADATA_CACHE[identity] = meta
    return meta


def extract_meta_lsx(pak_path: str | Path) -> tuple[bytes, str]:
    """Return ``(meta.lsx bytes, archive path)`` from an LSPK v18 pak."""
    entries = list_lspk_v18_entries(pak_path)
    matches = [e for e in entries if _is_meta_lsx_name(e.name)]
    if not matches:
        raise Bg3PakError("MissingMetaLsx", "pak does not contain meta.lsx")
    chosen = _select_meta_lsx_entry(matches)
    data = extract_lspk_entry(pak_path, chosen)
    return data, chosen.name


def list_lspk_v18_entries(pak_path: str | Path) -> list[LspkFileEntry]:
    path = Path(pak_path)
    header, file_size = _read_header(path)
    version, file_list_offset, file_list_size, flags, num_parts = header
    if version != LSPK_VERSION:
        raise Bg3PakError(
            "UnsupportedLspkVersion",
            "only LSPK v18 is supported",
            version=version,
        )
    if flags & PACKAGE_FLAG_SOLID:
        raise Bg3PakError(
            "UnsupportedSolidArchive",
            "solid LZ4-frame archives are not read",
        )
    del num_parts
    if file_list_offset < HEADER_SIZE or file_list_offset >= file_size:
        raise Bg3PakError("MalformedHeader", "file list offset is outside the pak")
    end = file_list_offset + file_list_size
    if end > file_size:
        raise Bg3PakError("TruncatedFileList", "file list extends past end of pak")
    with path.open("rb") as fh:
        fh.seek(file_list_offset)
        blob = fh.read(file_list_size)
    if len(blob) < 8:
        raise Bg3PakError("TruncatedFileList", "file list is shorter than 8 bytes")
    num_files, compressed_size = struct.unpack_from("<II", blob, 0)
    if 8 + compressed_size > len(blob):
        raise Bg3PakError("TruncatedFileList", "compressed file list is truncated")
    compressed = blob[8 : 8 + compressed_size]
    uncompressed_size = num_files * FILE_ENTRY_SIZE
    if uncompressed_size <= 0 or uncompressed_size > _MAX_FILE_LIST_BYTES:
        raise Bg3PakError(
            "MalformedHeader",
            "file list size is not plausible",
            num_files=num_files,
        )
    try:
        table = lz4_decompress_block(compressed, uncompressed_size)
    except Bg3PakError:
        raise
    except Exception as exc:
        raise Bg3PakError(
            "FileListDecompressFailed",
            "LZ4 file list decompression failed",
        ) from exc
    if len(table) != uncompressed_size:
        raise Bg3PakError(
            "FileListDecompressFailed",
            "LZ4 file list size mismatch",
            expected=uncompressed_size,
            actual=len(table),
        )
    return [_parse_file_entry(table, i * FILE_ENTRY_SIZE) for i in range(num_files)]


def extract_lspk_entry(pak_path: str | Path, entry: LspkFileEntry) -> bytes:
    if int(entry.archive_part or 0) != 0:
        raise Bg3PakError(
            "MultipartEntryUnsupported",
            "refusing to read an entry stored outside part 0",
            archive_part=entry.archive_part,
            name=entry.name,
        )
    path = Path(pak_path)
    size = path.stat().st_size
    if entry.offset < 0 or entry.size_on_disk < 0:
        raise Bg3PakError("TruncatedEntry", "entry offset/size is invalid", name=entry.name)
    end = entry.offset + entry.size_on_disk
    if end > size:
        raise Bg3PakError(
            "TruncatedEntry",
            "entry extends past end of pak",
            name=entry.name,
        )
    with path.open("rb") as fh:
        fh.seek(entry.offset)
        payload = fh.read(entry.size_on_disk)
    if len(payload) != entry.size_on_disk:
        raise Bg3PakError("TruncatedEntry", "could not read full entry", name=entry.name)
    return _decompress_entry(payload, entry)


def parse_meta_lsx(
    raw: bytes,
    *,
    pak_path: str = "",
    meta_lsx_name: str = "",
) -> BG3ModMetadata:
    text = _decode_lsx_bytes(raw)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise Bg3PakError("InvalidMetaXml", f"meta.lsx is not valid XML: {exc}") from exc
    module_info = _find_node(root, "ModuleInfo")
    if module_info is None:
        raise Bg3PakError("MissingModuleInfo", "meta.lsx has no ModuleInfo node")
    attrs = _node_attributes(module_info)
    uuid = _normalize_uuid(attrs.get("UUID", ""))
    if not uuid:
        raise Bg3PakError("MissingUUID", "ModuleInfo has no UUID attribute")
    deps = tuple(_dependency_uuids(root, module_info))
    return BG3ModMetadata(
        uuid=uuid,
        name=str(attrs.get("Name") or "").strip(),
        folder=str(attrs.get("Folder") or "").strip(),
        version64=str(attrs.get("Version64") or "").strip(),
        md5=str(attrs.get("MD5") or "").strip(),
        publish_handle=str(attrs.get("PublishHandle") or "").strip(),
        dependencies=deps,
        pak_path=pak_path,
        meta_lsx_name=meta_lsx_name,
    )


def lz4_decompress_block(src: bytes, uncompressed_size: int) -> bytes:
    """LZ4 block (not frame) decompressor. Used for v18 file lists and entries."""
    if uncompressed_size < 0 or uncompressed_size > _MAX_FILE_LIST_BYTES * 4:
        raise Bg3PakError("DecompressionFailed", "uncompressed size is not plausible")
    out = bytearray()
    ip = 0
    length = len(src)
    try:
        while ip < length:
            token = src[ip]
            ip += 1
            lit_len = token >> 4
            if lit_len == 15:
                while True:
                    if ip >= length:
                        raise Bg3PakError("DecompressionFailed", "truncated LZ4 literals")
                    extra = src[ip]
                    ip += 1
                    lit_len += extra
                    if extra != 255:
                        break
            if ip + lit_len > length:
                raise Bg3PakError("DecompressionFailed", "truncated LZ4 literal run")
            out.extend(src[ip : ip + lit_len])
            ip += lit_len
            if ip >= length:
                break
            if ip + 2 > length:
                raise Bg3PakError("DecompressionFailed", "truncated LZ4 match offset")
            offset = src[ip] | (src[ip + 1] << 8)
            ip += 2
            if offset == 0:
                raise Bg3PakError("DecompressionFailed", "LZ4 match offset is 0")
            match_len = (token & 0x0F) + 4
            if (token & 0x0F) == 15:
                while True:
                    if ip >= length:
                        raise Bg3PakError("DecompressionFailed", "truncated LZ4 match length")
                    extra = src[ip]
                    ip += 1
                    match_len += extra
                    if extra != 255:
                        break
            copy_from = len(out) - offset
            if copy_from < 0:
                raise Bg3PakError("DecompressionFailed", "LZ4 match offset is past start")
            for _ in range(match_len):
                out.append(out[copy_from])
                copy_from += 1
            if len(out) > uncompressed_size:
                raise Bg3PakError("DecompressionFailed", "LZ4 output exceeded expected size")
    except Bg3PakError:
        raise
    except Exception as exc:
        raise Bg3PakError("DecompressionFailed", "LZ4 block decompress failed") from exc
    if len(out) != uncompressed_size:
        raise Bg3PakError(
            "DecompressionFailed",
            "LZ4 output size mismatch",
            expected=uncompressed_size,
            actual=len(out),
        )
    return bytes(out)


def lz4_compress_literals(src: bytes) -> bytes:
    """Store *src* as an LZ4 block of literals only. Test fixture helper."""
    out = bytearray()
    ip = 0
    n = len(src)
    # LZ4 requires the last 5 bytes to be literals; emit one final sequence.
    while ip < n:
        remaining = n - ip
        lit_len = remaining
        token_lit = 15 if lit_len >= 15 else lit_len
        out.append(token_lit << 4)
        extra = lit_len - 15
        if lit_len >= 15:
            while extra >= 255:
                out.append(255)
                extra -= 255
            out.append(extra)
        out.extend(src[ip : ip + lit_len])
        ip += lit_len
    return bytes(out)


def _read_header(path: Path) -> tuple[tuple[int, int, int, int, int], int]:
    try:
        st = path.stat()
    except OSError as exc:
        raise Bg3PakError("MissingPak", f"pak is not readable: {path}") from exc
    if st.st_size < HEADER_SIZE:
        raise Bg3PakError("MalformedHeader", "pak is shorter than an LSPK v18 header")
    with path.open("rb") as fh:
        header = fh.read(HEADER_SIZE)
    if len(header) < HEADER_SIZE:
        raise Bg3PakError("MalformedHeader", "could not read LSPK header")
    return _parse_header(header), st.st_size


def _parse_header(data: bytes) -> tuple[int, int, int, int, int]:
    if len(data) < HEADER_SIZE:
        raise Bg3PakError("MalformedHeader", "pak is shorter than an LSPK v18 header")
    if data[:4] != LSPK_SIGNATURE:
        raise Bg3PakError("MalformedHeader", "missing LSPK signature")
    return _parse_header_fields(data[:HEADER_SIZE])


def _parse_header_fields(header: bytes) -> tuple[int, int, int, int, int]:
    version = struct.unpack_from("<I", header, 4)[0]
    file_list_offset = struct.unpack_from("<Q", header, 8)[0]
    file_list_size = struct.unpack_from("<I", header, 16)[0]
    flags = header[20]
    num_parts = struct.unpack_from("<H", header, 38)[0]
    if num_parts == 0:
        num_parts = 1
    return version, file_list_offset, file_list_size, flags, num_parts


def _parse_file_entry(table: bytes, offset: int) -> LspkFileEntry:
    raw_name = table[offset : offset + NAME_FIELD_SIZE]
    name = raw_name.split(b"\x00", 1)[0].decode("utf-8", errors="replace").replace(
        "\\", "/"
    )
    off1, off2, part, flags, size_on_disk, uncompressed = struct.unpack_from(
        "<IHBBII", table, offset + NAME_FIELD_SIZE
    )
    return LspkFileEntry(
        name=name,
        offset=int(off1) | (int(off2) << 32),
        archive_part=int(part),
        compression=int(flags) & 0x0F,
        size_on_disk=int(size_on_disk),
        uncompressed_size=int(uncompressed),
    )


def _decompress_entry(payload: bytes, entry: LspkFileEntry) -> bytes:
    method = int(entry.compression)
    expected = int(entry.uncompressed_size or 0)
    if method == COMPRESSION_NONE:
        return payload
    if expected <= 0 or expected > _MAX_META_BYTES:
        if expected <= 0:
            expected = _MAX_META_BYTES
        else:
            raise Bg3PakError(
                "DecompressionFailed",
                "uncompressed entry size is not plausible",
                name=entry.name,
            )
    try:
        if method == COMPRESSION_ZLIB:
            out = _decompress_zlib(payload)
        elif method == COMPRESSION_LZ4:
            out = lz4_decompress_block(payload, expected)
        elif method == COMPRESSION_ZSTD:
            out = _decompress_zstd(payload)
        else:
            raise Bg3PakError(
                "UnsupportedCompression",
                "unknown LSPK compression method",
                method=method,
                name=entry.name,
            )
    except Bg3PakError:
        raise
    except Exception as exc:
        raise Bg3PakError(
            "DecompressionFailed",
            "entry decompression failed",
            method=method,
            name=entry.name,
        ) from exc
    if expected and expected != _MAX_META_BYTES and len(out) != expected:
        raise Bg3PakError(
            "DecompressionFailed",
            "decompressed size mismatch",
            expected=expected,
            actual=len(out),
            name=entry.name,
        )
    if len(out) > _MAX_META_BYTES:
        raise Bg3PakError("DecompressionFailed", "decompressed meta.lsx is too large")
    return out


def _decompress_zlib(payload: bytes) -> bytes:
    try:
        return zlib.decompress(payload)
    except zlib.error:
        return zlib.decompress(payload, -15)


def _decompress_zstd(payload: bytes) -> bytes:
    if _ZSTD_DECOMPRESS is None:
        raise Bg3PakError(
            "UnsupportedCompression",
            "zstd is not available in this Python environment",
            method=COMPRESSION_ZSTD,
        )
    return _ZSTD_DECOMPRESS(payload)


def _is_meta_lsx_name(name: str) -> bool:
    n = str(name or "").replace("\\", "/").rstrip("/").lower()
    return n == "meta.lsx" or n.endswith("/meta.lsx")


def _select_meta_lsx_entry(matches: list[LspkFileEntry]) -> LspkFileEntry:
    if len(matches) == 1:
        return matches[0]
    preferred = [
        e
        for e in matches
        if "/mods/" in e.name.replace("\\", "/").lower()
    ]
    pool = preferred or matches
    if len(pool) != 1:
        raise Bg3PakError(
            "MultipleMetaLsx",
            "pak contains more than one meta.lsx",
            count=len(matches),
        )
    return pool[0]


def _pak_identity(path: Path) -> _PakIdentity:
    try:
        st = path.stat()
    except OSError as exc:
        raise Bg3PakError("MissingPak", f"pak is not readable: {path}") from exc
    resolved = str(path.resolve()) if path.exists() else str(path)
    return _PakIdentity(
        path=os.path.normcase(resolved),
        size=int(st.st_size),
        mtime_ns=int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
    )


def _decode_lsx_bytes(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    return raw.decode("utf-8")


def _find_node(root: Element, node_id: str) -> Element | None:
    for el in root.iter("node"):
        if el.get("id") == node_id:
            return el
    return None


def _node_attributes(node: Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for child in list(node):
        if child.tag != "attribute":
            continue
        key = str(child.get("id") or "").strip()
        if not key:
            continue
        out[key] = str(child.get("value") or "")
    return out


def _dependency_uuids(root: Element, module_info: Element) -> Iterator[str]:
    seen: set[str] = set()
    for deps in root.iter("node"):
        if deps.get("id") != "Dependencies":
            continue
        # Prefer Dependencies under ModuleInfo; still accept region-level.
        for child in deps.iter("node"):
            if child.get("id") != "ModuleShortDesc":
                continue
            uuid = _normalize_uuid(_node_attributes(child).get("UUID", ""))
            if uuid and uuid not in seen:
                seen.add(uuid)
                yield uuid


def _normalize_uuid(raw: object) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    return text


def cache_stats() -> Mapping[str, int]:
    return {"entries": len(_METADATA_CACHE)}
