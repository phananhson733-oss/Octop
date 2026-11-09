"""Security grant parsing must never turn corrupt data into unrestricted access."""

from types import SimpleNamespace

import pytest

from octop.infra.errors import OctopError
from octop.infra.users.plugin_access import normalize_plugin_ui_grants, plugin_ui_grants


@pytest.mark.parametrize("raw", ["null", "{}", "[1]", "[null]", '[""]', '[" sab-tools"]', "broken"])
def test_invalid_grants_rejected(raw):
    with pytest.raises(OctopError):
        normalize_plugin_ui_grants(raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("[]", frozenset()),
        ("broken", frozenset()),
        ('["sab-tools"]', frozenset({"sab-tools"})),
    ],
)
def test_stored_grants_fail_closed(monkeypatch, raw, expected):
    from octop.infra.db.repos.user_roles import UserRoleRepo

    monkeypatch.setattr(
        UserRoleRepo, "get", lambda self, role_id: SimpleNamespace(policy_value=lambda key: raw)
    )
    assert plugin_ui_grants(None, SimpleNamespace(is_admin=False, role="customer")) == expected


def test_missing_role_denied(monkeypatch):
    from octop.infra.db.repos.user_roles import UserRoleRepo

    monkeypatch.setattr(UserRoleRepo, "get", lambda self, role_id: None)
    assert plugin_ui_grants(None, SimpleNamespace(is_admin=False, role="gone")) == frozenset()


def test_admin_bypasses_grants():
    assert plugin_ui_grants(None, SimpleNamespace(is_admin=True)) is None
