# ADR-0004: Approver identity (open)

**Status:** accepted; implemented (`DA_ADMIN_AUTH=easyauth`)

**Context.** `POST /admin/incidents/{target}/approve` currently authenticates with a shared admin token and records the
approver name from the request body. That is adequate for a pilot, not for production audit.

**Decision (proposed).** Front the API with API Management / Container Apps Easy Auth (Entra), require an app role for
approvers, and take the approver from the validated principal claim (`oid`/`preferred_username`). Reject body-supplied
identities. Optionally require a second approver for tier-2 runbooks in prod.

**Open questions.** Approval UX channel (GitHub issue comment vs Teams card).

## Implementation notes
- `easyauth` mode reads the validated principal from `X-MS-CLIENT-PRINCIPAL(-NAME)`, requires the `Incident.Approver` app role, and ignores any body-supplied approver. Audit entries record `verified_identity: true`.
- **Deployment requirement:** these headers are only trustworthy if the app is reachable exclusively through Easy Auth / APIM. Exclude `/webhooks/*` and `/health*` from Entra auth (GitHub and Azure Monitor use their own HMAC/token); protect `/admin/*`.
- `token` mode remains for dev/pilot and is the default until Easy Auth (or GitHub mode) is configured.
- **Two-person rule and expiry (implemented):** actions at or above `two_person_min_tier` in `two_person_environments` (default tier 2 in prod)
  need two distinct *verified* approvers; self-declared names cannot satisfy it. Votes are recorded as `approval.vote` audit events.
  Pending approvals expire after `approval_ttl_minutes` (default 120): the endpoint returns 410 and the agent re-checks at execution time.
- **GitHub mode (free):** `DA_ADMIN_AUTH=github` takes the approver's own GitHub token, resolves their login with `GET /user`, and
  requires push access to the approver repo via `GET /repos/{repo}` (plus an optional `DA_APPROVERS` allow-list). Tokens are only sent to
  GitHub and cached in memory by hash for 60s. No identity provider or hosting is needed.
