
#!/usr/bin/env python3
"""
Compare two websites after migration.

- Old website: Windows-1251, legacy .shtml URLs
- New website: UTF-8, static .html URLs
- Follows redirects
- Compares visible text and internal links
- Checks files with HEAD requests (no downloads)
- Crawls internal pages recursively
- Uses concurrent requests

Python 3.10+
No third-party dependencies.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import re
import sys
import threading

from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)


OLD_SITE = "https://temnyjles.ru"
NEW_SITE = "https://temnyjles.roman-79c.workers.dev"

WORKERS = 12
TIMEOUT = 20

FILE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".zip", ".rar", ".7z", ".gz",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".svg", ".ico", ".bmp",
    ".mp3", ".mp4", ".avi", ".mov",
    ".css", ".js", ".woff", ".woff2", ".ttf",
    ".eot", ".xml", ".txt", ".json",
}


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------

@dataclass
class Response:
    status: int
    url: str
    headers: dict
    body: bytes = b""
    redirects: list = field(default_factory=list)
    error: str | None = None


class RedirectRecorder(HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.redirects = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirects.append((code, req.full_url, newurl))
        return super().redirect_request(
            req, fp, code, msg, headers, newurl
        )


def request(url: str, method="GET") -> Response:
    recorder = RedirectRecorder()
    opener = build_opener(recorder)

    req = Request(
        url,
        method=method,
        headers={
            "User-Agent": "SiteMigrationChecker/1.0",
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
    )

    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            body = r.read() if method == "GET" else b""
            return Response(
                status=r.status,
                url=r.url,
                headers=dict(r.headers.items()),
                body=body,
                redirects=recorder.redirects,
            )

    except HTTPError as e:
        return Response(
            status=e.code,
            url=e.url,
            headers=dict(e.headers.items()) if e.headers else {},
            redirects=recorder.redirects,
            error=str(e),
        )

    except Exception as e:
        return Response(
            status=0,
            url=url,
            redirects=recorder.redirects,
            error=str(e),
        )


# ----------------------------------------------------------------------
# HTML parsing
# ----------------------------------------------------------------------

class PageParser(HTMLParser):
    """Extract visible text and links from HTML."""

    SKIP_TAGS = {
        "script", "style", "noscript", "svg",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts = []
        self.links = set()
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag in self.SKIP_TAGS:
            self.skip_depth += 1

        # Navigational links
        if tag in ("a", "area") and attrs.get("href"):
            self.links.add(attrs["href"])

        # Linked resources
        if tag in ("link", "img", "script", "iframe",
                   "source", "video", "audio", "input"):
            for attr in ("href", "src", "poster"):
                if attrs.get(attr):
                    self.links.add(attrs[attr])

            # srcset can contain multiple resources
            if attrs.get("srcset"):
                for item in attrs["srcset"].split(","):
                    src = item.strip().split()
                    if src:
                        self.links.add(src[0])

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if not self.skip_depth:
            self.text_parts.append(data)

    def get_text(self):
        # Normalize all whitespace, including newlines and NBSP.
        return " ".join(" ".join(self.text_parts).split())


def get_charset(headers, body, fallback):
    """Determine HTML encoding from HTTP header, then meta tag."""

    content_type = headers.get("Content-Type", "")

    match = re.search(
        r"charset\s*=\s*[\"']?([^;\s\"']+)",
        content_type,
        re.I,
    )
    if match:
        return match.group(1)

    # Meta declarations are ASCII-compatible in both target encodings.
    head = body[:8192].decode("ascii", errors="ignore")

    match = re.search(
        r'<meta[^>]+charset\s*=\s*["\']?\s*([\w-]+)',
        head,
        re.I,
    )
    if match:
        return match.group(1)

    match = re.search(
        r'<meta[^>]+content\s*=\s*["\'][^"\']*charset=([\w-]+)',
        head,
        re.I,
    )
    if match:
        return match.group(1)

    return fallback


def parse_page(response, fallback_encoding):
    charset = get_charset(
        response.headers,
        response.body,
        fallback_encoding,
    )

    try:
        html = response.body.decode(charset, errors="replace")
    except LookupError:
        html = response.body.decode(
            fallback_encoding, errors="replace"
        )

    parser = PageParser()
    parser.feed(html)

    return parser.get_text(), parser.links


# ----------------------------------------------------------------------
# URL normalization and classification
# ----------------------------------------------------------------------

def canonical_path(path):
    """Consider old .shtml and new .html URLs equivalent."""

    path = path or "/"

    if path.lower().endswith(".shtml"):
        path = path[:-6] + ".html"

    return path


def url_key(url):
    """
    Generate a comparable URL identifier.

    Internal URLs are compared by path and query.
    External URLs retain their full address.
    """

    p = urlsplit(url)

    path = canonical_path(p.path)

    if not path:
        path = "/"

    return path + ("?" + p.query if p.query else "")


def is_file(url):
    path = urlsplit(url).path.lower()
    return any(path.endswith(ext) for ext in FILE_EXTENSIONS)


def is_internal(url, base):
    return urlsplit(url).netloc.lower() == urlsplit(base).netloc.lower()


def counterpart_url(base, key, old=False):
    """Build a fallback URL when a link exists on only one side."""

    p = urlsplit(key)
    path = p.path

    if old and path.lower().endswith(".html"):
        path = path[:-5] + ".shtml"

    return urlunsplit((
        urlsplit(base).scheme,
        urlsplit(base).netloc,
        path,
        p.query,
        "",
    ))


# ----------------------------------------------------------------------
# Comparison
# ----------------------------------------------------------------------

def compare_text(old_text, new_text):
    if old_text == new_text:
        return None

    matcher = difflib.SequenceMatcher(
        None, old_text, new_text, autojunk=False
    )

    differences = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue

        differences.append({
            "type": tag,
            "old": old_text[max(0, i1 - 50):min(len(old_text), i2 + 50)],
            "new": new_text[max(0, j1 - 50):min(len(new_text), j2 + 50)],
        })

        if len(differences) >= 5:
            break

    return differences


def compare_links(old_links, new_links, old_url, new_url,
                  old_base, new_base):
    """
    Return link differences and matched internal links.

    Matched links are keyed by normalized URL, so .shtml
    and .html are treated as the same destination.
    """

    def collect(links, page_url, base):
        result = {}

        for href in links:
            if not href:
                continue

            absolute = urljoin(page_url, href)
            p = urlsplit(absolute)

            if p.scheme not in ("http", "https"):
                continue

            if p.username or p.password:
                continue

            # Fragments do not affect the requested resource.
            absolute = urlunsplit((
                p.scheme, p.netloc, p.path, p.query, ""
            ))

            if is_internal(absolute, base):
                key = url_key(absolute)
            else:
                key = absolute

            result[key] = absolute

        return result

    old_map = collect(old_links, old_url, old_base)
    new_map = collect(new_links, new_url, new_base)

    old_keys = set(old_map)
    new_keys = set(new_map)

    return {
        "old_only": sorted(old_keys - new_keys),
        "new_only": sorted(new_keys - old_keys),
        "old_map": old_map,
        "new_map": new_map,
    }


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

class Reporter:
    def __init__(self):
        self.lock = threading.Lock()
        self.stats = {
            "pages": 0,
            "text_equal": 0,
            "text_different": 0,
            "page_errors": 0,
            "link_differences": 0,
            "files_checked": 0,
            "file_errors": 0,
            "redirect_errors": 0,
        }

    def log(self, message=""):
        with self.lock:
            print(message, flush=True)

    def increment(self, key, amount=1):
        with self.lock:
            self.stats[key] += amount

    def summary(self):
        self.log("\n" + "=" * 70)
        self.log("SUMMARY")
        self.log("=" * 70)

        for key, value in self.stats.items():
            self.log(f"{key:25} {value}")


# ----------------------------------------------------------------------
# Crawler
# ----------------------------------------------------------------------

def crawl(old_base, new_base, workers):
    reporter = Reporter()

    old_base = old_base.rstrip("/") + "/"
    new_base = new_base.rstrip("/") + "/"

    # Each queue entry represents one logical page.
    # Values are the corresponding old and new URLs.
    queue = deque([
        ("/", old_base, new_base)
    ])

    visited = set()
    pending_files = {}

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as executor:

        while queue:
            batch = []

            # Prepare a batch of page pairs.
            while queue and len(batch) < workers:
                key, old_url, new_url = queue.popleft()

                if key in visited:
                    continue

                visited.add(key)
                batch.append((key, old_url, new_url))

            if not batch:
                continue

            futures = {}

            # Fetch old and new pages concurrently.
            for key, old_url, new_url in batch:
                futures[(key, "old")] = executor.submit(
                    request, old_url, "GET"
                )
                futures[(key, "new")] = executor.submit(
                    request, new_url, "GET"
                )

            for key, old_url, new_url in batch:
                old_response = futures[(key, "old")].result()
                new_response = futures[(key, "new")].result()

                reporter.increment("pages")

                reporter.log(f"\nPAGE: {key}")
                reporter.log(f"  OLD: {old_url}")
                reporter.log(
                    f"       HTTP {old_response.status} "
                    f"-> {old_response.url}"
                )
                reporter.log(f"  NEW: {new_url}")
                reporter.log(
                    f"       HTTP {new_response.status} "
                    f"-> {new_response.url}"
                )

                if old_response.error or new_response.error:
                    reporter.increment("page_errors")
                    reporter.log(
                        f"  ERROR: old={old_response.error}, "
                        f"new={new_response.error}"
                    )
                    continue

                if not (200 <= old_response.status < 300):
                    reporter.increment("page_errors")
                    reporter.log(
                        f"  OLD PAGE ERROR: {old_response.status}"
                    )
                    continue

                if not (200 <= new_response.status < 300):
                    reporter.increment("page_errors")
                    reporter.log(
                        f"  NEW PAGE ERROR: {new_response.status}"
                    )
                    continue

                # Check the legacy URL redirect on the NEW website.
                if (
                    urlsplit(old_url).path.lower().endswith(".shtml")
                    and urlsplit(new_url).path.lower().endswith(".shtml")
                ):
                    redirects = new_response.redirects

                    if not redirects or redirects[0][0] != 301:
                        reporter.increment("redirect_errors")
                        reporter.log(
                            "  WARNING: Expected HTTP 301 "
                            "for legacy .shtml URL on NEW site"
                        )
                    else:
                        redirect_code, redirect_from, redirect_to = redirects[0]

                        expected_path = urlsplit(new_url).path[:-6] + ".html"
                        actual_path = urlsplit(redirect_to).path

                        if actual_path != expected_path:
                            reporter.increment("redirect_errors")
                            reporter.log(
                                f"  WARNING: Redirect destination mismatch: "
                                f"{actual_path} (expected {expected_path})"
                            )
                        else:
                            reporter.log(
                                f"  REDIRECT: OK ({redirect_code}) "
                                f"-> {redirect_to}"
                            )
                            
                old_text, old_links = parse_page(
                    old_response, "windows-1251"
                )
                new_text, new_links = parse_page(
                    new_response, "utf-8"
                )

                # Compare visible text.
                differences = compare_text(old_text, new_text)

                if differences is None:
                    reporter.increment("text_equal")
                    reporter.log("  TEXT: OK")
                else:
                    reporter.increment("text_different")
                    reporter.log("  TEXT: DIFFERENT")

                    for diff in differences:
                        reporter.log(f"    {diff['type']}:")
                        reporter.log(f"      OLD: {diff['old']!r}")
                        reporter.log(f"      NEW: {diff['new']!r}")

                # Compare links.
                links = compare_links(
                    old_links,
                    new_links,
                    old_response.url,
                    new_response.url,
                    old_base,
                    new_base,
                )

                old_map = links["old_map"]
                new_map = links["new_map"]

                if links["old_only"] or links["new_only"]:
                    reporter.increment("link_differences")
                    reporter.log("  LINKS: DIFFERENT")

                    for link in links["old_only"]:
                        reporter.log(f"    OLD ONLY: {link}")

                    for link in links["new_only"]:
                        reporter.log(f"    NEW ONLY: {link}")
                else:
                    reporter.log("  LINKS: OK")

                # Build the list of matching destinations.
                all_keys = set(old_map) | set(new_map)

                for link_key in all_keys:
                    # External links are compared but not crawled.
                    if link_key.startswith(("http://", "https://")):
                        if not (
                            is_internal(link_key, old_base)
                            or is_internal(link_key, new_base)
                        ):
                            continue

                    old_target = old_map.get(link_key)
                    new_target = new_map.get(link_key)

                    if not old_target:
                        old_target = counterpart_url(
                            old_base, link_key, old=True
                        )

                    if not new_target:
                        new_target = counterpart_url(
                            new_base, link_key, old=False
                        )

                    # Only crawl internal resources.
                    old_internal = is_internal(old_target, old_base)
                    new_internal = is_internal(new_target, new_base)

                    if not (old_internal and new_internal):
                        continue

                    if is_file(old_target) or is_file(new_target):
                        pending_files[link_key] = (
                            old_target, new_target
                        )
                    else:
                        if link_key not in visited:
                            queue.append((
                                link_key,
                                old_target,
                                new_target,
                            ))

        # Check all files concurrently using HEAD.
        reporter.log("\n" + "=" * 70)
        reporter.log("CHECKING FILES (HEAD requests, no downloads)")
        reporter.log("=" * 70)

        file_futures = {}

        for key, (old_url, new_url) in pending_files.items():
            file_futures[(key, "old")] = executor.submit(
                request, old_url, "HEAD"
            )
            file_futures[(key, "new")] = executor.submit(
                request, new_url, "HEAD"
            )

        for key, (old_url, new_url) in pending_files.items():
            old_r = file_futures[(key, "old")].result()
            new_r = file_futures[(key, "new")].result()

            reporter.increment("files_checked")

            old_ok = 200 <= old_r.status < 400
            new_ok = 200 <= new_r.status < 400

            if old_ok and new_ok:
                reporter.log(f"  OK: {key}")
            else:
                reporter.increment("file_errors")
                reporter.log(f"  ERROR: {key}")
                reporter.log(
                    f"    OLD: {old_r.status} {old_r.error or ''}"
                )
                reporter.log(
                    f"    NEW: {new_r.status} {new_r.error or ''}"
                )

    reporter.summary()


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Compare two websites after migration"
    )

    parser.add_argument(
        "--old", default=OLD_SITE,
        help="Old website URL"
    )
    parser.add_argument(
        "--new", default=NEW_SITE,
        help="New website URL"
    )
    parser.add_argument(
        "-j", "--workers", type=int, default=WORKERS,
        help="Number of concurrent page pairs"
    )

    args = parser.parse_args()

    crawl(args.old, args.new, args.workers)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
