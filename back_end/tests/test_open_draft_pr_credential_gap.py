"""A missing GitHub credential at PR-open time must raise, not silently no-op
(integration, needs test Neo4j).

Previously `open_draft_pr_for_ticket` returned "" when `client.is_active` was
False: the work branch was already pushed, but the ticket was left with no PR,
no error, and no way for a human to know why. It now raises, so
`finalize_write_run`'s post-push guard (run_dispatch.py) can audit it as
`implement_run.post_push_failed` — a real, user-visible attention.required
notification.
"""

from uuid import uuid4

import pytest

from domains.delivery.services.implement_run_service import open_draft_pr_for_ticket
from domains.repositories.models import Repository
from domains.tickets.models import Ticket

pytestmark = pytest.mark.integration


class _InactiveClient:
    is_active = False


@pytest.mark.asyncio
async def test_missing_credential_raises_instead_of_silent_empty_uid(monkeypatch):
    repo = Repository(
        uid=uuid4().hex,
        org_uid=uuid4().hex,
        slug=f"repo-{uuid4().hex[:8]}",
        mode="github",
        name="test-repo",
        github_owner="acme",
        github_repo="widgets",
    )
    await repo.save()
    ticket = Ticket(uid=uuid4().hex, repository_uid=repo.uid, title="Do the thing")
    await ticket.save()

    monkeypatch.setattr(
        "domains.delivery.services.implement_run_service.get_provider_client",
        lambda repo: _InactiveClient(),
    )

    with pytest.raises(RuntimeError, match="no usable GitHub credential"):
        await open_draft_pr_for_ticket(
            repository_uid=repo.uid,
            ticket_uid=ticket.uid,
            work_branch="opensweep/abc123-do-the-thing",
            base_branch="main",
        )
