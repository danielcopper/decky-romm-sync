"""RomM adapter — REST API surface and its HTTP transport.

Anything that speaks the RomM REST protocol lives here. ``romm_api``
is the public surface consumed by services through the sub-Protocols in
``services/protocols/transport.py``; ``http`` is the transport it sits
on top of. Both modules are deep-imported by bootstrap and tests, so
this package intentionally re-exports nothing.
"""
