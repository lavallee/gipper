"""Validate the built wheel and sdist, without installing runtime dependencies."""
import email
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
version = project["version"]
wheel = root / "dist" / f"gipper-{version}-py3-none-any.whl"
sdist = root / "dist" / f"gipper-{version}.tar.gz"
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    assert "gipper/bootstrap.sh" in names, "missing remote bootstrap"
    assert all(n.startswith(("gipper/", f"gipper-{version}.dist-info/")) for n in names)
    metadata = email.message_from_bytes(archive.read(f"gipper-{version}.dist-info/METADATA"))
    assert metadata["Version"] == version
    # Package indexes display the README captured at build time. A stale status
    # must fail before upload even when package metadata versions agree.
    description = metadata.get_payload()
    for declared in re.findall(r"\*\*Status:\s*(\d+\.\d+\.\d+)", description):
        assert declared == version, f"README status {declared} disagrees with package {version}"
    assert metadata["License-Expression"] == "MIT"
    assert metadata["Requires-Python"] == project["requires-python"]
    assert all("extra ==" in requirement for requirement in metadata.get_all("Requires-Dist", [])), "runtime dependency added"
    assert archive.read(f"gipper-{version}.dist-info/licenses/LICENSE") == (root / "LICENSE").read_bytes()
    # Installed package version must agree without importing the source checkout.
    assert f'__version__ = "{version}"' in archive.read("gipper/__init__.py").decode()
with tarfile.open(sdist) as archive:
    names = [name.removeprefix(f"gipper-{version}/") for name in archive.getnames()]
    for required in ("LICENSE", "README.md", "SECURITY.md", "gipper/bootstrap.sh", "examples/smoke.toml", "examples/smoke/hello.txt", "deploy/gipper-reap.service", "scripts/check_dist.py"):
        assert required in names, f"sdist missing {required}"
    forbidden = (".env", ".git", ".venv", "plans", "__pycache__")
    for name in names:
        assert not set(Path(name).parts).intersection(forbidden), f"private/build content: {name}"
        assert not name.endswith((".pyc", ".pem")), f"unexpected artifact: {name}"
print(f"Validated wheel and sdist for {version}")
