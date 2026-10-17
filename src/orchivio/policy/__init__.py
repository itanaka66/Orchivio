"""Approval levels and platform authorization."""

from .approval import KNOWN_ACTIONS, ApprovalLevel, PlatformRole, is_allowed, level_for_action

__all__ = ["KNOWN_ACTIONS", "ApprovalLevel", "PlatformRole", "is_allowed", "level_for_action"]
