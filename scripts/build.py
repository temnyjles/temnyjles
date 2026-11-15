
from pathlib import Path
import re
import shutil

SOURCE = Path("src")
OUTPUT = Path("dist")

# SSI include directive.
include_re = re.compile(
    r'<!--#include\s+file="([^"]+)"\s*-->'
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


# Start with a clean output directory.
if OUTPUT.exists():
    shutil.rmtree(OUTPUT)

redirects = []

for path in SOURCE.rglob("*"):

    if path.is_dir():
        continue

    relative = path.relative_to(SOURCE)

    if path.suffix.lower() in (".html", ".shtml"):

        # Generate .html output from both source extensions.
        target = OUTPUT / relative.with_suffix(".html")
        target.parent.mkdir(parents=True, exist_ok=True)

        content = process(path)

        target.write_text(content, encoding="utf-8")

        # Preserve old .shtml URLs with permanent redirects.
        if path.suffix.lower() == ".shtml":

            old_url = "/" + relative.as_posix()

            # The homepage should redirect directly to /.
            if relative.as_posix().lower() == "index.shtml":
                new_url = "/"
            else:
                new_url = "/" + relative.with_suffix(".html").as_posix()

            redirects.append(f"{old_url} {new_url} 301")

    else:
        # Copy all other files unchanged.
        target = OUTPUT / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(path, target)


# Generate Cloudflare Pages redirects.
(OUTPUT / "_redirects").write_text(
    "/index.shtml / 301\n"
    "/*.shtml /:splat.html 301\n",
    encoding="utf-8"
)

print(f"Website successfully built in {OUTPUT}/")
print(f"Generated {len(redirects)} redirects.")
