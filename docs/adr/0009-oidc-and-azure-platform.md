# ADR-0009: OIDC login in the API-as-BFF; Azure Container Apps platform

**Status:** Accepted (Phase 5). Supersedes the "next" section of ADR-0004.

## Authentication
- **The core API plays the BFF role.** A separate BFF deployable is not justified while one team owns both. The browser only ever talks to the web origin, which proxies `/v1`.
- **Flow:** OIDC Authorization Code + **PKCE (S256)** as a confidential client. `state`, `nonce` and the PKCE verifier travel in a signed, 10-minute, `HttpOnly`, `SameSite=Lax` cookie scoped to `/v1/auth`. Lax is required so the cookie survives the identity provider's top-level redirect; the cookie is single-use.
- **ID token validation:** JWKS signature, an algorithm allow-list (`RS256`/`ES256`/`PS256`, so no `none` and no HMAC confusion), `iss`, `aud`, `exp`/`iat` with 60 s leeway, `nonce`, and a required `sub`. Provider tokens are then **discarded**: DocuMind never calls APIs on the user's behalf, so there is nothing to store or refresh.
- **Identity mapping:** the identity is `(issuer, subject)`, never the email address. On first login, a **pre-provisioned** user may be linked by email only when the provider asserts `email_verified=true`. There is no just-in-time provisioning: an unknown identity gets `401 NOT_PROVISIONED`. Provisioning via SCIM or an admin UI is future work.
- **Session:** DocuMind's own signed `HttpOnly; SameSite=Strict` cookie, as before. Dev login returns 404 in OIDC mode, and the app refuses to start in production without an https issuer and redirect URI.
- **Verified locally** against a containerized OIDC provider (`ghcr.io/navikt/mock-oauth2-server`): full redirect → code → token → session flow, an unprovisioned identity, and forged state. Token validation has offline unit tests: wrong `aud`/`iss`, expiry, nonce replay, wrong key, `alg=none`, and the HS256/public-key confusion attack.

## Production web
An nginx image serves the built SPA and proxies `/v1`, with SSE buffering off for chat streams. It sets a strict CSP (`script-src 'self'`; `connect-src 'self' <blob origin>`; `frame-ancestors 'none'`), HSTS, nosniff, `Referrer-Policy: no-referrer` and Permissions-Policy. The headers come from a shared include in **every** `location`, because nginx drops inherited `add_header` directives otherwise. That bug was caught during verification.

## Azure (infra/terraform)
| Concern | Choice |
|---|---|
| Compute | Container Apps: `web` (external ingress), `api` (internal ingress only), `worker` (no ingress), `migrate` (manual job per release) |
| Identity | One user-assigned managed identity per runtime. ACR pull via identity. No registry passwords. |
| Secrets | Key Vault (RBAC, purge protection). Apps reference secrets by versionless ID. The OIDC client secret is set out-of-band. |
| Database | Postgres Flexible Server 16, zone-redundant HA, geo-redundant backups, `VECTOR` extension allow-listed |
| Storage | ZRS, **`shared_access_key_enabled = false`**. The API mints **user-delegation SAS** (Storage Blob Delegator role). CORS allows PUT from our origin only. |
| Telemetry | Log Analytics for container logs. Set `OTEL_ENDPOINT` to a collector to forward traces to Application Insights. |

`terraform fmt -check` and `terraform validate` pass (azurerm ~> 4.10), and CI runs both.

## Not verified (no Azure subscription in this environment)
- `terraform plan`/`apply`, and runtime behaviour of the Azure resources.
- The **user-delegation SAS** code path (`BlobStorage` https mode). Azurite cannot emulate it.
- Entra ID itself. The flow is standard OIDC and was tested against a mock provider; Entra specifics (v2.0 issuer, `email_verified` claim availability) must be checked when the app registration exists.

## Before GA (tracked)
Postgres and storage behind a VNet with private endpoints (currently public with an Azure-services firewall rule). Entra authentication for Postgres instead of passwords. WAF/Front Door in front of `web`. Alert rules (ADR-0008).
