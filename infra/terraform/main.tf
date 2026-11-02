locals {
  tags = { app = "documind", env = var.name, managed_by = "terraform" }
  img  = { for k in ["core-api", "web"] : k => "${var.registry}/documind-${k}:${var.image_tag}" }
}

data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "rg" {
  name     = "rg-${var.name}"
  location = var.location
  tags     = local.tags
}

resource "azurerm_log_analytics_workspace" "logs" {
  name                = "log-${var.name}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  retention_in_days   = 30
  tags                = local.tags
}

# One identity per runtime, mirroring the separate API and worker database roles.
resource "azurerm_user_assigned_identity" "api" {
  name                = "id-${var.name}-api"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
}

resource "azurerm_user_assigned_identity" "worker" {
  name                = "id-${var.name}-worker"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
}

# ---------- secrets ----------

resource "azurerm_key_vault" "kv" {
  name                       = "kv-${var.name}"
  resource_group_name        = azurerm_resource_group.rg.name
  location                   = var.location
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = true
  soft_delete_retention_days = 30
  tags                       = local.tags
}

resource "random_password" "db" {
  for_each = toset(["admin", "owner", "app", "worker"])
  length   = 40
  special  = false
}

resource "random_password" "session" {
  length  = 64
  special = false
}

resource "azurerm_role_assignment" "kv_deployer" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

locals {
  pg_host = azurerm_postgresql_flexible_server.pg.fqdn
  secrets = {
    "session-secret"   = random_password.session.result
    "db-url-app"       = "postgresql+asyncpg://documind_app:${random_password.db["app"].result}@${local.pg_host}:5432/documind?ssl=require"
    "db-url-worker"    = "postgresql+asyncpg://documind_worker:${random_password.db["worker"].result}@${local.pg_host}:5432/documind?ssl=require"
    "db-url-migration" = "postgresql+asyncpg://documind_owner:${random_password.db["owner"].result}@${local.pg_host}:5432/documind?ssl=require"
  }
}

resource "azurerm_key_vault_secret" "s" {
  for_each     = nonsensitive(toset(keys(local.secrets)))
  name         = each.key
  value        = local.secrets[each.key]
  key_vault_id = azurerm_key_vault.kv.id
  depends_on   = [azurerm_role_assignment.kv_deployer]
}

# The OIDC client secret is created in Entra ID and stored out-of-band:
#   az keyvault secret set --vault-name kv-NAME --name oidc-client-secret --value ...

resource "azurerm_role_assignment" "kv_read" {
  for_each = {
    api    = azurerm_user_assigned_identity.api.principal_id
    worker = azurerm_user_assigned_identity.worker.principal_id
  }
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = each.value
}

# ---------- data ----------

resource "azurerm_postgresql_flexible_server" "pg" {
  name                          = "psql-${var.name}"
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = var.location
  version                       = "16"
  sku_name                      = var.postgres_sku
  storage_mb                    = 65536
  backup_retention_days         = 14
  geo_redundant_backup_enabled  = true
  public_network_access_enabled = true # ADR-0009: move to VNet + private endpoint before GA
  administrator_login           = "dmadmin"
  administrator_password        = random_password.db["admin"].result
  zone                          = "1"
  tags                          = local.tags

  high_availability {
    mode = "ZoneRedundant"
  }

  lifecycle {
    ignore_changes = [zone, high_availability[0].standby_availability_zone]
  }
}

resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.pg.id
  value     = "VECTOR"
}

resource "azurerm_postgresql_flexible_server_database" "db" {
  name      = "documind"
  server_id = azurerm_postgresql_flexible_server.pg.id
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "azure" {
  name             = "allow-azure-services"
  server_id        = azurerm_postgresql_flexible_server.pg.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

resource "azurerm_storage_account" "blob" {
  name                            = "st${var.name}docs"
  resource_group_name             = azurerm_resource_group.rg.name
  location                        = var.location
  account_tier                    = "Standard"
  account_replication_type        = "ZRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  shared_access_key_enabled       = false # only Entra ID + user-delegation SAS
  allow_nested_items_to_be_public = false
  tags                            = local.tags

  blob_properties {
    delete_retention_policy {
      days = 7
    }
    # The browser PUTs bytes directly with a write-only SAS: only our origin, only PUT.
    cors_rule {
      allowed_origins    = ["https://${var.public_hostname}"]
      allowed_methods    = ["PUT"]
      allowed_headers    = ["x-ms-blob-type", "content-type", "content-length"]
      exposed_headers    = ["etag"]
      max_age_in_seconds = 600
    }
  }
}

resource "azurerm_storage_container" "c" {
  for_each              = toset(["quarantine", "originals"])
  name                  = each.key
  storage_account_id    = azurerm_storage_account.blob.id
  container_access_type = "private"
}

# api: verifies uploaded bytes and mints user-delegation SAS. worker: full blob I/O.
resource "azurerm_role_assignment" "blob" {
  for_each = {
    api_data      = { p = azurerm_user_assigned_identity.api.principal_id, r = "Storage Blob Data Contributor" }
    api_delegator = { p = azurerm_user_assigned_identity.api.principal_id, r = "Storage Blob Delegator" }
    worker_data   = { p = azurerm_user_assigned_identity.worker.principal_id, r = "Storage Blob Data Contributor" }
  }
  scope                = azurerm_storage_account.blob.id
  role_definition_name = each.value.r
  principal_id         = each.value.p
}
