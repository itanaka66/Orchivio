"""Integration gateway: business events (outbox) delivered to outbound webhooks (FR-082/083)."""

from .dispatcher import (
    Dispatcher,
    DispatchResult,
    SecretUnavailable,
    build_event,
    matches,
    resolve_secret,
    valid_pattern,
)
from .rest import RestClient
from .ssrf import Destination, DestinationError, Policy, check_url, is_public, resolve_public
from .transport import PinnedTransport, Response, Transport, sign, verify_signature


def dispatcher_from_settings(settings, session_factory, preview_engine, *, transport=None) -> Dispatcher:
    """The dispatcher configured from ``Settings`` (destination policy, retry and breaker limits)."""
    policy = Policy(allow_http=settings.webhook_allow_http, allow_private=settings.webhook_allow_private_networks,
                    allowed_hosts=tuple(settings.webhook_allowed_hosts))
    return Dispatcher(session_factory, preview_engine,
                      transport or PinnedTransport(policy, timeout=settings.webhook_timeout_seconds),
                      lease_seconds=settings.worker_lease_seconds, max_attempts=settings.webhook_max_attempts,
                      backoff_seconds=settings.webhook_backoff_seconds, pause_after_failures=settings.webhook_pause_after_failures)


def destination_policy(settings) -> Policy:
    return Policy(allow_http=settings.webhook_allow_http, allow_private=settings.webhook_allow_private_networks,
                  allowed_hosts=tuple(settings.webhook_allowed_hosts))


def integration_client_factory(settings, session_factory, *, transport=None):
    """``application_id -> IntegrationClient``, sharing one pinned transport (destination policy from ``Settings``)."""
    shared = transport or PinnedTransport(destination_policy(settings), timeout=settings.webhook_timeout_seconds)
    return lambda application_id: RestClient(application_id, session_factory, shared)


__all__ = ["integration_client_factory", "RestClient", "dispatcher_from_settings", "destination_policy", "Destination", "DestinationError", "DispatchResult", "Dispatcher", "PinnedTransport", "Policy", "Response",
           "SecretUnavailable", "Transport", "build_event", "check_url", "is_public", "matches", "resolve_public",
           "resolve_secret", "sign", "valid_pattern", "verify_signature"]
