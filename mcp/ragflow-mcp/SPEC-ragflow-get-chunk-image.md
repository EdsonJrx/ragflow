# SPEC — `ragflow_get_chunk_image` para o Gateway MCP RAGFlow

**Status:** pronto para implementação
**Projeto-alvo:** `ragflow-mcp-cloudflare-gateway`
**Arquivo principal:** `app.py`
**Objetivo:** permitir que o Codex recupere e interprete imagens/figuras vinculadas aos chunks retornados pelo RAGFlow, mantendo o isolamento por usuário aplicado pelo Cloudflare Access e pelas API keys individuais do RAGFlow.

---

## 1. Instrução principal para o Codex

Implemente integralmente este spec no projeto existente.

Antes de alterar qualquer arquivo:

1. Leia o `app.py`, `requirements.txt`, `stack.yml`, `codex-config.toml.example` e `README.md`.
2. Preserve a autenticação atual do Cloudflare Access.
3. Preserve o mapeamento de identidade Cloudflare → API key RAGFlow.
4. Preserve o caráter somente leitura do MCP.
5. Não crie uma rota HTTP pública adicional para imagens.
6. Não aceite `image_id` diretamente como argumento público da ferramenta.
7. Execute validação sintática, testes automatizados e apresente um resumo final das alterações.

---

## 2. Contexto existente

O gateway atual expõe:

- `ragflow_whoami`
- `ragflow_list_datasets`
- `ragflow_retrieval`

O acesso funciona da seguinte forma:

```text
Codex
  → Cloudflare Access
  → JWT Cf-Access-Jwt-Assertion
  → identidade Cloudflare
  → Docker Secret
  → API key individual do RAGFlow
  → datasets permitidos ao usuário
```

A nova ferramenta deve usar exatamente a mesma identidade e API key já resolvidas pelo gateway.

---

## 3. Resultado esperado

Depois da implementação, o MCP deverá expor:

- `ragflow_whoami`
- `ragflow_list_datasets`
- `ragflow_retrieval`
- `ragflow_get_chunk_image`

O fluxo esperado será:

```text
1. Codex chama ragflow_retrieval.
2. Um chunk retornado contém image.available = true.
3. O resultado informa:
   - reference: FIG-001
   - tool: ragflow_get_chunk_image
   - arguments: dataset_id, document_id, chunk_id e reference
4. Codex chama ragflow_get_chunk_image.
5. O gateway:
   - revalida o dataset;
   - revalida o documento e o chunk;
   - obtém o image_id do chunk autorizado;
   - busca os bytes da imagem no RAGFlow;
   - normaliza a imagem;
   - retorna metadados + ImageContent MCP.
6. O Codex consegue analisar e citar a figura.
```

---

## 4. Base técnica do RAGFlow

Usar os seguintes endpoints internos do RAGFlow:

### 4.1 Consultar o chunk autorizado

```http
GET /api/v1/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}
Authorization: Bearer {API_KEY_DO_USUARIO}
```

Esse endpoint deve ser chamado antes da imagem para confirmar:

- dataset;
- documento;
- chunk;
- `img_id` ou `image_id`.

### 4.2 Recuperar os bytes da imagem

```http
GET /api/v1/documents/images/{image_id}
Authorization: Bearer {API_KEY_DO_USUARIO}
Accept: image/*
```

No RAGFlow, o identificador da imagem é composto pelo bucket/dataset e pelo nome do objeto. Em chunks criados com imagem, o padrão atual é equivalente a:

```python
img_id = f"{dataset_id}-{chunk_id}"
```

### Regra crítica de segurança

A ferramenta pública **não deve aceitar `image_id`**.

O cliente fornece apenas:

- `dataset_id`;
- `document_id`;
- `chunk_id`;
- `reference` opcional.

O gateway deve obter o `image_id` exclusivamente da resposta do endpoint de chunk autorizado.

Isso evita que um usuário tente montar manualmente um identificador de imagem pertencente a outro dataset.

---

## 5. Contrato da ferramenta MCP

### Nome

```text
ragflow_get_chunk_image
```

### Descrição

```text
Return the image attached to one authorized RAGFlow chunk.
Call this only with dataset_id, document_id and chunk_id returned by
ragflow_retrieval. The gateway revalidates dataset access and chunk ownership
before requesting the image bytes from RAGFlow.
```

### Entrada

```json
{
  "dataset_id": "string obrigatório",
  "document_id": "string obrigatório",
  "chunk_id": "string obrigatório",
  "reference": "FIG-001 opcional",
  "max_dimension": 1600,
  "output_format": "png"
}
```

### Restrições

- `dataset_id`, `document_id` e `chunk_id`: somente letras, números, `_` e `-`.
- Tamanho máximo de cada ID: 128 caracteres.
- `reference`: formato `FIG-001`, opcional.
- `max_dimension`: mínimo 256, máximo 4096.
- `output_format`: `png` ou `jpeg`.
- Não retornar a API key do RAGFlow.
- Não retornar o Client Secret do Cloudflare.
- Não registrar bytes ou base64 da imagem nos logs.

### Saída MCP

Retornar `CallToolResult` com dois conteúdos:

1. `TextContent` contendo JSON com os metadados;
2. `ImageContent` contendo a imagem em base64.

Exemplo conceitual:

```json
{
  "content": [
    {
      "type": "text",
      "text": "{\"reference\":\"FIG-001\",\"dataset_name\":\"Segurança\",\"document_name\":\"Manual.pdf\"}"
    },
    {
      "type": "image",
      "mimeType": "image/png",
      "data": "<base64>"
    }
  ]
}
```

---

## 6. Metadados obrigatórios

O `TextContent` deve incluir:

```json
{
  "reference": "FIG-001",
  "identity": "Edson",
  "dataset_id": "...",
  "dataset_name": "...",
  "document_id": "...",
  "document_name": "...",
  "chunk_id": "...",
  "image_id": "...",
  "positions": [],
  "declared_source_mime_type": "image/jpeg",
  "returned_mime_type": "image/png",
  "original_dimensions": {
    "width": 2480,
    "height": 3508
  },
  "returned_dimensions": {
    "width": 1131,
    "height": 1600
  }
}
```

O `image_id` pode aparecer na resposta como metadado técnico, mas nunca deve ser aceito como argumento de entrada.

---

## 7. Alteração do `ragflow_retrieval`

Cada chunk retornado deve ser enriquecido com:

```json
{
  "dataset_name": "Segurança do Trabalho",
  "document_name": "Manual.pdf",
  "source": {
    "dataset_id": "...",
    "dataset_name": "Segurança do Trabalho",
    "document_id": "...",
    "document_name": "Manual.pdf",
    "chunk_id": "...",
    "positions": []
  },
  "image": {
    "available": true,
    "reference": "FIG-001",
    "image_id": "...",
    "tool": "ragflow_get_chunk_image",
    "arguments": {
      "dataset_id": "...",
      "document_id": "...",
      "chunk_id": "...",
      "reference": "FIG-001"
    }
  }
}
```

Para chunks sem imagem:

```json
{
  "image": {
    "available": false
  }
}
```

### Comportamento esperado do Codex

Quando a imagem for relevante para a resposta, o Codex deve usar os argumentos fornecidos em `image.arguments` para chamar a ferramenta.

Não deve tentar inventar IDs.

---

## 8. Normalização da imagem

Usar Pillow para validar e normalizar os bytes retornados pelo RAGFlow.

### Regras

1. Rejeitar resposta vazia.
2. Rejeitar imagem maior que `RAGFLOW_MAX_IMAGE_BYTES`.
3. Rejeitar imagem com mais pixels que `RAGFLOW_MAX_IMAGE_PIXELS`.
4. Corrigir orientação EXIF.
5. Redimensionar mantendo proporção.
6. Nunca ampliar uma imagem menor.
7. Converter para PNG por padrão.
8. Para JPEG, converter modos incompatíveis para RGB.
9. Não confiar exclusivamente no `Content-Type` retornado pelo RAGFlow.
10. Validar os bytes com Pillow.

### Variáveis de ambiente

Adicionar:

```text
RAGFLOW_MAX_IMAGE_BYTES=8388608
RAGFLOW_MAX_IMAGE_PIXELS=40000000
RAGFLOW_IMAGE_MAX_DIMENSION=1600
```

---

## 9. Dependência

Adicionar a `requirements.txt`:

```text
Pillow>=11,<13
```

Manter:

```text
mcp>=1.27,<2
```

A implementação utiliza:

```python
from mcp.types import CallToolResult, ImageContent, TextContent
```

---

## 10. Implementação de referência

Aplique o seguinte diff ao `app.py`.

> Esta implementação de referência foi validada com `python -m py_compile`.
> Ela ainda deve ser testada em execução contra a instância real do RAGFlow.

```diff
--- a/app.py	2026-07-22 15:17:18.000000000 +0000
+++ b/app.py	2026-07-22 18:01:23.536518195 +0000
@@ -1,18 +1,24 @@
 from __future__ import annotations

+import base64
 import contextlib
 import contextvars
+import io
 import json
 import logging
 import os
 from dataclasses import dataclass
 from pathlib import Path
-from typing import Any
+from typing import Any, Literal
+from urllib.parse import quote

 import httpx
 import jwt
 from jwt import PyJWKClient
 from mcp.server.fastmcp import FastMCP
+from mcp.types import CallToolResult, ImageContent, TextContent
+from PIL import Image as PILImage
+from PIL import ImageOps, UnidentifiedImageError
 from starlette.applications import Starlette
 from starlette.datastructures import Headers
 from starlette.responses import JSONResponse
@@ -55,6 +61,9 @@
 REQUEST_TIMEOUT_SECONDS = float(os.getenv("RAGFLOW_REQUEST_TIMEOUT_SECONDS", "90"))
 MAX_PAGE_SIZE = min(int(os.getenv("RAGFLOW_MAX_PAGE_SIZE", "100")), 100)
 MAX_RESULT_PAGE_SIZE = min(int(os.getenv("RAGFLOW_MAX_RESULT_PAGE_SIZE", "50")), 100)
+MAX_IMAGE_BYTES = int(os.getenv("RAGFLOW_MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))
+MAX_IMAGE_PIXELS = int(os.getenv("RAGFLOW_MAX_IMAGE_PIXELS", "40000000"))
+DEFAULT_IMAGE_MAX_DIMENSION = int(os.getenv("RAGFLOW_IMAGE_MAX_DIMENSION", "1600"))

 CF_ACCESS_ISSUER = f"https://{CF_ACCESS_TEAM_DOMAIN}"
 CF_ACCESS_CERTS_URL = f"{CF_ACCESS_ISSUER}/cdn-cgi/access/certs"
@@ -265,6 +274,155 @@
     return payload


+
+async def ragflow_binary_request(
+    path: str,
+    *,
+    accept: str = "application/octet-stream",
+) -> tuple[bytes, str]:
+    response = await http_client.request(
+        "GET",
+        path,
+        headers={**ragflow_headers(), "Accept": accept},
+    )
+
+    if response.status_code == 401:
+        raise PermissionError("The mapped RAGFlow API key is invalid or revoked")
+    if response.status_code == 403:
+        raise PermissionError("RAGFlow denied access for this user")
+    if response.status_code >= 400:
+        message = None
+        try:
+            payload = response.json()
+            if isinstance(payload, dict):
+                message = payload.get("message")
+        except ValueError:
+            pass
+        raise RuntimeError(message or f"RAGFlow binary request failed (HTTP {response.status_code})")
+
+    declared_size = response.headers.get("content-length")
+    if declared_size and declared_size.isdigit() and int(declared_size) > MAX_IMAGE_BYTES:
+        raise ValueError("The RAGFlow image exceeds the configured size limit")
+
+    data = response.content
+    if not data:
+        raise RuntimeError("RAGFlow returned an empty image")
+    if len(data) > MAX_IMAGE_BYTES:
+        raise ValueError("The RAGFlow image exceeds the configured size limit")
+
+    return data, response.headers.get("content-type", "application/octet-stream")
+
+
+def validate_ragflow_id(name: str, value: str) -> str:
+    normalized = str(value or "").strip()
+    if not normalized:
+        raise ValueError(f"{name} must not be empty")
+    if len(normalized) > 128 or not all(ch.isalnum() or ch in "-_" for ch in normalized):
+        raise ValueError(f"{name} contains invalid characters")
+    return normalized
+
+
+def path_segment(value: str) -> str:
+    return quote(value, safe="")
+
+
+async def get_accessible_dataset_map() -> dict[str, dict[str, Any]]:
+    datasets = await fetch_accessible_datasets()
+    return {
+        str(item["id"]): item
+        for item in datasets
+        if item.get("id") is not None
+    }
+
+
+async def require_accessible_dataset(dataset_id: str) -> dict[str, Any]:
+    dataset_map = await get_accessible_dataset_map()
+    dataset = dataset_map.get(dataset_id)
+    if dataset is None:
+        raise PermissionError("The requested dataset is not accessible to the authenticated user")
+    return dataset
+
+
+async def fetch_authorized_chunk(
+    dataset_id: str,
+    document_id: str,
+    chunk_id: str,
+) -> dict[str, Any]:
+    payload = await ragflow_request(
+        "GET",
+        (
+            f"/datasets/{path_segment(dataset_id)}"
+            f"/documents/{path_segment(document_id)}"
+            f"/chunks/{path_segment(chunk_id)}"
+        ),
+    )
+    chunk = payload.get("data")
+    if not isinstance(chunk, dict) or not chunk:
+        raise RuntimeError("Chunk not found or not accessible")
+
+    returned_dataset_id = str(chunk.get("dataset_id") or chunk.get("kb_id") or "")
+    returned_document_id = str(chunk.get("document_id") or chunk.get("doc_id") or "")
+    returned_chunk_id = str(chunk.get("id") or chunk.get("chunk_id") or chunk_id)
+
+    if returned_dataset_id and returned_dataset_id != dataset_id:
+        raise PermissionError("The chunk does not belong to the requested dataset")
+    if returned_document_id and returned_document_id != document_id:
+        raise PermissionError("The chunk does not belong to the requested document")
+    if returned_chunk_id and returned_chunk_id != chunk_id:
+        raise PermissionError("The returned chunk does not match the requested chunk")
+
+    return chunk
+
+
+def encode_mcp_image(
+    raw_data: bytes,
+    *,
+    output_format: Literal["png", "jpeg"],
+    max_dimension: int,
+) -> tuple[bytes, str, dict[str, Any]]:
+    try:
+        with PILImage.open(io.BytesIO(raw_data)) as source:
+            original_width, original_height = source.size
+            if original_width <= 0 or original_height <= 0:
+                raise ValueError("Invalid image dimensions")
+            if original_width * original_height > MAX_IMAGE_PIXELS:
+                raise ValueError("The RAGFlow image exceeds the configured pixel limit")
+
+            source.load()
+            image = ImageOps.exif_transpose(source)
+            image.thumbnail((max_dimension, max_dimension))
+            returned_width, returned_height = image.size
+
+            buffer = io.BytesIO()
+            if output_format == "jpeg":
+                if image.mode not in {"RGB", "L"}:
+                    image = image.convert("RGB")
+                image.save(buffer, format="JPEG", quality=90, optimize=True)
+                mime_type = "image/jpeg"
+            else:
+                if image.mode == "P":
+                    image = image.convert("RGBA")
+                image.save(buffer, format="PNG", optimize=True)
+                mime_type = "image/png"
+
+            encoded = buffer.getvalue()
+            if len(encoded) > MAX_IMAGE_BYTES:
+                raise ValueError("The normalized image exceeds the configured size limit")
+
+            return encoded, mime_type, {
+                "original_dimensions": {
+                    "width": original_width,
+                    "height": original_height,
+                },
+                "returned_dimensions": {
+                    "width": returned_width,
+                    "height": returned_height,
+                },
+            }
+    except UnidentifiedImageError as exc:
+        raise ValueError("RAGFlow returned bytes that are not a supported image") from exc
+
+
 async def fetch_accessible_datasets() -> list[dict[str, Any]]:
     datasets: list[dict[str, Any]] = []
     page = 1
@@ -437,16 +595,163 @@
         len(selected_ids),
         len(data.get("chunks") or []),
     )
+    raw_chunks = data.get("chunks") or []
+    dataset_map = {
+        str(item["id"]): item
+        for item in accessible
+        if item.get("id") is not None
+    }
+    chunks: list[dict[str, Any]] = []
+    figure_counter = 0
+    for raw_chunk in raw_chunks:
+        if not isinstance(raw_chunk, dict):
+            continue
+        chunk = dict(raw_chunk)
+        chunk_dataset_id = str(chunk.get("dataset_id") or chunk.get("kb_id") or "")
+        chunk_document_id = str(chunk.get("document_id") or chunk.get("doc_id") or "")
+        chunk_id = str(chunk.get("id") or chunk.get("chunk_id") or "")
+        image_id = str(chunk.get("image_id") or chunk.get("img_id") or "").strip()
+        dataset_meta = dataset_map.get(chunk_dataset_id, {})
+
+        chunk["dataset_name"] = dataset_meta.get("name") or chunk.get("dataset_name")
+        chunk["document_name"] = (
+            chunk.get("document_name")
+            or chunk.get("document_keyword")
+            or chunk.get("docnm_kwd")
+        )
+        chunk["source"] = {
+            "dataset_id": chunk_dataset_id or None,
+            "dataset_name": chunk.get("dataset_name"),
+            "document_id": chunk_document_id or None,
+            "document_name": chunk.get("document_name"),
+            "chunk_id": chunk_id or None,
+            "positions": chunk.get("positions") or chunk.get("position_int") or [],
+        }
+
+        if image_id and chunk_dataset_id and chunk_document_id and chunk_id:
+            figure_counter += 1
+            figure_reference = f"FIG-{figure_counter:03d}"
+            chunk["image"] = {
+                "available": True,
+                "reference": figure_reference,
+                "image_id": image_id,
+                "tool": "ragflow_get_chunk_image",
+                "arguments": {
+                    "dataset_id": chunk_dataset_id,
+                    "document_id": chunk_document_id,
+                    "chunk_id": chunk_id,
+                    "reference": figure_reference,
+                },
+            }
+        else:
+            chunk["image"] = {"available": False}
+
+        chunks.append(chunk)
+
     return {
         "identity": ctx.display_name,
         "question": question,
         "dataset_ids": selected_ids,
-        "chunks": data.get("chunks") or [],
+        "chunks": chunks,
         "total": data.get("total"),
         "doc_aggs": data.get("doc_aggs") or [],
     }


+@mcp.tool(structured_output=False)
+async def ragflow_get_chunk_image(
+    dataset_id: str,
+    document_id: str,
+    chunk_id: str,
+    reference: str | None = None,
+    max_dimension: int = DEFAULT_IMAGE_MAX_DIMENSION,
+    output_format: Literal["png", "jpeg"] = "png",
+) -> CallToolResult:
+    """
+    Return the image attached to one authorized RAGFlow chunk.
+
+    Call this only with dataset_id, document_id and chunk_id returned by
+    ragflow_retrieval. The gateway revalidates dataset access and chunk ownership
+    before requesting the image bytes from RAGFlow.
+    """
+    dataset_id = validate_ragflow_id("dataset_id", dataset_id)
+    document_id = validate_ragflow_id("document_id", document_id)
+    chunk_id = validate_ragflow_id("chunk_id", chunk_id)
+
+    if reference is not None:
+        reference = reference.strip().upper()
+        if (
+            len(reference) > 32
+            or not reference.startswith("FIG-")
+            or not reference[4:].isdigit()
+        ):
+            raise ValueError("reference must use the format FIG-001")
+
+    if not 256 <= max_dimension <= 4096:
+        raise ValueError("max_dimension must be between 256 and 4096")
+    if output_format not in {"png", "jpeg"}:
+        raise ValueError("output_format must be 'png' or 'jpeg'")
+
+    ctx = current_identity()
+    dataset = await require_accessible_dataset(dataset_id)
+    chunk = await fetch_authorized_chunk(dataset_id, document_id, chunk_id)
+    image_id = str(chunk.get("image_id") or chunk.get("img_id") or "").strip()
+    if not image_id:
+        raise ValueError("The requested chunk does not contain an image")
+
+    raw_image, declared_mime_type = await ragflow_binary_request(
+        f"/documents/images/{path_segment(image_id)}",
+        accept="image/*",
+    )
+    image_data, mime_type, dimensions = encode_mcp_image(
+        raw_image,
+        output_format=output_format,
+        max_dimension=max_dimension,
+    )
+
+    metadata = {
+        "reference": reference or f"FIG-{chunk_id[:8].upper()}",
+        "identity": ctx.display_name,
+        "dataset_id": dataset_id,
+        "dataset_name": dataset.get("name"),
+        "document_id": document_id,
+        "document_name": (
+            chunk.get("document_name")
+            or chunk.get("document_keyword")
+            or chunk.get("docnm_kwd")
+        ),
+        "chunk_id": chunk_id,
+        "image_id": image_id,
+        "positions": chunk.get("positions") or chunk.get("position_int") or [],
+        "declared_source_mime_type": declared_mime_type,
+        "returned_mime_type": mime_type,
+        **dimensions,
+    }
+
+    logger.info(
+        "Returned chunk image identity=%s dataset_id=%s document_id=%s chunk_id=%s bytes=%d",
+        ctx.identity,
+        dataset_id,
+        document_id,
+        chunk_id,
+        len(image_data),
+    )
+
+    return CallToolResult(
+        content=[
+            TextContent(
+                type="text",
+                text=json.dumps(metadata, ensure_ascii=False),
+            ),
+            ImageContent(
+                type="image",
+                data=base64.b64encode(image_data).decode("ascii"),
+                mimeType=mime_type,
+            ),
+        ]
+    )
+
+
 async def healthz(request) -> JSONResponse:
     return JSONResponse(
         {
```

---

## 11. Alteração em `requirements.txt`

```diff
--- a/requirements.txt
+++ b/requirements.txt
@@
 mcp>=1.27,<2
 httpx>=0.28,<1
 PyJWT[crypto]>=2.10,<3
+Pillow>=11,<13
 starlette>=0.47,<1
 uvicorn[standard]>=0.35,<1
```

---

## 12. Alteração em `stack.yml`

Adicionar ao bloco `environment`:

```yaml
RAGFLOW_MAX_IMAGE_BYTES: "8388608"
RAGFLOW_MAX_IMAGE_PIXELS: "40000000"
RAGFLOW_IMAGE_MAX_DIMENSION: "1600"
```

Não publicar porta adicional.

Não adicionar acesso direto ao MinIO.

O gateway deve continuar acessando apenas a API interna do RAGFlow.

---

## 13. Alteração no Codex

Atualizar `codex-config.toml.example`:

```toml
enabled_tools = [
  "ragflow_whoami",
  "ragflow_list_datasets",
  "ragflow_retrieval",
  "ragflow_get_chunk_image",
]
```

---

## 14. Instruções MCP recomendadas

Atualizar as instruções do `FastMCP` para orientar o cliente:

```python
instructions=(
    "Read-only access to the RAGFlow datasets authorized for the authenticated "
    "Cloudflare Access identity. Never infer access to a dataset that is not "
    "returned by ragflow_list_datasets. When ragflow_retrieval returns a chunk "
    "with image.available=true and the figure is relevant, call "
    "ragflow_get_chunk_image using exactly the arguments provided in "
    "image.arguments. Cite the returned figure using image.reference."
)
```

---

## 15. Testes obrigatórios

Criar testes automatizados para os cenários abaixo.

### 15.1 Chunk com imagem

Dado um chunk autorizado com:

```json
{
  "id": "chunk-a",
  "kb_id": "dataset-a",
  "doc_id": "document-a",
  "img_id": "dataset-a-chunk-a"
}
```

Quando `ragflow_get_chunk_image` for chamado, deve retornar:

- um `TextContent`;
- um `ImageContent`;
- `mimeType` igual a `image/png` por padrão;
- referência informada;
- dataset/documento/chunk corretos.

### 15.2 Chunk sem imagem

Deve retornar erro claro:

```text
The requested chunk does not contain an image
```

### 15.3 Dataset não autorizado

Usuário A tenta usar dataset do usuário B.

Resultado:

```text
The requested dataset is not accessible to the authenticated user
```

O endpoint `/documents/images/...` não deve ser chamado.

### 15.4 Documento divergente

O endpoint de chunk retorna `doc_id` diferente.

Resultado: acesso negado.

### 15.5 Chunk divergente

O endpoint de chunk retorna um ID diferente.

Resultado: acesso negado.

### 15.6 Imagem inexistente

RAGFlow retorna 404, erro JSON ou conteúdo vazio.

A ferramenta deve retornar erro controlado sem traceback ou segredo.

### 15.7 MIME incorreto

RAGFlow declara `image/jpeg`, mas os bytes são PNG.

A ferramenta deve detectar e retornar PNG normalizado.

### 15.8 Limite de bytes

Imagem acima de `RAGFLOW_MAX_IMAGE_BYTES` deve ser rejeitada.

### 15.9 Limite de pixels

Imagem acima de `RAGFLOW_MAX_IMAGE_PIXELS` deve ser rejeitada antes da decodificação completa.

### 15.10 IDs inválidos

Entradas contendo `/`, `\`, `..`, espaços ou caracteres especiais devem ser rejeitadas.

### 15.11 Isolamento por usuário

Executar teste de integração com duas identidades:

```text
Usuário A → dataset A → imagem A
Usuário B → dataset B → imagem B
```

Confirmar:

- A recupera imagem A;
- A não recupera imagem B;
- B recupera imagem B;
- revogar o token A não afeta B.

---

## 16. Critérios de aceite

A implementação será considerada concluída quando:

- [ ] `ragflow_get_chunk_image` aparecer em `tools/list`;
- [ ] `ragflow_retrieval` marcar corretamente chunks com imagem;
- [ ] o Codex receber um conteúdo MCP do tipo imagem;
- [ ] a referência `FIG-001` for preservada entre retrieval e imagem;
- [ ] o dataset for revalidado;
- [ ] o documento for revalidado;
- [ ] o chunk for revalidado;
- [ ] nenhum `image_id` arbitrário for aceito do cliente;
- [ ] imagens grandes forem limitadas;
- [ ] MIME e bytes forem normalizados;
- [ ] os testes de isolamento passarem;
- [ ] nenhuma credencial aparecer em logs;
- [ ] o MCP continuar somente leitura;
- [ ] `python -m py_compile app.py` passar;
- [ ] o container iniciar;
- [ ] `codex mcp get ragflow --json` mostrar a nova ferramenta;
- [ ] um teste real com PDF contendo figura funcionar.

---

## 17. Comandos de validação

```bash
python -m py_compile app.py
```

```bash
docker build -t SEU_REGISTRY/ragflow-mcp-cloudflare-gateway:1.1.0 .
docker push SEU_REGISTRY/ragflow-mcp-cloudflare-gateway:1.1.0
```

```bash
docker stack deploy -c stack.yml ragflow_mcp
docker service logs -f ragflow_mcp_ragflow-mcp-gateway
```

No Windows/Codex:

```powershell
codex mcp list
codex mcp get ragflow --json
```

Teste dentro do Codex:

```text
Pesquise no RAGFlow documentos relacionados a segurança do trabalho.
Quando um resultado tiver image.available=true, carregue a figura usando
ragflow_get_chunk_image e explique o que ela mostra. Cite o dataset, documento,
chunk e a referência FIG retornada.
```

---

## 18. Fora de escopo

Não implementar neste trabalho:

- upload de imagens;
- edição de chunks;
- exclusão de imagens;
- geração de URL pública;
- acesso direto ao MinIO;
- cache persistente de imagens;
- OCR adicional;
- alteração do modelo de permissões do RAGFlow;
- autenticação diferente do Cloudflare Access atual.

---

## 19. Entrega esperada do Codex

Ao finalizar, apresentar:

1. arquivos alterados;
2. resumo das decisões;
3. testes executados;
4. resultado dos testes;
5. limitações encontradas;
6. instruções de build e deploy;
7. confirmação de que nenhuma credencial foi adicionada ao código ou aos logs.
