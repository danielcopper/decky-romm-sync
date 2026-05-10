"""ES-DE core info adapter — concrete CoreInfoProvider Protocol implementation.

Wraps the module-level functions in domain.es_de_config behind an instance
boundary so FirmwareService can be tested by injecting a fake rather than
patching the module directly.

Imports from domain.es_de_config are deferred to call time because the
module requires domain.es_de_config.configure() to have been called first;
bootstrap guarantees this before any service method runs.
"""

from __future__ import annotations


class EsDeCoreInfoAdapter:
    """Live CoreInfoProvider backed by domain.es_de_config."""

    def get_active_core(
        self,
        system_name: str,
        rom_filename: str | None = None,
    ) -> tuple[str | None, str | None]:
        from domain import es_de_config

        return es_de_config.get_active_core(system_name, rom_filename=rom_filename)

    def get_available_cores(self, system_name: str) -> list[dict]:
        from domain import es_de_config

        return es_de_config.get_available_cores(system_name)
