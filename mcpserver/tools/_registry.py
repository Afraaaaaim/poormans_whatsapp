"""
Tool registry — metadata + role-based permissions for MCP tools.
"""

from typing import TypedDict

ROLE_HIERARCHY = ["guest", "user", "admin", "owner"]


def role_gte(role: str, minimum: str) -> bool:
    """Return True if `role` >= `minimum` in the hierarchy."""
    try:
        return ROLE_HIERARCHY.index(role) >= ROLE_HIERARCHY.index(minimum)
    except ValueError:
        return False


class ToolMeta(TypedDict):
    description: str
    allowed_roles: list[str]
    category: str
    tags: list[str]


REGISTRY: dict[str, ToolMeta] = {
    "add_user": ToolMeta(
        description="Register a new user with a phone number, display name, and role.",
        allowed_roles=["admin", "owner"],
        category="users",
        tags=["add user", "register", "create user", "new user", "onboard"],
    ),
    "deactivate_user": ToolMeta(
        description="Deactivate a user by their list number or display name. Sets is_active to False.",
        allowed_roles=["admin", "owner"],
        category="users",
        tags=["deactivate", "disable", "suspend", "block user", "turn off"],
    ),
    "reactivate_user": ToolMeta(
        description="Reactivate a previously deactivated user by their list number or display name.",
        allowed_roles=["admin", "owner"],
        category="users",
        tags=["reactivate", "enable", "restore", "unblock user", "turn on", "activate"],
    ),
    "get_assistant_info": ToolMeta(
        description="Returns information about this assistant — its name, purpose, what powers it, and the personal space it lives on.",
        allowed_roles=["admin", "owner", "user","guest"],
        category="info",
        tags=["who are you", "what are you", "about assistant", "what is this", "tell me about yourself", "what powers you"],
    ),
    "get_owner_info": ToolMeta(
        description=(
            "Returns detailed information about Afraim Joseph — a software engineer with 3+ years of experience "
            "specializing in AI and ML. Covers his background, skills, tech stack, projects, and contact links. "
            "Skills include: LangChain, LangGraph, RAG pipelines, prompt engineering, FastAPI, Python, RabbitMQ, "
            "Celery, PostgreSQL, MongoDB, Redis, Qdrant, Elasticsearch, Azure OpenAI, Docker, CI/CD, GitHub Actions. "
            "Has built systems handling 25GB+ document corpora, 200k+ record datasets, sub-2s latency, and 99% uptime. "
            "Integrations include Oracle Fusion Cloud, WhatsApp Business API, Azure Graph, and OCR pipelines. "
            "Open to senior AI/ML roles and consulting. Located in Kochi, India. Open to remote work."
        ),
        allowed_roles=["admin", "owner", "user","guest"],
        category="info",
        tags=[
            # Identity
            "who owns you", "who made you", "who is aforaium", "who is afraim", "about afraim",
            # Experience
            "years of experience", "yoe", "experience", "background", "career", "senior engineer",
            # Skills & tech
            "skills", "tech stack", "langchain", "langgraph", "rag", "retrieval augmented generation",
            "prompt engineering", "fastapi", "python", "rabbitmq", "celery", "postgresql", "mongodb",
            "redis", "qdrant", "elasticsearch", "vector search", "azure openai", "docker", "ci/cd",
            "github actions", "huggingface", "ragas", "websockets", "rest api",
            # Projects & work
            "projects", "portfolio", "work", "what has he built", "agentic systems", "llm", "ml engineering",
            "ai engineering", "rag pipeline", "document pipeline", "integrations", "oracle fusion",
            "whatsapp api", "ocr", "multi-tenant",
            # Contact & links
            "contact", "resume", "cv", "github", "linkedin", "email", "whatsapp", "website", "hire",
            "open to work", "remote", "consulting", "freelance",
        ],
    ),
}


def check_permission(tool_name: str, role: str) -> tuple[bool, str]:
    meta = REGISTRY.get(tool_name)
    if meta is None:
        return False, f"Tool '{tool_name}' does not exist."

    allowed = meta["allowed_roles"]
    if any(role_gte(role, r) for r in allowed):
        return True, "ok"

    return False, (
        f"Permission denied: '{tool_name}' requires one of {allowed}, "
        f"but caller has role '{role}'."
    )


def search_tools(query: str, role: str, top_k: int = 3) -> list[dict]:
    """
    Keyword + tag matching tool search.
    Filters out tools the caller's role cannot access.
    Returns ranked list of {name, description, score}.
    """
    q = query.lower()
    results = []

    for name, meta in REGISTRY.items():
        allowed, _ = check_permission(name, role)
        if not allowed:
            continue

        score = 0
        for tag in meta["tags"]:
            if tag in q or q in tag:
                score += 3
        for word in q.split():
            if word in meta["description"].lower():
                score += 1
        if q in name or name in q:
            score += 5

        if score > 0:
            results.append({"name": name, "description": meta["description"], "score": score})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]