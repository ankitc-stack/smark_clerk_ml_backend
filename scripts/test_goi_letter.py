#!/usr/bin/env python
"""Test GOI letter generation via API."""
import json
import urllib.request
import urllib.error

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


def run_test(label: str, prompt: str, skeleton: dict) -> None:
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"PROMPT: {prompt}")
    print("=" * 60)

    payload = {
        "user_id": "test_user",
        "doc_type": "GOI_LETTER",
        "prompt": prompt,
        "skeleton": skeleton,
    }
    data = api_post("/documents/fill", payload)

    if "error" in data:
        print(f"ERROR: {data['error']}")
        return

    filled_skel = data.get("filled") or {}

    # _slots: regex-extracted fields
    slots = filled_skel.get("_slots") or {}
    print("\n_SLOTS (regex extracted):")
    for k, v in slots.items():
        marker = "[Y]" if v else "[ ]"
        print(f"  {marker} {k}: {repr(v)}")

    # Body paragraphs from numbered_paragraphs section
    sections = filled_skel.get("sections") or []
    body_sec = next((s for s in sections if s.get("type") == "numbered_paragraphs"), None)
    paras = []
    if body_sec:
        items = (body_sec.get("content") or {}).get("items") or []
        paras = [it.get("text", "") for it in items if it.get("text")]
    print("\nBODY PARAS (from skeleton sections):")
    if paras:
        for i, p in enumerate(paras, 1):
            safe = p.encode("ascii", errors="replace").decode("ascii")
            print(f"  {i}. {safe}")
    else:
        print("  (empty)")

    warns = data.get("warnings", [])
    if warns:
        print("\nWARNINGS:")
        for w in warns:
            print(f"  ! {w}")


def main() -> None:
    print("Fetching GOI_LETTER skeleton...")
    skel_resp = api_get("/templates/skeleton?doc_type=GOI_LETTER")
    skeleton = skel_resp["skeleton"]
    print(f"  Template ID: {skel_resp.get('template_id')}")

    TESTS = [
        (
            "ACR Submission Request",
            "GOI letter to Secy MoD about Annual Confidential Report of Army Officers for the year 2025-26, dated 28 Feb 2026, Ref No B/45678/AG/2026",
        ),
        (
            "Parliament Session Inputs",
            "GOI letter requesting inputs for Parliament Session from all Directorates by 15 Mar 2026, reference No AG/12345/Parl/2026 dated 01 Mar 2026",
        ),
        (
            "Budget Allocation",
            "GOI letter regarding budget allocation for modernization of Army Signal Corps equipment for FY 2026-27, to JS Finance MoD",
        ),
    ]

    for label, prompt in TESTS:
        run_test(label, prompt, skeleton)

    print("\n\nDone.")


if __name__ == "__main__":
    main()
