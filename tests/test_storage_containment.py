from pathlib import Path
import pytest
from app.core import storage

@pytest.mark.parametrize('key', ['../outside.pdf', '/etc/passwd', 'a/../../outside', 'a/../b', 'a\\..\\b', '', 'a\x00b'])
def test_storage_rejects_unsafe_keys(tmp_path, monkeypatch, key):
    monkeypatch.setenv('LOCAL_STORAGE_DIR', str(tmp_path / 'objects'))
    with pytest.raises(ValueError):
        storage._get_local_file_path(key, for_write=True)

def test_storage_does_not_follow_symlink_outside_root(tmp_path, monkeypatch):
    root = tmp_path / 'objects'
    root.mkdir()
    (root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv('LOCAL_STORAGE_DIR', str(root))
    with pytest.raises(ValueError):
        storage._get_local_file_path('escape/outside.pdf', for_write=True)

def test_missing_key_stays_in_canonical_root(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCAL_STORAGE_DIR', str(tmp_path / 'objects'))
    # A basename elsewhere must never become a substitute for the requested key.
    marker = Path('/tmp') / ('audit-storage-' + tmp_path.name + '.pdf')
    marker.write_bytes(b'%PDF-unrelated')
    try:
        result = Path(storage._get_local_file_path('sources/' + marker.name))
        assert result == tmp_path / 'objects' / 'sources' / marker.name
        assert not result.exists()
    finally:
        marker.unlink()


def test_canonical_local_storage_roundtrip(tmp_path, monkeypatch):
    from io import BytesIO
    monkeypatch.setenv('LOCAL_STORAGE_DIR', str(tmp_path / 'objects'))
    monkeypatch.setenv('FILE_ENCRYPTION_KEY', '0123456789abcdef0123456789abcdef')
    monkeypatch.setattr(storage, 'remote_storage_enabled', lambda: False)
    data = b'%PDF-1.7 canonical source retained'
    key = 'sources/original.pdf'
    storage.upload_fileobj(BytesIO(data), key)
    assert (tmp_path / 'objects' / key).read_bytes() != data
    output = tmp_path / 'download.pdf'
    storage.download_to_path(key, str(output))
    assert output.read_bytes() == data
    storage.delete_object(key)
    assert not (tmp_path / 'objects' / key).exists()
