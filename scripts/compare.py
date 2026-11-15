
#!/usr/bin/env python3
"""
Sequential website migration crawler.

Old site:
    https://temnyjles.ru/
    Windows-1251, mostly .shtml

New site:
    https://temnyjles.roman-79c.workers.dev/
    UTF-8, mostly .html

Checks:
- Each HTML page is queued and fetched at most once.
- Each resource is queued and checked at most once.
- URLs are normalized before deduplication.
- Old and new page text is compared.
- Internal links are compared.
- Legacy .shtml URLs on the new site must redirect with HTTP 301.
- Resources are checked separately from HTML pages.
- Sequential execution: no threads or concurrency.

Python 3.10+, standard library only.
"""

from __future__ import annotations

import re
import sys
import time
import socket
import urllib.request
import urllib.error

from collections import deque
from html.parser import HTMLParser
from urllib.parse import (
    urljoin,
    urlsplit,
    urlunsplit,
    unquote,
)


# ============================================================
# CONFIGURATION
# ============================================================

OLD_BASE = "https://temnyjles.ru/"
NEW_BASE = "https://temnyjles.roman-79c.workers.dev/"

USER_AGENT = "MigrationChecker/1.0"

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 30
MAX_RESPONSE_SIZE = 20 * 1024 * 1024

OLD_ENCODING_FALLBACK = "windows-1251"
NEW_ENCODING_FALLBACK = "utf-8"

# Add extensions here if your site uses additional page types.
PAGE_EXTENSIONS = {
    ".html",
    ".shtml",
    ".htm",
}

# Extensions treated as downloadable/static resources.
RESOURCE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".ico", ".bmp", ".tif", ".tiff",
    ".css", ".js", ".json", ".xml",
    ".pdf", ".zip", ".rar", ".7z", ".gz",
    ".mp3", ".mp4", ".avi", ".mov", ".webm",
    ".wav", ".ogg",
    ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
}

# Ignore fragments; these do not identify separate documents.
# Query strings are retained because they can identify different pages.
IGNORE_QUERY_PARAMETERS = False

# Resource check method. HEAD is efficient; GET fallback is used
# when the server does not support HEAD.
RESOURCE_USE_HEAD = True

# Maximum number of bytes read when falling back to GET for resources.
RESOURCE_GET_LIMIT = 1024

# Optional delay between requests, in seconds.
REQUEST_DELAY = 0


# ============================================================
# GLOBAL STATE
# ============================================================

page_queue = deque()
resource_queue = deque()

# These sets are updated BEFORE an item is added to its queue.
# They therefore represent every URL ever scheduled, not just
# URLs already processed.
queued_pages = set()
queued_resources = set()

# Separate completion sets for reporting and defensive checks.
checked_pages = set()
checked_resources = set()

errors = []
warnings = []

page_count = 0
resource_count = 0

def report_error(message):
    errors.append(message)
    print(f"  [ERROR] {message}", flush=True)


def report_warning(message):
    warnings.append(message)
    print(f"  [WARN]  {message}", flush=True)


def report_difference(message):
    print(f"  [DIFF]  {message}", flush=True)

# ============================================================
# URL HELPERS
# ============================================================

def remove_fragment(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def normalize_path(path):
    if not path:
        path = "/"

    path = unquote(path)
    path = re.sub(r"/+", "/", path)

    if not path.startswith("/"):
        path = "/" + path

    # Keep root as root.
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return path


def normalize_url(url):
    """
    Normalize an absolute or relative URL into a logical internal path.

    Old and new domains are treated as the same internal site.
    .shtml and .html map to the same logical page key.
    """

    url = remove_fragment(url.strip())

    absolute = urljoin(NEW_BASE, url)
    p = urlsplit(absolute)

    path = normalize_path(p.path)

    if IGNORE_QUERY_PARAMETERS:
        query = ""
    else:
        query = p.query

    # Canonicalize old/new page extensions for deduplication.
    lower_path = path.lower()

    if lower_path.endswith(".shtml"):
        path = path[:-6] + ".html"

    return path + (("?" + query) if query else "")


def is_internal_url(url):
    p = urlsplit(url)

    if not p.netloc:
        return True

    host = p.netloc.lower().split(":")[0]

    old_host = urlsplit(OLD_BASE).netloc.lower().split(":")[0]
    new_host = urlsplit(NEW_BASE).netloc.lower().split(":")[0]

    return host in {old_host, new_host}


def logical_key_to_old_url(key):
    p = urlsplit(key)
    path = p.path

    # Logical .html maps to legacy .shtml on old site.
    if path.lower().endswith(".html"):
        path = path[:-5] + ".shtml"

    return urljoin(OLD_BASE, path.lstrip("/")) + (
        ("?" + p.query) if p.query else ""
    )


def logical_key_to_new_url(key):
    p = urlsplit(key)
    path = p.path

    return urljoin(NEW_BASE, path.lstrip("/")) + (
        ("?" + p.query) if p.query else ""
    )


def logical_key_to_new_legacy_url(key):
    p = urlsplit(key)
    path = p.path

    if path.lower().endswith(".html"):
        path = path[:-5] + ".shtml"

    return urljoin(NEW_BASE, path.lstrip("/")) + (
        ("?" + p.query) if p.query else ""
    )


def extension_of(url):
    path = urlsplit(url).path.lower()

    # No extension for directory-like URLs.
    filename = path.rsplit("/", 1)[-1]

    if "." not in filename:
        return ""

    return "." + filename.rsplit(".", 1)[-1]


def is_page_url(url):
    ext = extension_of(url)

    if not ext:
        return True

    return ext in PAGE_EXTENSIONS


def is_resource_url(url):
    return extension_of(url) in RESOURCE_EXTENSIONS


# ============================================================
# QUEUE MANAGEMENT
# ============================================================

def add_page(url):
    """
    Schedule an HTML page once.

    Crucially, queued_pages is updated BEFORE appending to the queue.
    """

    if not is_internal_url(url):
        return

    key = normalize_url(url)

    if not is_page_url(key):
        return

    if key in queued_pages:
        return

    queued_pages.add(key)
    page_queue.append(key)


def add_resource(url):
    """
    Schedule a resource once.

    Crucially, queued_resources is updated BEFORE appending.
    """

    if not is_internal_url(url):
        return

    key = normalize_url(url)

    if not is_resource_url(key):
        return

    if key in queued_resources:
        return

    queued_resources.add(key)
    resource_queue.append(key)


# ============================================================
# HTTP
# ============================================================

class RedirectRecorder(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.redirects = []

    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        self.redirects.append({
            "code": code,
            "url": newurl,
        })

        return super().redirect_request(
            req, fp, code, msg, headers, newurl
        )


def request_url(url, method="GET", read_body=True, max_bytes=None):
    """
    Perform one HTTP request and record redirects.

    Returns:
        {
            status,
            final_url,
            redirects,
            headers,
            body,
            error
        }
    """

    recorder = RedirectRecorder()

    opener = urllib.request.build_opener(recorder)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
        },
        method=method,
    )

    result = {
        "status": None,
        "final_url": url,
        "redirects": [],
        "headers": {},
        "body": b"",
        "error": None,
    }

    try:
        if REQUEST_DELAY:
            time.sleep(REQUEST_DELAY)

        with opener.open(request, timeout=CONNECT_TIMEOUT) as response:
            result["status"] = response.status
            result["final_url"] = response.geturl()
            result["headers"] = dict(response.headers.items())
            result["redirects"] = list(recorder.redirects)

            if read_body:
                if max_bytes is None:
                    body = response.read(MAX_RESPONSE_SIZE + 1)
                    if len(body) > MAX_RESPONSE_SIZE:
                        raise ValueError(
                            f"Response exceeded {MAX_RESPONSE_SIZE} bytes"
                        )
                    result["body"] = body
                else:
                    result["body"] = response.read(max_bytes)

    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["final_url"] = exc.geturl()
        result["headers"] = dict(exc.headers.items()) if exc.headers else {}
        result["redirects"] = list(recorder.redirects)
        result["error"] = str(exc)

    except Exception as exc:
        result["redirects"] = list(recorder.redirects)
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


def get_page(url):
    return request_url(url, method="GET", read_body=True)


def check_resource(url):
    if RESOURCE_USE_HEAD:
        result = request_url(
            url,
            method="HEAD",
            read_body=False,
        )

        # Some servers do not implement HEAD correctly.
        if result["status"] in (405, 501) or result["status"] is None:
            result = request_url(
                url,
                method="GET",
                read_body=True,
                max_bytes=RESOURCE_GET_LIMIT,
            )

        return result

    return request_url(
        url,
        method="GET",
        read_body=True,
        max_bytes=RESOURCE_GET_LIMIT,
    )


# ============================================================
# HTML PARSING
# ============================================================

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)

        self.text_parts = []
        self.links = []

        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)

        if tag in ("script", "style", "noscript", "svg"):
            self.skip_depth += 1
            return

        if self.skip_depth:
            return

        if tag in ("a", "area"):
            href = attrs.get("href")
            if href:
                self.links.append(href)

        elif tag in ("img", "script", "iframe", "source", "video", "audio"):
            src = attrs.get("src")
            if src:
                self.links.append(src)

        elif tag == "link":
            href = attrs.get("href")
            if href:
                self.links.append(href)

        elif tag == "object":
            data = attrs.get("data")
            if data:
                self.links.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in ("script", "style", "noscript", "svg"):
            if self.skip_depth:
                self.skip_depth -= 1

    def handle_data(self, data):
        if self.skip_depth:
            return

        text = data.strip()

        if text:
            self.text_parts.append(text)


def detect_encoding(headers, body, fallback):
    content_type = ""

    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = value
            break

    match = re.search(
        r"charset\s*=\s*[\"']?([^;\s\"']+)",
        content_type,
        re.I,
    )

    if match:
        return match.group(1)

    # Look for charset in the first part of the document.
    head = body[:8192].decode("ascii", errors="ignore")

    match = re.search(
        r"<meta[^>]+charset\s*=\s*[\"']?([^\"'\s/>]+)",
        head,
        re.I,
    )

    if match:
        return match.group(1)

    match = re.search(
        r"<meta[^>]+content\s*=\s*[\"'][^\"']*charset=([^\"';\s]+)",
        head,
        re.I,
    )

    if match:
        return match.group(1)

    return fallback


def decode_body(result, fallback):
    encoding = detect_encoding(
        result["headers"],
        result["body"],
        fallback,
    )

    try:
        return result["body"].decode(encoding, errors="replace")
    except LookupError:
        return result["body"].decode(fallback, errors="replace")


def parse_html(result, fallback):
    html = decode_body(result, fallback)

    parser = PageParser()

    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass

    text = " ".join(parser.text_parts)
    text = re.sub(r"\s+", " ", text).strip()

    # Normalize empty placeholder text.
    text = re.sub(r"\(\s*none\s*\)", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()

    return text, parser.links


# ============================================================
# REDIRECT CHECKS
# ============================================================

def first_redirect_status(result):
    redirects = result["redirects"]

    if not redirects:
        return None

    return redirects[0]["code"]


def same_destination(actual, expected):
    """
    Compare redirect destination paths, ignoring domain and fragments.
    """

    actual_abs = urljoin(NEW_BASE, actual)
    expected_abs = urljoin(NEW_BASE, expected)

    a = urlsplit(actual_abs)
    e = urlsplit(expected_abs)

    actual_path = normalize_path(a.path)
    expected_path = normalize_path(e.path)

    if actual_path == "/index.html":
        actual_path = "/"

    if actual_path == "/index.shtml":
        actual_path = "/"

    return actual_path == expected_path


def check_legacy_redirect(key):
    legacy_url = logical_key_to_new_legacy_url(key)
    canonical_url = logical_key_to_new_url(key)

    result = get_page(legacy_url)

    if result["error"] and result["status"] is None:
        report_error(
            f"LEGACY REQUEST FAILED: {legacy_url} — {result['error']}"
        )
        return

    code = first_redirect_status(result)

    if code != 301:
        report_error(
            f"LEGACY REDIRECT NOT 301: {legacy_url} "
            f"(first redirect: {code}, final status: {result['status']})"
        )
        return

    if result["status"] is None or not (200 <= result["status"] < 300):
        report_error(
            f"LEGACY REDIRECT FINAL STATUS: {legacy_url} "
            f"(status: {result['status']})"
        )
        return

    if not same_destination(result["final_url"], canonical_url):
        # The home page may legitimately redirect to /.
        if key in ("/index.html", "/"):
            if not same_destination(result["final_url"], NEW_BASE):
                report_error(
                    f"LEGACY REDIRECT WRONG DESTINATION: {legacy_url} "
                    f"-> {result['final_url']}"
                )
        else:
            report_error(
                f"LEGACY REDIRECT WRONG DESTINATION: {legacy_url} "
                f"-> {result['final_url']} (expected {canonical_url})"
            )


# ============================================================
# PAGE PROCESSING
# ============================================================

def discover_links(links, base_url):
    discovered_pages = set()
    discovered_resources = set()

    for href in links:
        href = href.strip()

        if not href:
            continue

        if href.startswith((
            "mailto:",
            "tel:",
            "javascript:",
            "data:",
        )):
            continue

        absolute = urljoin(base_url, href)

        if not is_internal_url(absolute):
            continue

        key = normalize_url(absolute)

        if is_resource_url(key):
            discovered_resources.add(key)
        elif is_page_url(key):
            discovered_pages.add(key)

    return discovered_pages, discovered_resources


def process_page(key):
    """
    Process one logical page.

    Always returns (new_pages, new_resources), including on errors.
    """

    old_url = logical_key_to_old_url(key)
    new_url = logical_key_to_new_url(key)

    old_result = get_page(old_url)

    if old_result["status"] is None or not (200 <= old_result["status"] < 300):
        report_error(
            f"OLD PAGE FAILED: {old_url} "
            f"(status: {old_result['status']}, error: {old_result['error']})"
        )
        return set(), set()

    new_result = get_page(new_url)

    if new_result["status"] is None or not (200 <= new_result["status"] < 300):
        report_error(
            f"NEW PAGE FAILED: {new_url} "
            f"(status: {new_result['status']}, error: {new_result['error']})"
        )
        return set(), set()

    old_text, old_links = parse_html(
        old_result,
        OLD_ENCODING_FALLBACK,
    )

    new_text, new_links = parse_html(
        new_result,
        NEW_ENCODING_FALLBACK,
    )

    if old_text != new_text:
        report_difference(
            f"TEXT DIFFERENCE: {key} "
            f"(old {len(old_text)} chars, new {len(new_text)} chars)"
        )

    old_pages, old_resources = discover_links(old_links, old_url)
    new_pages, new_resources = discover_links(new_links, new_url)

    missing_new_links = old_pages - new_pages
    extra_new_links = new_pages - old_pages

    if missing_new_links:
        report_difference(
            f"LINKS MISSING ON NEW PAGE: {key}: "
            + ", ".join(sorted(missing_new_links))
        )

    if extra_new_links:
        report_difference(
            f"EXTRA LINKS ON NEW PAGE: {key}: "
            + ", ".join(sorted(extra_new_links))
        )

    # Discover pages from both versions.
    # The add_page function performs deduplication immediately.
    for page in old_pages:
        add_page(page)

    for page in new_pages:
        add_page(page)

    # Discover resources from both versions.
    for resource in old_resources:
        add_resource(resource)

    for resource in new_resources:
        add_resource(resource)

    # Verify old-style URL on the NEW site redirects to canonical URL.
    check_legacy_redirect(key)

    return old_pages | new_pages, old_resources | new_resources


# ============================================================
# RESOURCE PROCESSING
# ============================================================

def process_resource(key):
    url = logical_key_to_new_url(key)

    result = check_resource(url)

    if result["status"] is None or not (200 <= result["status"] < 400):
        report_error(
            f"RESOURCE FAILED: {url} "
            f"(status: {result['status']}, error: {result['error']})"
        )


# ============================================================
# MAIN CRAWLER
# ============================================================

def main():
    global page_count, resource_count

    print("=" * 70)
    print("WEBSITE MIGRATION CHECKER")
    print("=" * 70)
    print("Old:", OLD_BASE)
    print("New:", NEW_BASE)
    print("Mode: sequential")
    print("Deduplication: at queue insertion")
    print()

    add_page("/")

    while page_queue:
        key = page_queue.popleft()

        # Defensive guard. Normally impossible because add_page()
        # inserts into queued_pages before appending.
        if key in checked_pages:
            continue

        checked_pages.add(key)
        page_count += 1

        print(
            f"[{page_count:04d}] "
            f"Queue: {len(page_queue):05d} | {key}",
            flush=True,
        )

        process_page(key)

        print(f"[DONE] {key}", flush=True)

    print()
    print("All HTML pages processed.")
    print(f"Unique pages scheduled: {len(queued_pages)}")
    print(f"Pages checked: {len(checked_pages)}")
    print()

    # Process resources only after all pages have been crawled.
    while resource_queue:
        key = resource_queue.popleft()

        if key in checked_resources:
            continue

        checked_resources.add(key)
        resource_count += 1

        print(
            f"[RESOURCE {resource_count:05d}] "
            f"Queue: {len(resource_queue):05d} | {key}",
            flush=True,
        )

        process_resource(key)

    print()
    print("=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)

    print(f"Pages scheduled:   {len(queued_pages)}")
    print(f"Pages checked:     {len(checked_pages)}")
    print(f"Resources queued:  {len(queued_resources)}")
    print(f"Resources checked: {len(checked_resources)}")
    print(f"Errors:            {len(errors)}")
    print(f"Warnings:          {len(warnings)}")

    if errors:
        print()
        print("--- ERRORS ---")
        for item in errors:
            print("[ERROR]", item)

    if warnings:
        print()
        print("--- WARNINGS ---")
        for item in warnings:
            print("[WARN]", item)

    print()
    print("Finished.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
        print(f"Pages scheduled: {len(queued_pages)}")
        print(f"Pages checked: {len(checked_pages)}")
        print(f"Resources scheduled: {len(queued_resources)}")
        print(f"Resources checked: {len(checked_resources)}")
        sys.exit(130)
