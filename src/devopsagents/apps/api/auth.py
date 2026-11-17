from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from devopsagents.core.config.settings import Settings


def require_token(request: Request, secret: str, header: str) -> None:
    """Constant-time shared-secret check. Fails closed when no secret is configured."""
    given = request.headers.get(header, "")
    if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


@dataclass(frozen=True)
class Principal:
    name: str
    roles: frozenset[str] = field(default_factory=frozenset)


def _principal_from_easyauth(request: Request) -> Principal | None:
    """Container Apps Easy Auth / APIM inject the validated Entra principal as headers.

    SECURITY: only trustworthy when the app is reachable *exclusively* through Easy Auth (which
    strips client-supplied copies of these headers). Never enable `admin_auth=easyauth` otherwise.
    """
    raw = request.headers.get("x-ms-client-principal")
    if not raw:
        return None
    try:
        data = json.loads(base64.b64decode(raw, validate=True))
    except (ValueError, json.JSONDecodeError):
        return None
    claims = data.get("claims") or []
    roles = {str(c.get("val")) for c in claims if c.get("typ") in ("roles", "role")}
    name = request.headers.get("x-ms-client-principal-name") or next(
        (str(c.get("val")) for c in claims if c.get("typ") in ("preferred_username", "name")), ""
    )
    return Principal(name=name, roles=frozenset(roles)) if name else None


def authenticate_admin(request: Request, settings: Settings, *, role: str | None = None) -> Principal | None:
    """Admin auth. `token` mode -> shared secret (returns None, identity is unverified).
    `easyauth` mode -> validated Entra principal, optionally requiring an app role."""
    if settings.admin_auth == "token":
        require_token(request, settings.admin_token.get_secret_value(), "x-admin-token")
        return None
    principal = _principal_from_easyauth(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if role and role not in principal.roles:
        raise HTTPException(status_code=403, detail=f"requires role {role}")
    return principal
