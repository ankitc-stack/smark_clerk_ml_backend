#!/usr/bin/env python
"""Simple smoke test for /documents/{id}/refill endpoint.

This script:
1. Fetches a skeleton for GOI_LETTER
2. Fills it using a sample prompt
3. Submits a refill request to modify the subject or a paragraph
4. Prints results for inspection.

Run with the local backend running on http://localhost:8000.
"""
import json
import urllib.request
import urllib.error
import sys
import os
# ensure project root on path so we can import app modules
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


def main() -> None:
    print("Fetching GOI_LETTER skeleton...")
    skel_resp = api_get("/templates/skeleton?doc_type=GOI_LETTER")
    skeleton = skel_resp["skeleton"]

    prompt = (
        "GOI letter to Secy MoD about Annual Confidential Report of Army Officers "
        "for the year 2025-26, dated 28 Feb 2026, Ref No B/45678/AG/2026"
    )
    print("Creating new document via /documents/fill")
    payload = {
        "user_id": "test_user",
        "doc_type": "GOI_LETTER",
        "prompt": prompt,
        "skeleton": skeleton,
    }
    fill_resp = api_post("/documents/fill", payload)
    if "error" in fill_resp:
        print("Fill error:", fill_resp["error"])
        return

    doc_id = fill_resp["document_id"]
    version = fill_resp["version_id"]
    print(f"Filled document {doc_id} version {version}")

    # perform a refill trying a more explicit paragraph text
    edit_prompt = "Add another paragraph: \"Travel arrangements will be coordinated with HQ.\""
    refill_payload = {"prompt": edit_prompt, "version": version, "user_id": "test_user"}
    # first, compare with a subject-change instruction
    from app.ml.ollama_client import ollama_chat
    import asyncio
    edit_system = open(r"c:\Users\ankit\Downloads\army_smart_clerk_backend_tailored\data\prompt_library\system_prompts\fill_edit_v1.txt").read()
    current_filled = fill_resp.get("filled") or {}
    current_filled_str = json.dumps(current_filled, ensure_ascii=False)

    debug_prompts = [
        ("Subject change", "Change the subject line to mention Exercise Vijay."),
        ("Add travel para", edit_prompt),
    ]
    for label, instr in debug_prompts:
        print(f"\n=== Debug LLM call: {label} ===")
        user_msg = f"""DOC_TYPE: GOI_LETTER

EDIT_INSTRUCTION:
{instr}

RULES_CONTEXT (apply where relevant):

CURRENT_DOCUMENT_JSON (edit ONLY text fields; return complete JSON):
{current_filled_str}
"""
        print(user_msg[:500], "...\n")
        raw_output = asyncio.run(ollama_chat(edit_system, user_msg))
        print("raw output length:", len(raw_output))
        # show beginning and end portions for inspection
        print("raw output start:\n", raw_output[:1000])
        print("raw output end:\n", raw_output[-1000:])

    # now call the actual refill request
    print("Calling /documents/{}/refill with explicit travel text".format(doc_id))
    refill_resp = api_post(f"/documents/{doc_id}/refill", refill_payload)
    if "error" in refill_resp:
        print("Refill error:", refill_resp["error"])
        return

    print("Refill succeeded, new version", refill_resp.get("version_id"))
    print("Updated slots:")
    slots = refill_resp.get("filled", {}).get("_slots", {})
    for k, v in slots.items():
        print(f"  {k}: {v}")

    print("New body paragraphs:")
    sections = refill_resp.get("filled", {}).get("sections", [])
    body_sec = next((s for s in sections if s.get("type") == "numbered_paragraphs"), {})
    items = (body_sec.get("content") or {}).get("items") or []
    for i, it in enumerate(items, 1):
        print(f"  {i}. {it.get('text')}")

    for i, it in enumerate(items, 1):
        print(f"  {i}. {it.get('text')}")


if __name__ == "__main__":
    main()
