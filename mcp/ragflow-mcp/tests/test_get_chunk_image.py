import base64
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"
spec = importlib.util.spec_from_file_location("ragflow_mcp_app", APP_PATH)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
assert spec.loader is not None
spec.loader.exec_module(app)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        value = self.routes.get((method, path))
        if value is None and method == "GET" and path.endswith("/presigned"):
            value = FakeResponse(
                payload={
                    "code": 0,
                    "data": {
                        "url": "https://minio.example.test/bucket/object?signature=test",
                        "expires_in": 900,
                        "expires_at": "2026-07-22T21:30:00Z",
                    },
                }
            )
        if value is None:
            raise KeyError((method, path))
        if callable(value):
            return value(method, path, kwargs)
        return value


def png_bytes(width=20, height=10, color=(255, 0, 0, 255)):
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def identity():
    token = app.identity_context.set(app.IdentityContext(identity="a@example.com", display_name="User A", api_key="ragflow-a"))
    yield
    app.identity_context.reset(token)


@pytest.fixture(autouse=True)
def image_limits(monkeypatch):
    monkeypatch.setattr(app, "RAGFLOW_MAX_IMAGE_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(app, "RAGFLOW_MAX_IMAGE_PIXELS", 40_000_000)


async def call_image_tool(**kwargs):
    tool = getattr(app.ragflow_get_chunk_image, "fn", app.ragflow_get_chunk_image)
    result = await tool(**kwargs)
    metadata = json.loads(result.content[0].text)
    image = result.content[1]
    return metadata, image


async def call_pdf_tool(**kwargs):
    tool = getattr(app.ragflow_get_document_pdf, "fn", app.ragflow_get_document_pdf)
    return await tool(**kwargs)


@pytest.mark.asyncio
async def test_chunk_with_image_returns_text_and_image(identity, monkeypatch):
    client = FakeClient({
        ("GET", "/datasets"): FakeResponse(payload={"code": 0, "data": [{"id": "dataset-a", "name": "Dataset A"}], "total": 1}),
        ("GET", "/datasets/dataset-a/documents/document-a/chunks/chunk-a"): FakeResponse(payload={"code": 0, "data": {"id": "chunk-a", "kb_id": "dataset-a", "doc_id": "document-a", "img_id": "dataset-a-chunk-a", "document_name": "Manual.pdf"}}),
        ("GET", "/documents/images/dataset-a-chunk-a"): FakeResponse(content=png_bytes(), headers={"content-type": "image/png"}),
    })
    monkeypatch.setattr(app, "http_client", client)

    metadata, image = await call_image_tool(dataset_id="dataset-a", document_id="document-a", chunk_id="chunk-a", reference="FIG-001")

    assert metadata["reference"] == "FIG-001"
    assert metadata["dataset_id"] == "dataset-a"
    assert metadata["document_id"] == "document-a"
    assert metadata["chunk_id"] == "chunk-a"
    assert metadata["image_id"] == "dataset-a-chunk-a"
    assert metadata["signed_url"].startswith("https://minio.example.test/")
    assert metadata["signed_url_expires_in"] == 900
    assert image.type == "image"
    assert image.mimeType == "image/png"
    assert base64.b64decode(image.data)

    tool = getattr(app.ragflow_get_chunk_image, "fn", app.ragflow_get_chunk_image)
    result = await tool(dataset_id="dataset-a", document_id="document-a", chunk_id="chunk-a", reference="FIG-001")
    link = result.content[2]
    assert link.type == "resource_link"
    assert str(link.uri).startswith("https://minio.example.test/")


@pytest.mark.asyncio
async def test_chunk_without_image_returns_clear_error(identity, monkeypatch):
    monkeypatch.setattr(app, "http_client", FakeClient({
        ("GET", "/datasets"): FakeResponse(payload={"code": 0, "data": [{"id": "dataset-a", "name": "Dataset A"}], "total": 1}),
        ("GET", "/datasets/dataset-a/documents/document-a/chunks/chunk-a"): FakeResponse(payload={"code": 0, "data": {"id": "chunk-a", "kb_id": "dataset-a", "doc_id": "document-a"}}),
    }))
    with pytest.raises(ValueError, match="The requested chunk does not contain an image"):
        await call_image_tool(dataset_id="dataset-a", document_id="document-a", chunk_id="chunk-a")


@pytest.mark.asyncio
async def test_dataset_not_authorized_does_not_fetch_image(identity, monkeypatch):
    client = FakeClient({("GET", "/datasets"): FakeResponse(payload={"code": 0, "data": [{"id": "dataset-a"}], "total": 1})})
    monkeypatch.setattr(app, "http_client", client)
    with pytest.raises(PermissionError, match="The requested dataset is not accessible"):
        await call_image_tool(dataset_id="dataset-b", document_id="document-b", chunk_id="chunk-b")
    assert not any(path.startswith("/documents/images/") for _, path, _ in client.calls)


@pytest.mark.asyncio
async def test_document_divergence_denies_access(identity, monkeypatch):
    monkeypatch.setattr(app, "http_client", FakeClient({
        ("GET", "/datasets"): FakeResponse(payload={"code": 0, "data": [{"id": "dataset-a"}], "total": 1}),
        ("GET", "/datasets/dataset-a/documents/document-a/chunks/chunk-a"): FakeResponse(payload={"code": 0, "data": {"id": "chunk-a", "kb_id": "dataset-a", "doc_id": "other-doc", "img_id": "dataset-a-chunk-a"}}),
    }))
    with pytest.raises(PermissionError, match="requested document"):
        await call_image_tool(dataset_id="dataset-a", document_id="document-a", chunk_id="chunk-a")


@pytest.mark.asyncio
async def test_chunk_divergence_denies_access(identity, monkeypatch):
    monkeypatch.setattr(app, "http_client", FakeClient({
        ("GET", "/datasets"): FakeResponse(payload={"code": 0, "data": [{"id": "dataset-a"}], "total": 1}),
        ("GET", "/datasets/dataset-a/documents/document-a/chunks/chunk-a"): FakeResponse(payload={"code": 0, "data": {"id": "other", "kb_id": "dataset-a", "doc_id": "document-a", "img_id": "dataset-a-chunk-a"}}),
    }))
    with pytest.raises(PermissionError, match="returned chunk"):
        await call_image_tool(dataset_id="dataset-a", document_id="document-a", chunk_id="chunk-a")


@pytest.mark.asyncio
async def test_missing_image_is_controlled_error(identity, monkeypatch):
    monkeypatch.setattr(app, "http_client", FakeClient({
        ("GET", "/datasets"): FakeResponse(payload={"code": 0, "data": [{"id": "dataset-a"}], "total": 1}),
        ("GET", "/datasets/dataset-a/documents/document-a/chunks/chunk-a"): FakeResponse(payload={"code": 0, "data": {"id": "chunk-a", "kb_id": "dataset-a", "doc_id": "document-a", "img_id": "dataset-a-chunk-a"}}),
        ("GET", "/documents/images/dataset-a-chunk-a"): FakeResponse(status_code=404, payload={"message": "not found"}),
    }))
    with pytest.raises(RuntimeError, match="not found"):
        await call_image_tool(dataset_id="dataset-a", document_id="document-a", chunk_id="chunk-a")


@pytest.mark.asyncio
async def test_declared_jpeg_png_bytes_returns_png(identity, monkeypatch):
    monkeypatch.setattr(app, "http_client", FakeClient({
        ("GET", "/datasets"): FakeResponse(payload={"code": 0, "data": [{"id": "dataset-a"}], "total": 1}),
        ("GET", "/datasets/dataset-a/documents/document-a/chunks/chunk-a"): FakeResponse(payload={"code": 0, "data": {"id": "chunk-a", "kb_id": "dataset-a", "doc_id": "document-a", "img_id": "dataset-a-chunk-a"}}),
        ("GET", "/documents/images/dataset-a-chunk-a"): FakeResponse(content=png_bytes(), headers={"content-type": "image/jpeg"}),
    }))
    metadata, image = await call_image_tool(dataset_id="dataset-a", document_id="document-a", chunk_id="chunk-a")
    assert metadata["declared_source_mime_type"] == "image/jpeg"
    assert metadata["returned_mime_type"] == "image/png"
    assert image.mimeType == "image/png"


@pytest.mark.asyncio
async def test_byte_limit_rejects_image(identity, monkeypatch):
    monkeypatch.setattr(app, "RAGFLOW_MAX_IMAGE_BYTES", 10)
    monkeypatch.setattr(app, "http_client", FakeClient({
        ("GET", "/datasets"): FakeResponse(payload={"code": 0, "data": [{"id": "dataset-a"}], "total": 1}),
        ("GET", "/datasets/dataset-a/documents/document-a/chunks/chunk-a"): FakeResponse(payload={"code": 0, "data": {"id": "chunk-a", "kb_id": "dataset-a", "doc_id": "document-a", "img_id": "dataset-a-chunk-a"}}),
        ("GET", "/documents/images/dataset-a-chunk-a"): FakeResponse(content=png_bytes(), headers={"content-length": "999"}),
    }))
    with pytest.raises(ValueError, match="size limit"):
        await call_image_tool(dataset_id="dataset-a", document_id="document-a", chunk_id="chunk-a")


def test_pixel_limit_rejects_before_load(monkeypatch):
    monkeypatch.setattr(app, "RAGFLOW_MAX_IMAGE_PIXELS", 1)
    with pytest.raises(ValueError, match="pixel limit"):
        app.encode_mcp_image(png_bytes(width=2, height=2), output_format="png", max_dimension=1600)


@pytest.mark.parametrize("bad_id", ["a/b", "a\\b", "..", "has space", "abc$", ""])
def test_invalid_ids_rejected(bad_id):
    with pytest.raises(ValueError, match="contains invalid characters"):
        app.validate_ragflow_id("dataset_id", bad_id)


@pytest.mark.asyncio
async def test_signed_url_expiry_is_bounded(identity):
    with pytest.raises(ValueError, match="signed_url_expires_in"):
        await call_image_tool(
            dataset_id="dataset-a",
            document_id="document-a",
            chunk_id="chunk-a",
            signed_url_expires_in=3601,
        )


@pytest.mark.asyncio
async def test_authorized_pdf_returns_resource_link(identity, monkeypatch):
    client = FakeClient(
        {
            ("GET", "/datasets"): FakeResponse(
                payload={"code": 0, "data": [{"id": "dataset-a", "name": "Dataset A"}], "total": 1}
            ),
            ("GET", "/datasets/dataset-a/documents/document-a/presigned"): FakeResponse(
                payload={
                    "code": 0,
                    "data": {
                        "url": "https://minio.example.test/bucket/manual.pdf?signature=test",
                        "expires_in": 900,
                        "expires_at": "2026-07-23T03:00:00Z",
                        "filename": "Manual.pdf",
                        "content_type": "application/pdf",
                    },
                }
            ),
        }
    )
    monkeypatch.setattr(app, "http_client", client)

    result = await call_pdf_tool(dataset_id="dataset-a", document_id="document-a")
    metadata = json.loads(result.content[0].text)

    assert metadata["document_name"] == "Manual.pdf"
    assert metadata["content_type"] == "application/pdf"
    assert result.content[1].type == "resource_link"
    assert result.content[1].mimeType == "application/pdf"


@pytest.mark.asyncio
async def test_pdf_rejects_inaccessible_dataset(identity, monkeypatch):
    client = FakeClient(
        {("GET", "/datasets"): FakeResponse(payload={"code": 0, "data": [{"id": "dataset-a"}], "total": 1})}
    )
    monkeypatch.setattr(app, "http_client", client)

    with pytest.raises(PermissionError, match="dataset is not accessible"):
        await call_pdf_tool(dataset_id="dataset-b", document_id="document-b")

    assert not any(path.endswith("/presigned") for _, path, _ in client.calls)


def test_retrieval_enriches_pdf_tool_arguments():
    chunks = app.enrich_chunks(
        [
            {
                "id": "chunk-a",
                "kb_id": "dataset-a",
                "doc_id": "document-a",
                "docnm_kwd": "Manual.pdf",
            }
        ],
        [{"id": "dataset-a", "name": "Dataset A"}],
    )

    assert chunks[0]["document"] == {
        "available": True,
        "filename": "Manual.pdf",
        "tool": "ragflow_get_document_pdf",
        "arguments": {"dataset_id": "dataset-a", "document_id": "document-a"},
    }


def test_service_identity_status_reports_complete_configuration(tmp_path, monkeypatch):
    identity_map_path = tmp_path / "identity-map.json"
    api_keys_dir = tmp_path / "api-keys"
    api_keys_dir.mkdir()
    identity_map_path.write_text('{"user@example.com":"user-key"}', encoding="utf-8")
    (api_keys_dir / "user-key").write_text("secret-value", encoding="utf-8")
    monkeypatch.setattr(app, "RAGFLOW_SERVICE_IDENTITY", "user@example.com")
    monkeypatch.setattr(app, "RAGFLOW_IDENTITY_MAP_PATH", identity_map_path)
    monkeypatch.setattr(app, "RAGFLOW_API_KEYS_DIR", api_keys_dir)

    assert app.service_identity_status() == {
        "identity_map_present": True,
        "identity_map_valid": True,
        "service_identity_mapped": True,
        "api_key_secret_present": True,
        "api_key_secret_nonempty": True,
    }


def test_service_identity_status_does_not_expose_mapping(tmp_path, monkeypatch):
    identity_map_path = tmp_path / "identity-map.json"
    identity_map_path.write_text('{"other@example.com":"other-key"}', encoding="utf-8")
    monkeypatch.setattr(app, "RAGFLOW_SERVICE_IDENTITY", "user@example.com")
    monkeypatch.setattr(app, "RAGFLOW_IDENTITY_MAP_PATH", identity_map_path)
    monkeypatch.setattr(app, "RAGFLOW_API_KEYS_DIR", tmp_path)

    status = app.service_identity_status()

    assert status["identity_map_valid"] is True
    assert status["service_identity_mapped"] is False
    assert "user@example.com" not in json.dumps(status)
    assert "other-key" not in json.dumps(status)


def test_service_token_jwt_without_email_uses_configured_identity(monkeypatch):
    monkeypatch.setattr(app, "RAGFLOW_SERVICE_IDENTITY", "service@example.com")
    monkeypatch.setattr(app, "verify_cloudflare_jwt", lambda token: {"type": "app"})
    monkeypatch.setattr(app, "resolve_api_key", lambda identity: "service-api-key")

    context = app.identity_from_headers(app.Headers({app.JWT_HEADER: "valid-service-jwt"}))

    assert context.identity == "service@example.com"
    assert context.display_name == "service"
    assert context.api_key == "service-api-key"


def test_invalid_service_token_jwt_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(app, "RAGFLOW_SERVICE_IDENTITY", "service@example.com")

    def reject_token(token):
        raise PermissionError("invalid JWT")

    monkeypatch.setattr(app, "verify_cloudflare_jwt", reject_token)

    with pytest.raises(PermissionError, match="invalid JWT"):
        app.identity_from_headers(app.Headers({app.JWT_HEADER: "invalid-service-jwt"}))


def test_streamable_http_lifespan_initializes_session_manager(monkeypatch):
    monkeypatch.setattr(app, "RAGFLOW_SERVICE_IDENTITY", "service@example.com")
    monkeypatch.setattr(app, "resolve_api_key", lambda identity: "service-api-key")
    app.mcp.settings.transport_security.allowed_hosts.append("mcp-ragflow.engepar.site")

    try:
        with TestClient(app.app, base_url="https://mcp-ragflow.engepar.site") as client:
            response = client.post(
                "/mcp",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "1.0"},
                    },
                },
            )
    finally:
        app.mcp.settings.transport_security.allowed_hosts.remove("mcp-ragflow.engepar.site")

    assert response.status_code == 200
    assert "ragflow-mcp-cloudflare-gateway" in response.text


@pytest.mark.asyncio
async def test_user_isolation(identity, monkeypatch):
    async def request(method, path, **kwargs):
        auth = kwargs["headers"]["Authorization"]
        if path == "/datasets" and auth == "Bearer ragflow-a":
            return FakeResponse(payload={"code": 0, "data": [{"id": "dataset-a"}], "total": 1})
        if path == "/datasets" and auth == "Bearer ragflow-b":
            return FakeResponse(payload={"code": 0, "data": [{"id": "dataset-b"}], "total": 1})
        if path == "/datasets/dataset-a/documents/document-a/chunks/chunk-a":
            return FakeResponse(payload={"code": 0, "data": {"id": "chunk-a", "kb_id": "dataset-a", "doc_id": "document-a", "img_id": "dataset-a-chunk-a"}})
        if path == "/datasets/dataset-b/documents/document-b/chunks/chunk-b":
            return FakeResponse(payload={"code": 0, "data": {"id": "chunk-b", "kb_id": "dataset-b", "doc_id": "document-b", "img_id": "dataset-b-chunk-b"}})
        if path.endswith("/presigned"):
            return FakeResponse(
                payload={
                    "code": 0,
                    "data": {
                        "url": "https://minio.example.test/bucket/object?signature=test",
                        "expires_in": 900,
                        "expires_at": "2026-07-22T21:30:00Z",
                    },
                }
            )
        if path.startswith("/documents/images/"):
            return FakeResponse(content=png_bytes(), headers={"content-type": "image/png"})
        raise AssertionError(path)

    client = FakeClient({})
    client.request = request
    monkeypatch.setattr(app, "http_client", client)

    await call_image_tool(dataset_id="dataset-a", document_id="document-a", chunk_id="chunk-a")
    with pytest.raises(PermissionError):
        await call_image_tool(dataset_id="dataset-b", document_id="document-b", chunk_id="chunk-b")

    app.identity_context.set(app.IdentityContext(identity="b@example.com", display_name="User B", api_key="ragflow-b"))
    await call_image_tool(dataset_id="dataset-b", document_id="document-b", chunk_id="chunk-b")
