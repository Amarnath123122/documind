output "web_fqdn" {
  description = "Point the public_hostname CNAME here, then bind the custom domain and managed certificate."
  value       = azurerm_container_app.web.ingress[0].fqdn
}

output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.pg.fqdn
}

output "key_vault" {
  value = azurerm_key_vault.kv.name
}
