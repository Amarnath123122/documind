from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings

_PG = "postgresql+asyncpg://{}@localhost:55432/documind"
# Azurite's documented public development key; never valid against real Azure.
AZURITE = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://localhost:10000/devstoreaccount1;"
)


class Settings(BaseSettings):
    env: Literal["local", "test", "production"] = "local"
    auth_mode: Literal["dev", "oidc"] = "dev"
    database_url: str = _PG.format("documind_app:dev-app-pw")
    worker_database_url: str = _PG.format("documind_worker:dev-worker-pw")
    migration_database_url: str = _PG.format("documind_owner:dev-owner-pw")
    seed_database_url: str = _PG.format("postgres:dev-superuser-pw")
    session_secret: str = "dev-only-change-me-0123456789abcdef"
    session_max_age_s: int = 8 * 3600

    # OIDC (AUTH_MODE=oidc). Confidential client: the API acts as the BFF (ADR-0009).
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:5173/v1/auth/callback"

    # Azurite connection string locally; "https://<account>.blob.core.windows.net" in Azure.
    blob_connection_string: str = AZURITE
    # Base URL the *browser* uses for SAS uploads (relative path = same-origin proxy).
    blob_public_base: str = "http://localhost:10000/devstoreaccount1"
    max_upload_bytes: int = 25 * 1024 * 1024
    upload_ttl_s: int = 15 * 60
    max_pages: int = 500

    llm_provider: Literal["mock", "ollama"] = "mock"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"
    retrieval_candidates: int = 20
    retrieval_top_k: int = 6
    max_context_chars: int = 6000
    max_output_tokens: int = 512
    max_question_chars: int = 2000

    otel_endpoint: str = ""  # e.g. http://jaeger:4318; empty disables export

    worker_lease_s: int = 120
    worker_poll_s: float = 1.0

    @model_validator(mode="after")
    def _fail_closed_in_production(self) -> "Settings":
        # Invariant: test/dev auth and dev storage keys can never ship to production.
        if self.env == "production":
            if self.auth_mode == "dev":
                raise ValueError("AUTH_MODE=dev is forbidden when ENV=production")
            if self.session_secret.startswith("dev-") or len(self.session_secret) < 32:
                raise ValueError("SESSION_SECRET must be a strong non-dev secret in production")
            if not (self.oidc_issuer.startswith("https://") and self.oidc_client_id
                    and self.oidc_redirect_uri.startswith("https://")):
                raise ValueError("Production needs an https OIDC issuer, client id and redirect")
            if "devstoreaccount1" in self.blob_connection_string:
                raise ValueError("Azurite storage is forbidden when ENV=production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
