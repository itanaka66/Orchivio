"""Ports: interfaces to infrastructure, with reference adapters (docs/INTERFACES.md)."""

from .audit import AuditRecord, AuditSink, InMemoryAuditSink
from .integration import IntegrationCall, IntegrationClient, IntegrationError, IntegrationResult, MockIntegrations
from .jobs import Handler, InMemoryJobQueue, Job, JobContext, JobQueue, JobStatus
from .storage import ArtifactStore, InvalidKey, LocalArtifactStore, StoredObject, validate_key

__all__ = [
    "IntegrationCall",
    "IntegrationClient",
    "IntegrationError",
    "IntegrationResult",
    "MockIntegrations",
    "ArtifactStore",
    "AuditRecord",
    "AuditSink",
    "Handler",
    "InMemoryAuditSink",
    "InMemoryJobQueue",
    "InvalidKey",
    "Job",
    "JobContext",
    "JobQueue",
    "JobStatus",
    "LocalArtifactStore",
    "StoredObject",
    "validate_key",
]
