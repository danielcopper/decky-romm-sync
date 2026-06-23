# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| Latest  | Yes       |
| Older   | No        |

Only the latest release receives security fixes.

## Reporting a Vulnerability

If you discover a security vulnerability in decky-romm-sync, please report it responsibly:

1. **Do NOT open a public GitHub issue.**
2. Use [GitHub Security Advisories](https://github.com/danielcopper/decky-romm-sync/security/advisories/new) to report
   privately.
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

You should receive a response within 7 days.

## Scope

This plugin handles:

- **A scoped RomM Client API Token** stored in the plugin's settings file (`settings.json`) in Decky's settings
  directory. Your RomM username and password are used **once, in memory only**, to mint that token at sign-in and are
  then discarded — they are never written to disk. The plugin stores only the server URL, the minted token (plus its
  server-side id and the origin it was minted against), and the SSL-verification flag.
- **An optional SteamGridDB API key** stored in the same `settings.json`
- **HTTP requests** to self-hosted RomM servers (optionally with SSL verification disabled for self-signed certificates)

### Known security considerations

- The settings file is stored with `0600` permissions (owner-only read/write); the plugin actively migrates an older
  world-readable `0644` file to `0600` on load.
- Credentials and tokens are never logged — masked in all log output.
- The RomM Client API Token is **bound to the origin it was minted against** (`scheme://host[:port]`) and is only ever
  sent to that exact origin. If the configured server URL no longer matches, the bearer is withheld rather than sent — a
  changed or hostile host never receives the credential.
- Path components supplied by the RomM server (filenames, ROM and save paths) are validated against path traversal
  before they are used to build local filesystem paths, so a compromised or malicious server cannot write outside the
  plugin's directories.
- The `allow_insecure_ssl` option disables certificate verification for self-hosted servers with self-signed
  certificates. This is an opt-in user setting with a warning in the UI.
