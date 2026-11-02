import ast
import pathlib
import uuid

import pytest

from documind.common.config import Settings
from documind.documents.domain import ALLOWED, InvalidTransition, transition
from documind.documents.domain import DocumentStatus as S
from documind.identity.domain import Membership, Permission, Principal, Role

T1, T2 = uuid.uuid4(), uuid.uuid4()


def _p(role: Role) -> Principal:
    return Principal(uuid.uuid4(), "x@t", "x", (Membership(T1, "T1", role),))


@pytest.mark.parametrize(("role", "perm", "allowed"), [
    (Role.VIEWER, Permission.DOCUMENT_READ, True),
    (Role.VIEWER, Permission.DOCUMENT_CREATE, False),
    (Role.VIEWER, Permission.DOCUMENT_DELETE, False),
    (Role.WORKSPACE_EDITOR, Permission.DOCUMENT_CREATE, True),
    (Role.WORKSPACE_EDITOR, Permission.DOCUMENT_DELETE, False),
    (Role.TENANT_ADMIN, Permission.DOCUMENT_DELETE, True),
    (Role.TENANT_ADMIN, Permission.AUDIT_READ, True),
])
def test_permission_matrix(role, perm, allowed):
    assert _p(role).can(perm, T1) is allowed


def test_no_membership_fails_closed():
    assert not _p(Role.TENANT_ADMIN).can(Permission.DOCUMENT_READ, T2)


def test_pipeline_happy_path():
    s = S.CREATED
    for nxt in [S.UPLOADING, S.QUARANTINED, S.SCANNING, S.EXTRACTING, S.REDACTING,
                S.CHUNKING, S.EMBEDDING, S.INDEXING, S.READY, S.DELETING, S.DELETED]:
        s = transition(s, nxt)
    assert s is S.DELETED


@pytest.mark.parametrize(("frm", "to"), [
    (S.DELETING, S.READY),       # retried worker cannot revive deleted content
    (S.DELETED, S.DELETING),
    (S.QUARANTINED, S.READY),    # cannot skip scanning
    (S.CREATED, S.INDEXING),
])
def test_forbidden_transitions(frm, to):
    with pytest.raises(InvalidTransition):
        transition(frm, to)


def test_every_state_has_transition_rules():
    assert set(ALLOWED) == set(S)


def test_production_rejects_dev_auth():
    with pytest.raises(ValueError):
        Settings(env="production", auth_mode="dev", session_secret="x" * 40)
    with pytest.raises(ValueError):
        Settings(env="production", auth_mode="oidc", session_secret="dev-weak")


SRC = pathlib.Path(__file__).parents[2] / "src" / "documind"
FORBIDDEN_IN_DOMAIN = ("fastapi", "sqlalchemy", "azure", "starlette", "pydantic")


PURE = sorted([*SRC.glob("*/domain.py"), SRC / "documents" / "content.py"])


@pytest.mark.parametrize("path", PURE, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_domain_has_no_framework_imports(path):
    tree = ast.parse(path.read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    bad = {m for m in mods if m.split(".")[0] in FORBIDDEN_IN_DOMAIN}
    assert not bad, f"{path} imports {bad}"
