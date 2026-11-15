
#!/usr/bin/env python3
"""
Compare two websites after migration.

Features:
- Old website: Windows-1251, legacy .shtml URLs
- New website: UTF-8, static .html URLs
- Compares visible text and internal links
- Tests legacy .shtml -> .html redirects independently
- Checks linked files with HEAD requests (no downloads)
- Crawls internal pages recursively
- Uses concurrent requests
- Reports only problems, followed by a final summary

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
from urllib.error import HTTPError
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
            "User-Agent": "SiteMigrationChecker/2.0",
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
            headers={},
            redirects=recorder.redirects,
            error=str(e),
        )


# ----------------------------------------------------------------------
# HTML PARSING
# ----------------------------------------------------------------------

class PageParser(HTMLParser):
    """Extract visible text and links from HTML."""

    SKIP_TAGS = {
        "script", "style", "noscript", "svg",
    }

    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote",
        "br", "dd", "div", "dl", "dt", "fieldset",
        "figcaption", "figure", "footer", "form",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav",
        "ol", "p", "pre", "section", "table",
        "td", "th", "tr", "ul",
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

        if tag in self.BLOCK_TAGS and not self.skip_depth:
            self.text_parts.append(" ")

        # Navigational links
        if tag in ("a", "area") and attrs.get("href"):
            self.links.add(attrs["href"])

        # Linked resources
        if tag in (
            "link", "img", "script", "iframe",
            "source", "video", "audio", "input",
        ):
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

        if tag in self.BLOCK_TAGS and not self.skip_depth:
            self.text_parts.append(" ")

    def handle_data(self, data):

        if not self.skip_depth:
            self.text_parts.append(data)

    def get_text(self):
        return " ".join("".join(self.text_parts).split())


def get_charset(headers, body, fallback):
    """Determine encoding from HTTP header, then meta tag."""

    content_type = next(
        (
            value for key, value in headers.items()
            if key.lower() == "content-type"
        ),
        "",
    )

    match = re.search(
        r"charset\s*=\s*[\"']?([^;\s\"']+)",
        content_type,
        re.I,
    )

    if match:
        return match.group(1)

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
# URL NORMALIZATION AND CLASSIFICATION
# ----------------------------------------------------------------------

def canonical_path(path):
    """Treat old .shtml and new .html URLs as equivalent."""

    path = path or "/"

    if path.lower().endswith(".shtml"):
        path = path[:-6] + ".html"

    return path


def url_key(url):
    p = urlsplit(url)
    path = canonical_path(p.path)

    if not path:
        path = "/"

    return path + ("?" + p.query if p.query else "")


def is_file(url):
    path = urlsplit(url).path.lower()
    return any(path.endswith(ext) for ext in FILE_EXTENSIONS)


def is_internal(url, base):
    return (
        urlsplit(url).netloc.lower()
        == urlsplit(base).netloc.lower()
    )


def counterpart_url(base, key, old=False):
    """Build the corresponding URL on the other website."""

    p = urlsplit(key)
    path = p.path

    if old and path.lower().endswith(".html"):
        path = path[:-5] + ".shtml"

    base_parts = urlsplit(base)

    return urlunsplit((
        base_parts.scheme,
        base_parts.netloc,
        path,
        p.query,
        "",
    ))


# ----------------------------------------------------------------------
# COMPARISON
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
            "old": old_text[
                max(0, i1 - 50):min(len(old_text), i2 + 50)
            ],
            "new": new_text[
                max(0, j1 - 50):min(len(new_text), j2 + 50)
            ],
        })

        if len(differences) >= 5:
            break

    return differences


def compare_links(
    old_links,
    new_links,
    old_url,
    new_url,
    old_base,
    new_base,
):
    """Compare links using normalized internal URL keys."""

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
# REDIRECT VALIDATION
# ----------------------------------------------------------------------

def check_legacy_redirect(
    old_url,
    new_canonical_url,
):
    """
    Independently test the new site's legacy .shtml URL.

    Expected:
      Initial response: 301
      Destination: corresponding .html URL
      Final response: 2xx
    """

    old_parts = urlsplit(old_url)
    new_parts = urlsplit(new_canonical_url)

    if not old_parts.path.lower().endswith(".shtml"):
        return None

    legacy_url = urlunsplit((
        new_parts.scheme,
        new_parts.netloc,
        old_parts.path,
        old_parts.query,
        "",
    ))

    response = request(legacy_url, "GET")

    issues = []

    if not response.redirects:
        issues.append(
            f"Expected HTTP 301 for {legacy_url}, "
            f"but no redirect occurred (final status: "
            f"{response.status})"
        )
        return issues

    first_code, first_from, first_to = response.redirects[0]

    expected_path = new_parts.path
    actual_first_path = urlsplit(
        urljoin(legacy_url, first_to)
    ).path

    if first_code != 301:
        issues.append(
            f"Expected HTTP 301, received HTTP {first_code}"
        )

    if actual_first_path != expected_path:
        issues.append(
            f"Wrong redirect destination: {actual_first_path} "
            f"(expected {expected_path})"
        )

    final_path = urlsplit(response.url).path

    if not (200 <= response.status < 300):
        issues.append(
            f"Redirect destination returned HTTP {response.status}"
        )

    if final_path != expected_path:
        issues.append(
            f"Final URL is {final_path}, "
            f"expected {expected_path}"
        )

    if response.error:
        issues.append(f"Request error: {response.error}")

    return issues or None


# ----------------------------------------------------------------------
# REPORTING
# ----------------------------------------------------------------------

class Reporter:

    def __init__(self):

        self.lock = threading.Lock()

        self.stats = {
            "pages_checked": 0,
            "files_checked": 0,
            "text_differences": 0,
            "link_differences": 0,
            "page_errors": 0,
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
        self.log("MIGRATION AUDIT SUMMARY")
        self.log("=" * 70)

        for key, value in self.stats.items():
            label = key.replace("_", " ").capitalize()
            self.log(f"{label:25} {value:>8}")

        errors = sum(
            self.stats[key]
            for key in (
                "text_differences",
                "link_differences",
                "page_errors",
                "file_errors",
                "redirect_errors",
            )
        )

        self.log("-" * 70)

        if errors:
            self.log(f"RESULT: ISSUES FOUND ({errors})")
        else:
            self.log("RESULT: ALL CHECKS PASSED")

        self.log("=" * 70)

        return errors


# ----------------------------------------------------------------------
# CRAWLER
# ----------------------------------------------------------------------

def crawl(old_base, new_base, workers):

    reporter = Reporter()

    old_base = old_base.rstrip("/") + "/"
    new_base = new_base.rstrip("/") + "/"

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

            while queue and len(batch) < workers:

                key, old_url, new_url = queue.popleft()

                if key in visited:
                    continue

                visited.add(key)
                batch.append((key, old_url, new_url))

            if not batch:
                continue

            futures = {}

            # Fetch old and new canonical page URLs.
            for key, old_url, new_url in batch:

                futures[(key, "old")] = executor.submit(
                    request, old_url, "GET"
                )

                futures[(key, "new")] = executor.submit(
                    request, new_url, "GET"
                )

                # Test legacy redirect independently.
                if urlsplit(old_url).path.lower().endswith(".shtml"):
                    futures[(key, "legacy")] = executor.submit(
                        check_legacy_redirect,
                        old_url,
                        new_url,
                    )

            for key, old_url, new_url in batch:

                old_response = futures[(key, "old")].result()
                new_response = futures[(key, "new")].result()

                reporter.increment("pages_checked")

                issues = []

                # Page request errors
                if old_response.error or new_response.error:

                    reporter.increment("page_errors")

                    if old_response.error:
                        issues.append(
                            f"OLD request error: {old_response.error}"
                        )

                    if new_response.error:
                        issues.append(
                            f"NEW request error: {new_response.error}"
                        )

                if not (200 <= old_response.status < 300):

                    reporter.increment("page_errors")

                    issues.append(
                        f"OLD page returned HTTP {old_response.status}"
                    )

                if not (200 <= new_response.status < 300):

                    reporter.increment("page_errors")

                    issues.append(
                        f"NEW page returned HTTP {new_response.status}"
                    )

                # A canonical .html URL should serve directly.
                if new_response.redirects:

                    reporter.increment("redirect_errors")

                    first_code, _, first_to = (
                        new_response.redirects[0]
                    )

                    issues.append(
                        "Canonical NEW URL unexpectedly redirects: "
                        f"HTTP {first_code} -> {first_to}"
                    )

                # Independent legacy redirect test.
                if (key, "legacy") in futures:

                    redirect_issues = futures[
                        (key, "legacy")
                    ].result()

                    if redirect_issues:

                        reporter.increment(
                            "redirect_errors",
                            len(redirect_issues),
                        )

                        for problem in redirect_issues:
                            issues.append(
                                f"Legacy redirect: {problem}"
                            )

                # Do not compare content if either page failed.
                pages_ok = (
                    200 <= old_response.status < 300
                    and 200 <= new_response.status < 300
                    and not old_response.error
                    and not new_response.error
                )

                if pages_ok:

                    old_text, old_links = parse_page(
                        old_response, "windows-1251"
                    )

                    new_text, new_links = parse_page(
                        new_response, "utf-8"
                    )

                    # Compare visible text.
                    differences = compare_text(
                        old_text, new_text
                    )

                    if differences:

                        reporter.increment("text_differences")

                        issues.append("Visible text differs:")

                        for diff in differences:

                            issues.append(
                                f"  {diff['type']}:"
                            )

                            issues.append(
                                f"    OLD: {diff['old']!r}"
                            )

                            issues.append(
                                f"    NEW: {diff['new']!r}"
                            )

                    # Compare links.
                    links = compare_links(
                        old_links,
                        new_links,
                        old_response.url,
                        new_response.url,
                        old_base,
                        new_base,
                    )

                    if links["old_only"] or links["new_only"]:

                        reporter.increment("link_differences")

                        issues.append("Link differences:")

                        for link in links["old_only"]:
                            issues.append(f"  OLD ONLY: {link}")

                        for link in links["new_only"]:
                            issues.append(f"  NEW ONLY: {link}")

                    old_map = links["old_map"]
                    new_map = links["new_map"]

                    # Build matching internal destinations.
                    all_keys = set(old_map) | set(new_map)

                    for link_key in all_keys:

                        # External links are not crawled.
                        if link_key.startswith(
                            ("http://", "https://")
                        ):
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

                        old_internal = is_internal(
                            old_target, old_base
                        )

                        new_internal = is_internal(
                            new_target, new_base
                        )

                        if not (old_internal and new_internal):
                            continue

                        if is_file(old_target) or is_file(new_target):

                            pending_files[link_key] = (
                                old_target, new_target
                            )

                        elif link_key not in visited:

                            queue.append((
                                link_key,
                                old_target,
                                new_target,
                            ))

                # Print only pages with problems.
                if issues:

                    reporter.log("")
                    reporter.log(f"PAGE: {key}")

                    for issue in issues:
                        reporter.log(f"  {issue}")

        # --------------------------------------------------------------
        # CHECK FILES
        # --------------------------------------------------------------

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

            old_ok = (
                200 <= old_r.status < 400
                and not old_r.error
            )

            new_ok = (
                200 <= new_r.status < 400
                and not new_r.error
            )

            if old_ok and new_ok:
                continue

            reporter.increment("file_errors")

            reporter.log("")
            reporter.log(f"FILE ERROR: {key}")

            reporter.log(
                f"  OLD: HTTP {old_r.status} "
                f"{old_r.error or old_r.url}"
            )

            reporter.log(
                f"  NEW: HTTP {new_r.status} "
                f"{new_r.error or new_r.url}"
            )

    return reporter.summary()


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description="Compare two websites after migration"
    )

    parser.add_argument(
        "--old",
        default=OLD_SITE,
        help="Old website URL",
    )

    parser.add_argument(
        "--new",
        default=NEW_SITE,
        help="New website URL",
    )

    parser.add_argument(
        "-j",
        "--workers",
        type=int,
        default=WORKERS,
        help="Number of concurrent page pairs",
    )

    args = parser.parse_args()

    errors = crawl(
        args.old,
        args.new,
        args.workers,
    )

    # Useful for automated checks and CI.
    sys.exit(1 if errors else 0)


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
