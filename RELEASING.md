# Releasing gipper

Release preparation is separate from permission to publish, make the repository
public, or spend money. No release or automatic PyPI publishing workflow runs
on a push. The current package version remains 0.2.2 until a release is chosen.

## First public release gate

- Confirm MIT ownership/permission for all contributions. Full third-party
  research captures are removed from the release tree; source links remain.
- Decide whether to publish the existing history or create a clean public
  snapshot. Internal planning and old source captures remain in historical
  commits. Do not change repository visibility before that review.
- Scan both the candidate tree and all history with a redacted secret scanner.
  The preparation scan found only the literal synthetic test key in
  `tests/conftest.py` and historical `tests/test_phase0.py`; inspect findings
  individually. Any real credential requires rotation before publication.
- Enable GitHub private vulnerability reporting and confirm SECURITY.md's link.
- Confirm repository ownership, default branch, and package-index name ownership
  before setting public metadata or publishing. Installation from Git is the
  documented route until a PyPI release is verified.
- Review unrelated changes in the live checkout before integration. Do not merge
  while paid clients are running from that editable checkout.

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

## Publish only after authorization

1. Choose a semantic version; update `pyproject.toml`, `gipper.__version__`, and
   `uv.lock` together. Move Unreleased notes to a dated changelog heading.
2. Repeat validation above from the exact candidate commit and review archive
   contents. Install the wheel in a fresh environment outside the source tree;
   check `gipper --help` and the bundled `bootstrap.sh`.
3. Commit the scoped release, tag it `vX.Y.Z`, and push only after authorization.
4. Create a GitHub release with focused changes, validation, limitations, and the
   wheel/sdist attached. Confirm uploaded assets match local checksums.
5. If publishing to PyPI is authorized and the name belongs to this project,
   publish the reviewed artifacts with `uv publish`. Verify index metadata and
   a fresh installation before advertising `pip install gipper`.

No site deployment is needed for this release. If a website is added later,
publish through a repository Artoo artifact and `artoo deploy`.
