# Releasing gipper

Releases are built and published manually; pushing a commit does not publish a
package. Keep the CLI, Python API, documentation, and distribution metadata in
sync for each release.

## Public release checks

- Confirm MIT ownership/permission for contributions. Link third-party research
  sources; do not bundle full pages or repositories without appropriate licensing.
- Retain Git history. Review all reachable history as well as the candidate
  tree for credentials and sensitive data. Rotate any exposed real credential
  before publication; removing it from the current tree is insufficient.
- Inspect secret-scanner findings individually. The synthetic key in test
  fixtures is deliberately fake; do not disable a whole detection rule for it.
- Enable GitHub private vulnerability reporting and verify SECURITY.md's link.
- Confirm repository metadata, default branch, and package-index ownership.
  Document source installation until a package-index release is verified.
- Integrate safely if an editable checkout serves active clients. Preserve
  unrelated work and avoid changing code under a running paid job.

## Validate a candidate

```sh
uv sync --locked --extra test
uv run --extra test pytest -q
uv build
uv run --no-project scripts/check_dist.py
gitleaks dir --redact --no-banner .
gitleaks git --redact --no-banner --log-opts=--all .
```

Run tree scans on a clean export so caches and installed dependencies are not
mistaken for release content. Do not globally ignore the generic API key rule
because of the known test fixture. CI has no provider keys and never rents.
Review CI on Python 3.12–3.14 and the installed-wheel smoke check. The repository
has no lint baseline; lint adoption is a separate change.

If provider behavior changed, obtain authorization for a bounded real smoke run,
review estimate/dry-run first, then verify closure in both CLI and provider
console. A unit-test pass does not qualify a provider's live behavior.

## Publish a release

1. Choose a semantic version; update `pyproject.toml`, `gipper.__version__`, and
   `uv.lock` together. Move Unreleased notes to a dated changelog heading.
2. Repeat validation above from the exact candidate commit and review archive
   contents. Install the wheel in a fresh environment outside the source tree;
   check `gipper --help` and the bundled `bootstrap.sh`.
3. Commit the scoped release, tag it `vX.Y.Z`, and push the reviewed commit and tag.
4. Create a GitHub release with focused changes, validation, limitations, and the
   wheel/sdist attached. Confirm uploaded assets match local checksums.
5. If the PyPI name belongs to this project and a package-index release is planned,
   publish the reviewed artifacts with `uv publish`. Verify index metadata and
   a fresh installation before advertising `pip install gipper`.

No site deployment is needed for this release. If a website is added later,
publish through a repository Artoo artifact and `artoo deploy`.
