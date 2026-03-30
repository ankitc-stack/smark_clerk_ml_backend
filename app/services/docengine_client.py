"""
app/services/docengine_client.py

HTTP client for the Doc-Engine microservice (smartclerk-backend-docengine-main).

The Doc-Engine runs as a separate FastAPI service (default :8001) and owns:
  - Blueprint-driven document structure & validation
  - Section catalog resolution
  - Deterministic patch application
  - Optimistic concurrency versioning
  - Audit logging

This module is a thin httpx wrapper.  All methods raise DocEngineError on
non-2xx responses.  Callers are responsible for mapping errors to API responses.

Usage (from app.state, set in lifespan):
    client: DocEngineClient = request.app.state.docengine
    result = await client.create_document("tpl_leave_cert_v1", {})
"""
from __future__ import annotations

import httpx
from typing import Any


class DocEngineError(Exception):
    """Raised when doc-engine returns a non-2xx response."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"DocEngine error {status_code}: {body}")


class DocEngineClient:
    """Async httpx client bound to a single doc-engine base URL."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _post(self, path: str, json: dict) -> dict:
        resp = await self._client.post(path, json=json)
        if not resp.is_success:
            raise DocEngineError(resp.status_code, resp.text)
        return resp.json()

    async def _patch(self, path: str, json: dict) -> dict:
        resp = await self._client.patch(path, json=json)
        if not resp.is_success:
            raise DocEngineError(resp.status_code, resp.text)
        return resp.json()

    async def _get(self, path: str) -> dict:
        resp = await self._client.get(path)
        if not resp.is_success:
            raise DocEngineError(resp.status_code, resp.text)
        return resp.json()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        """Return True if the doc-engine health endpoint responds OK."""
        try:
            await self._get("/health")
            return True
        except Exception:
            return False

    async def create_document(self, template_id: str, inputs: dict | None = None) -> dict:
        """
        POST /documents/
        Create a new blueprint document from a template.

        Returns the full DocumentResponse dict:
          { document_id, version, document_type, blueprint_id, data }
        """
        return await self._post("/documents/", {
            "template_id": template_id,
            "inputs": inputs or {},
        })

    async def get_document(self, doc_id: str) -> dict:
        """GET /documents/{doc_id} — fetch current document data (sections + version)."""
        resp = await self._client.get(f"/documents/{doc_id}")
        if resp.status_code == 404:
            raise DocEngineError(404, "Document not found")
        if resp.status_code >= 400:
            raise DocEngineError(resp.status_code, resp.text)
        return resp.json()

    async def apply_command(self, doc_id: str, version: int, action_obj: dict) -> dict:
        """
        POST /documents/{doc_id}/command
        Apply a structured ActionObject to a blueprint document.

        Returns either:
          { status: "applied",              version, updates }
          { status: "needs_clarification",  question, options }

        Raises DocEngineError on 404 (not found), 409 (version conflict),
        422 (blueprint validation error).
        """
        return await self._post(f"/documents/{doc_id}/command", {
            "version": version,
            "action": action_obj,
        })

    async def patch_section(
        self,
        doc_id: str,
        section_id: str,
        version: int,
        content: dict,
        alignment: str | None = None,
    ) -> dict:
        """
        PATCH /documents/{doc_id}/sections/{section_id}
        Replace the Lexical JSON content of a single section.

        `content` must be:
          { "richtext": { "format": "lexical", "state": { ... } } }

        `alignment` (optional): "left" | "center" | "right" — persisted on the section.

        Returns: { ok: true, version: <new_version> }

        Raises DocEngineError on 404, 409 (version conflict), 422 (blueprint rule).
        """
        body: dict = {"version": version, "content": content}
        if alignment is not None:
            body["alignment"] = alignment
        return await self._patch(f"/documents/{doc_id}/sections/{section_id}", body)
