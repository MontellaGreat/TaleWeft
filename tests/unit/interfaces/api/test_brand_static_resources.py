# Modified for TaleWeft on 2026-10-22: independent branding; upstream attribution retained.
"""The packaged backend must serve brand/legal files rather than SPA HTML."""
from fastapi.testclient import TestClient

from interfaces.api.settings import BackendSettings
from interfaces.main import create_app


def test_brand_and_legal_resources_are_public_and_not_spa_fallback(tmp_path):
    frontend = tmp_path / "dist"
    legal = frontend / "legal"
    legal.mkdir(parents=True)
    (frontend / "index.html").write_text("<html>SPA</html>", encoding="utf-8")
    mark = '<svg xmlns="http://www.w3.org/2000/svg"><title>TaleWeft</title></svg>'
    (frontend / "taleweft-mark.svg").write_text(mark, encoding="utf-8")
    license_bytes = b"Commons Clause\r\nApache License"
    (legal / "LICENSE.txt").write_bytes(license_bytes)
    (tmp_path / "private.txt").write_text("private sentinel", encoding="utf-8")
    app = create_app(BackendSettings(frontend_dir=frontend))
    client = TestClient(app, base_url="http://127.0.0.1")
    try:
        response = client.get("/taleweft-mark.svg")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")
        assert response.text == mark
        response = client.get("/legal/LICENSE.txt")
        assert response.status_code == 200
        assert response.content == license_bytes
        assert response.headers["content-type"].startswith("text/plain")
        assert client.get("/legal/missing.txt").status_code == 404
        response = client.get("/legal/%2e%2e/private.txt")
        assert response.status_code == 404
        assert "private sentinel" not in response.text
    finally:
        client.close()
        app.state.security_audit_log.close()
