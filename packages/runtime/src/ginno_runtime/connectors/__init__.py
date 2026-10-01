"""Connector module (connector-module-design.md)."""

from .builtin import ensure_builtin_connectors
from .registry import (
    STATUS_CONNECTED,
    STATUS_DISABLED,
    STATUS_DISCONNECTED,
    STATUS_ERROR,
    STATUS_INSTALLING,
    STATUS_NOT_INSTALLED,
    Connector,
    ConnectorRegistry,
    registry,
)

__all__ = [
    "Connector",
    "ConnectorRegistry",
    "registry",
    "ensure_builtin_connectors",
    "STATUS_CONNECTED",
    "STATUS_DISCONNECTED",
    "STATUS_NOT_INSTALLED",
    "STATUS_INSTALLING",
    "STATUS_ERROR",
    "STATUS_DISABLED",
]
