"""Queue platform defaults (super admins, email tool, MCP server) after tenant creation."""

import logging

from configs import dify_config
from events.tenant_event import tenant_was_created
from tasks.provision_platform_defaults_task import provision_platform_defaults_task

logger = logging.getLogger(__name__)


@tenant_was_created.connect
def handle(sender, **kwargs) -> None:
    """Keep tenant creation non-blocking while provisioning platform defaults asynchronously."""
    if not (
        dify_config.SUPER_ADMIN_EMAIL_LIST
        or dify_config.PLATFORM_EMAIL_TOOL_ENABLED
        or dify_config.PLATFORM_MCP_SERVER_URL
    ):
        return

    try:
        provision_platform_defaults_task.delay(sender.id)
    except Exception:
        logger.exception("Failed to queue platform defaults for tenant %s", sender.id)
