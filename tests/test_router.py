"""Tests for dynamic expert discovery in SemanticRouter."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from bike_shop.router import _parse_frontmatter, SemanticRouter


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    """Tests for the frontmatter parser."""

    def test_multiline_description(self, tmp_path: Path) -> None:
        md = tmp_path / "expert.md"
        md.write_text(textwrap.dedent("""\
            ---
            name: dev-py
            description: >
              Agent de desenvolvimento Python hands-on. Escreve codigo com extrema qualidade,
              sempre questiona e entende profundamente antes de agir.
            tools: Read, Write
            model: opus
            ---
        """))
        result = _parse_frontmatter(str(md))
        assert result is not None
        name, desc = result
        assert name == "dev-py"
        assert desc == "Agent de desenvolvimento Python hands-on"

    def test_single_line_description(self, tmp_path: Path) -> None:
        md = tmp_path / "expert.md"
        md.write_text(textwrap.dedent("""\
            ---
            name: simple
            description: A simple expert that does one thing.
            model: sonnet
            ---
        """))
        result = _parse_frontmatter(str(md))
        assert result is not None
        name, desc = result
        assert name == "simple"
        assert desc == "A simple expert that does one thing"

    def test_no_frontmatter(self, tmp_path: Path) -> None:
        md = tmp_path / "expert.md"
        md.write_text("# Just a markdown file\n")
        assert _parse_frontmatter(str(md)) is None

    def test_missing_name(self, tmp_path: Path) -> None:
        md = tmp_path / "expert.md"
        md.write_text(textwrap.dedent("""\
            ---
            description: No name field here.
            ---
        """))
        assert _parse_frontmatter(str(md)) is None

    def test_missing_description(self, tmp_path: Path) -> None:
        md = tmp_path / "expert.md"
        md.write_text(textwrap.dedent("""\
            ---
            name: no-desc
            model: opus
            ---
        """))
        assert _parse_frontmatter(str(md)) is None

    def test_file_not_found(self) -> None:
        assert _parse_frontmatter("/nonexistent/path.md") is None


# ---------------------------------------------------------------------------
# SemanticRouter._discover_experts / _build_prompt
# ---------------------------------------------------------------------------


def _create_expert(directory: Path, name: str, desc: str) -> None:
    path = directory / f"{name}.md"
    path.write_text(textwrap.dedent(f"""\
        ---
        name: {name}
        description: >
          {desc}
        tools: Read
        model: opus
        ---
    """))


class TestDiscoverExperts:
    """Tests for dynamic expert discovery."""

    @patch("bike_shop.router.Tracer")
    def test_discovers_experts_from_directory(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        _create_expert(tmp_path, "alpha", "First expert. Does alpha things.")
        _create_expert(tmp_path, "beta", "Second expert. Does beta things.")

        router = SemanticRouter(experts_dir=str(tmp_path))

        assert router._validated_experts == {"alpha", "beta"}
        assert "- alpha: First expert" in router._router_prompt
        assert "- beta: Second expert" in router._router_prompt
        assert "- none:" in router._router_prompt

    @patch("bike_shop.router.Tracer")
    def test_empty_directory_fallback(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        router = SemanticRouter(experts_dir=str(empty))

        assert len(router._validated_experts) == 0
        assert "- none:" in router._router_prompt

    @patch("bike_shop.router.Tracer")
    def test_prompt_contains_no_hardcoded_experts(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        _create_expert(tmp_path, "only-one", "The only expert. Nothing else.")

        router = SemanticRouter(experts_dir=str(tmp_path))

        # Should NOT contain old hardcoded experts as agent entries
        assert "- dev-py:" not in router._router_prompt
        assert "- architect:" not in router._router_prompt
        # Should contain the discovered one
        assert "only-one" in router._router_prompt

    @patch("bike_shop.router.Tracer")
    def test_skips_unparseable_files(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        _create_expert(tmp_path, "good", "A good expert. Works well.")
        bad = tmp_path / "bad.md"
        bad.write_text("# No frontmatter here\n")

        router = SemanticRouter(experts_dir=str(tmp_path))

        assert router._validated_experts == {"good"}
