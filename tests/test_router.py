"""Tests for dynamic expert discovery in SemanticRouter."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

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
# SemanticRouter.route() with subprocess mock
# ---------------------------------------------------------------------------


class TestRoute:
    """Tests for route() with subprocess mocked."""

    @patch("bike_shop.router.Tracer")
    def _make_router(self, _mock_tracer: object, tmp_path: Path) -> SemanticRouter:
        _create_expert(tmp_path, "dev-py", "Python development expert. Writes code.")
        _create_expert(tmp_path, "architect", "System design expert. Makes diagrams.")
        return SemanticRouter(experts_dir=str(tmp_path))

    @patch("bike_shop.router.Tracer")
    @patch("bike_shop.router.subprocess.run")
    def test_route_delegates_to_expert(
        self, mock_run: MagicMock, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        _create_expert(tmp_path, "dev-py", "Python development expert. Writes code.")
        router = SemanticRouter(experts_dir=str(tmp_path))

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"agent": "dev-py", "model": "opus", "reason": "complex coding"}),
            stderr="",
        )

        result = router.route("implement the auth module")

        assert result["agent"] == "dev-py"
        assert result["model_name"] == "opus"
        # Verify the dynamic prompt was passed to subprocess
        call_args = mock_run.call_args[0][0]
        assert "dev-py" in call_args[2]  # prompt is 3rd arg after "claude", "-p"

    @patch("bike_shop.router.Tracer")
    @patch("bike_shop.router.subprocess.run")
    def test_route_unknown_expert_falls_back(
        self, mock_run: MagicMock, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        _create_expert(tmp_path, "dev-py", "Python development expert. Writes code.")
        router = SemanticRouter(experts_dir=str(tmp_path))

        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"agent": "nonexistent", "model": "sonnet", "reason": "test"}),
            stderr="",
        )

        result = router.route("do something")

        assert result["agent"] is None  # fell back because expert not on disk

    @patch("bike_shop.router.Tracer")
    @patch("bike_shop.router.subprocess.run")
    def test_route_timeout_falls_back_to_sonnet(
        self, mock_run: MagicMock, _mock_tracer: object, tmp_path: Path,
    ) -> None:
        _create_expert(tmp_path, "dev-py", "Python development expert. Writes code.")
        router = SemanticRouter(experts_dir=str(tmp_path))

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)

        result = router.route("anything")

        assert result["agent"] is None
        assert result["model_name"] == "sonnet"
        assert result["reason"] == "router_fallback"
