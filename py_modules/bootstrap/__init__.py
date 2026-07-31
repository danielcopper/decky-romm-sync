"""Composition root — the only place concrete adapters meet services.

``adapters.py`` owns adapter instantiation and the typed bundles it
hands to ``main.py``; ``services.py`` turns those bundles into the live
service instances. The names re-exported below are the composition
root's whole public surface — consumers import them from ``bootstrap``,
never from a submodule.
"""

from .adapters import (
    AdapterBundle,
    BootstrapHandles,
    BootstrapResult,
    CallbackBundle,
    RuntimeAdaptersBundle,
    RuntimeBundle,
    StateBundle,
    bootstrap,
)
from .services import WiringConfig, wire_services

__all__ = [
    "AdapterBundle",
    "BootstrapHandles",
    "BootstrapResult",
    "CallbackBundle",
    "RuntimeAdaptersBundle",
    "RuntimeBundle",
    "StateBundle",
    "WiringConfig",
    "bootstrap",
    "wire_services",
]
