from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jwt import PyJWKClient
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("token_registry")

DATA_DIR = Path(os.getenv("TOKEN_REGISTRY_DATA_DIR", "/data"))
DATABASE_PATH = Path(os.getenv("TOKEN_REGISTRY_DATABASE_PATH", str(DATA_DIR / "registry.db")))
MASTER_KEY_PATH = Path(os.getenv("TOKEN_REGISTRY_MASTER_KEY_PATH", "/run/secrets/token_registry_master_key"))
ADMIN_TEAM_DOMAIN = os.getenv("CF_ACCESS_TEAM_DOMAIN", "").strip()
ADMIN_AUDIENCE = os.getenv("CF_ACCESS_AUDIENCE", "").strip()
ADMIN_EMAILS = {item.strip().lower() for item in os.getenv("TOKEN_REGISTRY_ADMIN_EMAILS", "").split(",") if item.strip()}
PUBLIC_ORIGIN = os.getenv("TOKEN_REGISTRY_PUBLIC_ORIGIN", "").rstrip("/")
JWT_HEADER = "cf-access-jwt-assertion"
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
PRINCIPAL_TYPES = {"email", "service_token"}


@dataclass(frozen=True)
class Admin:
    email: str
    jwt_token: str


def json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def read_master_key() -> bytes:
    try:
        encoded = MASTER_KEY_PATH.read_text(encoding="utf-8").strip()
        key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Token registry master key is unavailable") from exc
    if len(key) != 32:
        raise RuntimeError("Token registry master key must decode to exactly 32 bytes")
    return key


def encrypt_credential(credential: str) -> str:
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(read_master_key()).encrypt(nonce, credential.encode("utf-8"), b"token-registry:v1")
    return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def decrypt_credential(payload: str) -> str:
    raw = base64.urlsafe_b64decode(payload)
    return AESGCM(read_master_key()).decrypt(raw[:12], raw[12:], b"token-registry:v1").decode("utf-8")


def hash_resolver_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encrypt_for_resolver(credential: str, resolver_key: str, application_slug: str) -> str:
    nonce = secrets.token_bytes(12)
    key = hashlib.sha256(resolver_key.encode("utf-8")).digest()
    encrypted = AESGCM(key).encrypt(nonce, credential.encode("utf-8"), application_slug.encode("utf-8"))
    return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_database() -> None:
    with database() as db:
        db.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                cf_team_domain TEXT NOT NULL,
                cf_audience TEXT NOT NULL,
                resolver_key_hash TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS principals (
                id INTEGER PRIMARY KEY,
                application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                principal_type TEXT NOT NULL,
                principal_value TEXT NOT NULL,
                identity TEXT NOT NULL,
                display_name TEXT NOT NULL,
                encrypted_credential TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(application_id, principal_type, principal_value)
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                application_slug TEXT,
                target TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def verify_access_jwt(token: str, team_domain: str, audience: str) -> dict[str, Any]:
    issuer = f"https://{team_domain}"
    key = PyJWKClient(f"{issuer}/cdn-cgi/access/certs").get_signing_key_from_jwt(token)
    return jwt.decode(token, key.key, algorithms=["RS256"], audience=audience, issuer=issuer)


def require_admin(request: Request) -> Admin:
    token = request.headers.get(JWT_HEADER, "")
    if not token or not ADMIN_TEAM_DOMAIN or not ADMIN_AUDIENCE:
        raise PermissionError("Cloudflare Access administrator identity is required")
    claims = verify_access_jwt(token, ADMIN_TEAM_DOMAIN, ADMIN_AUDIENCE)
    email = str(claims.get("email") or request.headers.get("cf-access-authenticated-user-email") or "").lower()
    if not email or email not in ADMIN_EMAILS:
        raise PermissionError("Administrator is not authorized")
    return Admin(email, token)


def csrf_token(admin: Admin) -> str:
    return hmac.new(read_master_key(), admin.jwt_token.encode("utf-8"), hashlib.sha256).hexdigest()


def require_csrf(request: Request, admin: Admin) -> None:
    origin = request.headers.get("origin", "")
    if PUBLIC_ORIGIN and origin != PUBLIC_ORIGIN:
        raise PermissionError("Invalid request origin")
    supplied = request.headers.get("x-csrf-token", "")
    if not hmac.compare_digest(supplied, csrf_token(admin)):
        raise PermissionError("Invalid CSRF token")


def audit(db: sqlite3.Connection, actor: str, action: str, application_slug: str | None, target: str | None) -> None:
    db.execute(
        "INSERT INTO audit_log(actor, action, application_slug, target) VALUES (?, ?, ?, ?)",
        (actor, action, application_slug, target),
    )


async def admin_page(request: Request) -> Response:
    try:
        require_admin(request)
    except Exception:
        return json_error("Administrator authentication failed", 403)
    return FileResponse(Path(__file__).parent / "static" / "index.html")


async def admin_session(request: Request) -> JSONResponse:
    try:
        admin = require_admin(request)
        return JSONResponse({"email": admin.email, "csrf_token": csrf_token(admin)})
    except Exception:
        return json_error("Administrator authentication failed", 403)


async def list_applications(request: Request) -> JSONResponse:
    try:
        require_admin(request)
        with database() as db:
            rows = db.execute(
                """
                SELECT a.slug, a.name, a.cf_team_domain, a.cf_audience, a.enabled,
                       a.created_at, a.updated_at, COUNT(p.id) AS principal_count
                FROM applications a
                LEFT JOIN principals p ON p.application_id = a.id
                GROUP BY a.id ORDER BY a.name
                """
            ).fetchall()
        return JSONResponse([dict(row) for row in rows])
    except Exception:
        return json_error("Unable to list applications", 403)


async def create_application(request: Request) -> JSONResponse:
    try:
        admin = require_admin(request)
        require_csrf(request, admin)
        body = await request.json()
        slug = str(body.get("slug", "")).strip().lower()
        name = str(body.get("name", "")).strip()
        team_domain = str(body.get("cf_team_domain", "")).strip()
        audience = str(body.get("cf_audience", "")).strip()
        if not SLUG_PATTERN.fullmatch(slug) or not name or not team_domain or not audience:
            return json_error("Application fields are invalid", 400)
        resolver_key = secrets.token_urlsafe(32)
        with database() as db:
            db.execute(
                """
                INSERT INTO applications(slug, name, cf_team_domain, cf_audience, resolver_key_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (slug, name, team_domain, audience, hash_resolver_key(resolver_key)),
            )
            audit(db, admin.email, "application.created", slug, None)
        return JSONResponse({"slug": slug, "resolver_key": resolver_key}, status_code=201)
    except sqlite3.IntegrityError:
        return json_error("Application slug already exists", 409)
    except Exception:
        logger.exception("Application creation failed")
        return json_error("Unable to create application", 403)


async def list_principals(request: Request) -> JSONResponse:
    try:
        require_admin(request)
        slug = request.path_params["slug"]
        with database() as db:
            rows = db.execute(
                """
                SELECT p.id, p.principal_type, p.principal_value, p.identity,
                       p.display_name, p.enabled, p.created_at, p.updated_at
                FROM principals p JOIN applications a ON a.id = p.application_id
                WHERE a.slug = ? ORDER BY p.display_name
                """,
                (slug,),
            ).fetchall()
        return JSONResponse([dict(row) for row in rows])
    except Exception:
        return json_error("Unable to list users", 403)


async def upsert_principal(request: Request) -> JSONResponse:
    try:
        admin = require_admin(request)
        require_csrf(request, admin)
        slug = request.path_params["slug"]
        body = await request.json()
        principal_type = str(body.get("principal_type", "")).strip()
        principal_value = str(body.get("principal_value", "")).strip()
        identity = str(body.get("identity", "")).strip().lower()
        display_name = str(body.get("display_name", "")).strip()
        credential = str(body.get("credential", "")).strip()
        if principal_type not in PRINCIPAL_TYPES or not principal_value or not identity or not display_name or not credential:
            return json_error("User fields are invalid", 400)
        if principal_type == "email":
            principal_value = principal_value.lower()
        encrypted = encrypt_credential(credential)
        with database() as db:
            application = db.execute("SELECT id FROM applications WHERE slug = ?", (slug,)).fetchone()
            if not application:
                return json_error("Application not found", 404)
            db.execute(
                """
                INSERT INTO principals(
                    application_id, principal_type, principal_value, identity,
                    display_name, encrypted_credential
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(application_id, principal_type, principal_value) DO UPDATE SET
                    identity = excluded.identity,
                    display_name = excluded.display_name,
                    encrypted_credential = excluded.encrypted_credential,
                    enabled = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (application["id"], principal_type, principal_value, identity, display_name, encrypted),
            )
            audit(db, admin.email, "principal.upserted", slug, identity)
        return JSONResponse({"status": "saved"}, status_code=201)
    except Exception:
        logger.exception("User registration failed")
        return json_error("Unable to save user", 403)


async def set_principal_enabled(request: Request) -> JSONResponse:
    try:
        admin = require_admin(request)
        require_csrf(request, admin)
        principal_id = int(request.path_params["principal_id"])
        body = await request.json()
        enabled = bool(body.get("enabled"))
        with database() as db:
            row = db.execute(
                """
                SELECT p.identity, a.slug FROM principals p
                JOIN applications a ON a.id = p.application_id WHERE p.id = ?
                """,
                (principal_id,),
            ).fetchone()
            if not row:
                return json_error("User not found", 404)
            db.execute(
                "UPDATE principals SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(enabled), principal_id),
            )
            audit(db, admin.email, "principal.enabled" if enabled else "principal.disabled", row["slug"], row["identity"])
        return JSONResponse({"status": "updated"})
    except Exception:
        return json_error("Unable to update user", 403)


async def resolve_credential(request: Request) -> JSONResponse:
    try:
        slug = request.path_params["slug"]
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise PermissionError
        resolver_key = authorization.removeprefix("Bearer ").strip()
        body = await request.json()
        access_jwt = str(body.get("access_jwt", "")).strip()
        if not access_jwt:
            raise PermissionError
        with database() as db:
            application = db.execute(
                "SELECT * FROM applications WHERE slug = ? AND enabled = 1",
                (slug,),
            ).fetchone()
            if not application or not hmac.compare_digest(
                application["resolver_key_hash"], hash_resolver_key(resolver_key)
            ):
                raise PermissionError
            claims = verify_access_jwt(access_jwt, application["cf_team_domain"], application["cf_audience"])
            email = str(claims.get("email") or "").strip().lower()
            common_name = str(claims.get("common_name") or "").strip()
            if email:
                principal_type, principal_value = "email", email
            elif common_name:
                principal_type, principal_value = "service_token", common_name
            else:
                raise PermissionError
            principal = db.execute(
                """
                SELECT p.* FROM principals p
                WHERE p.application_id = ? AND p.principal_type = ?
                      AND p.principal_value = ? AND p.enabled = 1
                """,
                (application["id"], principal_type, principal_value),
            ).fetchone()
            if not principal:
                raise PermissionError
        return JSONResponse(
            {
                "identity": principal["identity"],
                "display_name": principal["display_name"],
                "credential_envelope": encrypt_for_resolver(
                    decrypt_credential(principal["encrypted_credential"]),
                    resolver_key,
                    slug,
                ),
            },
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        return json_error("Access denied", 403)


async def healthz(request: Request) -> JSONResponse:
    try:
        read_master_key()
        with database() as db:
            db.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "ok", "service": "token-registry"})
    except Exception:
        return JSONResponse({"status": "degraded", "service": "token-registry"}, status_code=503)


@asynccontextmanager
async def lifespan(application):
    initialize_database()
    read_master_key()
    yield


starlette_app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/", admin_page, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/api/admin/session", admin_session, methods=["GET"]),
        Route("/api/admin/applications", list_applications, methods=["GET"]),
        Route("/api/admin/applications", create_application, methods=["POST"]),
        Route("/api/admin/applications/{slug:str}/principals", list_principals, methods=["GET"]),
        Route("/api/admin/applications/{slug:str}/principals", upsert_principal, methods=["POST"]),
        Route("/api/admin/principals/{principal_id:int}", set_principal_enabled, methods=["PATCH"]),
        Route("/v1/resolve/{slug:str}", resolve_credential, methods=["POST"]),
    ],
)


async def app(scope, receive, send) -> None:
    async def send_with_security_headers(message) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            headers.extend(
                [
                    (b"content-security-policy", b"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                ]
            )
            message["headers"] = headers
        await send(message)

    await starlette_app(scope, receive, send_with_security_headers)
