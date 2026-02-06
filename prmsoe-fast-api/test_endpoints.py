"""
End-to-end test for all PRMSOE endpoints.

Usage:
    1. Start server:  python app.py
    2. Run tests:     python test_endpoints.py

Creates a test user in Supabase auth + profiles, then exercises the full flow:
  upload CSV → poll status → get drafts → send → feedback queue → swipe → analytics
"""

import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ.get("TEST_BASE_URL", "http://localhost:8000")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

sb_headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

TEST_EMAIL = "testuser@prmsoe-test.local"
TEST_PASSWORD = "testpass123456"

client = httpx.Client(base_url=BASE, timeout=30)


def log(label: str, data):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    if isinstance(data, dict):
        for k, v in data.items():
            print(f"  {k}: {v}")
    else:
        print(f"  {data}")


def setup_test_user() -> str:
    """Create (or retrieve) a test user in Supabase Auth + profiles table."""
    sb = httpx.Client(base_url=SUPABASE_URL, headers=sb_headers, timeout=15)

    # Try to create user via Auth admin API
    resp = sb.post("/auth/v1/admin/users", json={
        "email": TEST_EMAIL,
        "password": TEST_PASSWORD,
        "email_confirm": True,
    })

    if resp.status_code == 200:
        user_id = resp.json()["id"]
        log("Created test user", {"user_id": user_id, "email": TEST_EMAIL})
    elif resp.status_code == 422:
        # User already exists — fetch by email
        resp2 = sb.get("/auth/v1/admin/users")
        users = resp2.json().get("users", [])
        user_id = next(u["id"] for u in users if u.get("email") == TEST_EMAIL)
        log("Found existing test user", {"user_id": user_id})
    else:
        print(f"Failed to create user: {resp.status_code} {resp.text}")
        sys.exit(1)

    # Ensure profiles row exists
    resp = sb.get(
        "/rest/v1/profiles",
        params={"id": f"eq.{user_id}", "select": "id"},
        headers={**sb_headers, "Prefer": "return=representation"},
    )
    if not resp.json():
        sb.post(
            "/rest/v1/profiles",
            json={
                "id": user_id,
                "mission_statement": "I want to reduce food waste in urban areas",
                "intent_type": "VALIDATION",
            },
            headers={**sb_headers, "Prefer": "return=representation"},
        )
        log("Created profile", {"mission": "I want to reduce food waste in urban areas"})

    sb.close()
    return user_id


def cleanup_test_data(user_id: str):
    """Remove test data from previous runs (contacts, research, jobs, outreach)."""
    sb = httpx.Client(base_url=SUPABASE_URL, headers=sb_headers, timeout=15)

    # Get contact IDs for this user
    resp = sb.get("/rest/v1/contacts", params={"user_id": f"eq.{user_id}", "select": "id"})
    contact_ids = [c["id"] for c in resp.json()]

    if contact_ids:
        # Delete outreach_attempts for these contacts
        for cid in contact_ids:
            sb.delete("/rest/v1/outreach_attempts", params={"contact_id": f"eq.{cid}"})
        # Delete research for these contacts
        for cid in contact_ids:
            sb.delete("/rest/v1/research", params={"contact_id": f"eq.{cid}"})
        # Delete contacts
        sb.delete("/rest/v1/contacts", params={"user_id": f"eq.{user_id}"})

    # Delete enrichment jobs
    sb.delete("/rest/v1/enrichment_jobs", params={"user_id": f"eq.{user_id}"})

    sb.close()
    log("Cleanup", f"Removed {len(contact_ids)} contacts + related data")


def test_upload(user_id: str) -> dict:
    """POST /ingest/upload"""
    with open("test.csv", "rb") as f:
        resp = client.post(
            "/ingest/upload",
            data={"user_id": user_id},
            files={"file": ("test.csv", f, "text/csv")},
        )
    assert resp.status_code == 200, f"Upload failed: {resp.status_code} {resp.text}"
    data = resp.json()
    log("POST /ingest/upload", data)
    assert data["contacts_created"] > 0, "No contacts created"
    assert "job_id" in data
    return data


def test_status(job_id: str, user_id: str) -> dict:
    """GET /ingest/status/{job_id} — poll until complete."""
    log("Polling GET /ingest/status", f"job_id={job_id}")
    for i in range(60):  # max 2 minutes
        resp = client.get(f"/ingest/status/{job_id}", params={"user_id": user_id})
        assert resp.status_code == 200, f"Status failed: {resp.status_code} {resp.text}"
        data = resp.json()
        status = data["status"]
        processed = data["processed_count"]
        total = data["total_contacts"]
        failed = data["failed_count"]
        print(f"  [{i}] status={status} processed={processed}/{total} failed={failed}")
        if status in ("COMPLETED", "FAILED"):
            log("Job finished", data)
            return data
        time.sleep(2)
    raise TimeoutError("Job did not complete in 2 minutes")


def test_drafts(user_id: str) -> list:
    """GET /feed/drafts"""
    resp = client.get("/feed/drafts", params={"user_id": user_id, "limit": 20, "offset": 0})
    assert resp.status_code == 200, f"Drafts failed: {resp.status_code} {resp.text}"
    data = resp.json()
    log("GET /feed/drafts", {
        "total": data["total"],
        "has_more": data["has_more"],
        "drafts_returned": len(data["drafts"]),
    })
    for d in data["drafts"]:
        print(f"  - {d['full_name']} @ {d['company_name']}")
        print(f"    strategy: {d['strategy_tag']}")
        print(f"    draft: {d['draft_message'][:100]}...")
        print(f"    research: {d['research']['news_summary'][:80]}...")
    return data["drafts"]


def test_send(draft: dict) -> dict:
    """POST /action/send"""
    resp = client.post("/action/send", json={
        "contact_id": draft["contact_id"],
        "message_body": draft["draft_message"],
        "strategy_tag": draft["strategy_tag"] or "DIRECT_PITCH",
    })
    assert resp.status_code == 200, f"Send failed: {resp.status_code} {resp.text}"
    data = resp.json()
    log("POST /action/send", data)
    assert "outreach_id" in data
    assert "feedback_due_at" in data
    return data


def test_feedback_queue(user_id: str) -> list:
    """GET /feedback/queue"""
    resp = client.get("/feedback/queue", params={"user_id": user_id})
    assert resp.status_code == 200, f"Queue failed: {resp.status_code} {resp.text}"
    data = resp.json()
    log("GET /feedback/queue", {"pending_count": len(data["pending"])})
    for p in data["pending"]:
        print(f"  - {p['full_name']} @ {p['company_name']} (sent: {p['sent_at']})")
    return data["pending"]


def test_swipe(outreach_id: str):
    """POST /feedback/swipe"""
    resp = client.post("/feedback/swipe", json={
        "outreach_id": outreach_id,
        "outcome": "REPLIED",
    })
    assert resp.status_code == 200, f"Swipe failed: {resp.status_code} {resp.text}"
    data = resp.json()
    log("POST /feedback/swipe", data)
    assert data["ok"] is True


def test_analytics(user_id: str):
    """GET /analytics/dashboard"""
    resp = client.get("/analytics/dashboard", params={"user_id": user_id})
    assert resp.status_code == 200, f"Analytics failed: {resp.status_code} {resp.text}"
    data = resp.json()
    log("GET /analytics/dashboard", data)
    return data


def main():
    print("PRMSOE End-to-End Test")
    print(f"Target: {BASE}\n")

    # Setup
    user_id = setup_test_user()
    cleanup_test_data(user_id)

    # 1. Upload CSV
    upload = test_upload(user_id)

    # 2. Poll enrichment status
    test_status(upload["job_id"], user_id)

    # 3. Get drafts
    drafts = test_drafts(user_id)
    if not drafts:
        print("\nNo drafts found — enrichment may have failed. Check server logs.")
        sys.exit(1)

    # 4. Send first draft
    send_result = test_send(drafts[0])

    # 5. Check feedback queue
    # Note: feedback_due_at is now()+3 days, so the queue will be empty unless
    # we manually set it. Let's test the endpoint returns successfully anyway.
    queue = test_feedback_queue(user_id)
    if not queue:
        print("  (queue empty — expected, feedback_due_at is 3 days from now)")
        # Use the outreach_id from send directly for swipe test
        test_swipe(send_result["outreach_id"])
    else:
        test_swipe(queue[0]["outreach_id"])

    # 6. Analytics
    test_analytics(user_id)

    print(f"\n{'='*60}")
    print("  ALL TESTS PASSED")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
