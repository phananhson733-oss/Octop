"""Check the initiating user's expert-template grants before execution.

This restricts use, not history reads. It does not replace ownership checks or
protect expert mutation endpoints; those retain their separate authorization.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from octop.i18n import error_message
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.resource_grants import normalize_resource_grants, role_resource_grants

POLICY_ALLOWED_EXPERT_TEMPLATES = "allowed_expert_templates"


INSTALLING_EXPERT_PREFIX = "expert_assignment_installing:"
READY_PREFIX = "ready:"
DISCARDING_PREFIX = "discarding:"
POLICY_ASSIGNED_EXPERTS = "assigned_expert_templates"


def parse_reservation(value: str) -> tuple[str, str]:
    """Split an installing-key value into ``(state, agent_id)``.

    ``<id>`` is an installation not yet verified, ``ready:<id>`` a verified one awaiting the
    final commit, ``discarding:<id>`` one an admin is removing. All three keep the instance
    from being invoked.
    """
    for state, prefix in (("ready", READY_PREFIX), ("discarding", DISCARDING_PREFIX)):
        if value.startswith(prefix):
            return state, value.removeprefix(prefix)
    return "installing", value


def account_expert_grants(repos: RepoBundle, user_id: int) -> frozenset[str] | None:
    user = repos.user_repo.get(user_id)
    if user is None or user.disabled:
        raise OctopError(ErrorCode.USER_DISABLED, error_message(ErrorCode.USER_DISABLED))
    actor = SimpleNamespace(role=user.role, is_admin=user.role == "admin")
    ceiling = role_resource_grants(repos.db, actor, POLICY_ALLOWED_EXPERT_TEMPLATES)
    personal = repos.user_policy_repo.get(user_id, POLICY_ASSIGNED_EXPERTS)
    if personal is None or actor.is_admin:
        return ceiling
    try:
        selected = (
            frozenset(json.loads(normalize_resource_grants(personal.value)))
            if personal.enabled
            else frozenset()
        )
    except OctopError:
        return frozenset()
    return selected if ceiling is None else selected & ceiling


def assert_expert_invocation(repos: RepoBundle, agent_id: str, user_id: int) -> None:
    grants = account_expert_grants(repos, user_id)
    agent = repos.agent_repo.get(agent_id)
    if agent and agent.user_id is not None and agent.published_expert_id:
        reservation = repos.user_policy_repo.get(
            agent.user_id, INSTALLING_EXPERT_PREFIX + agent.published_expert_id
        )
        if (
            reservation
            and reservation.enabled
            and parse_reservation(reservation.value)[1] == agent_id
        ):
            raise OctopError(ErrorCode.FORBIDDEN, error_message(ErrorCode.FORBIDDEN))
    if grants is None:
        return
    if (
        agent is None
        or agent.published_expert_id not in grants
        or (agent.user_id != user_id and not agent.is_shared)
    ):
        raise OctopError(ErrorCode.FORBIDDEN, error_message(ErrorCode.FORBIDDEN))


def assert_execution_access(
    repos: RepoBundle, agent_id: str, request_user: object, thread_id: str
) -> None:
    """Resolve a server-built request's principal, including background resumes.

    Existing ownerless internal agents remain compatible when no principal is
    available. Customer-owned agents always inherit at least their owner check.
    HTTP/IM adapters must never forward a client-supplied user field unchecked.
    """
    agent = repos.agent_repo.get(agent_id)
    if agent is None:
        raise OctopError(ErrorCode.AGENT_NOT_FOUND, error_message(ErrorCode.AGENT_NOT_FOUND))
    thread = repos.thread_repo.get(thread_id) if thread_id else None
    if thread is not None and thread.agent_id != agent_id:
        raise OctopError(ErrorCode.FORBIDDEN, error_message(ErrorCode.FORBIDDEN))
    user_id: int | None
    if request_user is not None:
        if isinstance(request_user, bool) or not str(request_user).isdigit():
            raise OctopError(ErrorCode.FORBIDDEN, error_message(ErrorCode.FORBIDDEN))
        user_id = int(str(request_user))
        if thread is not None and thread.user_id != user_id:
            actor = repos.user_repo.get(user_id)
            if actor is None or actor.role != "admin":
                raise OctopError(ErrorCode.FORBIDDEN, error_message(ErrorCode.FORBIDDEN))
    else:
        user_id = thread.user_id if thread is not None else agent.user_id
    if user_id is not None:
        assert_expert_invocation(repos, agent_id, user_id)


def assert_expert_management(repos: RepoBundle, user_id: int) -> None:
    """Curated accounts use assigned experts; only unrestricted actors configure them."""
    if account_expert_grants(repos, user_id) is not None:
        raise OctopError(ErrorCode.FORBIDDEN, error_message(ErrorCode.FORBIDDEN))
