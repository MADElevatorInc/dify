from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from configs import dify_config
from models.account import Account, AccountStatus, Tenant, TenantAccountJoin, TenantAccountRole
from models.tools import BuiltinToolProvider, MCPToolProvider
from services.tools import mcp_tools_manage_service
from services.workspace import platform_defaults_service as service


@pytest.fixture
def workspace(sqlite_session: Session) -> tuple[str, str]:
    owner = Account(name="Owner", email="owner@example.com")
    tenant = Tenant(name="Workspace")
    sqlite_session.add_all(
        [owner, tenant, TenantAccountJoin(account_id=owner.id, tenant_id=tenant.id, role=TenantAccountRole.OWNER)]
    )
    sqlite_session.commit()
    return tenant.id, owner.id


def test_super_admins_join_as_admin_once_and_banned_accounts_are_skipped(
    sqlite_session: Session, workspace: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, owner_id = workspace
    boss = Account(name="Boss", email="boss@example.com")
    banned = Account(name="Banned", email="banned@example.com", status=AccountStatus.BANNED)
    sqlite_session.add_all([boss, banned])
    sqlite_session.commit()
    monkeypatch.setattr(dify_config, "SUPER_ADMIN_EMAILS", "Boss@Example.com, banned@example.com, owner@example.com")
    members = Mock()
    members.get_role.side_effect = lambda _, account_id: TenantAccountRole.OWNER if account_id == owner_id else None

    service.join_super_admins(tenant_id, members=members)

    members.join_member.assert_called_once_with(
        workspace_id=tenant_id,
        account_id=boss.id,
        email="boss@example.com",
        role=TenantAccountRole.ADMIN,
        operator_account_id=None,
    )


def test_email_tool_uses_platform_smtp_settings(workspace: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id, owner_id = workspace
    for key, value in {
        "PLATFORM_EMAIL_TOOL_ENABLED": True,
        "SMTP_SERVER": "smtp.example.com",
        "SMTP_PORT": 465,
        "SMTP_USERNAME": "user",
        "SMTP_PASSWORD": "secret",
        "SMTP_USE_TLS": True,
        "SMTP_OPPORTUNISTIC_TLS": False,
        "MAIL_DEFAULT_SEND_FROM": "Madify <noreply@example.com>",
    }.items():
        monkeypatch.setattr(dify_config, key, value)
    monkeypatch.setattr(
        service.PluginService, "list_installed_plugin_ids", Mock(return_value=[service.EMAIL_PLUGIN_ID])
    )
    add = Mock()
    monkeypatch.setattr(service.BuiltinToolManageService, "add_builtin_tool_provider", add)

    assert service.add_platform_email_tool(tenant_id, owner_id) is None

    credentials = add.call_args.kwargs["credentials"]
    assert credentials == {
        "email_account": "user",
        "email_password": "secret",
        "smtp_server": "smtp.example.com",
        "smtp_port": "465",
        "encrypt_method": "SSL",
        "sender_address": "noreply@example.com",
    }
    assert add.call_args.kwargs["tenant_id"] == tenant_id
    assert add.call_args.kwargs["user_id"] == owner_id


def test_email_tool_waits_for_plugin_install_and_skips_when_configured(
    sqlite_session: Session, workspace: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, owner_id = workspace
    monkeypatch.setattr(dify_config, "PLATFORM_EMAIL_TOOL_ENABLED", True)
    monkeypatch.setattr(service.PluginService, "list_installed_plugin_ids", Mock(return_value=[]))
    monkeypatch.setattr(
        service.marketplace,
        "batch_fetch_plugin_manifests",
        Mock(return_value=[SimpleNamespace(latest_package_identifier="langgenius/email:1@abc")]),
    )
    install = Mock(return_value=SimpleNamespace(all_installed=False, task_id="install-1"))
    monkeypatch.setattr(service.PluginService, "install_from_marketplace_pkg", install)
    add = Mock()
    monkeypatch.setattr(service.BuiltinToolManageService, "add_builtin_tool_provider", add)

    assert service.add_platform_email_tool(tenant_id, owner_id) == "install-1"
    install.assert_called_once_with(tenant_id, ["langgenius/email:1@abc"])
    add.assert_not_called()

    sqlite_session.add(
        BuiltinToolProvider(name="Mine", tenant_id=tenant_id, user_id=owner_id, provider=service.EMAIL_TOOL_PROVIDER)
    )
    sqlite_session.commit()
    assert service.add_platform_email_tool(tenant_id, owner_id, install_task_id="install-1") is None
    add.assert_not_called()


def test_nothing_is_provisioned_when_platform_defaults_are_off(
    workspace: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, _ = workspace
    monkeypatch.setattr(dify_config, "SUPER_ADMIN_EMAILS", "")
    monkeypatch.setattr(dify_config, "PLATFORM_EMAIL_TOOL_ENABLED", False)
    monkeypatch.setattr(dify_config, "PLATFORM_MCP_SERVER_URL", "")
    create = Mock()
    monkeypatch.setattr(service.MCPToolManageService, "create_provider", create)
    members = Mock()

    assert service.provision_workspace(tenant_id, members=members) is None

    create.assert_not_called()
    members.join_member.assert_not_called()


def test_platform_mcp_server_is_added_once_with_its_tools(
    sqlite_session: Session, workspace: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, owner_id = workspace
    monkeypatch.setattr(dify_config, "PLATFORM_MCP_SERVER_URL", "http://mcp.example.com/mcp")
    monkeypatch.setattr(dify_config, "PLATFORM_MCP_SERVER_NAME", "Syspro-MCP")
    monkeypatch.setattr(dify_config, "PLATFORM_MCP_SERVER_IDENTIFIER", "syspro-erp")
    monkeypatch.setattr(mcp_tools_manage_service.encrypter, "encrypt_token", lambda _, value: f"enc:{value}")
    # The API entity loads the creator through the Flask-bound session, which unit tests do not register.
    monkeypatch.setattr(
        mcp_tools_manage_service.ToolTransformService,
        "mcp_provider_to_user_provider",
        lambda provider: SimpleNamespace(id=provider.id),
    )
    reconnect = Mock(return_value=SimpleNamespace(authed=True, tools='[{"name": "execute_sql"}]'))
    monkeypatch.setattr(service.MCPToolManageService, "reconnect_with_url", reconnect)

    service.add_platform_mcp_server(tenant_id, owner_id)
    service.add_platform_mcp_server(tenant_id, owner_id)

    providers = sqlite_session.scalars(select(MCPToolProvider).where(MCPToolProvider.tenant_id == tenant_id)).all()
    assert [(p.name, p.server_identifier, p.user_id, p.authed) for p in providers] == [
        ("Syspro-MCP", "syspro-erp", owner_id, True)
    ]
    assert providers[0].tools == '[{"name": "execute_sql"}]'
    reconnect.assert_called_once()
