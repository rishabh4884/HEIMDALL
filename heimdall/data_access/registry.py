"""Registry that maps a configured source id to an adapter factory.

Registered adapters: ``irsa-spherex`` (public IRSA archive) and
``local-fits`` (a local directory of SPHEREx L2 FITS files). Test fixtures are
never registered here.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import ENV_DATA_SOURCE, Settings
from ..contracts.outcomes import ErrorCode, HeimdallError
from .interface import DataSource

AdapterFactory = Callable[[Settings], DataSource]


class AdapterRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, source_id: str, factory: AdapterFactory) -> None:
        if source_id in self._factories:
            raise ValueError(f"adapter {source_id} is already registered")
        self._factories[source_id] = factory

    def available(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def create(self, settings: Settings, source_id: str | None = None) -> DataSource:
        wanted = source_id or settings.data_source
        if wanted is None:
            raise HeimdallError.of(
                ErrorCode.SOURCE_NOT_CONFIGURED,
                f"no data source configured; set {ENV_DATA_SOURCE}",
                variable=ENV_DATA_SOURCE,
            )
        factory = self._factories.get(wanted)
        if factory is None:
            raise HeimdallError.of(
                ErrorCode.CONFIG_INVALID,
                f"unknown data source '{wanted}'",
                variable=ENV_DATA_SOURCE,
                available=", ".join(self.available()) or "(none registered)",
            )
        source = factory(settings)
        if source.source_id != wanted:
            raise HeimdallError.of(ErrorCode.INTERNAL, "adapter source_id does not match its registration")
        return source


def _irsa(settings: Settings) -> DataSource:
    from .irsa_spherex import IrsaSpherexSource
    return IrsaSpherexSource(settings)


def _local(settings: Settings) -> DataSource:
    from .local_fits import LocalFitsSource
    return LocalFitsSource(settings)


# Process-wide registry used by the application.
default_registry = AdapterRegistry()
default_registry.register("irsa-spherex", _irsa)
default_registry.register("local-fits", _local)
