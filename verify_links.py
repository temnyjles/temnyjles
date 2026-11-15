
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit, unquote
import sys

ROOT = Path("dist").resolve()
START = ROOT / "index.html"

# HTML attributes containing local file references.
URL_ATTRIBUTES = {
    "href",
    "src",
    "action",
    "poster",
}


class LinkParser(HTMLParser):
    """Extract local file references from HTML."""

    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name.lower() in URL_ATTRIBUTES and value:
                self.links.append(value)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def resolve_url(url, current_file):
    """
    Resolve a URL to a local file.

    Returns None for external or unsupported URLs.
    """

    url = url.strip()

    if not url:
        return current_file

    parts = urlsplit(url)

    # Ignore external links and special URL schemes.
    if parts.scheme or parts.netloc:
        return None

    # Fragment-only references point to the current page.
    if not parts.path:
        return current_file

    # Decode URL-encoded characters.
    path = unquote(parts.path)

    # Root-relative URLs start at the website root.
    if path.startswith("/"):
        target = ROOT / path.lstrip("/")
    else:
        target = current_file.parent / path

    target = target.resolve()

    # Prevent references outside the website directory.
    try:
        target.relative_to(ROOT)
    except ValueError:
        return None

    # Directory URLs should resolve to index.html.
    if target.is_dir():
        target = target / "index.html"

    return target


def get_links(path):
    """Extract links from an HTML file."""

    parser = LinkParser()

    try:
        with path.open("r", encoding="utf-8") as f:
            parser.feed(f.read())
    except (OSError, UnicodeError) as error:
        print(f"Cannot read {path.relative_to(ROOT)}: {error}")
        return []

    return parser.links


def main():

    if not START.is_file():
        print(f"ERROR: Starting page not found: {START}")
        sys.exit(1)

    # Files that have been reached through links.
    reachable = set()

    # HTML pages already processed.
    visited = set()

    # Missing local references.
    missing = set()

    # Pages waiting to be processed.
    queue = [START]

    print(f"Checking website: {ROOT}")
    print(f"Starting page: {START.relative_to(ROOT)}")
    print()

    while queue:

        current = queue.pop(0)
        current = current.resolve()

        if current in visited:
            continue

        visited.add(current)
        reachable.add(current)

        # print(f"Checking: {current.relative_to(ROOT)}")

        for url in get_links(current):

            target = resolve_url(url, current)

            # External URLs are ignored.
            if target is None:
                continue

            if not target.is_file():
                missing.add((current, url))
                continue

            # Mark every referenced file as reachable.
            reachable.add(target)

            # Follow links to HTML pages.
            if target.suffix.lower() in (".html", ".htm"):
                if target not in visited:
                    queue.append(target)

    # Find every file in the output directory.
    all_files = {
        path.resolve()
        for path in ROOT.rglob("*")
        if path.is_file()
    }

    unreachable = all_files - reachable

    # --------------------------------------------------
    # Report results
    # --------------------------------------------------

    print("\n" + "=" * 60)
    print("BROKEN LINKS")
    print("=" * 60)

    if missing:
        for source, url in sorted(missing, key=lambda x: (str(x[0]), x[1])):
            print(f"{source.relative_to(ROOT)}")
            print(f"    -> {url}")
    else:
        print("No broken local links found.")

    print("\n" + "=" * 60)
    print("UNREACHABLE FILES")
    print("=" * 60)

    if unreachable:
        for path in sorted(unreachable):
            print(path.relative_to(ROOT))
    else:
        print("All files are reachable.")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    print(f"HTML pages visited: {len(visited)}")
    print(f"Files in website:   {len(all_files)}")
    print(f"Reachable files:    {len(reachable)}")
    print(f"Broken links:       {len(missing)}")
    print(f"Unreachable files:  {len(unreachable)}")

    if missing or unreachable:
        sys.exit(1)


if __name__ == "__main__":
    main()
