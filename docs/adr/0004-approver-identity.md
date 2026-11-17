# ADR-0004: Approver identity (open)

**Status:** accepted; implemented (`DA_ADMIN_AUTH=easyauth`)

**Context.** `POST /admin/incidents/{target}/approve` currently authenticates with a shared admin token and records the
approver name from the request body. That is adequate for a pilot, not for production audit.

**Decision (proposed).** Front the API with API Management / Container Apps Easy Auth (Entra), require an app role for
approvers, and take the approver from the validated principal claim (`oid`/`preferred_username`). Reject body-supplied
identities. Optionally require a second approver for tier-2 runbooks in prod.

**Open questions.** Approval UX channel (GitHub issue comment vs Teams card) and expiry of pending approvals.

## Implementation notes
- `easyauth` mode reads the validated principal from `X-MS-CLIENT-PRINCIPAL(-NAME)`, requires the `Incident.Approver` app role, and ignores any body-supplied approver. Audit entries record `verified_identity: true`.
- **Deployment requirement:** these headers are only trustworthy if the app is reachable exclusively through Easy Auth / APIM. Exclude `/webhooks/*` and `/health*` from Entra auth (GitHub and Azure Monitor use their own HMAC/token); protect `/admin/*`.
- `token` mode remains for dev/pilot and is the Terraform default until Easy Auth is configured.
- Not yet done: two-person approval for tier-2 runbooks, approval expiry.
