# ADR-0004: Cookie session in the API for now; dedicated BFF with OIDC later

**Status:** Accepted (temporary)

**Decision.** Local deterministic identity (`POST /v1/dev/login`) issues a signed `HttpOnly; SameSite=Strict` session cookie plus a double-submit CSRF token bound to the session. The browser talks only to the Vite origin, which proxies `/v1`: same-origin, no CORS, no tokens in JavaScript.

**Guardrails.** `Settings` refuses `ENV=production` with `AUTH_MODE=dev` or a weak secret (unit-tested). The dev login route returns 404 unless `AUTH_MODE=dev`.

**Next.** `apps/bff` performs Entra ID OIDC Authorization Code + PKCE and keeps tokens server-side.
