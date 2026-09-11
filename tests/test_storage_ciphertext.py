import pytest
from app.core.storage import encrypt_data, decrypt_data

def test_truncated_ciphertext_fails_and_legacy_formats_survive(monkeypatch):
    monkeypatch.setenv("FILE_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
    source=b"%PDF-a real encryption boundary payload"
    encrypted=encrypt_data(source)
    assert encrypted != source
    assert decrypt_data(encrypted)==source
    for size in [1,8,12,20,27]:
        with pytest.raises(RuntimeError,match="DECRYPTION FAILED"):
            decrypt_data(encrypted[:size])
    corrupt=encrypted[:-1]+bytes([encrypted[-1]^1])
    with pytest.raises(RuntimeError,match="DECRYPTION FAILED"):
        decrypt_data(corrupt)
    for data in [b"%PDF-legacy",b"{}",b"[]",b"PK\x03\x04legacy",b"PK\x05\x06empty"]:
        assert decrypt_data(data)==data
