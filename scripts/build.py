
from pathlib import Path
import re
import shutil

SOURCE = Path("src")
OUTPUT = Path("dist")

def raise_ssi_error(message, content, document_uri, offset):
    """Raise an SSI error with line number and surrounding context."""

    line = content.count("\n", 0, offset) + 1

    context = content[
        max(0, offset - 150):offset + 300
    ]

    raise ValueError(
        f"{message}\n"
        f"Document: {document_uri}\n"
        f"Line: {line}\n"
        f"Context:\n{context}"
    )

# ============================================================
# SSI INCLUDE PROCESSING
# ============================================================

include_re = re.compile(
    r'<!--#include\s+file="([^"]+)"\s*-->'
)

def resolve_include(path, document_uri, source_content, offset):
    """Find an include, supporting .html -> .shtml fallback."""

    if path.is_file():
        return path

    if path.suffix.lower() == ".html":
        alternative = path.with_suffix(".shtml")

        if alternative.is_file():
            return alternative

    raise_ssi_error(
        f"SSI include not found: {path}",
        document_uri,
        content=source_content,
        offset=offset
    )

def process_includes(path, document_uri, stack=()):
    """Expand SSI includes recursively."""

    path = path.resolve()

    if path in stack:
        raise_ssi_error(
            f"Recursive include: {path}",
            document_uri,
            path=path
        )

    content = path.read_text(encoding="utf-8")

    def replace(match):
        included = (path.parent / match.group(1)).resolve()

        included = resolve_include(
            included,
            document_uri,
            content,
            match.start()
        )

        return process_includes(
            included,
            document_uri,
            stack + (path,)
        )

    return include_re.sub(replace, content)


# ============================================================
# SSI CONDITIONAL PROCESSING
# ============================================================

conditional_re = re.compile(
    r'<!--#(?:(?P<if>if\s+expr="([^"]+)")|'
    r'(?P<else>else)|'
    r'(?P<endif>endif))\s*-->'
)

def evaluate_expr(expr, document_uri):
    """Evaluate supported SSI expressions."""

    match = re.fullmatch(
        r"""\s*\$DOCUMENT_URI\s*(==|!=|=)\s*(['"])(.*?)\2\s*""",
        expr
    )

    if not match:
        raise ValueError(f"Unsupported SSI expression: {expr}")

    operator, _, expected = match.groups()

    if operator in ("=", "=="):
        return document_uri == expected

    if operator == "!=":
        return document_uri != expected


def process_conditionals(content, document_uri):
    """Process SSI conditionals, including nested blocks."""

    result = []
    stack = []
    active = True
    position = 0

    for match in conditional_re.finditer(content):

        # Include ordinary content only when active.
        if active:
            result.append(content[position:match.start()])

        if match.group("if"):

            parent_active = active

            condition = evaluate_expr(
                match.group(2),
                document_uri
            )

            stack.append({
                "parent": parent_active,
                "condition": condition,
                "else": False,
                "offset": match.start()
            })

            active = parent_active and condition

        elif match.group("else"):

            if not stack:
                raise_ssi_error(
                    "SSI else without matching if",
                    content,
                    document_uri,
                    match.start()
                )

            frame = stack[-1]

            if frame["else"]:
                raise_ssi_error(
                    "Duplicate SSI else",
                    content,
                    document_uri,
                    match.start()
                )

            frame["else"] = True

            active = (
                frame["parent"]
                and not frame["condition"]
            )

        elif match.group("endif"):

            if not stack:
                raise_ssi_error(
                    "SSI endif without matching if",
                    content,
                    document_uri,
                    match.start()
                )

            frame = stack.pop()
            active = frame["parent"]

        position = match.end()

    # Append remaining content.
    if active:
        result.append(content[position:])

    # Check for unclosed conditionals.
    if stack:
        raise_ssi_error(
            "SSI if without matching endif",
            content,
            document_uri,
            stack[-1]["offset"]
        )

    return "".join(result)


# ============================================================
# BUILD WEBSITE
# ============================================================

# Start with a clean output directory.
if OUTPUT.exists():
    shutil.rmtree(OUTPUT)

for path in SOURCE.rglob("*"):

    if path.is_dir():
        continue

    relative = path.relative_to(SOURCE)

    if path.suffix.lower() in (".html", ".shtml"):

        # Generate .html output from both source extensions.
        target = OUTPUT / relative.with_suffix(".html")
        target.parent.mkdir(parents=True, exist_ok=True)

        # Original URL, used for DOCUMENT_URI.
        document_uri = "/" + relative.as_posix()

        # Stage 1: Expand includes.
        content = process_includes(path, document_uri)

        # Stage 2: Evaluate conditionals.
        content = process_conditionals(content, document_uri)

        target.write_text(content, encoding="utf-8")

    else:

        # Copy all other files unchanged.
        target = OUTPUT / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(path, target)


# ============================================================
# CLOUDFLARE PAGES REDIRECTS
# ============================================================

(OUTPUT / "_redirects").write_text(
    "/index.shtml / 301\n"
    "/*.shtml /:splat.html 301\n",
    encoding="utf-8"
)


print(f"Website successfully built in {OUTPUT}/")
