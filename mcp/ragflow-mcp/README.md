# RAGFlow MCP Cloudflare Gateway

Read-only MCP gateway for RAGFlow behind Cloudflare Access.

The gateway resolves the authenticated Cloudflare identity to an individual
RAGFlow API key stored as a Docker secret. It exposes only read operations:

- `ragflow_whoami`
- `ragflow_list_datasets`
- `ragflow_retrieval`
- `ragflow_get_chunk_image`
- `ragflow_get_document_pdf`

`ragflow_get_chunk_image` never accepts `image_id` from the client. The tool
requires `dataset_id`, `document_id` and `chunk_id`, revalidates the chunk via
RAGFlow, then obtains the image identifier from that authorized chunk. Its
metadata includes a short-lived `signed_url` when the RAGFlow storage exposes
a public presigning endpoint. The tool also returns that URL as an MCP
`ResourceLink`, alongside the existing metadata and `ImageContent`.

`ragflow_get_document_pdf` accepts only `dataset_id` and `document_id`,
revalidates access in RAGFlow, and returns a short-lived PDF `ResourceLink`.

## Configuration

Required environment:

- `CF_ACCESS_TEAM_DOMAIN`
- `CF_ACCESS_AUDIENCE`
- `RAGFLOW_BASE_URL`
- `RAGFLOW_IDENTITY_MAP_PATH`
- `RAGFLOW_API_KEYS_DIR`
- `RAGFLOW_SERVICE_IDENTITY`
- `RAGFLOW_MCP_ALLOWED_HOSTS`

Dynamic registry mode:

- `TOKEN_REGISTRY_URL`
- `TOKEN_REGISTRY_APPLICATION`
- `TOKEN_REGISTRY_RESOLVER_KEY_PATH`

When `TOKEN_REGISTRY_URL` is configured, the gateway requires a Cloudflare JWT
and resolves the identity through Token Registry. Local identity-map files are
used only when registry mode is disabled. See `stack.registry.yml`.

## Per-user service tokens

Create one Cloudflare Access service token and one RAGFlow API key per user.
Store each RAGFlow key in a separate Docker secret. The Cloudflare client
secret stays only on that user's computer and is never added to this map.

Use this identity-map format:

```json
{
  "users": {
    "informatica@engepar.com": {
      "display_name": "Informatica",
      "api_key_secret": "ragflow_api_key_informatica"
    }
  },
  "service_tokens": {
    "CLOUDFLARE_CLIENT_ID.access": {
      "identity": "edson@engepar.com",
      "display_name": "Edson",
      "api_key_secret": "ragflow_api_key_edson"
    }
  }
}
```

`service_tokens` keys are the service-token Client IDs (`common_name` in the
validated Cloudflare JWT), not client secrets. When this section exists, an
unmapped service token is rejected. The previous flat email-to-secret map
remains supported for migration.

Mount every referenced Docker secret below `RAGFLOW_API_KEYS_DIR`:

```yaml
services:
  ragflow-mcp:
    secrets:
      - source: ragflow_api_key_edson
        target: ragflow-api-keys/ragflow_api_key_edson

secrets:
  ragflow_api_key_edson:
    external: true
```

Each Codex user configures their own Cloudflare service-token credentials in
`MCP_CF_ACCESS_CLIENT_ID` and `MCP_CF_ACCESS_CLIENT_SECRET`. The gateway never
accepts a RAGFlow API key from an MCP argument or request header.

Image limits:

- `RAGFLOW_MAX_IMAGE_BYTES=8388608`
- `RAGFLOW_MAX_IMAGE_PIXELS=40000000`
- `RAGFLOW_IMAGE_MAX_DIMENSION=1600`

Signed URL expiration is selected per tool call with
`signed_url_expires_in` and is restricted to 60-3600 seconds (default 900).
The RAGFlow service must configure `MINIO_PUBLIC_HOST` without a URL scheme,
for example `minio-ragflow.engepar.site`, and `MINIO_PUBLIC_SECURE=true`.

## Build

```bash
docker build -t SEU_REGISTRY/ragflow-mcp-cloudflare-gateway:1.4.0 .
docker push SEU_REGISTRY/ragflow-mcp-cloudflare-gateway:1.4.0
docker stack deploy -c stack.yml ragflow_mcp
docker service logs -f ragflow_mcp_ragflow-mcp-gateway
```
