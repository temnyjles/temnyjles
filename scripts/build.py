
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import re
import shutil

SOURCE = Path("src")
OUTPUT = Path("dist")

include_re = re.compile(
    r'<!--#include\s+file="([^"]+)"\s*-->'
)

# HTML attributes that may contain internal file references.
url_re = re.compile(
    r'(?P<prefix>\b(?:href|src|action|poster)\s*=\s*)'
    r'(?P<quote>["\'])'
    r'(?P<url>.*?)'
    r'(?P=quote)',
    re.IGNORECASE
)


def resolve_include(path):
    """Find an include, supporting .html -> .shtml fallback."""

    if path.is_file():
        return path

    if path.suffix.lower() == ".html":
        alternative = path.with_suffix(".shtml")

        if alternative.is_file():
            return alternative

    raise FileNotFoundError(f"SSI include not found: {path}")


def process(path, stack=()):
    """Process SSI includes recursively."""

    path = path.resolve()

    if path in stack:
        raise ValueError(f"Recursive include: {path}")

    content = path.read_text(encoding="utf-8")

    def replace(match):
        included = (path.parent / match.group(1)).resolve()
        included = resolve_include(included)

        return process(included, stack + (path,))

    return include_re.sub(replace, content)


def normalize_links(content):
    """Convert local .shtml URLs to .html URLs."""

    def replace(match):
        url = match.group("url")

        parts = urlsplit(url)

        # Ignore external URLs, mailto:, javascript:, etc.
        if parts.scheme or parts.netloc:
            return match.group(0)

        # Only change .shtml paths.
        if parts.path.lower().endswith(".shtml"):
            new_path = parts.path[:-6] + ".html"
            url = urlunsplit((
                parts.scheme,
                parts.netloc,
                new_path,
                parts.query,
                parts.fragment
            ))

        return (
            match.group("prefix")
            + match.group("quote")
            + url
            + match.group("quote")
        )

    return url_re.sub(replace, content)


# Start with a clean output directory.
if OUTPUT.exists():
    shutil.rmtree(OUTPUT)


for path in SOURCE.rglob("*"):

    if path.is_dir():
        continue

    relative = path.relative_to(SOURCE)

    if path.suffix.lower() in (".html", ".shtml"):

        target = OUTPUT / relative #.with_suffix(".html")
        target.parent.mkdir(parents=True, exist_ok=True)

        content = process(path)

        # Normalize internal links after SSI expansion.
        # content = normalize_links(content)

        target.write_text(content, encoding="utf-8")

    else:
        target = OUTPUT / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(path, target)

print("Website successfully built in dist/")
