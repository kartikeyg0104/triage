"""Every relative link in the top-level docs must point to a file that exists."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ("README.md", "CONTRIBUTING.md", "SECURITY.md")

# [text](target) or [text](target "title"); images too. Code spans are removed first.
LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


def relative_links(text: str) -> list[str]:
    links = []
    for target in LINK.findall(CODE.sub("", text)):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE) or target.startswith("#"):
            continue  # http:, https:, mailto: and in-page anchors are not files
        links.append(target.split("#", 1)[0])
    return links


def test_relative_links_ignore_urls_anchors_and_code():
    text = "[a](docs/A.md) [b](https://x.y) [c](#top) [d](B.md#part) `[e](nope.md)`\n```\n[f](nope.md)\n```"
    assert relative_links(text) == ["docs/A.md", "B.md"]


@pytest.mark.parametrize("name", DOCS)
def test_every_relative_link_points_to_a_file_that_exists(name):
    path = ROOT / name
    assert path.is_file(), f"{name} is missing"
    broken = [link for link in relative_links(path.read_text()) if not (path.parent / link).exists()]
    assert broken == [], f"{name} links to missing files: {broken}"
