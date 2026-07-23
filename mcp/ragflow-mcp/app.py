from __future__ import annotations

import base64
import binascii
import contextlib
import contextvars
import io
import json
import hashlib
import logging
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from jwt import PyJWKClient
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, ResourceLink, TextContent
from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

logger = logging.getLogger("ragflow_mcp_gateway")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

RAGFLOW_BASE_URL = os.getenv("RAGFLOW_BASE_URL", "http://ragflow:9380").rstrip("/")
RAGFLOW_API_PREFIX = os.getenv("RAGFLOW_API_PREFIX", "/api/v1").rstrip("/")
RAGFLOW_API_KEYS_DIR = Path(os.getenv("RAGFLOW_API_KEYS_DIR", "/run/secrets/ragflow-api-keys"))
RAGFLOW_IDENTITY_MAP_PATH = Path(os.getenv("RAGFLOW_IDENTITY_MAP_PATH", "/run/secrets/ragflow-identity-map.json"))
RAGFLOW_DEFAULT_DATASET_PAGE_SIZE = min(int(os.getenv("RAGFLOW_MAX_PAGE_SIZE", "100")), 100)
RAGFLOW_MAX_RESULT_PAGE_SIZE = min(int(os.getenv("RAGFLOW_MAX_RESULT_PAGE_SIZE", "50")), 100)
RAGFLOW_REQUEST_TIMEOUT_SECONDS = float(os.getenv("RAGFLOW_REQUEST_TIMEOUT_SECONDS", "90"))
RAGFLOW_MAX_IMAGE_BYTES = int(os.getenv("RAGFLOW_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))
RAGFLOW_MAX_IMAGE_PIXELS = int(os.getenv("RAGFLOW_MAX_IMAGE_PIXELS", "40000000"))
RAGFLOW_IMAGE_MAX_DIMENSION = int(os.getenv("RAGFLOW_IMAGE_MAX_DIMENSION", "1600"))
TOKEN_REGISTRY_URL = os.getenv("TOKEN_REGISTRY_URL", "").rstrip("/")
TOKEN_REGISTRY_APPLICATION = os.getenv("TOKEN_REGISTRY_APPLICATION", "ragflow").strip()
TOKEN_REGISTRY_RESOLVER_KEY_PATH = Path(
    os.getenv("TOKEN_REGISTRY_RESOLVER_KEY_PATH", "/run/secrets/token_registry_resolver_key")
)

CF_ACCESS_TEAM_DOMAIN = os.getenv("CF_ACCESS_TEAM_DOMAIN", "")
CF_ACCESS_AUDIENCE = os.getenv("CF_ACCESS_AUDIENCE", "")
RAGFLOW_SERVICE_IDENTITY = os.getenv("RAGFLOW_SERVICE_IDENTITY", "").strip().lower()
INSTANCE_ID = socket.gethostname()
RAGFLOW_MCP_ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("RAGFLOW_MCP_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*,[::1]:*").split(",")
    if host.strip()
]
CF_ACCESS_ISSUER = f"https://{CF_ACCESS_TEAM_DOMAIN}" if CF_ACCESS_TEAM_DOMAIN else ""
CF_ACCESS_CERTS_URL = f"{CF_ACCESS_ISSUER}/cdn-cgi/access/certs" if CF_ACCESS_ISSUER else ""

IDENTITY_HEADER = "cf-access-authenticated-user-email"
JWT_HEADER = "cf-access-jwt-assertion"
SERVICE_TOKEN_ID_HEADER = "cf-access-client-id"
SERVICE_TOKEN_SECRET_HEADER = "cf-access-client-secret"
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
REFERENCE_PATTERN = re.compile(r"^FIG-\d+$")


@dataclass(frozen=True)
class IdentityContext:
    identity: str
    display_name: str
    api_key: str


@dataclass(frozen=True)
class IdentityMapping:
    identity: str
    display_name: str
    secret_name: str


identity_context: contextvars.ContextVar[IdentityContext | None] = contextvars.ContextVar("identity_context", default=None)
http_client = httpx.AsyncClient(base_url=f"{RAGFLOW_BASE_URL}{RAGFLOW_API_PREFIX}", timeout=RAGFLOW_REQUEST_TIMEOUT_SECONDS)

mcp = FastMCP(
    "ragflow-mcp-cloudflare-gateway",
    instructions=(
        "Read-only access to the RAGFlow datasets authorized for the authenticated "
        "Cloudflare Access identity. Never infer access to a dataset that is not "
        "returned by ragflow_list_datasets. When ragflow_retrieval returns a chunk "
        "with image.available=true and the figure is relevant, call "
        "ragflow_get_chunk_image using exactly the arguments provided in "
        "image.arguments. Cite the returned figure using image.reference. "
        "When a PDF source is relevant, call ragflow_get_document_pdf using "
        "the arguments provided in document.arguments."
    ),
    transport_security=TransportSecuritySettings(allowed_hosts=RAGFLOW_MCP_ALLOWED_HOSTS),
)


def current_identity() -> IdentityContext:
    ctx = identity_context.get()
    if ctx is None:
        raise PermissionError("Cloudflare Access identity is required")
    return ctx


def load_identity_map() -> dict[str, Any]:
    if not RAGFLOW_IDENTITY_MAP_PATH.exists():
        return {}
    with RAGFLOW_IDENTITY_MAP_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError("RAGFlow identity map must be a JSON object")
    return payload


def parse_identity_mapping(key: str, value: Any, *, service_token: bool = False) -> IdentityMapping:
    if isinstance(value, str) and not service_token:
        identity = key.strip().lower()
        return IdentityMapping(identity, identity.split("@", 1)[0] or identity, value)
    if not isinstance(value, dict):
        raise RuntimeError("RAGFlow identity map entries must be strings or JSON objects")
    identity = str(value.get("identity") or ("" if service_token else key)).strip().lower()
    secret_name = str(value.get("api_key_secret") or "").strip()
    display_name = str(value.get("display_name") or identity.split("@", 1)[0]).strip()
    if not identity or not secret_name or not display_name:
        raise RuntimeError("RAGFlow identity map entry is incomplete")
    return IdentityMapping(identity, display_name, secret_name)


def resolve_identity_mapping(identity: str) -> IdentityMapping:
    payload = load_identity_map()
    users = payload.get("users")
    if isinstance(users, dict):
        value = next((v for k, v in users.items() if str(k).lower() == identity.lower()), None)
        if value is not None:
            return parse_identity_mapping(identity, value)
    value = next((v for k, v in payload.items() if str(k).lower() == identity.lower()), None)
    if value is None or isinstance(value, dict):
        raise PermissionError("No RAGFlow API key is mapped for this Cloudflare identity")
    return parse_identity_mapping(identity, value)


def resolve_service_token_mapping(common_name: str) -> IdentityMapping | None:
    service_tokens = load_identity_map().get("service_tokens")
    if service_tokens is None:
        return None
    if not isinstance(service_tokens, dict):
        raise RuntimeError("RAGFlow service_tokens map must be a JSON object")
    value = service_tokens.get(common_name)
    if value is None:
        raise PermissionError("No RAGFlow API key is mapped for this Cloudflare service token")
    return parse_identity_mapping(common_name, value, service_token=True)


def read_secret(name: str) -> str:
    candidate = RAGFLOW_API_KEYS_DIR / name
    if not candidate.is_file():
        raise PermissionError("No RAGFlow API key is mapped for this Cloudflare identity")
    return candidate.read_text(encoding="utf-8").strip()


def resolve_api_key(identity: str) -> str:
    api_key = read_secret(resolve_identity_mapping(identity).secret_name)
    if not api_key:
        raise PermissionError("Mapped RAGFlow API key is empty")
    return api_key


def identity_context_from_mapping(mapping: IdentityMapping) -> IdentityContext:
    api_key = read_secret(mapping.secret_name)
    if not api_key:
        raise PermissionError("Mapped RAGFlow API key is empty")
    return IdentityContext(mapping.identity, mapping.display_name, api_key)


def service_identity_status() -> dict[str, bool]:
    """Return credential-free diagnostics for the service identity configuration."""
    status = {
        "identity_map_present": RAGFLOW_IDENTITY_MAP_PATH.is_file(),
        "identity_map_valid": False,
        "service_identity_mapped": False,
        "api_key_secret_present": False,
        "api_key_secret_nonempty": False,
    }
    if not RAGFLOW_SERVICE_IDENTITY or not status["identity_map_present"]:
        return status
    try:
        identity_map = load_identity_map()
    except (OSError, ValueError, TypeError):
        return status
    status["identity_map_valid"] = True
    try:
        mapping = resolve_identity_mapping(RAGFLOW_SERVICE_IDENTITY)
    except (PermissionError, RuntimeError):
        return status
    status["service_identity_mapped"] = True
    secret_path = RAGFLOW_API_KEYS_DIR / mapping.secret_name
    status["api_key_secret_present"] = secret_path.is_file()
    if status["api_key_secret_present"]:
        try:
            status["api_key_secret_nonempty"] = bool(secret_path.read_text(encoding="utf-8").strip())
        except OSError:
            pass
    return status


def verify_cloudflare_jwt(token: str) -> dict[str, Any]:
    if not CF_ACCESS_CERTS_URL or not CF_ACCESS_AUDIENCE:
        raise PermissionError("Cloudflare Access validation is not configured")
    jwks = PyJWKClient(CF_ACCESS_CERTS_URL)
    signing_key = jwks.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=CF_ACCESS_AUDIENCE,
        issuer=CF_ACCESS_ISSUER,
    )


def service_identity_context() -> IdentityContext:
    if not RAGFLOW_SERVICE_IDENTITY:
        raise PermissionError("Cloudflare Access identity is required")
    api_key = resolve_api_key(RAGFLOW_SERVICE_IDENTITY)
    display_name = RAGFLOW_SERVICE_IDENTITY.split("@", 1)[0] or RAGFLOW_SERVICE_IDENTITY
    return IdentityContext(identity=RAGFLOW_SERVICE_IDENTITY, display_name=display_name, api_key=api_key)


def identity_from_headers(headers: Headers) -> IdentityContext:
    token = headers.get(JWT_HEADER)
    if not token:
        return service_identity_context()
    claims = verify_cloudflare_jwt(token)
    identity = str(claims.get("email") or headers.get(IDENTITY_HEADER) or "").strip().lower()
    if identity:
        return identity_context_from_mapping(resolve_identity_mapping(identity))
    common_name = str(claims.get("common_name") or "").strip()
    if common_name:
        mapping = resolve_service_token_mapping(common_name)
        if mapping is not None:
            return identity_context_from_mapping(mapping)
    return service_identity_context()


async def resolve_registry_identity(access_jwt: str) -> IdentityContext:
    try:
        resolver_key = TOKEN_REGISTRY_RESOLVER_KEY_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("Token registry resolver key is unavailable") from exc
    if not resolver_key:
        raise RuntimeError("Token registry resolver key is empty")
    response = await http_client.post(
        f"{TOKEN_REGISTRY_URL}/api/v1/resolve/{quote(TOKEN_REGISTRY_APPLICATION, safe='')}",
        headers={"Authorization": f"Bearer {resolver_key}"},
        json={"access_jwt": access_jwt},
    )
    if response.status_code in {401, 403, 404}:
        raise PermissionError("No RAGFlow API key is registered for this Cloudflare identity")
    if response.status_code >= 400:
        raise RuntimeError(f"Token registry request failed (HTTP {response.status_code})")
    payload = response.json()
    identity = str(payload.get("identity") or "").strip().lower()
    display_name = str(payload.get("display_name") or "").strip()
    envelope = str(payload.get("credential_envelope") or "").strip()
    if not identity or not display_name or not envelope:
        raise RuntimeError("Token registry returned an invalid response")
    try:
        raw = base64.urlsafe_b64decode(envelope)
        key = hashlib.sha256(resolver_key.encode("utf-8")).digest()
        api_key = AESGCM(key).decrypt(
            raw[:12],
            raw[12:],
            TOKEN_REGISTRY_APPLICATION.encode("utf-8"),
        ).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError, InvalidTag) as exc:
        raise RuntimeError("Token registry credential envelope is invalid") from exc
    return IdentityContext(identity, display_name, api_key)


async def resolve_request_identity(headers: Headers) -> IdentityContext:
    access_jwt = headers.get(JWT_HEADER, "")
    if TOKEN_REGISTRY_URL:
        if not access_jwt:
            raise PermissionError("Cloudflare Access identity is required")
        return await resolve_registry_identity(access_jwt)
    return identity_from_headers(headers)


def ragflow_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {current_identity().api_key}"}


async def ragflow_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    response = await http_client.request(method, path, headers=ragflow_headers(), **kwargs)
    if response.status_code == 401:
        raise PermissionError("The mapped RAGFlow API key is invalid or revoked")
    if response.status_code == 403:
        raise PermissionError("RAGFlow denied access for this user")
    if response.status_code >= 400:
        raise RuntimeError(f"RAGFlow request failed (HTTP {response.status_code})")
    payload = response.json()
    if isinstance(payload, dict) and payload.get("code", 0) != 0:
        raise RuntimeError(str(payload.get("message") or "RAGFlow request failed"))
    if not isinstance(payload, dict):
        raise RuntimeError("RAGFlow returned an invalid response")
    return payload


async def ragflow_binary_request(path: str, *, accept: str = "application/octet-stream") -> tuple[bytes, str]:
    response = await http_client.request("GET", path, headers={**ragflow_headers(), "Accept": accept})
    if response.status_code == 401:
        raise PermissionError("The mapped RAGFlow API key is invalid or revoked")
    if response.status_code == 403:
        raise PermissionError("RAGFlow denied access for this user")
    if response.status_code >= 400:
        message = None
        with contextlib.suppress(ValueError):
            payload = response.json()
            if isinstance(payload, dict):
                message = payload.get("message")
        raise RuntimeError(message or f"RAGFlow binary request failed (HTTP {response.status_code})")
    declared_size = response.headers.get("content-length")
    if declared_size and declared_size.isdigit() and int(declared_size) > RAGFLOW_MAX_IMAGE_BYTES:
        raise ValueError("The RAGFlow image exceeds the configured size limit")
    data = response.content
    if not data:
        raise RuntimeError("RAGFlow returned an empty image")
    if len(data) > RAGFLOW_MAX_IMAGE_BYTES:
        raise ValueError("The RAGFlow image exceeds the configured size limit")
    return data, response.headers.get("content-type", "application/octet-stream")


def validate_ragflow_id(name: str, value: str) -> str:
    normalized = str(value or "").strip()
    if not ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{name} contains invalid characters")
    return normalized


def path_segment(value: str) -> str:
    return quote(value, safe="")


async def fetch_accessible_datasets() -> list[dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = await ragflow_request("GET", "/datasets", params={"page": page, "page_size": RAGFLOW_DEFAULT_DATASET_PAGE_SIZE})
        page_data = payload.get("data") or []
        if not isinstance(page_data, list) or not page_data:
            break
        datasets.extend(item for item in page_data if isinstance(item, dict))
        total = payload.get("total")
        if total is not None and len(datasets) >= int(total):
            break
        if len(page_data) < RAGFLOW_DEFAULT_DATASET_PAGE_SIZE:
            break
        page += 1
    return datasets


async def get_accessible_dataset_map() -> dict[str, dict[str, Any]]:
    return {str(item["id"]): item for item in await fetch_accessible_datasets() if item.get("id") is not None}


async def require_accessible_dataset(dataset_id: str) -> dict[str, Any]:
    dataset = (await get_accessible_dataset_map()).get(dataset_id)
    if dataset is None:
        raise PermissionError("The requested dataset is not accessible to the authenticated user")
    return dataset


async def fetch_authorized_chunk(dataset_id: str, document_id: str, chunk_id: str) -> dict[str, Any]:
    payload = await ragflow_request(
        "GET",
        f"/datasets/{path_segment(dataset_id)}/documents/{path_segment(document_id)}/chunks/{path_segment(chunk_id)}",
    )
    chunk = payload.get("data")
    if not isinstance(chunk, dict) or not chunk:
        raise RuntimeError("Chunk not found or not accessible")
    returned_dataset_id = str(chunk.get("dataset_id") or chunk.get("kb_id") or "")
    returned_document_id = str(chunk.get("document_id") or chunk.get("doc_id") or "")
    returned_chunk_id = str(chunk.get("id") or chunk.get("chunk_id") or chunk_id)
    if returned_dataset_id and returned_dataset_id != dataset_id:
        raise PermissionError("The chunk does not belong to the requested dataset")
    if returned_document_id and returned_document_id != document_id:
        raise PermissionError("The chunk does not belong to the requested document")
    if returned_chunk_id and returned_chunk_id != chunk_id:
        raise PermissionError("The returned chunk does not match the requested chunk")
    return chunk


def encode_mcp_image(raw_data: bytes, *, output_format: Literal["png", "jpeg"], max_dimension: int) -> tuple[bytes, str, dict[str, Any]]:
    try:
        with PILImage.open(io.BytesIO(raw_data)) as source:
            original_width, original_height = source.size
            if original_width <= 0 or original_height <= 0:
                raise ValueError("Invalid image dimensions")
            if original_width * original_height > RAGFLOW_MAX_IMAGE_PIXELS:
                raise ValueError("The RAGFlow image exceeds the configured pixel limit")
            source.load()
            image = ImageOps.exif_transpose(source)
            image.thumbnail((max_dimension, max_dimension))
            returned_width, returned_height = image.size
            buffer = io.BytesIO()
            if output_format == "jpeg":
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                image.save(buffer, format="JPEG", quality=90, optimize=True)
                mime_type = "image/jpeg"
            else:
                if image.mode == "P":
                    image = image.convert("RGBA")
                image.save(buffer, format="PNG", optimize=True)
                mime_type = "image/png"
            encoded = buffer.getvalue()
            if len(encoded) > RAGFLOW_MAX_IMAGE_BYTES:
                raise ValueError("The normalized image exceeds the configured size limit")
            return encoded, mime_type, {
                "original_dimensions": {"width": original_width, "height": original_height},
                "returned_dimensions": {"width": returned_width, "height": returned_height},
            }
    except UnidentifiedImageError as exc:
        raise ValueError("RAGFlow returned bytes that are not a supported image") from exc


def enrich_chunks(chunks: list[Any], accessible: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dataset_map = {str(item["id"]): item for item in accessible if item.get("id") is not None}
    enriched: list[dict[str, Any]] = []
    figure_counter = 0
    for raw_chunk in chunks:
        if not isinstance(raw_chunk, dict):
            continue
        chunk = dict(raw_chunk)
        dataset_id = str(chunk.get("dataset_id") or chunk.get("kb_id") or "")
        document_id = str(chunk.get("document_id") or chunk.get("doc_id") or "")
        chunk_id = str(chunk.get("id") or chunk.get("chunk_id") or "")
        image_id = str(chunk.get("image_id") or chunk.get("img_id") or "").strip()
        dataset_meta = dataset_map.get(dataset_id, {})
        chunk["dataset_name"] = dataset_meta.get("name") or chunk.get("dataset_name")
        chunk["document_name"] = chunk.get("document_name") or chunk.get("document_keyword") or chunk.get("docnm_kwd")
        positions = chunk.get("positions") or chunk.get("position_int") or []
        chunk["source"] = {
            "dataset_id": dataset_id or None,
            "dataset_name": chunk.get("dataset_name"),
            "document_id": document_id or None,
            "document_name": chunk.get("document_name"),
            "chunk_id": chunk_id or None,
            "positions": positions,
        }
        document_name = str(chunk.get("document_name") or "")
        if dataset_id and document_id and document_name.lower().endswith(".pdf"):
            chunk["document"] = {
                "available": True,
                "filename": document_name,
                "tool": "ragflow_get_document_pdf",
                "arguments": {
                    "dataset_id": dataset_id,
                    "document_id": document_id,
                },
            }
        else:
            chunk["document"] = {"available": False}
        if image_id and dataset_id and document_id and chunk_id:
            figure_counter += 1
            reference = f"FIG-{figure_counter:03d}"
            chunk["image"] = {
                "available": True,
                "reference": reference,
                "image_id": image_id,
                "tool": "ragflow_get_chunk_image",
                "arguments": {
                    "dataset_id": dataset_id,
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "reference": reference,
                },
            }
        else:
            chunk["image"] = {"available": False}
        enriched.append(chunk)
    return enriched


@mcp.tool()
async def ragflow_whoami() -> dict[str, str]:
    ctx = current_identity()
    return {"identity": ctx.display_name, "cloudflare_identity": ctx.identity}


@mcp.tool()
async def ragflow_list_datasets(page: int = 1, page_size: int = RAGFLOW_DEFAULT_DATASET_PAGE_SIZE) -> dict[str, Any]:
    page_size = min(max(int(page_size), 1), RAGFLOW_DEFAULT_DATASET_PAGE_SIZE)
    payload = await ragflow_request("GET", "/datasets", params={"page": max(int(page), 1), "page_size": page_size})
    return {"identity": current_identity().display_name, "datasets": payload.get("data") or [], "total": payload.get("total")}


@mcp.tool()
async def ragflow_retrieval(
    question: str,
    dataset_ids: list[str] | None = None,
    document_ids: list[str] | None = None,
    page: int = 1,
    page_size: int = 10,
    similarity_threshold: float = 0.2,
    vector_similarity_weight: float = 0.3,
    keyword: bool = False,
    top_k: int = 1024,
) -> dict[str, Any]:
    ctx = current_identity()
    accessible = await fetch_accessible_datasets()
    accessible_ids = {str(item["id"]) for item in accessible if item.get("id") is not None}
    selected_ids = [validate_ragflow_id("dataset_id", item) for item in (dataset_ids or sorted(accessible_ids))]
    unauthorized = [item for item in selected_ids if item not in accessible_ids]
    if unauthorized:
        raise PermissionError("One or more requested datasets are not accessible to the authenticated user")
    payload = await ragflow_request(
        "POST",
        "/retrieval",
        json={
            "question": question,
            "dataset_ids": selected_ids,
            "document_ids": document_ids or [],
            "page": max(int(page), 1),
            "page_size": min(max(int(page_size), 1), RAGFLOW_MAX_RESULT_PAGE_SIZE),
            "similarity_threshold": similarity_threshold,
            "vector_similarity_weight": vector_similarity_weight,
            "keyword": keyword,
            "top_k": min(max(int(top_k), 1), 1024),
        },
    )
    data = payload.get("data") or {}
    chunks = enrich_chunks(data.get("chunks") or [], accessible)
    logger.info("Retrieval identity=%s datasets=%d chunks=%d", ctx.identity, len(selected_ids), len(chunks))
    return {"identity": ctx.display_name, "question": question, "dataset_ids": selected_ids, "chunks": chunks, "total": data.get("total"), "doc_aggs": data.get("doc_aggs") or []}


@mcp.tool(structured_output=False)
async def ragflow_get_chunk_image(
    dataset_id: str,
    document_id: str,
    chunk_id: str,
    reference: str | None = None,
    max_dimension: int = RAGFLOW_IMAGE_MAX_DIMENSION,
    output_format: Literal["png", "jpeg"] = "png",
    signed_url_expires_in: int = 900,
) -> CallToolResult:
    dataset_id = validate_ragflow_id("dataset_id", dataset_id)
    document_id = validate_ragflow_id("document_id", document_id)
    chunk_id = validate_ragflow_id("chunk_id", chunk_id)
    if reference is not None:
        reference = reference.strip().upper()
        if len(reference) > 32 or not REFERENCE_PATTERN.fullmatch(reference):
            raise ValueError("reference must use the format FIG-001")
    if not 256 <= int(max_dimension) <= 4096:
        raise ValueError("max_dimension must be between 256 and 4096")
    if output_format not in {"png", "jpeg"}:
        raise ValueError("output_format must be 'png' or 'jpeg'")
    if not 60 <= int(signed_url_expires_in) <= 3600:
        raise ValueError("signed_url_expires_in must be between 60 and 3600 seconds")

    ctx = current_identity()
    dataset = await require_accessible_dataset(dataset_id)
    chunk = await fetch_authorized_chunk(dataset_id, document_id, chunk_id)
    image_id = str(chunk.get("image_id") or chunk.get("img_id") or "").strip()
    if not image_id:
        raise ValueError("The requested chunk does not contain an image")

    signed_url_payload = await ragflow_request(
        "GET",
        f"/documents/images/{path_segment(image_id)}/presigned",
        params={"expires_in": int(signed_url_expires_in)},
    )
    signed_url_data = signed_url_payload.get("data") or {}
    if not isinstance(signed_url_data, dict) or not signed_url_data.get("url"):
        raise RuntimeError("RAGFlow did not return a presigned image URL")

    raw_image, declared_mime_type = await ragflow_binary_request(f"/documents/images/{path_segment(image_id)}", accept="image/*")
    image_data, mime_type, dimensions = encode_mcp_image(raw_image, output_format=output_format, max_dimension=int(max_dimension))
    metadata = {
        "reference": reference or f"FIG-{chunk_id[:8].upper()}",
        "identity": ctx.display_name,
        "dataset_id": dataset_id,
        "dataset_name": dataset.get("name"),
        "document_id": document_id,
        "document_name": chunk.get("document_name") or chunk.get("document_keyword") or chunk.get("docnm_kwd"),
        "chunk_id": chunk_id,
        "image_id": image_id,
        "positions": chunk.get("positions") or chunk.get("position_int") or [],
        "declared_source_mime_type": declared_mime_type,
        "returned_mime_type": mime_type,
        "signed_url": signed_url_data["url"],
        "signed_url_expires_in": signed_url_data.get("expires_in", int(signed_url_expires_in)),
        "signed_url_expires_at": signed_url_data.get("expires_at"),
        **dimensions,
    }
    logger.info("Returned chunk image identity=%s dataset_id=%s document_id=%s chunk_id=%s bytes=%d", ctx.identity, dataset_id, document_id, chunk_id, len(image_data))
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
            ImageContent(type="image", data=base64.b64encode(image_data).decode("ascii"), mimeType=mime_type),
            ResourceLink(
                type="resource_link",
                uri=signed_url_data["url"],
                name=metadata["reference"],
                title=f"{metadata['reference']} - {metadata['document_name'] or 'RAGFlow image'}",
                description="Short-lived URL for the authorized RAGFlow chunk image",
                mimeType=declared_mime_type if declared_mime_type.startswith("image/") else None,
            ),
        ]
    )


@mcp.tool(structured_output=False)
async def ragflow_get_document_pdf(
    dataset_id: str,
    document_id: str,
    signed_url_expires_in: int = 900,
) -> CallToolResult:
    """Return a short-lived link for one authorized RAGFlow PDF document."""
    dataset_id = validate_ragflow_id("dataset_id", dataset_id)
    document_id = validate_ragflow_id("document_id", document_id)
    if not 60 <= int(signed_url_expires_in) <= 3600:
        raise ValueError("signed_url_expires_in must be between 60 and 3600 seconds")

    ctx = current_identity()
    dataset = await require_accessible_dataset(dataset_id)
    payload = await ragflow_request(
        "GET",
        f"/datasets/{path_segment(dataset_id)}/documents/{path_segment(document_id)}/presigned",
        params={"expires_in": int(signed_url_expires_in)},
    )
    data = payload.get("data") or {}
    if not isinstance(data, dict) or not data.get("url"):
        raise RuntimeError("RAGFlow did not return a presigned document URL")
    filename = str(data.get("filename") or "document.pdf")
    content_type = str(data.get("content_type") or "application/octet-stream")
    if content_type != "application/pdf" and not filename.lower().endswith(".pdf"):
        raise ValueError("The requested document is not a PDF")

    metadata = {
        "identity": ctx.display_name,
        "dataset_id": dataset_id,
        "dataset_name": dataset.get("name"),
        "document_id": document_id,
        "document_name": filename,
        "content_type": "application/pdf",
        "signed_url": data["url"],
        "signed_url_expires_in": data.get("expires_in", int(signed_url_expires_in)),
        "signed_url_expires_at": data.get("expires_at"),
    }
    logger.info(
        "Returned document PDF link identity=%s dataset_id=%s document_id=%s",
        ctx.identity,
        dataset_id,
        document_id,
    )
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
            ResourceLink(
                type="resource_link",
                uri=data["url"],
                name=filename,
                title=filename,
                description="Short-lived URL for the authorized RAGFlow PDF document",
                mimeType="application/pdf",
            ),
        ]
    )


async def healthz(request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "ragflow-mcp-cloudflare-gateway",
            "readonly": True,
            "token_registry_configured": bool(TOKEN_REGISTRY_URL),
            "service_identity_configured": bool(RAGFLOW_SERVICE_IDENTITY),
            "instance": INSTANCE_ID,
            **service_identity_status(),
        }
    )


MCP_ASGI_APP = mcp.streamable_http_app()


async def mcp_scope(scope, receive, send) -> None:
    headers = Headers(scope=scope)
    try:
        ctx = await resolve_request_identity(headers)
    except PermissionError as exc:
        logger.warning(
            "MCP authentication rejected instance=%s method=%s path=%s service_identity_configured=%s reason=%s",
            INSTANCE_ID,
            scope.get("method", ""),
            scope.get("path", ""),
            bool(RAGFLOW_SERVICE_IDENTITY),
            str(exc),
        )
        response = JSONResponse(
            {
                "error": str(exc),
                "instance": INSTANCE_ID,
                "service_identity_configured": bool(RAGFLOW_SERVICE_IDENTITY),
            },
            status_code=401,
        )
        await response(scope, receive, send)
        return
    except Exception:
        logger.exception(
            "MCP authentication failed method=%s path=%s",
            scope.get("method", ""),
            scope.get("path", ""),
        )
        response = JSONResponse({"error": "Authentication configuration failed"}, status_code=500)
        await response(scope, receive, send)
        return
    token = identity_context.set(ctx)
    try:
        await MCP_ASGI_APP(scope, receive, send)
    finally:
        identity_context.reset(token)



def create_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Mount("/", app=mcp_scope),
        ],
        lifespan=MCP_ASGI_APP.router.lifespan_context,
    )


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))
