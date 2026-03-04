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