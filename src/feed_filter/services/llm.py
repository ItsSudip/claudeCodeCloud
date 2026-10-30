"""LLM clients used by the filtering engine.

Two implementations share one interface:

* :class:`AnthropicLLMClient` - calls the Claude API with structured (JSON-schema) output.
* :class:`MockLLMClient` - deterministic keyword heuristics; no network. Used in tests and
  via ``--llm mock`` so you can exercise the full pipeline without spending tokens.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from feed_filter.services.profile import InterestProfileSpec

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- data types
@dataclass
class ItemForEvaluation:
    """The slice of a raw item that the LLM sees."""

    ref: str  # opaque id echoed back by the model (we use the raw_item id)
    title: str
    source: str
    url: str | None = None
    author: str | None = None
    body: str | None = None
    score: int | None = None
    comments: int | None = None

    def to_prompt_dict(self, body_limit: int = 1200) -> dict[str, Any]:
        data: dict[str, Any] = {"ref": self.ref, "title": self.title, "source": self.source}
        if self.author:
            data["author"] = self.author
        if self.url:
            data["url"] = self.url
        if self.score is not None:
            data["engagement"] = {"score": self.score, "comments": self.comments}
        if self.body:
            data["body"] = self.body[:body_limit]
        return data


@dataclass
class Evaluation:
    ref: str
    relevance_score: int
    theme: str
    summary: str
    takeaways: list[str] = field(default_factory=list)
    is_clickbait: bool = False
    is_outrage: bool = False
    is_repetitive: bool = False
    save_for_later: bool = False
    reasoning: str = ""

    def __post_init__(self) -> None:
        self.relevance_score = int(max(1, min(10, round(self.relevance_score))))
        self.takeaways = [t.strip() for t in self.takeaways if t and t.strip()][:5]


class LLMClient(Protocol):
    model_name: str

    def evaluate_batch(
        self,
        profile: InterestProfileSpec,
        items: list[ItemForEvaluation],
        recent_titles: list[str],
    ) -> list[Evaluation]: ...

    def compose_overview(self, profile: InterestProfileSpec, highlights: list[dict]) -> str: ...


# ---------------------------------------------------------------------------- prompts
SYSTEM_PROMPT = """You are The Feed Filter: a sharp, skeptical research assistant who reads a \
person's \
social feeds so they don't have to. You will receive their interest profile and a batch of items \
(posts, stories, articles). For each item you decide how relevant it is to this specific reader, \
file it under one of their themes, and distil the genuinely useful takeaways.

Scoring guide (1-10, integers):
- 9-10: directly advances something the reader cares about; they would be annoyed to miss it.
- 7-8: clearly relevant and substantive.
- 5-6: tangentially relevant, or relevant but thin on substance.
- 3-4: mostly noise for this reader.
- 1-2: irrelevant, spam, or in a category they asked to avoid.

Flags:
- is_clickbait: the title withholds information, exaggerates, uses "you won't believe", numbered \
listicles with no substance, or the body does not deliver on the title.
- is_outrage: the item exists mainly to provoke anger (rants, dunking, culture-war framing, \
all-caps indignation) rather than to inform.
- is_repetitive: the story is substantially the same as one of the recently covered titles \
provided, or a duplicate of another item in this batch (keep the single best version unflagged).
- save_for_later: long-form or reference material worth a focused read rather than a skim.

Write summaries in plain, specific language: what happened and why it matters to this reader. \
Takeaways are concrete facts, numbers or decisions, not restatements of the title. Do not \
invent details that are not in the item. Use the exact theme names from the profile."""

EVALUATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "relevance_score": {"type": "integer", "minimum": 1, "maximum": 10},
                    "theme": {"type": "string"},
                    "summary": {"type": "string"},
                    "takeaways": {"type": "array", "items": {"type": "string"}},
                    "is_clickbait": {"type": "boolean"},
                    "is_outrage": {"type": "boolean"},
                    "is_repetitive": {"type": "boolean"},
                    "save_for_later": {"type": "boolean"},
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "ref",
                    "relevance_score",
                    "theme",
                    "summary",
                    "takeaways",
                    "is_clickbait",
                    "is_outrage",
                    "is_repetitive",
                    "save_for_later",
                    "reasoning",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["evaluations"],
    "additionalProperties": False,
}


def build_evaluation_prompt(
    items: list[ItemForEvaluation], recent_titles: list[str], theme_names: list[str]
) -> str:
    payload = {
        "allowed_themes": theme_names,
        "recently_covered_titles": recent_titles[:60],
        "items": [item.to_prompt_dict() for item in items],
    }
    return (
        "Evaluate every item below against the interest profile. Return one evaluation per item, "
        "echoing its `ref` exactly.\n\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    )


def parse_evaluations(raw: dict[str, Any], expected_refs: list[str]) -> list[Evaluation]:
    """Validate the model's JSON, dropping unknown refs and filling missing ones conservatively."""
    by_ref: dict[str, Evaluation] = {}
    for entry in raw.get("evaluations", []):
        try:
            evaluation = Evaluation(
                ref=str(entry["ref"]),
                relevance_score=int(entry.get("relevance_score", 1)),
                theme=str(entry.get("theme") or "Saved for Later"),
                summary=str(entry.get("summary") or "").strip(),
                takeaways=[str(t) for t in entry.get("takeaways", [])],
                is_clickbait=bool(entry.get("is_clickbait", False)),
                is_outrage=bool(entry.get("is_outrage", False)),
                is_repetitive=bool(entry.get("is_repetitive", False)),
                save_for_later=bool(entry.get("save_for_later", False)),
                reasoning=str(entry.get("reasoning") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping malformed evaluation %r: %s", entry, exc)
            continue
        if evaluation.ref in expected_refs:
            by_ref[evaluation.ref] = evaluation
    results: list[Evaluation] = []
    for ref in expected_refs:
        if ref in by_ref:
            results.append(by_ref[ref])
        else:
            log.warning("Model returned no evaluation for ref %s; scoring it 1", ref)
            results.append(
                Evaluation(
                    ref=ref,
                    relevance_score=1,
                    theme="Saved for Later",
                    summary="No evaluation returned by the model.",
                    reasoning="missing from model output",
                )
            )
    return results


# ---------------------------------------------------------------------------- Anthropic
class AnthropicLLMClient:
    """Claude-backed evaluator using structured JSON output and server-side refusal fallbacks."""

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        *,
        effort: str = "medium",
        max_tokens: int = 16000,
        max_attempts: int = 3,
        client: Any | None = None,
    ):
        import anthropic  # imported lazily so the mock client works without the SDK configured

        self._anthropic = anthropic
        self.model_name = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_attempts = max_attempts
        self._client = client or anthropic.Anthropic()

    def _system_blocks(self, profile: InterestProfileSpec) -> list[dict[str, Any]]:
        # Stable prefix (system prompt + profile) is cached; only the item batch changes.
        return [
            {
                "type": "text",
                "text": SYSTEM_PROMPT + "\n\n" + profile.to_prompt_text(),
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _create(self, *, system: list[dict[str, Any]], user: str, schema: dict | None) -> str:
        anthropic = self._anthropic
        output_config: dict[str, Any] = {"effort": self.effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}

        delay = 2.0
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._client.beta.messages.create(
                    model=self.model_name,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_config=output_config,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.RateLimitError as exc:
                retry_after = float(exc.response.headers.get("retry-after", delay))
                log.warning(
                    "Rate limited (attempt %d/%d); sleeping %.0fs",
                    attempt,
                    self.max_attempts,
                    retry_after,
                )
                if attempt == self.max_attempts:
                    raise
                time.sleep(retry_after)
                delay *= 2
                continue
            except anthropic.APIStatusError as exc:
                if exc.status_code >= 500 and attempt < self.max_attempts:
                    log.warning("Server error %s (attempt %d); retrying", exc.status_code, attempt)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
            except anthropic.APIConnectionError:
                if attempt < self.max_attempts:
                    log.warning("Connection error (attempt %d); retrying", attempt)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise RuntimeError(
                    "The model declined this request"
                    + (f" (category: {details.category})" if details and details.category else "")
                )
            if response.stop_reason == "max_tokens":
                raise RuntimeError(
                    "Model output hit max_tokens; lower FEED_FILTER_LLM_BATCH_SIZE "
                    "or raise max_tokens"
                )
            text = next((b.text for b in response.content if b.type == "text"), "")
            usage = getattr(response, "usage", None)
            if usage is not None:
                log.debug(
                    "LLM usage: in=%s out=%s cache_read=%s",
                    getattr(usage, "input_tokens", "?"),
                    getattr(usage, "output_tokens", "?"),
                    getattr(usage, "cache_read_input_tokens", "?"),
                )
            return text
        raise RuntimeError("unreachable")  # pragma: no cover

    def evaluate_batch(
        self,
        profile: InterestProfileSpec,
        items: list[ItemForEvaluation],
        recent_titles: list[str],
    ) -> list[Evaluation]:
        if not items:
            return []
        prompt = build_evaluation_prompt(items, recent_titles, profile.theme_names())
        text = self._create(
            system=self._system_blocks(profile), user=prompt, schema=EVALUATION_SCHEMA
        )
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Model returned invalid JSON: {exc}: {text[:200]!r}") from exc
        return parse_evaluations(raw, [item.ref for item in items])

    def compose_overview(self, profile: InterestProfileSpec, highlights: list[dict]) -> str:
        if not highlights:
            return ""
        prompt = (
            "Write a 2-4 sentence editor's note for today's digest for this reader. Mention the "
            "most important thread(s) across the highlights, be concrete, no hype, no bullet "
            "points, no preamble. Highlights:\n"
            + json.dumps(highlights, ensure_ascii=False, indent=1)
        )
        return self._create(system=self._system_blocks(profile), user=prompt, schema=None).strip()


# ---------------------------------------------------------------------------- Mock
_CLICKBAIT_RE = re.compile(
    r"you won'?t believe|will shock you|number \d+ will|top \d+|\bneed\b.*\bthis\b|"
    r"what happened next|"
    r"this one trick|\bshocking\b|\bmind[- ]blowing\b|\bjaw[- ]dropping\b",
    re.IGNORECASE,
)
_OUTRAGE_RE = re.compile(
    r"\bdisgusting\b|\bidiots?\b|\boutrage\b|\bfight me\b|\bhot take\b|\brant\b|\bat it again\b|"
    r"\bfurious\b|\bdestroys?\b|\bowned\b|\bslams?\b|\bwoke\b|\bdisgrace",
    re.IGNORECASE,
)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9+#.-]{1,}", text.lower()) if len(t) > 2}


class MockLLMClient:
    """Keyword-overlap evaluator. Deterministic, offline, and good enough to demo the pipeline."""

    model_name = "mock-heuristic-v1"

    def evaluate_batch(
        self,
        profile: InterestProfileSpec,
        items: list[ItemForEvaluation],
        recent_titles: list[str],
    ) -> list[Evaluation]:
        recent = [re.sub(r"\s+", " ", t.lower()).strip() for t in recent_titles]
        seen_titles: set[str] = set()
        results: list[Evaluation] = []
        for item in items:
            text = f"{item.title} {item.body or ''}"
            words = _tokens(text)
            best_theme, best_hits = "Saved for Later", 0
            for theme in profile.themes:
                hits = 0
                for keyword in theme.keywords:
                    kw = keyword.lower()
                    if kw in text.lower() or _tokens(kw) <= words:
                        hits += 1
                if hits > best_hits:
                    best_theme, best_hits = theme.name, hits
            score = min(10, 2 + best_hits * 2 + (1 if item.body else 0))
            if any(a.lower() in text.lower() for a in profile.avoid):
                score = min(score, 2)
            norm_title = re.sub(r"\s+", " ", item.title.lower()).strip()
            repetitive = norm_title in recent or norm_title in seen_titles
            seen_titles.add(norm_title)
            clickbait = bool(_CLICKBAIT_RE.search(item.title))
            outrage = bool(_OUTRAGE_RE.search(text))
            body = (item.body or "").strip()
            summary = body[:200] if body else item.title
            takeaways = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()][:3]
            results.append(
                Evaluation(
                    ref=item.ref,
                    relevance_score=score,
                    theme=best_theme,
                    summary=summary,
                    takeaways=takeaways,
                    is_clickbait=clickbait,
                    is_outrage=outrage,
                    is_repetitive=repetitive,
                    save_for_later=len(body) > 400 or "thread" in item.title.lower(),
                    reasoning=f"mock: {best_hits} keyword hit(s) for {best_theme}",
                )
            )
        return results

    def compose_overview(self, profile: InterestProfileSpec, highlights: list[dict]) -> str:
        if not highlights:
            return ""
        titles = [h["title"] for h in highlights[:3]]
        return (
            f"Today's digest covers {len(highlights)} items. Leading the pack: "
            + "; ".join(titles)
            + "."
        )


def engagement_signal(score: int | None, comments: int | None) -> float:
    """Normalise raw engagement (likes/upvotes + comments) to 0..1 on a log scale."""
    total = max(0, (score or 0)) + 2 * max(0, (comments or 0))
    if total <= 0:
        return 0.0
    return min(1.0, math.log10(total + 1) / 4.0)  # 10k engagement ~= 1.0


def build_llm_client(provider: str, *, model: str, effort: str) -> LLMClient:
    provider = provider.strip().lower()
    if provider == "mock":
        return MockLLMClient()
    if provider == "anthropic":
        return AnthropicLLMClient(model, effort=effort)
    raise ValueError(f"Unknown LLM provider {provider!r}; expected 'anthropic' or 'mock'")
