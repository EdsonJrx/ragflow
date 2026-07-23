import base64
import importlib.util
import sqlite3
import sys
from pathlib import Path

from starlette.testclient import TestClient


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("token_registry_app", APP_PATH)
registry = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = registry
assert spec.loader is not None
spec.loader.exec_module(registry)


def configure(tmp_path, monkeypatch):
    key_path = tmp_path / "master-key"
    key_path.write_text(base64.urlsafe_b64encode(b"k" * 32).decode("ascii"), encoding="utf-8")
    monkeypatch.setattr(registry, "MASTER_KEY_PATH", key_path)
    monkeypatch.setattr(registry, "DATABASE_PATH", tmp_path / "registry.db")
    monkeypatch.setattr(registry, "ADMIN_TEAM_DOMAIN", "company.cloudflareaccess.com")
    monkeypatch.setattr(registry, "ADMIN_AUDIENCE", "admin-audience")
    monkeypatch.setattr(registry, "ADMIN_EMAILS", {"admin@example.com"})
    monkeypatch.setattr(registry, "PUBLIC_ORIGIN", "https://tokens.example.com")

    def verify(token, team_domain, audience):
        if token == "admin-jwt":
            return {"email": "admin@example.com"}
        if token == "user-jwt" and audience == "ragflow-audience":
            return {"common_name": "user-client.access"}
        raise PermissionError

    monkeypatch.setattr(registry, "verify_access_jwt", verify)


def test_create_application_register_user_and_resolve(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    with TestClient(registry.app) as client:
        admin_headers = {registry.JWT_HEADER: "admin-jwt"}
        session = client.get("/api/admin/session", headers=admin_headers).json()
        mutation_headers = {
            **admin_headers,
            "Origin": "https://tokens.example.com",
            "X-CSRF-Token": session["csrf_token"],
        }
        created = client.post(
            "/api/admin/applications",
            headers=mutation_headers,
            json={
                "slug": "ragflow",
                "name": "RAGFlow",
                "cf_team_domain": "company.cloudflareaccess.com",
                "cf_audience": "ragflow-audience",
            },
        )
        assert created.status_code == 201
        resolver_key = created.json()["resolver_key"]

        saved = client.post(
            "/api/admin/applications/ragflow/principals",
            headers=mutation_headers,
            json={
                "principal_type": "service_token",
                "principal_value": "user-client.access",
                "identity": "user@example.com",
                "display_name": "User",
                "credential": "ragflow-secret",
            },
        )
        assert saved.status_code == 201

        resolved = client.post(
            "/v1/resolve/ragflow",
            headers={"Authorization": f"Bearer {resolver_key}"},
            json={"access_jwt": "user-jwt"},
        )

    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["identity"] == "user@example.com"
    assert payload["display_name"] == "User"
    envelope = payload["credential_envelope"]
    assert "ragflow-secret" not in envelope
    key = registry.hashlib.sha256(resolver_key.encode("utf-8")).digest()
    raw = registry.base64.urlsafe_b64decode(envelope)
    assert registry.AESGCM(key).decrypt(raw[:12], raw[12:], b"ragflow").decode() == "ragflow-secret"
    assert resolved.headers["cache-control"] == "no-store"
    with sqlite3.connect(tmp_path / "registry.db") as db:
        encrypted = db.execute("SELECT encrypted_credential FROM principals").fetchone()[0]
    assert "ragflow-secret" not in encrypted


def test_resolution_rejects_wrong_application_key(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    registry.initialize_database()
    with TestClient(registry.app) as client:
        response = client.post(
            "/v1/resolve/ragflow",
            headers={"Authorization": "Bearer wrong"},
            json={"access_jwt": "user-jwt"},
        )
    assert response.status_code == 403
    assert "credential" not in response.text.lower()


def test_admin_mutation_requires_csrf_and_origin(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    with TestClient(registry.app) as client:
        response = client.post(
            "/api/admin/applications",
            headers={registry.JWT_HEADER: "admin-jwt"},
            json={
                "slug": "ragflow",
                "name": "RAGFlow",
                "cf_team_domain": "company.cloudflareaccess.com",
                "cf_audience": "audience",
            },
        )
    assert response.status_code == 403
