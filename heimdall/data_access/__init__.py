"""Data-source boundary: the interface every adapter implements, and the adapter registry."""

from .interface import (
    AccessReport,
    DataSource,
    DiscoveryQuery,
    DiscoveryResult,
    RetrievalRequest,
    RetrievalResult,
    RetrievedProduct,
    SkyRegion,
    SourceDescription,
)
from .registry import AdapterFactory, AdapterRegistry, default_registry

__all__ = [name for name in dir() if not name.startswith("_")]
