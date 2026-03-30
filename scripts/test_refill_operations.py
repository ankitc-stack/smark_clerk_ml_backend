#!/usr/bin/env python
"""Comprehensive test for refill operations: rewrite, expand, delete, add.

This script:
1. Creates a GOI_LETTER document via /fill
2. Tests sequential refill operations:
   - Rewrite a paragraph to be more concise
   - Expand a paragraph with more detail
   - Delete a paragraph
   - Add a new paragraph
3. Prints before/after comparisons.

Run with the local backend running on http://localhost:8000.
"""
import json
import urllib.request
import urllib.error
import sys
import os

sys.path.append(os.getcwd())
BASE = "http://localhost:8000"


def api_get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}") as resp:
        return json.loads(resp.read())


def api_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()}


def get_body_paras(filled: dict) -> list[str]:
    """Extract numbered paragraphs from filled skeleton."""
    sections = filled.get("sections", [])
    body_sec = next((s for s in sections if s.get("type") == "numbered_paragraphs"), {})
    items = (body_sec.get("content") or {}).get("items") or []
    return [it.get("text", "") for it in items]


def print_paras(paras: list[str], label: str = "Paragraphs") -> None:
    """Pretty-print list of paragraphs."""
    print(f"\n{label}:")
    for i, p in enumerate(paras, 1):
        preview = p[:80] + "..." if len(p) > 80 else p
        print(f"  {i}. {preview}")


def print_full_paras(paras: list[str], label: str = "Paragraphs") -> None:
    """Print full paragraph text (untruncated)."""
    print(f"\n{label}:")
    for i, p in enumerate(paras, 1):
        print(f"  {i}. {p}")


def print_response_info(resp: dict, label: str = "") -> None:
    """Print version, warnings, and error info from response."""
    if label:
        print(f"\n{label}:")
    if "version_id" in resp:
        print(f"  Version: {resp['version_id']}")
    if "warnings" in resp and resp["warnings"]:
        print(f"  Warnings:")
        for w in resp["warnings"]:
            print(f"    - {w}")
    if "error" in resp:
        print(f"  ERROR: {resp['error']}")


def main() -> None:
    print("="*70)
    print("REFILL OPERATIONS TEST")
    print("="*70)

    # Step 1: Create document
    print("\n[1] Creating GOI_LETTER document...")
    skel_resp = api_get("/templates/skeleton?doc_type=GOI_LETTER")
    skeleton = skel_resp["skeleton"]

    fill_prompt = (
        "GOI letter to Secy MoD about Annual Confidential Report of Army Officers "
        "for the year 2025-26, dated 28 Feb 2026, Ref No B/45678/AG/2026"
    )
    fill_resp = api_post("/documents/fill", {
        "user_id": "test_user",
        "doc_type": "GOI_LETTER",
        "prompt": fill_prompt,
        "skeleton": skeleton,
    })

    if "error" in fill_resp:
        print(f"ERROR: {fill_resp['error']}")
        return

    doc_id = fill_resp["document_id"]
    version = fill_resp["version_id"]
    print(f"✓ Created document {doc_id} version {version}")

    # Show initial state
    initial_paras = get_body_paras(fill_resp.get("filled", {}))
    print_full_paras(initial_paras, "Initial paragraphs")

    # Step 2: Rewrite paragraph 1 to be more concise
    print("\n[2] Testing REWRITE: Make paragraph 1 more concise...")
    rewrite_resp = api_post(f"/documents/{doc_id}/refill", {
        "prompt": "Rewrite paragraph 1 to be more concise and direct.",
        "version": version,
        "user_id": "test_user",
    })

    if "error" in rewrite_resp:
        print(f"ERROR: {rewrite_resp['error']}")
        return

    print_response_info(rewrite_resp)
    version = rewrite_resp.get("version_id")
    rewritten_paras = get_body_paras(rewrite_resp.get("filled", {}))
    print_full_paras(rewritten_paras, "After rewrite")

    # Step 3: Expand paragraph 2 with more detail
    print("\n[3] Testing EXPAND: Add more detail to paragraph 2...")
    expand_resp = api_post(f"/documents/{doc_id}/refill", {
        "prompt": "Expand paragraph 2 with more specific details and context.",
        "version": version,
        "user_id": "test_user",
    })

    if "error" in expand_resp:
        print(f"ERROR: {expand_resp['error']}")
        return

    print_response_info(expand_resp)
    version = expand_resp.get("version_id")
    expanded_paras = get_body_paras(expand_resp.get("filled", {}))
    print_full_paras(expanded_paras, "After expand")

    # Step 4: Add a paragraph about administrative procedures
    print("\n[4] Testing ADD: Insert paragraph about administrative procedures...")
    add_resp = api_post(f"/documents/{doc_id}/refill", {
        "prompt": "Add another paragraph about the administrative procedures required for submission.",
        "version": version,
        "user_id": "test_user",
    })

    if "error" in add_resp:
        print(f"ERROR: {add_resp['error']}")
        return

    print_response_info(add_resp)
    version = add_resp.get("version_id")
    after_add = get_body_paras(add_resp.get("filled", {}))
    print_full_paras(after_add, "After add")
    print(f"({len(after_add)} paras total)")

    # Step 5: Delete paragraph 2
    print("\n[5] Testing DELETE: Remove paragraph 2...")
    delete_resp = api_post(f"/documents/{doc_id}/refill", {
        "prompt": "Delete paragraph 2.",
        "version": version,
        "user_id": "test_user",
    })

    if "error" in delete_resp:
        print(f"ERROR: {delete_resp['error']}")
        return

    print_response_info(delete_resp)
    version = delete_resp.get("version_id")
    after_delete = get_body_paras(delete_resp.get("filled", {}))
    print_full_paras(after_delete, "After delete")
    print(f"({len(after_delete)} paras total)")

    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    print(f"Initial state:     {len(initial_paras)} paragraphs")
    print(f"After rewrite:     {len(rewritten_paras)} paragraphs (same structure)")
    print(f"After expand:      {len(expanded_paras)} paragraphs (same structure)")
    print(f"After add:         {len(after_add)} paragraphs (+1)")
    print(f"After delete:      {len(after_delete)} paragraphs (-1)")
    print(f"\nFinal document:    {doc_id} version {version}")
    print("="*70)


if __name__ == "__main__":
    main()
