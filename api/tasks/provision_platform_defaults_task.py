"""Provision platform-wide defaults (super admins, email tool, MCP server) into one workspace."""

import logging

from celery import shared_task

from extensions.ext_application_services import application_services
from services.workspace.platform_defaults_service import provision_workspace

logger = logging.getLogger(__name__)


@shared_task(queue="plugin", bind=True, max_retries=60, default_retry_delay=10)
def provision_platform_defaults_task(self, tenant_id: str, install_task_id: str | None = None) -> None:
    """Retry while the email plugin installs; every step skips work already done."""
    pending_install = provision_workspace(
        tenant_id, members=application_services().workspaces.members, install_task_id=install_task_id
    )
    if pending_install:
        logger.info("Waiting for plugin install %s before finishing tenant %s", pending_install, tenant_id)
        raise self.retry(args=(tenant_id, pending_install))
