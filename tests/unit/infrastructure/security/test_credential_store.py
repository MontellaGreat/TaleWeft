import os
from pathlib import Path

import pytest

from infrastructure.security.credential_store import CredentialStore, CredentialUnavailable, PREFIX


@pytest.mark.skipif(os.name != 'nt', reason='real Windows DPAPI')
def test_real_dpapi_roundtrip_does_not_write_plaintext(tmp_path):
    store = CredentialStore(tmp_path / 'vault')
    secret = 'private-credential-never-in-database-0123456789'
    reference = store.store(secret)
    assert reference.startswith(PREFIX)
    assert store.resolve(reference) == secret
    assert all(secret.encode() not in path.read_bytes() for path in store.directory.iterdir())
    assert CredentialStore(store.directory).resolve(reference) == secret


def test_missing_corrupt_and_path_traversal_references_fail_closed(tmp_path):
    store = CredentialStore(tmp_path)
    for reference in (PREFIX + '../outside', PREFIX + 'a' * 32):
        with pytest.raises(CredentialUnavailable):
            store.resolve(reference)


def test_failed_os_protection_never_creates_plaintext_file(tmp_path):
    class FailedProtection:
        def protect(self, value): raise OSError('sensitive-provider-error')
    store = CredentialStore(tmp_path / 'vault', FailedProtection())
    with pytest.raises(CredentialUnavailable) as error:
        store.store('private-key')
    assert 'private-key' not in str(error.value) and 'sensitive-provider-error' not in str(error.value)
    assert not store.directory.exists()
