# MCP Token Registry

Central credential registry for MCP gateways and other applications protected
by Cloudflare Access.

## Security model

- Administrators authenticate through a dedicated Cloudflare Access
  application and an explicit email allowlist.
- Every managed application receives a random resolver key shown only once.
- Resolver keys are stored as SHA-256 digests.
- User credentials are encrypted with AES-256-GCM before SQLite persistence.
- The encryption key is a Docker secret and never enters the database.
- The resolver independently validates the user's Cloudflare JWT against the
  managed application's team domain and audience.
- Resolution responses use `Cache-Control: no-store`.
- Resolved credentials cross the internal network only inside an AES-GCM
  envelope encrypted with the managed application's resolver key.
- Credentials are never returned by administrative list endpoints or logs.
- Administrative writes require an origin check and a JWT-derived CSRF token.

The registry manages credentials but never invokes write operations in the
target application. The RAGFlow MCP remains read-only.

## First deployment

Generate a 32-byte encryption key on Windows:

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
[Convert]::ToBase64String($bytes)
```

Create a Portainer Docker secret named `token_registry_master_key` containing
only the generated value.

Create a Cloudflare Access self-hosted application for the administrative
hostname, for example:

```text
tokens.engepar.site
```

Configure its tunnel route to:

```text
http://token-registry:8080
```

Allow only administrator email addresses. Obtain that Access application's AUD
tag and configure these Portainer stack environment variables:

```yaml
TOKEN_REGISTRY_ADMIN_EMAILS: informatica@engepar.com
TOKEN_REGISTRY_PUBLIC_ORIGIN: https://tokens.engepar.site
CF_ACCESS_TEAM_DOMAIN: engepar.cloudflareaccess.com
CF_ACCESS_AUDIENCE: AUD_DO_TOKEN_REGISTRY
```

Deploy `stack.yml`. The service must share `mcp_shared` with the existing
Cloudflare tunnel.

## Register RAGFlow

1. Open `https://tokens.engepar.site`.
2. Create application `RAGFlow` with slug `ragflow`.
3. Enter the team domain and AUD of `mcp-ragflow.engepar.site`.
4. Save the resolver key shown once.
5. Create Docker secret `token_registry_resolver_key` with that value.
6. Redeploy the RAGFlow MCP using `mcp/ragflow-mcp/stack.registry.yml`.

That is the final stack update required for user onboarding.

## Add a user

1. Create a unique Cloudflare service token for the user.
2. Ensure the MCP Access policy accepts that service token.
3. Create an API key in the user's RAGFlow account.
4. In Token Registry, open RAGFlow and select **Novo usuário**.
5. Choose `Service token`.
6. Enter the Cloudflare Client ID, internal identity, display name and RAGFlow
   API key.

The Cloudflare Client Secret remains only on the user's computer. The registry
stores the Client ID and the encrypted RAGFlow key. No environment variable,
stack edit or redeploy is needed for subsequent users.

## Build and operations

```bash
docker build -t edsonjrx/mcp-token-registry:1.0.0 .
docker push edsonjrx/mcp-token-registry:1.0.0
docker stack deploy -c stack.yml token_registry
docker service logs -f token_registry_token-registry
```

Back up the named volume and the master-key secret together. A database backup
without its matching master key cannot recover credentials. The supplied
SQLite deployment runs one replica pinned to a manager node. For a multi-node
high-availability deployment, replace SQLite with PostgreSQL before increasing
replicas.
