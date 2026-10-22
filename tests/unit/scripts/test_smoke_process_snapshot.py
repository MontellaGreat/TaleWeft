from scripts.verify_backend_smoke import normalize_process_rows


def test_single_powershell_process_result_is_normalized(monkeypatch):
    assert normalize_process_rows({'ProcessId': 7, 'ParentProcessId': 1}) == [
        {'ProcessId': 7, 'ParentProcessId': 1}
    ]
