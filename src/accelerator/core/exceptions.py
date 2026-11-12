"""Domain exceptions for the Legacy-to-Cloud Accelerator."""

from __future__ import annotations


class AcceleratorError(Exception):
    """Base exception for all accelerator errors."""


class ConfigError(AcceleratorError):
    """Invalid or missing client/system configuration."""


class PluginNotFoundError(AcceleratorError):
    """Requested plugin is not registered."""


class PluginExecutionError(AcceleratorError):
    """A plugin failed during execution."""


class StateTransitionError(AcceleratorError):
    """Illegal object lifecycle transition attempted."""


class EvidenceRequiredError(StateTransitionError):
    """Required stored evidence is missing to advance object state."""


class AuthenticationError(AcceleratorError):
    """Invalid or missing authentication credentials."""


class AuthorizationError(AcceleratorError):
    """Authenticated principal lacks the required RBAC permission."""


class SecretsError(AcceleratorError):
    """Failed to retrieve or resolve a credential reference."""


class NotFoundError(AcceleratorError):
    """The requested resource does not exist (or the caller may not know it exists)."""


class UnsafePathError(AcceleratorError):
    """A file path or name points outside the places the product is allowed to read or write."""
