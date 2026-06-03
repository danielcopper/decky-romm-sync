"""Shared certifi CA bundle helper.

Provides a single ``ca_bundle()`` function used by any module that
needs to create an ``ssl.SSLContext`` with the certifi CA bundle
(falling back to system defaults when certifi is not installed).
"""

try:
    import certifi  # type: ignore[import-not-found]  # optional: falls via system or pip

    def ca_bundle():
        """Return the path to the certifi CA bundle, or None if unavailable."""
        return certifi.where()
except ImportError:

    def ca_bundle():
        """Return the path to the certifi CA bundle, or None if unavailable."""
        return
