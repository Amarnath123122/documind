resource "azurerm_container_app_environment" "env" {
  name                       = "cae-${var.name}"
  resource_group_name        = azurerm_resource_group.rg.name
  location                   = var.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.logs.id
  tags                       = local.tags
}

locals {
  common_env = {
    ENV                    = "production"
    AUTH_MODE              = "oidc"
    OIDC_ISSUER            = var.oidc_issuer
    OIDC_CLIENT_ID         = var.oidc_client_id
    OIDC_REDIRECT_URI      = "https://${var.public_hostname}/v1/auth/callback"
    BLOB_CONNECTION_STRING = azurerm_storage_account.blob.primary_blob_endpoint
    BLOB_PUBLIC_BASE       = trimsuffix(azurerm_storage_account.blob.primary_blob_endpoint, "/")
  }
  kv = { for k, v in azurerm_key_vault_secret.s : k => v.versionless_id }
}

resource "azurerm_container_app" "api" {
  name                         = "ca-${var.name}-api"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.api.id]
  }

  registry {
    server   = var.registry
    identity = azurerm_user_assigned_identity.api.id
  }

  secret {
    name                = "session-secret"
    key_vault_secret_id = local.kv["session-secret"]
    identity            = azurerm_user_assigned_identity.api.id
  }

  secret {
    name                = "db-url-app"
    key_vault_secret_id = local.kv["db-url-app"]
    identity            = azurerm_user_assigned_identity.api.id
  }

  secret {
    name                = "oidc-client-secret"
    key_vault_secret_id = "${azurerm_key_vault.kv.vault_uri}secrets/oidc-client-secret"
    identity            = azurerm_user_assigned_identity.api.id
  }

  ingress {
    external_enabled = false # only the web app in the same environment reaches the API
    target_port      = 8000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = local.size.api_min
    max_replicas = 10

    container {
      name   = "api"
      image  = local.img["core-api"]
      cpu    = 0.5
      memory = "1Gi"

      dynamic "env" {
        for_each = merge(local.common_env, { AZURE_CLIENT_ID = azurerm_user_assigned_identity.api.client_id })
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name        = "SESSION_SECRET"
        secret_name = "session-secret"
      }
      env {
        name        = "DATABASE_URL"
        secret_name = "db-url-app"
      }
      env {
        name        = "OIDC_CLIENT_SECRET"
        secret_name = "oidc-client-secret"
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/healthz"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/readyz"
      }
    }

    http_scale_rule {
      name                = "http"
      concurrent_requests = "50"
    }
  }
}

resource "azurerm_container_app" "worker" {
  name                         = "ca-${var.name}-worker"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.worker.id]
  }

  registry {
    server   = var.registry
    identity = azurerm_user_assigned_identity.worker.id
  }

  secret {
    name                = "session-secret"
    key_vault_secret_id = local.kv["session-secret"]
    identity            = azurerm_user_assigned_identity.worker.id
  }

  secret {
    name                = "db-url-worker"
    key_vault_secret_id = local.kv["db-url-worker"]
    identity            = azurerm_user_assigned_identity.worker.id
  }

  template {
    # Always 1: the worker polls Postgres, so no HTTP traffic would ever wake it from zero.
    min_replicas = 1
    max_replicas = 5

    container {
      name    = "worker"
      image   = local.img["core-api"]
      command = ["python", "-m", "documind.ingestion.worker"]
      cpu     = local.size.worker_cpu
      memory  = local.size.worker_mem

      dynamic "env" {
        for_each = merge(local.common_env, { AZURE_CLIENT_ID = azurerm_user_assigned_identity.worker.client_id })
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name        = "SESSION_SECRET"
        secret_name = "session-secret"
      }
      env {
        name        = "WORKER_DATABASE_URL"
        secret_name = "db-url-worker"
      }
    }
  }
}

resource "azurerm_container_app" "web" {
  name                         = "ca-${var.name}-web"
  resource_group_name          = azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.env.id
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.api.id]
  }

  registry {
    server   = var.registry
    identity = azurerm_user_assigned_identity.api.id
  }

  ingress {
    external_enabled = true
    target_port      = 8080
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = local.size.web_min
    max_replicas = 6

    container {
      name   = "web"
      image  = local.img["web"]
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "API_UPSTREAM"
        value = "http://${azurerm_container_app.api.name}"
      }
      env {
        name  = "BLOB_ORIGIN"
        value = trimsuffix(azurerm_storage_account.blob.primary_blob_endpoint, "/")
      }
    }
  }
}

# Migrations run once per release, before the new revisions take traffic.
resource "azurerm_container_app_job" "migrate" {
  name                         = "caj-${var.name}-migrate"
  resource_group_name          = azurerm_resource_group.rg.name
  location                     = var.location
  container_app_environment_id = azurerm_container_app_environment.env.id
  replica_timeout_in_seconds   = 600
  tags                         = local.tags

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.api.id]
  }

  registry {
    server   = var.registry
    identity = azurerm_user_assigned_identity.api.id
  }

  secret {
    name                = "db-url-migration"
    key_vault_secret_id = local.kv["db-url-migration"]
    identity            = azurerm_user_assigned_identity.api.id
  }

  secret {
    name                = "session-secret"
    key_vault_secret_id = local.kv["session-secret"]
    identity            = azurerm_user_assigned_identity.api.id
  }

  template {
    container {
      name    = "migrate"
      image   = local.img["core-api"]
      command = ["alembic", "upgrade", "head"]
      cpu     = 0.5
      memory  = "1Gi"

      env {
        name        = "MIGRATION_DATABASE_URL"
        secret_name = "db-url-migration"
      }
      env {
        name        = "SESSION_SECRET"
        secret_name = "session-secret"
      }
    }
  }
}
