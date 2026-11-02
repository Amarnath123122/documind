"""Roles and permissions. Pure domain: no FastAPI, SQLAlchemy or cloud imports."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    TENANT_ADMIN = "tenant_admin"
    WORKSPACE_EDITOR = "workspace_editor"
    VIEWER = "viewer"


class Permission(StrEnum):
    WORKSPACE_READ = "workspace:read"
    DOCUMENT_READ = "document:read"
    DOCUMENT_CREATE = "document:create"
    DOCUMENT_DELETE = "document:delete"
    DOCUMENT_SHARE = "document:share"
    AUDIT_READ = "audit:read"
    USAGE_VIEW = "usage:view"
    WORKFLOW_RUN = "workflow:run"
    WORKFLOW_APPROVE = "workflow:approve"


_BASE = {Permission.WORKSPACE_READ, Permission.DOCUMENT_READ}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_BASE),
    # Editors may share/restrict documents they created (ownership checked in the use case).
    Role.WORKSPACE_EDITOR: frozenset(_BASE | {Permission.DOCUMENT_CREATE,
                                              Permission.DOCUMENT_SHARE,
                                              Permission.WORKFLOW_RUN}),
    Role.TENANT_ADMIN: frozenset(Permission),
}


@dataclass(frozen=True)
class Membership:
    tenant_id: UUID
    tenant_name: str
    role: Role

    @property
    def permissions(self) -> frozenset[Permission]:
        return ROLE_PERMISSIONS[self.role]


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    email: str
    display_name: str
    memberships: tuple[Membership, ...]

    def membership(self, tenant_id: UUID) -> Membership | None:
        return next((m for m in self.memberships if m.tenant_id == tenant_id), None)

    def can(self, permission: Permission, tenant_id: UUID) -> bool:
        """Fail closed: no membership in that tenant means no permission."""
        m = self.membership(tenant_id)
        return m is not None and permission in m.permissions
