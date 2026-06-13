"""Tests for lib/url_host.py — canonical origin derivation + comparison."""

import pytest

from lib.url_host import is_valid_server_url, normalize_origin, same_origin


class TestNormalizeOrigin:
    def test_basic_https(self):
        assert normalize_origin("https://romm.local") == "https://romm.local"

    def test_basic_http(self):
        assert normalize_origin("http://romm.local") == "http://romm.local"

    def test_default_https_port_folded_out(self):
        assert normalize_origin("https://romm.local:443") == "https://romm.local"

    def test_default_http_port_folded_out(self):
        assert normalize_origin("http://romm.local:80") == "http://romm.local"

    def test_non_default_port_kept(self):
        assert normalize_origin("https://romm.local:8443") == "https://romm.local:8443"

    def test_scheme_lowercased(self):
        assert normalize_origin("HTTPS://romm.local") == "https://romm.local"

    def test_host_lowercased(self):
        assert normalize_origin("https://RomM.Local") == "https://romm.local"

    def test_trailing_slash_dropped(self):
        assert normalize_origin("https://romm.local/") == "https://romm.local"

    def test_path_dropped(self):
        assert normalize_origin("https://romm.local/romm/") == "https://romm.local"

    def test_query_and_fragment_dropped(self):
        assert normalize_origin("https://romm.local/x?y=1#frag") == "https://romm.local"

    def test_scheme_sensitivity(self):
        """https and http are DIFFERENT origins (no downgrade folding)."""
        assert normalize_origin("https://romm.local") != normalize_origin("http://romm.local")

    def test_port_sensitivity(self):
        assert normalize_origin("https://romm.local:8080") != normalize_origin("https://romm.local:9090")

    def test_path_origin_equals_bare_origin(self):
        assert normalize_origin("https://romm.local/romm/") == normalize_origin("https://romm.local")

    # ── IPv6 literals ────────────────────────────────────────────────────────

    def test_ipv6_with_port_keeps_brackets(self):
        assert normalize_origin("https://[::1]:8080") == "https://[::1]:8080"

    def test_ipv6_with_port_round_trips(self):
        """The normalized IPv6 origin must re-parse through normalize_origin unchanged."""
        once = normalize_origin("https://[::1]:8080")
        assert once is not None
        assert normalize_origin(once) == once

    def test_ipv6_default_portless_keeps_brackets(self):
        assert normalize_origin("https://[::1]") == "https://[::1]"

    def test_ipv6_default_port_folded_out(self):
        assert normalize_origin("https://[::1]:443") == "https://[::1]"

    def test_ipv6_with_path_strips_path(self):
        assert normalize_origin("https://[::1]:8080/romm") == "https://[::1]:8080"

    def test_ipv6_full_address_lowercased(self):
        assert normalize_origin("https://[2001:DB8::1]:8080") == "https://[2001:db8::1]:8080"

    # ── trailing-dot FQDN ─────────────────────────────────────────────────────

    def test_trailing_dot_stripped(self):
        assert normalize_origin("https://host.com.") == "https://host.com"

    def test_trailing_dot_with_port_stripped(self):
        assert normalize_origin("https://host.com.:8443") == "https://host.com:8443"

    # ── rejects ────────────────────────────────────────────────────────────

    def test_no_scheme_rejected(self):
        assert normalize_origin("romm.local") is None

    def test_no_host_rejected(self):
        assert normalize_origin("https://") is None

    def test_non_http_scheme_rejected(self):
        assert normalize_origin("ftp://romm.local") is None

    def test_empty_rejected(self):
        assert normalize_origin("") is None

    def test_whitespace_only_rejected(self):
        # normalize_origin itself does not strip; a whitespace-only value has no scheme.
        assert normalize_origin("   ") is None

    def test_invalid_port_rejected(self):
        assert normalize_origin("https://romm.local:notaport") is None


class TestSameOrigin:
    def test_identical_true(self):
        assert same_origin("https://romm.local", "https://romm.local") is True

    def test_default_port_vs_bare_true(self):
        assert same_origin("https://romm.local:443", "https://romm.local") is True

    def test_path_vs_bare_true(self):
        assert same_origin("https://romm.local/romm/", "https://romm.local") is True

    def test_different_host_false(self):
        assert same_origin("https://a.local", "https://b.local") is False

    def test_different_scheme_false(self):
        assert same_origin("https://romm.local", "http://romm.local") is False

    def test_different_port_false(self):
        assert same_origin("https://romm.local:8080", "https://romm.local:9090") is False

    def test_none_left_false(self):
        assert same_origin(None, "https://romm.local") is False

    def test_none_right_false(self):
        assert same_origin("https://romm.local", None) is False

    def test_both_none_false(self):
        assert same_origin(None, None) is False

    def test_unparseable_left_false(self):
        assert same_origin("not-a-url", "https://romm.local") is False

    def test_two_unparseable_false(self):
        """Two unparseable (None-origin) inputs are never 'the same server'."""
        assert same_origin("nope", "also-nope") is False

    # ── IPv6 + trailing-dot equivalence (regression: bracket corruption) ──────

    def test_ipv6_same_self_true(self):
        """An IPv6 origin must equal itself — the round-trip the bracket bug broke."""
        assert same_origin("https://[::1]:8080", "https://[::1]:8080") is True

    def test_ipv6_path_vs_bare_true(self):
        assert same_origin("https://[::1]:8080/romm", "https://[::1]:8080") is True

    def test_ipv6_default_portless_vs_explicit_port_false(self):
        """[::1] (default port) and [::1]:8080 are different origins."""
        assert same_origin("https://[::1]", "https://[::1]:8080") is False

    def test_ipv6_default_port_vs_bare_true(self):
        assert same_origin("https://[::1]:443", "https://[::1]") is True

    def test_ipv6_does_not_collide_with_flattened_host(self):
        """A bracketed [::1]:8080 must NOT collide with a host literally named '::1:8080'.

        The bug flattened the former to the latter; they must stay distinct
        (the flattened form is unparseable → None → never equal).
        """
        assert same_origin("https://[::1]:8080", "https://::1:8080") is False

    def test_trailing_dot_vs_dotless_true(self):
        assert same_origin("https://host.com.", "https://host.com") is True


class TestIsValidServerUrl:
    @pytest.mark.parametrize(
        "url",
        ["http://romm.local", "https://romm.local", "https://romm.local:8443/romm", "  http://romm.local  "],
    )
    def test_valid(self, url):
        assert is_valid_server_url(url) is True

    @pytest.mark.parametrize(
        "url",
        ["romm.local", "ftp://romm.local", "", "   ", "https://", "://nohost"],
    )
    def test_invalid(self, url):
        assert is_valid_server_url(url) is False

    def test_trims_before_validating(self):
        assert is_valid_server_url("\t https://romm.local \n") is True
