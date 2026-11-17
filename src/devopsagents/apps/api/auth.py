from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from devopsagents.core.config.settings import Settings
from devopsagents.integrations.github.identity import GitHubIdentityVerifier


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


def require_bearer(request: Request, secret: str) -> None:
    """`Authorization: Bearer <secret>` only. Constant-time; fails closed when no secret is configured."""
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_alert_token(request: Request, secret: str) -> None:
    """Alert sources authenticate with `X-Alert-Token` or a standard `Authorization: Bearer` header
    (what Alertmanager's http_config.authorization sends). Constant-time; fails closed."""
    given = request.headers.get("x-alert-token", "")
    if not given:
        auth = request.headers.get("authorization", "")
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


_default_verifiers: dict[str, GitHubIdentityVerifier] = {}


def _github_principal(request: Request, settings: Settings) -> Principal:
    """The approver sends their own GitHub token; GitHub tells us who they are and whether they
    can push to the approver repo. Free, and no identity provider to run."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="unauthorized")
    repo = settings.approver_repo or settings.incident_repo
    if not repo:
        raise HTTPException(status_code=503, detail="approver repo is not configured")
    verifier = getattr(request.app.state, "identity_verifier", None)
    if verifier is None:
        verifier = _default_verifiers.setdefault(
            settings.github_api_url, GitHubIdentityVerifier(settings.github_api_url)
        )
    ident = verifier.verify(token, repo)
    if ident is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    allow = {x.strip().lower() for x in settings.approvers.split(",") if x.strip()}
    if not ident.can_write or (allow and ident.login.lower() not in allow):
        raise HTTPException(status_code=403, detail="not an approver")
    return Principal(name=ident.login, roles=frozenset({settings.approver_role}))


def authenticate_admin(request: Request, settings: Settings, *, role: str | None = None) -> Principal | None:
    """Admin auth. `token` -> shared secret (identity unverified). `easyauth` -> Entra principal from
    Easy Auth/APIM. `github` -> the caller's GitHub token, verified against GitHub."""
    if settings.admin_auth == "token":
        require_token(request, settings.admin_token.get_secret_value(), "x-admin-token")
        return None
    if settings.admin_auth == "github":
        return _github_principal(request, settings)
    principal = _principal_from_easyauth(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if role and role not in principal.roles:
        raise HTTPException(status_code=403, detail=f"requires role {role}")
    return principal
