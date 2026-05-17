# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| Latest  | Yes                |
| Older   | No                 |

Only the latest release receives security fixes.

## Reporting a Vulnerability

If you discover a security vulnerability in decky-romm-sync, please report it responsibly:

1. **Do NOT open a public GitHub issue.**
2. Use [GitHub Security Advisories](https://github.com/danielcopper/decky-romm-sync/security/advisories/new) to report privately.
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

You should receive a response within 7 days.

## Scope

This plugin handles:

- **RomM server credentials** (URL, username, password) stored in Decky's settings directory
- **SteamGridDB API keys** stored in the same settings file
- **HTTP requests** to self-hosted RomM servers (optionally with SSL verification disabled for self-signed certificates)

### Known security considerations

- Settings files are stored with `0600` permissions (owner-only read/write)
- Credentials are never logged — masked in all log output
- The `allow_insecure_ssl` option disables certificate verification for self-hosted servers with self-signed certificates. This is an opt-in user setting with a warning in the UI.
