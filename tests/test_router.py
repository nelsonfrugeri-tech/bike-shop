"""Tests for SemanticRouter — expert discovery and passthrough routing."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from bike_shop.config import MODEL_MAP
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

    def test_sentence_split_preserves_version_numbers(self, tmp_path: Path) -> None:
        md = tmp_path / "expert.md"
        md.write_text(textwrap.dedent("""\
            ---
            name: dev-ts
            description: TypeScript v5.7 expert for React v19.0 apps. Writes tests first.
            model: opus
            ---
        """))
        result = _parse_frontmatter(str(md))
        assert result is not None
        _, desc = result
        assert desc == "TypeScript v5.7 expert for React v19.0 apps"

    def test_sentence_split_on_period_space(self, tmp_path: Path) -> None:
        """Splits on '. ' boundary — first sentence only."""
        md = tmp_path / "expert.md"
        md.write_text(textwrap.dedent("""\
            ---
            name: doc-writer
            description: Writes docs and guides. Also changelogs and READMEs.
            model: sonnet
            ---
        """))
        result = _parse_frontmatter(str(md))
        assert result is not None
        _, desc = result
        assert desc == "Writes docs and guides"

    def test_sentence_split_single_sentence_no_trailing_period(self, tmp_path: Path) -> None:
        md = tmp_path / "expert.md"
        md.write_text(textwrap.dedent("""\
            ---
            name: minimal
            description: Just one sentence without period
            model: haiku
            ---
        """))
        result = _parse_frontmatter(str(md))
        assert result is not None
        _, desc = result
        assert desc == "Just one sentence without period"


# ---------------------------------------------------------------------------
# Helper
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


# ---------------------------------------------------------------------------
# SemanticRouter._discover_experts
# ---------------------------------------------------------------------------


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

    @patch("bike_shop.router.Tracer")
    def test_empty_directory_fallback(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        router = SemanticRouter(experts_dir=str(empty))

        assert len(router._validated_experts) == 0

    @patch("bike_shop.router.Tracer")
    def test_skips_unparseable_files(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        _create_expert(tmp_path, "good", "A good expert. Works well.")
        bad = tmp_path / "bad.md"
        bad.write_text("# No frontmatter here\n")

        router = SemanticRouter(experts_dir=str(tmp_path))

        assert router._validated_experts == {"good"}

    @patch("bike_shop.router.Tracer")
    def test_strips_quotes_from_name(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        md = tmp_path / "quoted.md"
        md.write_text(textwrap.dedent("""\
            ---
            name: "dev-py"
            description: A quoted expert. Does things.
            model: opus
            ---
        """))

        router = SemanticRouter(experts_dir=str(tmp_path))

        assert "dev-py" in router._validated_experts

    @patch("bike_shop.router.Tracer")
    def test_rejects_invalid_name_format(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        md = tmp_path / "bad-name.md"
        md.write_text(textwrap.dedent("""\
            ---
            name: dev py
            description: Name with space. Should be rejected.
            model: opus
            ---
        """))

        router = SemanticRouter(experts_dir=str(tmp_path))

        assert len(router._validated_experts) == 0

    @patch("bike_shop.router.Tracer")
    def test_skips_symlink_outside_dir(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        experts = tmp_path / "experts"
        experts.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "evil.md"
        target.write_text(textwrap.dedent("""\
            ---
            name: evil
            description: Should not be loaded. Evil expert.
            model: opus
            ---
        """))
        symlink = experts / "evil.md"
        symlink.symlink_to(target)

        router = SemanticRouter(experts_dir=str(experts))

        assert "evil" not in router._validated_experts


# ---------------------------------------------------------------------------
# SemanticRouter.route() — passthrough (no LLM)
# ---------------------------------------------------------------------------


class TestRoute:
    """Tests for passthrough route() — no subprocess, no LLM."""

    @patch("bike_shop.router.Tracer")
    def test_route_returns_passthrough_with_sonnet_default(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        _create_expert(tmp_path, "dev-py", "Python development expert. Writes code.")
        router = SemanticRouter(experts_dir=str(tmp_path))

        result = router.route("implement the auth module")

        assert result["agent"] is None
        assert result["model"] == MODEL_MAP["sonnet"]
        assert result["model_name"] == "sonnet"
        assert "passthrough" in result["reason"]
        assert result["memory"] == []

    @patch("bike_shop.router.Tracer")
    def test_route_returns_passthrough_with_empty_experts(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        router = SemanticRouter(experts_dir=str(empty))

        result = router.route("hello")

        assert result["agent"] is None
        assert result["model"] == MODEL_MAP["sonnet"]
        assert result["memory"] == []

    @patch("bike_shop.router.Tracer")
    def test_route_with_trace_id_creates_span(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        _create_expert(tmp_path, "dev-py", "Python development expert. Writes code.")
        router = SemanticRouter(experts_dir=str(tmp_path))

        # Should not raise even with trace_id
        result = router.route("test", trace_id="trace-123", parent_span_id="span-456")

        assert result["agent"] is None


# ---------------------------------------------------------------------------
# SemanticRouter.get_experts_description()
# ---------------------------------------------------------------------------


class TestGetExpertsDescription:
    """Tests for expert description formatting."""

    @patch("bike_shop.router.Tracer")
    def test_formats_experts_sorted(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        _create_expert(tmp_path, "dev-py", "Python development expert. Writes code.")
        _create_expert(tmp_path, "architect", "System design expert. Makes diagrams.")
        router = SemanticRouter(experts_dir=str(tmp_path))

        desc = router.get_experts_description()

        lines = desc.strip().split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("- architect:")
        assert lines[1].startswith("- dev-py:")

    @patch("bike_shop.router.Tracer")
    def test_returns_empty_string_when_no_experts(
        self, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        router = SemanticRouter(experts_dir=str(empty))

        assert router.get_experts_description() == ""
