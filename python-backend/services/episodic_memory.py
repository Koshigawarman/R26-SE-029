import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from schema import MemoryCase, MemoryMatch, RuntimeErrorInfo, CriticStrategy

logger = logging.getLogger(__name__)


class EpisodicMemory:
    """
    JSON-based Episodic Memory for PP1.

    Responsibility:
    - Store successful error-to-fix strategy cases.
    - Retrieve similar past cases for the current runtime error.
    - Provide memory context to the Critic Agent.

    Final version:
    - Replace keyword matching with ChromaDB / FAISS vector similarity search.
    """

    def __init__(self, memory_path: str = "memory/episodic_memory.json"):
        self.memory_path = Path(memory_path)
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.memory_path.exists():
            self.memory_path.write_text("[]", encoding="utf-8")

    def load_cases(self) -> List[MemoryCase]:
        try:
            raw = self.memory_path.read_text(encoding="utf-8").strip()
            if not raw:
                return []

            data = json.loads(raw)
            return [MemoryCase(**item) for item in data]

        except Exception as e:
            logger.error(f"Failed to load episodic memory: {e}")
            return []

    def save_cases(self, cases: List[MemoryCase]) -> None:
        try:
            data = [case.model_dump() for case in cases]
            self.memory_path.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"Failed to save episodic memory: {e}")

    def retrieve_similar(
        self,
        errors: List[RuntimeErrorInfo],
        stderr: str = "",
        top_k: int = 3
    ) -> List[MemoryMatch]:
        """
        Retrieves similar memory cases using simple keyword matching.

        """

        cases = self.load_cases()

        if not cases:
            return []

        query_text = self._build_query_text(errors, stderr)
        query_tokens = self._tokenize(query_text)

        matches: List[MemoryMatch] = []

        for case in cases:
            case_text = " ".join([
                case.error_pattern,
                case.root_cause,
                case.fix_strategy,
                " ".join(case.affected_files)
            ])

            case_tokens = self._tokenize(case_text)
            score = self._similarity_score(query_tokens, case_tokens, query_text, case)

            if score > 0:
                matches.append(
                    MemoryMatch(
                        case=case,
                        score=round(score, 3),
                        matched_pattern=case.error_pattern
                    )
                )

        matches.sort(key=lambda item: item.score, reverse=True)

        selected = matches[:top_k]

        if selected:
            logger.info(f"Episodic memory retrieved {len(selected)} similar case(s)")
        else:
            logger.info("No similar episodic memory case found")

        return selected

    def store_success_case(
        self,
        errors: List[RuntimeErrorInfo],
        critic_strategy: CriticStrategy,
        fixed_files: Optional[List[str]] = None
    ) -> None:
        """
        Stores an error-to-success case after the next debug attempt succeeds.

        Important:
        Only call this after the system verifies that the fix actually worked.
        """

        fixed_files = fixed_files or []
        cases = self.load_cases()

        error_pattern = self._extract_error_pattern(errors)

        new_case = MemoryCase(
            error_pattern=error_pattern,
            root_cause=critic_strategy.root_cause,
            fix_strategy=critic_strategy.fixing_strategy,
            affected_files=fixed_files or critic_strategy.affected_files,
            success=True,
            usage_count=1,
            created_at=datetime.utcnow().isoformat()
        )

        # Avoid exact duplicate cases
        for case in cases:
            if (
                case.error_pattern.lower() == new_case.error_pattern.lower()
                and case.fix_strategy.lower() == new_case.fix_strategy.lower()
            ):
                case.usage_count += 1
                self.save_cases(cases)
                logger.info("Existing episodic memory case usage count updated")
                return

        cases.append(new_case)
        self.save_cases(cases)

        logger.info(f"Stored new episodic memory case: {error_pattern}")

    def seed_from_dataset(self, dataset_path: str = "datasets/error_fix_cases.json") -> int:
        """
        Loads initial error-fix cases from datasets/error_fix_cases.json.

        Use this for PP1 to show an initial curated memory dataset.
        """

        path = Path(dataset_path)

        if not path.exists():
            logger.warning(f"Dataset file not found: {dataset_path}")
            return 0

        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)

            cases = self.load_cases()
            added_count = 0

            for item in data:
                new_case = MemoryCase(
                    error_pattern=item.get("error_pattern", "Unknown error"),
                    root_cause=item.get("root_cause", "Unknown root cause"),
                    fix_strategy=item.get("fix_strategy", "Apply minimal fix"),
                    affected_files=item.get("affected_files", []),
                    success=True,
                    usage_count=0,
                    created_at=datetime.utcnow().isoformat()
                )

                duplicate = any(
                    case.error_pattern.lower() == new_case.error_pattern.lower()
                    for case in cases
                )

                if not duplicate:
                    cases.append(new_case)
                    added_count += 1

            self.save_cases(cases)
            logger.info(f"Seeded {added_count} memory case(s) from dataset")
            return added_count

        except Exception as e:
            logger.error(f"Failed to seed episodic memory: {e}")
            return 0

    def _build_query_text(self, errors: List[RuntimeErrorInfo], stderr: str) -> str:
        parts = []

        for err in errors:
            parts.append(err.message)
            parts.append(err.type)
            if err.file:
                parts.append(err.file)
            parts.append(err.stack[:1000])

        if stderr:
            parts.append(stderr[:1000])

        return " ".join(parts)

    def _tokenize(self, text: str) -> set:
        text = text.lower()
        tokens = re.findall(r"[a-zA-Z0-9_./-]+", text)

        stop_words = {
            "the", "a", "an", "and", "or", "to", "of", "in", "on",
            "is", "are", "was", "were", "this", "that", "with",
            "for", "from", "by", "at", "as"
        }

        return {token for token in tokens if token not in stop_words and len(token) > 2}

    def _similarity_score(
        self,
        query_tokens: set,
        case_tokens: set,
        query_text: str,
        case: MemoryCase
    ) -> float:
        if not query_tokens or not case_tokens:
            return 0.0

        overlap = query_tokens.intersection(case_tokens)
        base_score = len(overlap) / max(len(query_tokens), 1)

        # Strong boost if error pattern appears directly in query
        pattern = case.error_pattern.lower()
        if pattern and pattern in query_text.lower():
            base_score += 0.8

        # Smaller boost for common Node.js/runtime terms
        important_terms = [
            "cannot find module",
            "module_not_found",
            "syntaxerror",
            "referenceerror",
            "typeerror",
            "eaddrinuse",
            "express",
            "router",
            "middleware",
            "app.listen"
        ]

        query_lower = query_text.lower()
        case_lower = " ".join([
            case.error_pattern,
            case.root_cause,
            case.fix_strategy
        ]).lower()

        for term in important_terms:
            if term in query_lower and term in case_lower:
                base_score += 0.3

        return min(base_score, 1.0)

    def _extract_error_pattern(self, errors: List[RuntimeErrorInfo]) -> str:
        if not errors:
            return "Unknown error"

        first = errors[0]
        message = first.message or ""

        common_patterns = [
            "Cannot find module",
            "MODULE_NOT_FOUND",
            "SyntaxError",
            "ReferenceError",
            "TypeError",
            "EADDRINUSE",
            "Cannot GET",
            "Router.use() requires a middleware function",
            "Cannot read properties of undefined"
        ]

        for pattern in common_patterns:
            if pattern.lower() in message.lower() or pattern.lower() in first.stack.lower():
                return pattern

        return message[:120] if message else first.type