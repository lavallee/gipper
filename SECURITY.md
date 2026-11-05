# Security

Only the latest release receives fixes. This is alpha software controlling paid
infrastructure; use provider spending alerts and inspect the provider console
when cleanup is uncertain.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/lavallee/gipper/security/advisories/new)
if enabled. If unavailable, open an issue requesting a private contact without
including exploit details, keys, account identifiers, or private data. Private
reporting must be enabled by the maintainer before the public launch. There is
no promised response time.

## Trust boundaries

- A job TOML is executable code. Its setup and command run on a rented machine;
  its input/output paths select local data to upload or overwrite. Review jobs
  before running them. Do not run gipper as local root.
- Providers and host operators can access uploaded data and job output. SSH
  currently disables host-key verification for ephemeral endpoints; it does not
  authenticate the remote host independently of the provider's endpoint data.
  Output archives come from that trusted host and are extracted locally with
  system `tar`. Use a dedicated local account or container for untrusted work.
- The remote bootstrap downloads and runs the uv installer from astral.sh if uv
  is absent. Provider images and job dependencies are additional supply-chain
  trust decisions. Pin your job image and dependencies where reproducibility
  matters. `bin/runpod-mcp` is an optional developer helper that executes the
  latest third-party MCP server via npx; it is not installed by the package.
- Provider keys stay local and are sent in API headers. Use environment variables
  or a mode-600 dotenv file. Known provider key values are redacted from API
  errors and receipts, but arbitrary secrets printed by jobs are not detected.
  The `[env]` name check is a guard, not a general secret detector.
- `gipper custody report` lists what reached rented machines, for use after a
  provider incident. It covers what passed through gipper or was declared; see
  [docs/custody.md](docs/custody.md) for its limits. Custody manifests hold file
  paths and hashes, and fingerprints are keyed hashes, never secret values.
- Receipts include paths, provider resource identifiers, host addresses, and
  commands; logs and collected files can contain sensitive job data. Restrict
  filesystem permissions and review everything before sharing it.
- Cleanup uses provider APIs. A killed client, offline user manager, unavailable
  API, or failed reaper can leave a machine billing. Remote `timeout` ends the
  command, not the rental. Check the provider console if closure is unconfirmed.
  Budget estimates/caps are client-side safeguards, not provider-enforced limits.
- The reaper manages all resources named `gipper-*` in each configured account,
  including ones not in its local ledger. Reserve that namespace and coordinate
  multiple clients/accounts; use a dedicated provider account when appropriate.
