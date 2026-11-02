variable "name" {
  description = "Short environment prefix, e.g. dmprod (lowercase letters/digits, 3-10 chars)."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9]{3,10}$", var.name))
    error_message = "The name must be 3-10 lowercase letters or digits."
  }
}

variable "location" {
  type    = string
  default = "centralindia"
}

variable "image_tag" {
  description = "Immutable image tag (git SHA) for the api/worker and web images."
  type        = string
}

variable "registry" {
  description = "Container registry login server, e.g. myacr.azurecr.io"
  type        = string
}

variable "oidc_issuer" {
  description = "Entra ID issuer: https://login.microsoftonline.com/TENANT_ID/v2.0"
  type        = string
}

variable "oidc_client_id" {
  type = string
}

variable "public_hostname" {
  description = "Hostname users browse to (custom domain bound to the web app)."
  type        = string
}

variable "postgres_sku" {
  type    = string
  default = "GP_Standard_D2ds_v5"
}
