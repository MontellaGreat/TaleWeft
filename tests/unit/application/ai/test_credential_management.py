import pytest

from application.ai.credential_management_service import (
    CredentialManagementService,
    CredentialReferenceInUse,
)


class FakeVault:
    def __init__(self):
        self.deleted = []
        self.items = [
            {"credential_ref": "ppcred:v1:" + "a" * 32, "ciphertext_sha256": "cipher-a", "size_bytes": 10,
             "created_at": "today", "updated_at": "today"},
            {"credential_ref": "ppcred:v1:" + "b" * 32, "ciphertext_sha256": "cipher-b", "size_bytes": 10,
             "created_at": "today", "updated_at": "today"},
        ]

    def inventory(self):
        return list(self.items)

    def delete(self, reference):
        self.deleted.append(reference)
        return True


def test_active_reference_cannot_be_deleted_and_export_is_non_secret(monkeypatch):
    class FakeDB:
        db_path = 'credential-management-test.sqlite'
        def fetch_all(self, query):
            if 'llm_profiles' in query:
                return [{"id": "profile", "api_key": active, "extra_headers": "{}", "extra_query": "{}", "extra_body": "{}"}]
            return []
    db = FakeDB()
    active = "ppcred:v1:" + "a" * 32
    orphan = "ppcred:v1:" + "b" * 32
    vault = FakeVault()
    monkeypatch.setattr("application.ai.credential_management_service.credentials_for_database", lambda _db: vault)
    service = CredentialManagementService(db)
    rows = service.list_references()
    assert {row["status"] for row in rows} == {"active", "orphaned"}
    assert rows[0]["owners"][0]["record_id_sha256"] != "profile"
    with pytest.raises(CredentialReferenceInUse) as error:
        service.delete_reference(active)
    assert error.value.owners[0]["field"] == "api_key"
    assert vault.deleted == []
    result = service.delete_reference(orphan)
    assert result == {"credential_ref": orphan, "deleted": True}
    assert vault.deleted == [orphan]
    exported = service.export_metadata()
    assert exported["format"] == "plotpilot-credential-refs-v1"
    assert "cipher-a" in str(exported)
    assert "private-api-key" not in str(exported)
