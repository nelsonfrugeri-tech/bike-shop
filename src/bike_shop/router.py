from __future__ import annotations

import glob
import logging
import os
import re
import time
from pathlib import Path

from bike_shop.config import MODEL_MAP
from bike_shop.observability import Tracer

logger = logging.getLogger(__name__)


def _parse_frontmatter(filepath: str) -> tuple[str, str] | None:
    """Extract name and first sentence of description from expert frontmatter.

    Returns (name, short_description) or None if parsing fails.
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None

    # Match YAML frontmatter between --- delimiters
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not fm_match:
        return None

    fm = fm_match.group(1)

    # Extract name
    name_match = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
    if not name_match:
        return None
    name = name_match.group(1).strip().strip('"').strip("'")

    # Extract description (may be multi-line folded with >)
    desc_match = re.search(
        r"^description:\s*>?\s*\n((?:\s{2,}.+\n?)+)", fm, re.MULTILINE
    )
    if desc_match:
        desc_lines = desc_match.group(1).strip().splitlines()
        full_desc = " ".join(line.strip() for line in desc_lines)
    else:
        # Single-line description
        desc_match = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
        if not desc_match:
            return None
        full_desc = desc_match.group(1).strip().strip('"').strip("'")

    # First sentence: split on ". " (period + space) to avoid truncating on
    # abbreviations like "e.g.", version numbers like "v2.0", or "Dr."
    parts = re.split(r"\.\s", full_desc, maxsplit=1)
    first_sentence = parts[0].strip().rstrip(".")

    # Validate name format: lowercase letters, digits, hyphens
    if not re.match(r"^[a-z][a-z0-9-]*$", name):
        logger.warning("[router] Invalid expert name format: '%s' — skipping", name)
        return None

    return name, first_sentence


class SemanticRouter:
    """Lightweight passthrough router + expert registry.

    Discovers available experts from disk but does NOT call any LLM.
    Claude Code decides expert selection and model choice via Agent tool.
    """

    EXPERTS_DIR = os.getenv(
        "EXPERTS_DIR",
        os.path.join(os.path.expanduser("~"), ".claude", "agents", "experts"),
    )

    def __init__(self, experts_dir: str | None = None) -> None:
        self._tracer = Tracer("semantic-router")
        self._experts_dir = experts_dir or self.EXPERTS_DIR
        self._experts = self._discover_experts()
        self._validated_experts: set[str] = set(self._experts.keys())

    def _discover_experts(self) -> dict[str, str]:
        """Scan experts directory for .md files and parse frontmatter.

        Returns dict mapping expert name to short description.
        """
        agents_dir = self._experts_dir
        pattern = os.path.join(agents_dir, "*.md")
        experts: dict[str, str] = {}

        resolved_dir = Path(agents_dir).resolve()
        for filepath in sorted(glob.glob(pattern)):
            if not Path(filepath).resolve().is_relative_to(resolved_dir):
                logger.warning("[router] Skipping symlink outside experts dir: %s", filepath)
                continue
            parsed = _parse_frontmatter(filepath)
            if parsed:
                name, short_desc = parsed
                experts[name] = short_desc
                logger.info("[router] Discovered expert: %s — %s", name, short_desc)
            else:
                logger.warning(
                    "[router] Could not parse frontmatter from %s", filepath,
                )

        if not experts:
            logger.warning(
                "[router] No expert files found in %s — routing will use direct mode",
                agents_dir,
            )
        return experts

    def get_experts_description(self) -> str:
        """Format expert list for injection into agent system prompts."""
        if not self._experts:
            return ""
        lines = [f"- {name}: {desc}" for name, desc in sorted(self._experts.items())]
        return "\n".join(lines)

    def route(
        self,
        message: str,
        thread_context: str = "",
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> dict:
        """Preprocess message — no LLM call. Claude Code decides expert/model."""
        start = time.time()

        if trace_id:
            span_id = self._tracer.start_span(
                "router.passthrough",
                trace_id=trace_id,
                parent_id=parent_span_id,
                input={"message": message[:300]},
            )
            self._tracer.end_span(
                span_id,
                trace_id=trace_id,
                output={
                    "decision": "passthrough",
                    "available_experts": list(self._experts.keys()),
                },
            )

        duration_ms = (time.time() - start) * 1000
        logger.info(
            "[router] passthrough (%.0fms) — Claude Code will decide expert/model",
            duration_ms,
        )

        return {
            "agent": None,
            "model": MODEL_MAP["sonnet"],
            "model_name": "sonnet",
            "reason": "passthrough — Claude Code decides",
            "memory": [],
        }
