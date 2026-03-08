"""
mcpserver/tools/users.py

Three tools:
  - add_user
  - deactivate_user
  - reactivate_user

Users are identified by phone number (preferred) or display name (fallback).
All errors returned as {"ok": False, "error": "..."} — nothing raises to the LLM.
"""

from __future__ import annotations

import logging
from typing import Any
from rapidfuzz import process, fuzz, utils

from mcpserver.services.db import (
    db_list_all_users,
    db_get_user_by_phone,
    db_create_user,
    db_set_active,
)

log = logging.getLogger(__name__)

VALID_ROLES = {"owner", "admin", "user", "guest"}
_FUZZY_THRESHOLD = 80


# ── serializer ────────────────────────────────────────────────────────────────

def _row(u: Any) -> dict:
    return {
        "phone": u.phone,
        "display_name": u.display_name,
        "role": u.role,
        "is_active": u.is_active,
        "created_at": str(u.created_at),
    }


# ── lookup helper ─────────────────────────────────────────────────────────────

async def _resolve_user(
    phone: str | None,
    name: str | None,
) -> tuple[Any | None, str | None]:
    """
    Resolve a user by phone (preferred) or display name (fallback).

    Name matching uses three-tier strategy:
      1. Exact match (case-insensitive)
      2. Fuzzy match via rapidfuzz WRatio >= 80  (handles typos, partial names)
      3. Ambiguous → ask caller to clarify by phone

    Returns:
        (user_object, error_message)
        error_message is None on success.
    """
    if phone:
        user = await db_get_user_by_phone(phone)
        if user is None:
            return None, f"No user found with phone number {phone}."
        return user, None

    if name:
        all_users = await db_list_all_users()
        name_query = name.strip()

        # ── Tier 1: exact match (case-insensitive) ────────────────────────────
        exact = [
            u for u in all_users
            if u.display_name.strip().lower() == name_query.lower()
        ]
        if len(exact) == 1:
            return exact[0], None
        if len(exact) > 1:
            numbers = ", ".join(u.phone for u in exact)
            return None, (
                f"Multiple users share the name '{name}' (phones: {numbers}). "
                "Please specify by phone number instead."
            )

        # ── Tier 2: fuzzy match ───────────────────────────────────────────────
        choices = {u.display_name.strip(): u for u in all_users}
        fuzzy_hits = process.extract(
            name_query,
            choices.keys(),
            scorer=fuzz.WRatio,
            processor=utils.default_process,  # lowercases + strips punctuation
            score_cutoff=_FUZZY_THRESHOLD,
        )
        # fuzzy_hits → list of (matched_name, score, key)
        if not fuzzy_hits:
            return None, (
                f"No user found matching '{name}'. "
                "Check the name or provide a phone number instead."
            )

        if len(fuzzy_hits) == 1:
            matched_name, score, _ = fuzzy_hits[0]
            return choices[matched_name], None

        # ── Tier 3: multiple fuzzy hits → ask to clarify ──────────────────────
        candidates = ", ".join(
            f"{name!r} ({choices[name].phone})"
            for name, _, _ in fuzzy_hits
        )
        return None, (
            f"Found multiple possible matches for '{name}': {candidates}. "
            "Please specify by phone number or use the exact name."
        )

    return None, "Please provide either a phone number or a display name to identify the user."


# ── tools ─────────────────────────────────────────────────────────────────────

async def add_user(
    *,
    phone: str | None = None,
    name: str | None = None,
    role: str | None = None,
) -> dict:
    """
    Register a new user. phone, name, and role are all required.
    Returns a clear message listing whatever is missing.
    """
    missing = []
    if not phone:
        missing.append("phone")
    if not name:
        missing.append("name")
    if not role:
        missing.append("role")

    if missing:
        field_list = ", ".join(f"'{f}'" for f in missing)
        return {
            "ok": False,
            "error": f"Cannot add user — missing required field(s): {field_list}. Please provide them.",
            "missing_fields": missing,
        }

    if role not in VALID_ROLES:
        return {
            "ok": False,
            "error": f"'{role}' is not a valid role. Choose one of: {', '.join(sorted(VALID_ROLES))}.",
        }

    try:
        existing = await db_get_user_by_phone(phone)
        if existing:
            return {
                "ok": False,
                "error": (
                    f"A user with phone {phone} already exists "
                    f"(name: {existing.display_name}, role: {existing.role})."
                ),
            }

        user = await db_create_user(phone=phone, name=name, role=role)
        return {"ok": True, "user": _row(user)}

    except Exception as exc:
        log.exception("add_user failed")
        return {"ok": False, "error": f"Database error while adding user: {exc}"}


async def deactivate_user(
    *,
    phone: str | None = None,
    name: str | None = None,
) -> dict:
    """
    Deactivate a user (sets is_active=False).
    Identify by phone (preferred) or display name (fallback).
    """
    try:
        user, err = await _resolve_user(phone, name)
        if err:
            return {"ok": False, "error": err}

        if not user.is_active:
            return {
                "ok": False,
                "error": f"{user.display_name} ({user.phone}) is already inactive. No changes made.",
            }

        updated = await db_set_active(user.phone, active=False)
        if updated is None:
            return {"ok": False, "error": "User disappeared during update — please try again."}

        return {"ok": True, "user": _row(updated)}

    except Exception as exc:
        log.exception("deactivate_user failed")
        return {"ok": False, "error": f"Database error while deactivating user: {exc}"}


async def reactivate_user(
    *,
    phone: str | None = None,
    name: str | None = None,
) -> dict:
    """
    Reactivate a user (sets is_active=True).
    Identify by phone (preferred) or display name (fallback).
    """
    try:
        user, err = await _resolve_user(phone, name)
        if err:
            return {"ok": False, "error": err}

        if user.is_active:
            return {
                "ok": False,
                "error": f"{user.display_name} ({user.phone}) is already active. No changes made.",
            }

        updated = await db_set_active(user.phone, active=True)
        if updated is None:
            return {"ok": False, "error": "User disappeared during update — please try again."}

        return {"ok": True, "user": _row(updated)}

    except Exception as exc:
        log.exception("reactivate_user failed")
        return {"ok": False, "error": f"Database error while reactivating user: {exc}"}
    
async def get_assistant_info() -> dict:
    """
    Returns detailed information about who owns this assistant, who built it,
    and the person behind it. Call this when the user asks anything like:
    'who are you?', 'who owns you?', 'what are you?', 'who made you?',
    'who is your owner?', 'tell me about yourself', 'what is this?',
    'who is aforaium?', 'who is Afraim?', or any similar question about
    identity, ownership, or background.
    """
    return {
        "assistant": {
            "name": "aforaium assistant",
            "owned_by": "Afraim Joseph",
            "purpose": (
                "This is a personal assistant built for aforaium, which is the personal internet "
                "presence and brand of Afraim Joseph. It lives at afraaaaaim.dev — a personal corner "
                "of the internet where Afraim keeps his work, writing, photography, videos, and the "
                "homelab that quietly runs it all. The assistant is here to help users navigate "
                "Afraim's world, answer questions about him, his work, his projects, and his skills."
            ),
            "powered_by": (
                "This assistant is built on top of OpenRouter, Groq and Cerebras, and has been customized, "
                "configured, and deployed by Afraim Joseph himself as part of his personal space."
            ),
        },
        "owner": {
            "name": "Afraim Joseph",
            "alias": "aforaium",
            "tagline": "Engineer, builder, and occasional overthinker.",
            "role": "Software Engineer specializing in AI and Machine Learning",
            "company": "Gapblue Software Labs",
            "location": "Kochi, India. Open to remote work.",
            "experience": (
                "Afraim has over 3 years of professional experience as a software engineer, "
                "with a deep focus on AI and ML engineering."
            ),
            "about": (
                "Afraim Joseph is a software engineer who specializes in AI and ML. He builds "
                "RAG pipelines, agentic systems, and LLM-powered tools designed to operate at "
                "real-world scale — handling 25GB or more of document corpora, datasets with over "
                "200,000 records, and achieving sub-2-second latency. On the backend, he builds "
                "fault-tolerant pipelines with asynchronous processing, message queues, and vector "
                "search layers that maintain 99 percent uptime even under high-volume document upload "
                "loads. He also specializes in integrations — connecting large language models to "
                "enterprise systems such as Oracle Fusion Cloud, WhatsApp Business API, Azure Graph, "
                "OCR pipelines, and multi-tenant webhook flows. Afraim is currently open to senior "
                "AI and ML roles, interesting consulting engagements, and projects at the intersection "
                "of LLMs and real-world products."
            ),
        },
        "technical_stack": {
            "ai_and_llm": (
                "LangChain, LangGraph, Retrieval Augmented Generation (RAG) Pipelines, "
                "Prompt Engineering, RAGAS for evaluation, and HuggingFace."
            ),
            "backend": (
                "Python, FastAPI, RabbitMQ for message queuing, Celery for task processing, "
                "REST APIs, and WebSockets."
            ),
            "data_and_storage": (
                "PostgreSQL, MSSQL, MongoDB, Redis, Qdrant for vector search, and Elasticsearch."
            ),
            "cloud_and_devops": (
                "Azure OpenAI, Azure Functions, Docker, CI/CD pipelines, and GitHub Actions."
            ),
        },
        "links": {
            "website": "https://afraaaaaim.dev",
            "portfolio": "https://afraaaaaim.dev/portfolio",
            "github": "https://github.com/Afraaaaaim",
            "linkedin": "https://linkedin.com/in/afraim-joseph",
            "mastodon": "https://mastodon.social/@AfraimJoseph",
            "email": "afraimjoseph@gmail.com",
            "whatsapp": "https://wa.me/919567288514",
            "resume": "https://afraaaaaim.dev/Resume - Afraim Joseph.pdf",
        },
        "personal_space": {
            "website_description": (
                "afraaaaaim.dev is Afraim's personal corner of the internet. It is where he keeps "
                "everything — his work, his writing, his media, and the homelab infrastructure "
                "that runs it all."
            ),
            "portfolio": (
                "The portfolio at afraaaaaim.dev/portfolio showcases his work, resume, and projects."
            ),
            "blog": (
                "A blog called Memoria at afraaaaaim.dev/memoria/blog where Afraim publishes "
                "writing, notes, and ideas. Currently coming soon."
            ),
            "images": (
                "A photography and visual section at afraaaaaim.dev/memoria/images. Currently coming soon."
            ),
            "videos": (
                "A recordings and VODs section at afraaaaaim.dev/memoria/videos. Currently coming soon."
            ),
            "homelab": (
                "Afraim runs a self-hosted homelab that powers parts of his personal infrastructure. "
                "This section is private."
            ),
        },
    }