"""Render the Pages site from owned HTML/CSS and public Markdown guides."""
import html
import shutil
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
BASE = "https://github.com/lavallee/gipper/blob/main/"
GUIDES = {
    "quickstart": ROOT / "docs/quickstart.md",
    "clients": ROOT / "docs/clients.md",
    "placement": ROOT / "docs/placement.md",
    "lessons": ROOT / "docs/lessons.md",
    "security": ROOT / "SECURITY.md",
}
DEST = ROOT / "docs"
md = MarkdownIt("commonmark").enable("table")


def render_guide(slug, source):
    tokens = md.parse(source.read_text())
    for token in tokens:
        for child in token.children or []:
            if child.type != "link_open":
                continue
            href = child.attrGet("href") or ""
            parts = urlsplit(href)
            if parts.scheme or parts.netloc or not parts.path:
                continue
            target = (source.parent / parts.path).resolve()
            mapped = next((name for name, path in GUIDES.items() if path == target), None)
            url = f"{mapped}.html" if mapped else BASE + str(target.relative_to(ROOT))
            if parts.fragment:
                url += "#" + parts.fragment
            child.attrSet("href", url)
    title = next(t.content for t in tokens if t.type == "inline")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><title>{html.escape(title)} — gipper</title>
<meta name="description" content="{html.escape(title)}: documentation for the gipper GPU job runner.">
<link rel="canonical" href="https://lavallee.github.io/gipper/guides/{slug}.html">
<link rel="stylesheet" href="../style.css"><link rel="icon" href="../favicon.svg" type="image/svg+xml"></head>
<body><a class="skip" href="#main">Skip to content</a><header class="site-header wrap"><a class="wordmark" href="../index.html">gipper <span>v{VERSION}</span></a><nav aria-label="Documentation"><a href="quickstart.html">Quickstart</a><a href="clients.html">Python API</a><a href="https://github.com/lavallee/gipper">GitHub</a></nav></header>
<main class="guide wrap" id="main"><a class="back" href="../index.html">gipper documentation</a>{md.renderer.render(tokens, md.options, {})}</main>
<footer class="wrap footer"><span>gipper v{VERSION} · MIT</span><a href="https://github.com/lavallee/gipper/issues">Report a documentation issue</a></footer></body></html>'''


def main():
    DEST.mkdir(exist_ok=True)
    (DEST / "guides").mkdir(exist_ok=True)
    (DEST / ".nojekyll").touch()
    (DEST / "index.html").write_text((ROOT / "site/index.html").read_text().replace("{{VERSION}}", VERSION))
    for name in ("style.css", "favicon.svg"):
        shutil.copyfile(ROOT / "site" / name, DEST / name)
    for slug, source in GUIDES.items():
        (DEST / "guides" / f"{slug}.html").write_text(render_guide(slug, source))
    print(f"Rendered gipper {VERSION}: landing page and {len(GUIDES)} guides")


if __name__ == "__main__":
    main()
