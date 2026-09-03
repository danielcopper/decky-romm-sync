"""BIOS/firmware subsystem — orchestration over the live resolver.

The package's public API is the :class:`FirmwareService` façade. It owns every
status-bearing BIOS query the QAM panel runs: what the RomM server holds, what
the installed emulators want, where each file goes, and what is on disk. What an
emulator wants is never stored — it is read per query through the
``FirmwareResolver`` seam, because it changes with every RetroDECK update and a
stored answer would drift silently. Raw filesystem I/O is delegated to the
``FirmwareFileStore`` Protocol and HTTP traffic flows through ``RommFirmwareApi``;
the classification scope, the destination layout, and the per-core filtering
remain this package's responsibility.

A platform's file list is the **union** of what the RomM library offers and what
the platform's emulators ask for. The two overlap but neither contains the
other: the library holds files nothing wants, and an emulator can want a file
the library has never had. That third kind is shown like any other and marked
``on_server: False`` — it is real, it may be missing, and nothing here can fetch
it, so it counts towards readiness and never towards a download button.

Readiness therefore needs no server at all. Which emulator is active comes from
ES-DE, what it wants comes from the resolver, what is on disk comes from the
filesystem; RomM contributes the download and nothing else. What an unreachable
server costs is the files only it knows about.
"""

from services.firmware.service import FirmwareService, FirmwareServiceConfig

__all__ = ["FirmwareService", "FirmwareServiceConfig"]
