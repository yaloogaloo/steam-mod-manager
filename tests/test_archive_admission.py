"""Real decoder fixtures; RAR store headers follow https://www.rarlab.com/technote.htm."""
import bz2
import gzip
import io
import lzma
from pathlib import Path
import struct
import subprocess
import tarfile
import zipfile
import zlib
import pytest
from services.archive_admission import extract_archive, inspect_archive
from services.importers.archive import find_7z_executable


def rar_store(path, name='data/unit.pack', data=b'actual RAR payload', version=4):
    name = name.encode('utf8')
    if version == 4:
        def block(kind, flags, body):
            header = struct.pack('<BHH', kind, flags, len(body)+7)+body
            return struct.pack('<H', zlib.crc32(header)&0xffff)+header
        file = struct.pack('<IIBIIBBHI',len(data),len(data),2,zlib.crc32(data),0,20,0x30,len(name),0x20)+name
        path.write_bytes(b'Rar!\x1a\x07\x00'+block(0x73,0,b'\0'*6)+block(0x74,0x8000,file)+data+block(0x7b,0,b''))
    else:
        def vint(n):
            result=bytearray()
            while n>=128:result.append((n&127)|128);n>>=7
            return bytes(result)+bytes([n])
        def block(body):
            value=vint(len(body))+body
            return struct.pack('<I',zlib.crc32(value))+value
        file=b'\x02\x02'+vint(len(data))+b'\x04'+vint(len(data))+b'\x20'+struct.pack('<I',zlib.crc32(data))+b'\x00\x00'+vint(len(name))+name
        path.write_bytes(b'Rar!\x1a\x07\x01\x00'+block(b'\x01\x00\x00')+block(file)+data+block(b'\x05\x00\x00'))
    return path


def archive_fixture(root, kind, name='data/unit.pack', data=b'actual payload'):
    source=root/('fixture.'+kind)
    if kind=='zip':
        with zipfile.ZipFile(source,'w') as z:z.writestr(name,data)
    elif kind in ('rar','rar5'):rar_store(source,name,data,version=5 if kind=='rar5' else 4)
    elif kind=='7z':
        import py7zr
        with py7zr.SevenZipFile(source,'w') as z:z.writestr(data,name)
    else:
        raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w') as tar:
            info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
        payload=raw.getvalue()
        if kind in ('tar.gz','tgz'):payload=gzip.compress(payload)
        elif kind in ('tar.bz2','tbz2'):payload=bz2.compress(payload)
        elif kind in ('tar.xz','txz'):payload=lzma.compress(payload)
        if kind=='tar.zst':
            from backports import zstd
            source.write_bytes(zstd.compress(payload))
        else:source.write_bytes(payload)
    return source


@pytest.mark.parametrize('kind',['zip','7z','rar','rar5','tar','tar.gz','tgz','tar.bz2','tbz2','tar.xz','txz','tar.zst'])
def test_real_format_is_extracted_and_renamed_signature_is_accepted(tmp_path,kind):
    source=archive_fixture(tmp_path,kind)
    renamed=tmp_path/'misleading.zip';source.rename(renamed)
    destination=tmp_path/'output'
    extract_archive(renamed,destination)
    assert (destination/'data/unit.pack').read_bytes()==b'actual payload'


@pytest.mark.parametrize('kind',['gz','bz2','xz','zst'])
def test_single_file_stream_is_not_treated_as_multi_file_archive(tmp_path,kind):
    data=b'ordinary compressed file';source=tmp_path/('payload.txt.'+kind)
    if kind=='zst':
        from backports import zstd
        source.write_bytes(zstd.compress(data))
    else:source.write_bytes({'gz':gzip.compress,'bz2':bz2.compress,'xz':lzma.compress}[kind](data))
    out=extract_archive(source,tmp_path/'out')
    files=[p for p in out.rglob('*') if p.is_file()]
    assert len(files)==1 and files[0].read_bytes()==data


@pytest.mark.parametrize('name',['../outside.pack','C:/outside.pack','data/../../bad','CON.txt','.info/manifest.json'])
def test_unsafe_paths_do_not_escape(tmp_path,name):
    source=archive_fixture(tmp_path,'zip',name)
    with pytest.raises(ValueError):extract_archive(source,tmp_path/'out')
    assert not (tmp_path/'out').exists() and not (tmp_path/'outside.pack').exists()


def test_tar_link_and_duplicate_and_encryption_are_rejected(tmp_path):
    source=tmp_path/'link.tar'
    with tarfile.open(source,'w') as tar:
        info=tarfile.TarInfo('link');info.type=tarfile.SYMTYPE;info.linkname='../outside';tar.addfile(info)
    with pytest.raises(ValueError,match='链接'):extract_archive(source,tmp_path/'out')
    source=tmp_path/'duplicate.zip'
    with zipfile.ZipFile(source,'w') as z:z.writestr('a.pack',b'a');z.writestr('A.pack',b'b')
    with pytest.raises(ValueError,match='重名'):extract_archive(source,tmp_path/'out')
    payload=tmp_path/'private.pack';payload.write_bytes(b'private')
    encrypted=tmp_path/'encrypted.7z'
    subprocess.run([find_7z_executable(),'a','-psecret','-mhe=on',str(encrypted),str(payload)],check=True,capture_output=True)
    with pytest.raises(ValueError,match='加密'):extract_archive(encrypted,tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_large_legitimate_payload_over_old_256mib_limit_streams(tmp_path):
    source=tmp_path/'large.zip';block=b'\0'*1024**2
    with zipfile.ZipFile(source,'w',compression=zipfile.ZIP_DEFLATED) as z:
        with z.open('large.pack','w',force_zip64=True) as out:
            for _ in range(272):out.write(block)
    destination=extract_archive(source,tmp_path/'out')
    assert (destination/'large.pack').stat().st_size==272*1024**2


def test_corrupt_unsupported_limit_and_cancel_clean_stage(tmp_path):
    source=tmp_path/'bad.rar';source.write_bytes(b'not an archive')
    with pytest.raises(ValueError):extract_archive(source,tmp_path/'out')
    source=archive_fixture(tmp_path,'zip',data=b'123456')
    with pytest.raises(ValueError,match='限额'):extract_archive(source,tmp_path/'out',max_bytes=5)
    def cancel():raise RuntimeError('cancel requested')
    with pytest.raises(RuntimeError,match='cancel'):extract_archive(source,tmp_path/'out',cancel_check=cancel)
    assert not (tmp_path/'out').exists()
