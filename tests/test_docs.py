"""The documentation is checked, not trusted.

`docs/PRIVACY.md` answers "how do you know?" for each requirement by naming
the test that proves it, and `README.md` points at files. Both go stale
silently — a renamed test leaves a privacy claim citing evidence that no
longer exists, which is worse than citing nothing. So the references are
checked here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "DELIVERY.md", ROOT / "docs" / "PRIVACY.md",
        ROOT / "docs" / "INTEGRATION.md", ROOT / "docs" / "API.md"]


def defined_test_names() -> set[str]:
    names = set()
    for path in (ROOT / "tests").glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names |= {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        }
    return names


def test_every_test_named_in_the_docs_exists():
    known = defined_test_names()
    cited = set()
    for doc in DOCS:
        cited |= set(re.findall(r"::(\w+)`", doc.read_text(encoding="utf-8")))

    assert cited, "the docs should name the tests that back their claims"
    missing = sorted(cited - known)
    assert not missing, f"docs cite tests that do not exist: {missing}"


def test_every_repository_path_named_in_the_docs_exists():
    """A layout section or a 'see this file' pointer that names something
    deleted is how a reader loses trust in the rest of the document."""
    missing = []
    for doc in DOCS:
        for match in re.findall(r"`((?:app|docs|tests|tools|scripts|api)/[\w./-]+)`", doc.read_text(encoding="utf-8")):
            candidate = match.rstrip(".")
            if not (ROOT / candidate).exists():
                missing.append(f"{doc.name} → {candidate}")

    assert not missing, "the docs point at paths that are not in the repository: " + ", ".join(sorted(set(missing)))


def test_the_docs_do_not_link_to_a_repository_that_is_not_this_one():
    """The deploy buttons import a GitHub repository by URL. Pointing them at
    a scratch repository ships a button that deploys the wrong code."""
    for doc in DOCS + [ROOT / "DEPLOY.md"]:
        text = doc.read_text(encoding="utf-8")
        for owner_repo in re.findall(r"github\.com[/%2F]+([\w-]+)[/%2F]+([\w-]+)", text, re.I):
            assert owner_repo[1].lower() == "vocalyze", (
                f"{doc.name} links to github.com/{owner_repo[0]}/{owner_repo[1]}, not this repository"
            )
