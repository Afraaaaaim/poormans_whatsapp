"""
Tool registry — metadata + role-based permissions for all MCP tools.

Each entry defines:
  - description: human-readable summary
  - allowed_roles: minimum roles that can invoke this tool
  - category: grouping label
  - tags: for semantic search_tools matching
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
    allowed_roles: list[str]   # roles that may call this tool
    category: str
    tags: list[str]


REGISTRY: dict[str, ToolMeta] = {
    # ── Users ────────────────────────────────────────────────────────────
    "get_user": ToolMeta(
        description="Fetch a user's profile by phone number or user ID.",
        allowed_roles=["user", "admin", "owner"],
        category="users",
        tags=["user", "profile", "lookup", "fetch", "who", "find user"],
    ),
    "list_users": ToolMeta(
        description="List all registered users with optional role filter.",
        allowed_roles=["admin", "owner"],
        category="users",
        tags=["users", "list", "all users", "roster", "directory"],
    ),
    "add_user": ToolMeta(
        description="Register a new user with a given phone number and role.",
        allowed_roles=["admin", "owner"],
        category="users",
        tags=["add user", "register", "create user", "new user", "onboard"],
    ),
    "update_user_role": ToolMeta(
        description="Change a user's role (promote or demote).",
        allowed_roles=["admin", "owner"],
        category="users",
        tags=["role", "promote", "demote", "change role", "update user"],
    ),
    "remove_user": ToolMeta(
        description="Remove a user from the system permanently.",
        allowed_roles=["owner","admin"],
        category="users",
        tags=["remove", "delete user", "ban", "kick", "deregister"],
    ),
}


def check_permission(tool_name: str, role: str) -> tuple[bool, str]:
    """
    Check if `role` is allowed to call `tool_name`.
    Returns (allowed: bool, reason: str).
    """
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
    Semantic-ish tool search using tag + description keyword matching.
    Filters out tools the caller's role cannot access.
    Returns ranked list of {name, description, score} dicts.
    """
    q = query.lower()
    results = []

    for name, meta in REGISTRY.items():
        # permission filter first
        allowed, _ = check_permission(name, role)
        if not allowed:
            continue

        score = 0
        # tag hits (weighted higher)
        for tag in meta["tags"]:
            if tag in q or q in tag:
                score += 3
        # description hits
        for word in q.split():
            if word in meta["description"].lower():
                score += 1
        # name hit
        if q in name or name in q:
            score += 5

        if score > 0:
            results.append({"name": name, "description": meta["description"], "score": score})

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]
