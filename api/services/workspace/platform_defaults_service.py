"""Provision platform-wide defaults into a workspace: super admins, the email tool and the platform MCP server.

Driven by SUPER_ADMIN_EMAILS, PLATFORM_EMAIL_TOOL_ENABLED and PLATFORM_MCP_SERVER_*. Each step skips work that
already exists, so the Celery task may retry and the backfill command may be re-run safely.
"""

import logging
from email.utils import parseaddr

from sqlalchemy import select

from configs import dify_config
from core.db.session_factory import session_factory
from core.entities.mcp_provider import MCPConfiguration
from core.helper import marketplace
from core.plugin.entities.plugin import PluginCategory
from core.plugin.entities.plugin_daemon import CredentialType, PluginInstallTaskStatus
from core.plugin.plugin_service import PluginService
from enums.account import TenantAccountRole
from models.account import Account, AccountStatus, TenantAccountJoin
from models.tools import BuiltinToolProvider, MCPToolProvider
from services.tools.builtin_tools_manage_service import (
    PLATFORM_EMAIL_CREDENTIAL_NAME as EMAIL_CREDENTIAL_NAME,
)
from services.tools.builtin_tools_manage_service import (
    PLATFORM_EMAIL_TOOL_PROVIDER as EMAIL_TOOL_PROVIDER,
)
from services.tools.builtin_tools_manage_service import BuiltinToolManageService
from services.tools.mcp_tools_manage_service import MCPToolManageService
from services.workspace.member_service import WorkspaceMemberService

logger = logging.getLogger(__name__)

EMAIL_PLUGIN_ID = "langgenius/email"


def provision_workspace(
    tenant_id: str, *, members: WorkspaceMemberService, install_task_id: str | None = None
) -> str | None:
    """Apply every configured platform default to one workspace.

    Returns:
        None when the workspace is fully provisioned, or the plugin install task id to wait for before the
        email tool credential can be saved.
    """
    owner_id = _owner_id(tenant_id)
    if owner_id is None:
        logger.warning("Skipping platform defaults for tenant %s: no owner", tenant_id)
        return None
    join_super_admins(tenant_id, members=members)
    add_platform_mcp_server(tenant_id, owner_id)
    return add_platform_email_tool(tenant_id, owner_id, install_task_id=install_task_id)


def join_super_admins(tenant_id: str, *, members: WorkspaceMemberService) -> None:
    emails = dify_config.SUPER_ADMIN_EMAIL_LIST
    if not emails:
        return
    with session_factory.create_session() as session:
        accounts = session.execute(
            # Only accounts that completed sign-in; a pending invite must not inherit admin rights.
            select(Account.id, Account.email).where(Account.email.in_(emails), Account.status == AccountStatus.ACTIVE)
        ).all()
    for account_id, email in accounts:
        if members.get_role(tenant_id, account_id) is not None:
            continue
        members.join_member(
            workspace_id=tenant_id,
            account_id=account_id,
            email=email,
            role=TenantAccountRole.ADMIN,
            operator_account_id=None,
        )
        logger.info("Joined super admin %s to tenant %s", account_id, tenant_id)


def add_platform_mcp_server(tenant_id: str, owner_id: str) -> None:
    server_url = dify_config.PLATFORM_MCP_SERVER_URL
    if not server_url:
        return
    identifier = dify_config.PLATFORM_MCP_SERVER_IDENTIFIER
    configuration = MCPConfiguration()
    with session_factory.create_session() as session, session.begin():
        exists = session.scalar(
            select(MCPToolProvider.id).where(
                MCPToolProvider.tenant_id == tenant_id, MCPToolProvider.server_identifier == identifier
            )
        )
        if exists:
            return
        try:
            provider = MCPToolManageService(session=session).create_provider(
                tenant_id=tenant_id,
                name=dify_config.PLATFORM_MCP_SERVER_NAME,
                server_url=server_url,
                user_id=owner_id,
                icon="🔌",
                icon_type="emoji",
                icon_background="#EFF1F5",
                server_identifier=identifier,
                configuration=configuration,
            )
        except ValueError:
            # A member already added this server under another identifier; keep theirs.
            logger.warning("Platform MCP server not added to tenant %s", tenant_id, exc_info=True)
            return

    logger.info("Added platform MCP server %s to tenant %s", identifier, tenant_id)

    # Best-effort, outside the transaction, like the console: members can refresh the tools if this fails.
    try:
        reconnect = MCPToolManageService.reconnect_with_url(
            server_url=server_url,
            headers={},
            timeout=configuration.timeout,
            sse_read_timeout=configuration.sse_read_timeout,
        )
        with session_factory.create_session() as session, session.begin():
            db_provider = MCPToolManageService(session=session).get_provider_by_id(
                provider_id=provider.id, tenant_id=tenant_id
            )
            db_provider.authed = reconnect.authed
            db_provider.tools = reconnect.tools
    except Exception:
        logger.warning("Failed to load platform MCP tools for tenant %s", tenant_id, exc_info=True)


def add_platform_email_tool(tenant_id: str, owner_id: str, *, install_task_id: str | None = None) -> str | None:
    if not dify_config.PLATFORM_EMAIL_TOOL_ENABLED:
        return None
    with session_factory.create_session() as session:
        exists = session.scalar(
            select(BuiltinToolProvider.id).where(
                BuiltinToolProvider.tenant_id == tenant_id, BuiltinToolProvider.provider == EMAIL_TOOL_PROVIDER
            )
        )
    if exists:
        return None

    if install_task_id:
        install_task = PluginService.fetch_install_task(tenant_id, install_task_id)
        if install_task.status in (PluginInstallTaskStatus.Pending, PluginInstallTaskStatus.Running):
            return install_task_id
        if install_task.status == PluginInstallTaskStatus.Failed:
            raise RuntimeError(f"Installing {EMAIL_PLUGIN_ID} failed for tenant {tenant_id}")
    elif EMAIL_PLUGIN_ID not in PluginService.list_installed_plugin_ids(tenant_id, PluginCategory.Tool):
        manifests = marketplace.batch_fetch_plugin_manifests([EMAIL_PLUGIN_ID])
        if not manifests:
            raise RuntimeError(f"{EMAIL_PLUGIN_ID} not found in the marketplace")
        response = PluginService.install_from_marketplace_pkg(tenant_id, [manifests[0].latest_package_identifier])
        if not response.all_installed:
            return response.task_id

    BuiltinToolManageService.add_builtin_tool_provider(
        user_id=owner_id,
        api_type=CredentialType.API_KEY,
        tenant_id=tenant_id,
        provider=EMAIL_TOOL_PROVIDER,
        credentials=email_tool_credentials(),
        name=EMAIL_CREDENTIAL_NAME,
    )
    logger.info("Added platform email tool credential to tenant %s", tenant_id)
    return None


def email_tool_credentials() -> dict[str, str]:
    """Map the platform SMTP_* mail settings onto the langgenius/email credential form."""
    tls_method = "TLS" if dify_config.SMTP_OPPORTUNISTIC_TLS else "SSL"
    encrypt_method = tls_method if dify_config.SMTP_USE_TLS else "NONE"
    return {
        "email_account": dify_config.SMTP_USERNAME or "",
        "email_password": dify_config.SMTP_PASSWORD or "",
        "smtp_server": dify_config.SMTP_SERVER or "",
        "smtp_port": str(dify_config.SMTP_PORT or ""),
        "encrypt_method": encrypt_method,
        "sender_address": parseaddr(dify_config.MAIL_DEFAULT_SEND_FROM or "")[1],
    }


def _owner_id(tenant_id: str) -> str | None:
    with session_factory.create_session() as session:
        return session.scalar(
            select(TenantAccountJoin.account_id).where(
                TenantAccountJoin.tenant_id == tenant_id, TenantAccountJoin.role == TenantAccountRole.OWNER
            )
        )
