import importlib.util
from pathlib import Path

from pdt import profiles

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "render_profiles_doc.py"
spec = importlib.util.spec_from_file_location("render_profiles_doc", SCRIPT)
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def test_docs_profiles_md_matches_the_profile_modules():
    current = (REPO / "docs" / "profiles.md").read_text(encoding="utf-8")
    assert current == renderer.render(), (
        "docs/profiles.md is stale; run `uv run python scripts/render_profiles_doc.py`")


def test_the_rendered_doc_names_every_profile_concern_and_command():
    text = renderer.render()
    for name in profiles.NAMES:
        assert f"## `{name}`" in text
        for command in profiles.module(name).COMMANDS:
            assert f"`pdt {command}`" in text
    for concern, description in profiles.CONCERNS:
        assert f"- **{concern}**: {description}" in text
        assert f"| {concern} |" in text


def test_the_rendered_doc_has_no_hard_wrapped_prose():
    # A prose line is one paragraph (AGENTS.md, "Writing Markdown"), so it ends a sentence.
    for line in renderer.render().splitlines():
        if line and not line.startswith(("|", "- ", "#")):
            assert line.endswith((".", ":")), line
