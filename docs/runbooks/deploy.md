# Runbook: first deployment to Azure

**Status: not yet executed.** No subscription was available while writing it. Treat each step as a checklist to verify.

## 0. Prerequisites
- An Azure subscription and an ACR. `az login`.
- **Entra app registration** (web, confidential):
  - Redirect URI `https://<public_hostname>/v1/auth/callback`.
  - Scopes `openid email profile`.
  - Create a client secret.
  - Enable the optional `email` claim, and confirm whether `email_verified` is emitted. If it isn't, provision users with their `oid`/`sub` instead of linking by email.
- A Terraform state storage account (`env/<name>.backend.hcl`).

## 1. Build and push images (immutable tags)
```bash
TAG=$(git rev-parse --short HEAD)
az acr build -r <acr> -t documind-core-api:$TAG services/core-api
az acr build -r <acr> -t documind-web:$TAG apps/web
```

## 2. Infrastructure
```bash
cd infra/terraform
terraform init -backend-config=env/prod.backend.hcl
terraform plan -out tfplan -var name=dmprod -var image_tag=$TAG -var registry=<acr>.azurecr.io \
  -var oidc_issuer=https://login.microsoftonline.com/<tenant>/v2.0 -var oidc_client_id=<app-id> \
  -var public_hostname=documind.example.com
terraform apply tfplan
az keyvault secret set --vault-name kv-dmprod --name oidc-client-secret --value '<secret>'
```
Grant the API identity `AcrPull` on the registry if it isn't already assigned.

## 3. Database roles (once, as the server admin)
Run the equivalent of `infra/compose/initdb/01-roles.sh` with the **Key Vault** passwords. Create `documind_owner`, `documind_app` and `documind_worker` (`NOSUPERUSER NOBYPASSRLS`), plus schema `documind` owned by `documind_owner`, with `search_path` per role. **Do not** run the dev seed.

## 4. Release
1. `az containerapp job start -n caj-dmprod-migrate -g rg-dmprod`, and wait for it to succeed.
2. The new revisions of `api`, `worker` and `web` roll out (Terraform `image_tag`).
3. Smoke test:
   - `/healthz` and `/readyz` on the api (from inside the environment).
   - SSO sign-in as a provisioned user.
   - Upload a small PDF and confirm it reaches `ready`.
   - Ask a question and confirm the citation.
   - Check that the response headers include the CSP.

## 5. Rollback
Container Apps: re-activate the previous revision. Migrations are additive (expand → migrate → contract), so the previous image runs against the newer schema. Never roll the database back by restoring a backup unless data loss is accepted in the incident.
