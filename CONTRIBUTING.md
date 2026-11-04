# Contributing

Bug reports and focused pull requests are welcome. Include the Python version,
provider, command, expected behavior, and a redacted error. Never attach API
keys, SSH keys, raw ledgers, or private job data. See [SECURITY.md](SECURITY.md)
for vulnerabilities.

## Development

On Linux with Python 3.12+ and uv:

```sh
git clone https://github.com/lavallee/gipper.git
cd gipper
uv sync --locked --extra test
uv run --extra test pytest -q
```

The tests use fake providers and a temporary ledger: no network or rental is
needed. Keep the runtime stdlib-only. Preserve the public interfaces in
[docs/clients.md](docs/clients.md), including module-global hooks used by clients.
Test provider parsing and placement/cleanup decisions with realistic fixtures.

If clients use an editable install of your checkout, check for active runs
before editing it and work on a branch in a separate worktree. See [AGENTS.md](AGENTS.md).

A real smoke run spends money and needs explicit operator authorization. Review
an estimate and dry run first, use tight budgets, and verify `gipper pod ls`
and the provider console after cleanup. Never touch resources outside the
`gipper-*` namespace. Qualify provider changes with live runs separately from
unit tests; record the result and any untested behavior in `docs/lessons.md`.

## Before a pull request

- Run `uv run ruff check gipper tests scripts` and `uv run --extra test pytest -q`.
- Run `uv build` and `uv run --no-project scripts/check_dist.py`.
- Update documentation and add an Unreleased changelog entry for user changes.
- Run a redacted secret scan; never paste credential values into an issue.

CI checks Python 3.12, 3.13, and 3.14 on Linux, builds both distributions, and
smoke-tests the installed wheel. Ruff checks correctness and import errors; avoid unrelated formatting churn.

Contributions are made under the repository's MIT license. Only submit material
you have the right to license; link third-party research sources instead of
copying complete pages or repositories.

## Documentation site

Edit `site/index.html`, `site/style.css`, or the Markdown guides in `docs/`.
`uv run --group site python scripts/build_site.py` deterministically renders
`docs/index.html` and `docs/guides/`. The root Artoo artifact owns publishing;
run `artoo build .`, `artoo verify .`, and `artoo deploy . --dry-run` before
`artoo deploy .`. GitHub Pages serves `main` / `docs`.
