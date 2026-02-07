"""PRMSOE – Modal + FastAPI backend for AI-driven LinkedIn outreach."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from enum import Enum

import httpx
import modal
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Load .env in local dev mode (set by __main__ or manually)
if os.environ.get("LOCAL_DEV", "").lower() in ("1", "true"):
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

LOCAL_DEV = os.environ.get("LOCAL_DEV", "").lower() in ("1", "true")

# ---------------------------------------------------------------------------
# Section 2: Modal App + Image
# ---------------------------------------------------------------------------

image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "fastapi[standard]",
    "supabase",
    "google-genai",
    "httpx",
    "python-multipart",
    "composio",
)

app = modal.App(name="prmsoe", image=image)

logger = logging.getLogger("prmsoe")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Section 3: Enums (mirror Postgres enums)
# ---------------------------------------------------------------------------


class ContactStatus(str, Enum):
    NEW = "NEW"
    RESEARCHING = "RESEARCHING"
    DRAFT_READY = "DRAFT_READY"
    SENT = "SENT"
    ARCHIVED = "ARCHIVED"


class StrategyTag(str, Enum):
    PAIN_POINT = "PAIN_POINT"
    VALIDATION_ASK = "VALIDATION_ASK"
    DIRECT_PITCH = "DIRECT_PITCH"
    MUTUAL_CONNECTION = "MUTUAL_CONNECTION"
    INDUSTRY_TREND = "INDUSTRY_TREND"


class FeedbackStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"


class OutcomeType(str, Enum):
    REPLIED = "REPLIED"
    GHOSTED = "GHOSTED"
    BOUNCED = "BOUNCED"


class JobStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


VALID_STRATEGY_TAGS = {t.value for t in StrategyTag}

# ---------------------------------------------------------------------------
# Section 4: Pydantic request/response models
# ---------------------------------------------------------------------------


class SendRequest(BaseModel):
    contact_id: str
    message_body: str
    strategy_tag: str


class SwipeRequest(BaseModel):
    outreach_id: str
    outcome: str


class ComposioConnectRequest(BaseModel):
    user_id: str
    callback_url: str


class AutoDetectRequest(BaseModel):
    user_id: str


# ---------------------------------------------------------------------------
# Section 5: Helpers
# ---------------------------------------------------------------------------


def get_supabase():
    from supabase import create_client

    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )


def get_composio():
    from composio import Composio

    return Composio(api_key=os.environ["COMPOSIO_API_KEY"])


def get_gmail_auth_config_id():
    """Fetch Gmail auth_config_id dynamically from Composio."""
    composio = get_composio()
    configs = composio.auth_configs.list()
    for cfg in configs.items:
        if cfg.toolkit.slug == "gmail":
            return cfg.id
    raise RuntimeError("No Gmail auth config found in Composio. Set one up at platform.composio.dev")


def search_youcom(query: str) -> dict:
    """Call You.com Web Search API and return raw JSON response."""
    resp = httpx.get(
        "https://ydc-index.io/v1/search",
        params={"query": query},
        headers={"X-API-Key": os.environ["YOUCOM_API_KEY"]},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def parse_youcom_response(data: dict) -> dict:
    """Extract structured fields from You.com search results."""
    hits = data.get("hits", [])[:3]
    snippets: list[str] = []
    pain_points = ""
    source_url = ""

    for i, hit in enumerate(hits):
        for snippet in hit.get("snippets", []):
            snippets.append(snippet)
        if i == 0:
            pain_points = hit.get("description", "")
            source_url = hit.get("url", "")

    return {
        "news_summary": " ".join(snippets)[:2000],
        "pain_points": pain_points[:1000],
        "source_url": source_url,
    }


def generate_draft(
    mission_statement: str,
    intent_type: str,
    research_summary: str,
    raw_role: str,
    company_name: str,
    full_name: str,
) -> dict:
    """Call Gemini to generate a draft message + strategy tag."""
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    prompt = f"""You are a LinkedIn outreach assistant. Generate a personalized connection message.

USER CONTEXT:
- Mission: {mission_statement}
- Intent: {intent_type}

CONTACT:
- Name: {full_name}
- Role: {raw_role}
- Company: {company_name}

RESEARCH:
{research_summary}

INSTRUCTIONS:
1. Write a LinkedIn message under 300 characters. Be conversational, specific, and reference the research.
2. Choose exactly ONE strategy tag from: PAIN_POINT, VALIDATION_ASK, DIRECT_PITCH, MUTUAL_CONNECTION, INDUSTRY_TREND
3. Return ONLY valid JSON (no markdown, no code fences):
{{"draft_message": "your message here", "strategy_tag": "TAG_HERE"}}"""

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
        config={
            "response_mime_type": "application/json",
        },
    )

    try:
        result = json.loads(response.text)
        draft = result.get("draft_message", "")
        tag = result.get("strategy_tag", "DIRECT_PITCH")

        if tag not in VALID_STRATEGY_TAGS:
            logger.warning(f"Gemini returned invalid strategy_tag: {tag}, defaulting to DIRECT_PITCH")
            tag = "DIRECT_PITCH"

        return {"draft_message": draft[:300], "strategy_tag": tag}
    except (json.JSONDecodeError, AttributeError, KeyError) as e:
        logger.warning(f"Gemini response parse failure: {e}")
        return {
            "draft_message": f"Hi {full_name}, I'd love to connect and learn more about your work at {company_name}.",
            "strategy_tag": "DIRECT_PITCH",
        }


# ---------------------------------------------------------------------------
# Section 6: FastAPI app + CORS
# ---------------------------------------------------------------------------

web_app = FastAPI(title="PRMSOE API")

web_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Section 7: Endpoint handlers
# ---------------------------------------------------------------------------


@web_app.post("/ingest/upload")
async def ingest_upload(
    file: UploadFile = File(...),
    user_id: str = Form(...),
):
    """Parse LinkedIn CSV, insert contacts, kick off enrichment."""
    sb = get_supabase()

    # Validate user exists
    profile = sb.table("profiles").select("id, mission_statement, intent_type").eq("id", user_id).execute()
    if not profile.data:
        raise HTTPException(status_code=404, detail="User profile not found")

    # Read and decode CSV
    raw_bytes = await file.read()
    text = raw_bytes.decode("utf-8-sig")
    lines = text.splitlines()

    # Find the header row (LinkedIn CSVs have preamble lines)
    header_idx = None
    for i, line in enumerate(lines):
        if "First Name" in line:
            header_idx = i
            break

    if header_idx is None:
        raise HTTPException(status_code=400, detail="CSV missing expected header row with 'First Name'")

    csv_text = "\n".join(lines[header_idx:])
    reader = csv.DictReader(io.StringIO(csv_text))

    contacts_to_insert: list[dict] = []
    skipped = 0

    for row in reader:
        company = (row.get("Company") or "").strip()
        if not company:
            skipped += 1
            continue

        first = (row.get("First Name") or "").strip()
        last = (row.get("Last Name") or "").strip()
        full_name = f"{first} {last}".strip()

        contacts_to_insert.append({
            "user_id": user_id,
            "full_name": full_name,
            "company_name": company,
            "raw_role": (row.get("Position") or "").strip(),
            "linkedin_url": (row.get("URL") or "").strip(),
            "status": ContactStatus.NEW.value,
        })

    if len(contacts_to_insert) > 500:
        raise HTTPException(status_code=400, detail="CSV exceeds 500 contact limit")

    if not contacts_to_insert:
        # Empty CSV — create completed job immediately
        job = sb.table("enrichment_jobs").insert({
            "user_id": user_id,
            "total_contacts": 0,
            "processed_count": 0,
            "failed_count": 0,
            "status": JobStatus.COMPLETED.value,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        return {
            "contacts_created": 0,
            "contacts_skipped": skipped,
            "job_id": job.data[0]["id"],
            "message": "No valid contacts found in CSV",
        }

    # Bulk insert contacts
    inserted = sb.table("contacts").insert(contacts_to_insert).execute()
    contact_ids = [c["id"] for c in inserted.data]

    # Create enrichment job
    job = sb.table("enrichment_jobs").insert({
        "user_id": user_id,
        "total_contacts": len(contact_ids),
        "processed_count": 0,
        "failed_count": 0,
        "status": JobStatus.RUNNING.value,
    }).execute()
    job_id = job.data[0]["id"]

    # Spawn background enrichment
    if LOCAL_DEV:
        threading.Thread(
            target=enrich_batch.local,
            kwargs={"job_id": job_id, "contact_ids": contact_ids},
            daemon=True,
        ).start()
    else:
        enrich_batch.spawn(job_id=job_id, contact_ids=contact_ids)

    return {
        "contacts_created": len(contact_ids),
        "contacts_skipped": skipped,
        "job_id": job_id,
        "message": f"{len(contact_ids)} contacts queued for enrichment",
    }


@web_app.get("/ingest/status/{job_id}")
async def ingest_status(job_id: str, user_id: str = Query(...)):
    """Return enrichment job progress."""
    sb = get_supabase()
    result = sb.table("enrichment_jobs").select("*").eq("id", job_id).eq("user_id", user_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Job not found")
    row = result.data[0]
    return {
        "job_id": row["id"],
        "status": row["status"],
        "total_contacts": row["total_contacts"],
        "processed_count": row["processed_count"],
        "failed_count": row["failed_count"],
    }


@web_app.get("/contacts/list")
async def contacts_list(
    user_id: str = Query(...),
    limit: int = Query(default=10, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Return paginated contacts for a user (any status)."""
    sb = get_supabase()

    count_resp = (
        sb.table("contacts")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .execute()
    )
    total = count_resp.count or 0

    contacts_resp = (
        sb.table("contacts")
        .select("id, full_name, raw_role, company_name, linkedin_url, status, created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )

    return {
        "contacts": contacts_resp.data or [],
        "total": total,
        "has_more": (offset + limit) < total,
    }


@web_app.get("/feed/drafts")
async def feed_drafts(
    user_id: str = Query(...),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Return contacts with DRAFT_READY status and their research."""
    sb = get_supabase()

    # Get total count
    count_resp = (
        sb.table("contacts")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("status", ContactStatus.DRAFT_READY.value)
        .execute()
    )
    total = count_resp.count or 0

    # Get paginated contacts
    contacts_resp = (
        sb.table("contacts")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", ContactStatus.DRAFT_READY.value)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )

    drafts = []
    if contacts_resp.data:
        contact_ids = [c["id"] for c in contacts_resp.data]
        research_resp = sb.table("research").select("*").in_("contact_id", contact_ids).execute()
        research_map = {r["contact_id"]: r for r in (research_resp.data or [])}

        for c in contacts_resp.data:
            r = research_map.get(c["id"], {})
            drafts.append({
                "contact_id": c["id"],
                "full_name": c["full_name"],
                "raw_role": c["raw_role"],
                "company_name": c["company_name"],
                "linkedin_url": c["linkedin_url"],
                "draft_message": c["draft_message"],
                "strategy_tag": c["strategy_tag"],
                "research": {
                    "news_summary": r.get("news_summary", ""),
                    "pain_points": r.get("pain_points", ""),
                    "source_url": r.get("source_url", ""),
                },
            })

    return {
        "drafts": drafts,
        "total": total,
        "has_more": (offset + limit) < total,
    }


@web_app.post("/action/send")
async def action_send(req: SendRequest):
    """Mark contact as sent and create outreach attempt."""
    sb = get_supabase()

    # Verify contact exists
    contact = sb.table("contacts").select("id, status").eq("id", req.contact_id).execute()
    if not contact.data:
        raise HTTPException(status_code=404, detail="Contact not found")

    now = datetime.now(timezone.utc)
    feedback_due = now + timedelta(days=3)

    # Update contact status
    sb.table("contacts").update({"status": ContactStatus.SENT.value}).eq("id", req.contact_id).execute()

    # Insert outreach attempt
    outreach = sb.table("outreach_attempts").insert({
        "contact_id": req.contact_id,
        "strategy_tag": req.strategy_tag,
        "message_body": req.message_body,
        "sent_at": now.isoformat(),
        "feedback_due_at": feedback_due.isoformat(),
        "feedback_status": FeedbackStatus.PENDING.value,
    }).execute()

    return {
        "outreach_id": outreach.data[0]["id"],
        "feedback_due_at": feedback_due.isoformat(),
    }


@web_app.get("/feedback/queue")
async def feedback_queue(user_id: str = Query(...)):
    """Return outreach attempts where feedback is due."""
    sb = get_supabase()

    # Get user's contact IDs
    contacts = sb.table("contacts").select("id").eq("user_id", user_id).execute()
    if not contacts.data:
        return {"pending": []}

    contact_ids = [c["id"] for c in contacts.data]
    now = datetime.now(timezone.utc).isoformat()

    # Get pending outreach attempts where feedback is due
    attempts = (
        sb.table("outreach_attempts")
        .select("*")
        .in_("contact_id", contact_ids)
        .lte("feedback_due_at", now)
        .eq("feedback_status", FeedbackStatus.PENDING.value)
        .execute()
    )

    if not attempts.data:
        return {"pending": []}

    # Build contact lookup for names
    due_contact_ids = list({a["contact_id"] for a in attempts.data})
    contact_info = sb.table("contacts").select("id, full_name, company_name").in_("id", due_contact_ids).execute()
    contact_map = {c["id"]: c for c in (contact_info.data or [])}

    pending = []
    for a in attempts.data:
        c = contact_map.get(a["contact_id"], {})
        pending.append({
            "outreach_id": a["id"],
            "full_name": c.get("full_name", ""),
            "company_name": c.get("company_name", ""),
            "strategy_tag": a["strategy_tag"],
            "sent_at": a["sent_at"],
            "message_preview": (a.get("message_body") or "")[:80],
        })

    return {"pending": pending}


@web_app.post("/feedback/swipe")
async def feedback_swipe(req: SwipeRequest):
    """Record feedback outcome for an outreach attempt."""
    sb = get_supabase()

    # Validate outcome
    if req.outcome not in {t.value for t in OutcomeType}:
        raise HTTPException(status_code=400, detail=f"Invalid outcome: {req.outcome}")

    result = sb.table("outreach_attempts").select("id").eq("id", req.outreach_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Outreach attempt not found")

    sb.table("outreach_attempts").update({
        "outcome": req.outcome,
        "feedback_status": FeedbackStatus.COMPLETED.value,
    }).eq("id", req.outreach_id).execute()

    return {"ok": True}


@web_app.get("/analytics/dashboard")
async def analytics_dashboard(user_id: str = Query(...)):
    """Aggregate outreach metrics for user."""
    sb = get_supabase()

    # Get user's contact IDs
    contacts = sb.table("contacts").select("id").eq("user_id", user_id).execute()
    if not contacts.data:
        return {
            "total_sent": 0,
            "total_completed": 0,
            "total_replied": 0,
            "global_reply_rate": 0.0,
            "by_strategy": [],
        }

    contact_ids = [c["id"] for c in contacts.data]

    # Build contact lookup for replied message details
    contact_info = sb.table("contacts").select("id, full_name, company_name").in_("id", contact_ids).execute()
    contact_map = {c["id"]: c for c in (contact_info.data or [])}

    attempts = sb.table("outreach_attempts").select("*").in_("contact_id", contact_ids).execute()
    rows = attempts.data or []

    total_sent = len(rows)
    total_completed = sum(1 for r in rows if r.get("feedback_status") == FeedbackStatus.COMPLETED.value)
    total_replied = sum(1 for r in rows if r.get("outcome") == OutcomeType.REPLIED.value)
    global_reply_rate = round(total_replied / total_completed, 3) if total_completed > 0 else 0.0

    # Group by strategy
    strategy_buckets: dict[str, dict] = {}
    for r in rows:
        tag = r.get("strategy_tag", "UNKNOWN")
        bucket = strategy_buckets.setdefault(tag, {"sent": 0, "replied": 0, "replied_messages": []})
        bucket["sent"] += 1
        if r.get("outcome") == OutcomeType.REPLIED.value:
            bucket["replied"] += 1
            c = contact_map.get(r["contact_id"], {})
            bucket["replied_messages"].append({
                "full_name": c.get("full_name", ""),
                "company_name": c.get("company_name", ""),
                "message_body": r.get("message_body", ""),
                "sent_at": r.get("sent_at", ""),
            })

    by_strategy = []
    for tag, b in sorted(strategy_buckets.items()):
        by_strategy.append({
            "strategy_tag": tag,
            "sent": b["sent"],
            "replied": b["replied"],
            "reply_rate": round(b["replied"] / b["sent"], 3) if b["sent"] > 0 else 0.0,
            "replied_messages": b["replied_messages"],
        })

    return {
        "total_sent": total_sent,
        "total_completed": total_completed,
        "total_replied": total_replied,
        "global_reply_rate": global_reply_rate,
        "by_strategy": by_strategy,
    }


# ---------------------------------------------------------------------------
# Section 7b: Composio Gmail integration endpoints
# ---------------------------------------------------------------------------


@web_app.post("/composio/connect")
async def composio_connect(req: ComposioConnectRequest):
    """Initiate Gmail OAuth via Composio. Returns redirect_url for user to approve."""
    try:
        composio = get_composio()
        gmail_auth_config_id = get_gmail_auth_config_id()

        connection_request = composio.connected_accounts.initiate(
            user_id=req.user_id,
            auth_config_id=gmail_auth_config_id,
            callback_url=req.callback_url,
        )

        return {"redirect_url": connection_request.redirect_url}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/composio/connect error: {e}")
        return JSONResponse(status_code=500, content={"detail": str(e)})


@web_app.get("/composio/status")
async def composio_status(user_id: str = Query(...)):
    """Check if user has an active Gmail connection via Composio."""
    composio = get_composio()

    connected_accounts = composio.connected_accounts.list(
        user_ids=[user_id],
        toolkit_slugs=["gmail"],
    )

    for account in connected_accounts.items:
        if account.status == "ACTIVE":
            return {"connected": True}

    return {"connected": False}


@web_app.post("/composio/disconnect")
async def composio_disconnect(req: AutoDetectRequest):
    """Disconnect (unlink) user's Gmail integration via Composio."""
    try:
        composio = get_composio()

        connected_accounts = composio.connected_accounts.list(
            user_ids=[req.user_id],
            toolkit_slugs=["gmail"],
        )

        for account in connected_accounts.items:
            if account.status == "ACTIVE":
                composio.connected_accounts.delete(account.id)

        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/composio/disconnect error: {e}")
        return JSONResponse(status_code=500, content={"detail": str(e)})


DEMO_USER_ID = "c877835e-4609-4075-9892-84bf9c3e8f97"


@web_app.post("/feedback/auto-detect")
async def feedback_auto_detect(req: AutoDetectRequest):
    """Scan Gmail for LinkedIn reply notifications and auto-mark matching outreach as REPLIED."""
    try:
        sb = get_supabase()

        # Mock scan for demo user — match contacts against fake LinkedIn emails
        if req.user_id == DEMO_USER_ID:
            mock_emails = [
                {
                    "from": "messaging-digest-noreply@linkedin.com",
                    "subject": "1 new message awaits your response",
                    "body": "Bedir Aygun Software Engineer sent you a new message. View message",
                },
            ]

            contacts = sb.table("contacts").select("id, full_name, company_name").eq("user_id", req.user_id).execute()
            if not contacts.data:
                return {"detected": [], "count": 0}
            contact_map = {c["id"]: c for c in contacts.data}

            # Match mock emails against contacts by name
            matched_contact_ids: set[str] = set()
            detected = []
            for email in mock_emails:
                text = (email["subject"] + " " + email["body"]).lower()
                for c in contacts.data:
                    name = c.get("full_name", "").lower().strip()
                    if name and name in text and c["id"] not in matched_contact_ids:
                        matched_contact_ids.add(c["id"])
                        detected.append({
                            "full_name": c.get("full_name", ""),
                            "company_name": c.get("company_name", ""),
                        })

            # Mark matching outreach attempts as REPLIED so cards disappear from queue
            if matched_contact_ids:
                attempts = (
                    sb.table("outreach_attempts")
                    .select("id")
                    .in_("contact_id", list(matched_contact_ids))
                    .eq("feedback_status", FeedbackStatus.PENDING.value)
                    .execute()
                )
                for a in (attempts.data or []):
                    sb.table("outreach_attempts").update({
                        "outcome": OutcomeType.REPLIED.value,
                        "feedback_status": FeedbackStatus.COMPLETED.value,
                    }).eq("id", a["id"]).execute()

            return {"detected": detected, "count": len(detected)}

        composio = get_composio()

        # 1. Get user's contacts with PENDING outreach attempts (same pattern as /feedback/queue)
        contacts = sb.table("contacts").select("id, full_name, company_name").eq("user_id", req.user_id).execute()
        if not contacts.data:
            return {"detected": [], "count": 0}

        contact_ids = [c["id"] for c in contacts.data]
        contact_map = {c["id"]: c for c in contacts.data}

        # Get PENDING outreach attempts (no date filter — scan all pending)
        attempts = (
            sb.table("outreach_attempts")
            .select("*")
            .in_("contact_id", contact_ids)
            .eq("feedback_status", FeedbackStatus.PENDING.value)
            .execute()
        )

        if not attempts.data:
            return {"detected": [], "count": 0}

        # 2. Build lookup: full_name.lower() → [outreach attempt rows]
        name_to_attempts: dict[str, list[dict]] = {}
        for a in attempts.data:
            c = contact_map.get(a["contact_id"], {})
            name = c.get("full_name", "").lower().strip()
            if name:
                name_to_attempts.setdefault(name, []).append(a)

        # 3. Get active Gmail connected account
        connected_accounts = composio.connected_accounts.list(
            user_ids=[req.user_id],
            toolkit_slugs=["gmail"],
        )

        connected_account_id = None
        for account in connected_accounts.items:
            if account.status == "ACTIVE":
                connected_account_id = account.id
                break

        if not connected_account_id:
            raise HTTPException(status_code=400, detail="Gmail not connected. Connect via /composio/connect first.")

        # 4. Fetch LinkedIn notification emails via Composio
        result = composio.tools.execute(
            "GMAIL_FETCH_EMAILS",
            user_id=req.user_id,
            connected_account_id=connected_account_id,
            arguments={
                "query": "from:linkedin.com newer_than:3d",
                "max_results": 50,
            },
            dangerously_skip_version_check=True,
        )

        response_data = result.get("data", {}) if isinstance(result, dict) else {}
        emails = response_data.get("emails", response_data.get("messages", []))
        if isinstance(emails, dict):
            emails = [emails]

        # 5. Match emails to pending contacts
        detected = []
        matched_attempt_ids = set()

        for email in emails:
            subject = (email.get("subject") or "").lower()
            body = (email.get("body") or email.get("snippet") or "").lower()
            text = subject + " " + body

            for name, attempt_list in name_to_attempts.items():
                if name in text:
                    for a in attempt_list:
                        if a["id"] not in matched_attempt_ids:
                            matched_attempt_ids.add(a["id"])
                            c = contact_map.get(a["contact_id"], {})
                            detected.append({
                                "full_name": c.get("full_name", ""),
                                "company_name": c.get("company_name", ""),
                            })

        # 6. Update matched outreach attempts → REPLIED + COMPLETED
        for attempt_id in matched_attempt_ids:
            sb.table("outreach_attempts").update({
                "outcome": OutcomeType.REPLIED.value,
                "feedback_status": FeedbackStatus.COMPLETED.value,
            }).eq("id", attempt_id).execute()

        return {"detected": detected, "count": len(detected)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/feedback/auto-detect error: {e}")
        return JSONResponse(status_code=500, content={"detail": str(e)})


# ---------------------------------------------------------------------------
# Section 8: enrich_batch (background Modal function)
# ---------------------------------------------------------------------------


@app.function(
    secrets=[modal.Secret.from_name("prmsoe-secrets")],
    timeout=3600,
)
def enrich_batch(job_id: str, contact_ids: list[str]):
    """Process contacts sequentially: research via You.com, draft via Gemini."""
    sb = get_supabase()

    # Fetch user profile for mission/intent context
    job = sb.table("enrichment_jobs").select("user_id").eq("id", job_id).execute()
    if not job.data:
        logger.error(f"Job {job_id} not found")
        return
    user_id = job.data[0]["user_id"]

    profile = sb.table("profiles").select("mission_statement, intent_type").eq("id", user_id).execute()
    mission = profile.data[0]["mission_statement"] if profile.data else ""
    intent = profile.data[0]["intent_type"] if profile.data else "VALIDATION"

    for i, contact_id in enumerate(contact_ids):
        try:
            # Set status to RESEARCHING
            sb.table("contacts").update({"status": ContactStatus.RESEARCHING.value}).eq("id", contact_id).execute()

            # Fetch contact details
            contact = sb.table("contacts").select("full_name, company_name, raw_role").eq("id", contact_id).execute()
            if not contact.data:
                logger.warning(f"Contact {contact_id} not found, skipping")
                sb.table("enrichment_jobs").update({
                    "failed_count": sb.table("enrichment_jobs").select("failed_count").eq("id", job_id).execute().data[0]["failed_count"] + 1,
                }).eq("id", job_id).execute()
                continue

            c = contact.data[0]
            company = c["company_name"]
            full_name = c["full_name"]
            raw_role = c["raw_role"]

            # You.com search (non-fatal — enrichment continues without research)
            search_query = f"{company} recent news problems"
            parsed = {"news_summary": "", "pain_points": "", "source_url": ""}
            raw_response = {}
            try:
                raw_response = search_youcom(search_query)
                parsed = parse_youcom_response(raw_response)
            except Exception as search_err:
                logger.warning(f"Research failed for {contact_id}, proceeding without: {search_err}")

            # Insert research
            sb.table("research").insert({
                "contact_id": contact_id,
                "news_summary": parsed["news_summary"],
                "pain_points": parsed["pain_points"],
                "source_url": parsed["source_url"],
                "raw_response": raw_response,
            }).execute()

            # Generate draft via Gemini
            research_text = f"News: {parsed['news_summary']}\nPain points: {parsed['pain_points']}"
            draft_result = generate_draft(
                mission_statement=mission,
                intent_type=intent,
                research_summary=research_text,
                raw_role=raw_role,
                company_name=company,
                full_name=full_name,
            )

            # Update contact with draft
            sb.table("contacts").update({
                "draft_message": draft_result["draft_message"],
                "strategy_tag": draft_result["strategy_tag"],
                "status": ContactStatus.DRAFT_READY.value,
            }).eq("id", contact_id).execute()

            # Increment processed count
            current = sb.table("enrichment_jobs").select("processed_count").eq("id", job_id).execute()
            sb.table("enrichment_jobs").update({
                "processed_count": current.data[0]["processed_count"] + 1,
            }).eq("id", job_id).execute()

            logger.info(f"Enriched contact {i + 1}/{len(contact_ids)}: {full_name} @ {company}")

        except Exception as e:
            logger.error(f"Failed to enrich contact {contact_id}: {e}")
            try:
                current = sb.table("enrichment_jobs").select("failed_count").eq("id", job_id).execute()
                sb.table("enrichment_jobs").update({
                    "failed_count": current.data[0]["failed_count"] + 1,
                }).eq("id", job_id).execute()
            except Exception as inner_e:
                logger.error(f"Failed to update failed_count: {inner_e}")

        # Rate limit delay (skip after last contact)
        if i < len(contact_ids) - 1:
            time.sleep(2)

    # Mark job completed
    sb.table("enrichment_jobs").update({
        "status": JobStatus.COMPLETED.value,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    logger.info(f"Job {job_id} completed")


# ---------------------------------------------------------------------------
# Section 9: ASGI entrypoint
# ---------------------------------------------------------------------------


@app.function(
    secrets=[modal.Secret.from_name("prmsoe-secrets")],
)
@modal.asgi_app()
def fastapi_app():
    return web_app


# ---------------------------------------------------------------------------
# Local dev: python app.py → uvicorn on :8000 with Swagger at /docs
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    os.environ["LOCAL_DEV"] = "true"
    # Re-trigger dotenv load now that LOCAL_DEV is set
    from dotenv import load_dotenv
    load_dotenv()
    uvicorn.run("app:web_app", host="0.0.0.0", port=8000, reload=True)
