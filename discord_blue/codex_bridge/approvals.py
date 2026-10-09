"""Full, bounded approval snapshots. Unknown scope stays in the native TUI."""

from __future__ import annotations

import json
from typing import Any

from discord_blue.doodads.agent_session.protocol import approval_content_displayable

Json = dict[str, Any]
FILE_APPROVAL = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL = "item/permissions/requestApproval"
ROUTING = {"threadId", "turnId", "itemId", "startedAtMs", "reason"}


def known_permissions(value: object) -> bool:
    if not isinstance(value, dict) or set(value) - {"network", "fileSystem"}:
        return False
    network = value.get("network")
    if network is not None and (
        not isinstance(network, dict) or set(network) - {"enabled"} or type(network.get("enabled")) not in (bool, type(None))
    ):
        return False
    fs = value.get("fileSystem")
    if fs is None:
        return True
    if not isinstance(fs, dict) or set(fs) - {"read", "write", "entries", "globScanMaxDepth"}:
        return False
    for key in ("read", "write"):
        paths = fs.get(key)
        if paths is not None and (
            not isinstance(paths, list) or any(not isinstance(p, str) or not p.startswith("/") for p in paths)
        ):
            return False
    depth = fs.get("globScanMaxDepth")
    if depth is not None and (type(depth) is not int or depth < 1):
        return False
    entries = fs.get("entries")
    if entries is None:
        return True
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"access", "path"} or entry["access"] not in {"read", "write", "deny"}:
            return False
        path = entry["path"]
        # Named/special roots and glob patterns stay local until their meaning can be shown unambiguously.
        if not isinstance(path, dict) or set(path) != {"type", "path"} or path["type"] != "path":
            return False
        if not isinstance(path["path"], str) or not path["path"].startswith("/"):
            return False
    return True


def content_snapshot(method: str, params: Json, item: Json | None, cwd: str) -> tuple[str, str, Json] | None:
    """Return the display and exact approve response, detached from incoming mutable data."""
    if not all(isinstance(params.get(key), str) and params[key] for key in ("threadId", "turnId", "itemId")):
        return None
    if method == FILE_APPROVAL:
        # grantRoot asks for session-wide writes; this bridge only offers approval of the displayed patch.
        if set(params) - (ROUTING | {"grantRoot"}) or params.get("grantRoot") is not None:
            return None
        if item is None or set(item) - {"id", "type", "status", "changes"} or item.get("status") != "inProgress":
            return None
        changes = item.get("changes")
        if not isinstance(changes, list) or not changes:
            return None
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"path", "kind", "diff"}:
                return None
            if not isinstance(change["path"], str) or not isinstance(change["diff"], str):
                return None
            kind = change["kind"]
            if not isinstance(kind, dict) or kind.get("type") not in {"add", "delete", "update"}:
                return None
            if set(kind) - {"type", "move_path"}:
                return None
        label, display, response = "file_change", {"cwd": cwd, "request": params, "changes": changes}, {"decision": "accept"}
    elif method == PERMISSIONS_APPROVAL:
        if set(params) - (ROUTING | {"cwd", "permissions", "environmentId"}) or not known_permissions(params.get("permissions")):
            return None
        if not isinstance(params.get("cwd"), str) or not params["cwd"]:
            return None
        label, display = "permissions", {"scope": "turn", "request": params}
        response = {"permissions": params["permissions"], "scope": "turn"}
    else:
        return None
    # JSON escapes control characters and non-ASCII display controls rather than allowing them to hide scope.
    text = json.dumps(display, indent=2)
    if not approval_content_displayable(label, text):
        return None
    return label, text, json.loads(json.dumps(response))
