import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests

from schema import CriticStrategy, MemoryMatch, RuntimeErrorInfo
from prompts.critic_prompt import (
    CRITIC_SYSTEM_PROMPT,
    build_critic_prompt,
)

logger = logging.getLogger(__name__)


class CriticAgent:
    """
    Diagnostic Critic Agent.

    Responsibility:
    - Receive raw errors from Orchestrator
    - Receive similar past cases from Episodic Memory
    - Use local/cloud model to analyze the error
    - Return fixing strategy only

    It must NOT generate fixed source code.
    """

    def __init__(
        self,
        ollama_url: str,
        model: str,
        use_openai_compatible: bool = False,
        openai_compatible_url: str = "",
        openai_compatible_api_key: str = "",
        openai_compatible_provider: str = "openai-compatible",
    ):
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.use_openai_compatible = use_openai_compatible
        self.openai_compatible_url = openai_compatible_url
        self.openai_compatible_api_key = openai_compatible_api_key
        self.openai_compatible_provider = openai_compatible_provider

    def execute(
        self,
        errors: List[RuntimeErrorInfo],
        stderr: str,
        stdout: str,
        memory_matches: List[MemoryMatch],
        file_list: List[str],
        attempt: int,
        file_contents: Optional[Dict[str, str]] = None,
    ) -> CriticStrategy:
        logger.info("Critic Agent analyzing error logs...")
        logger.info(f"Using critic model: {self.model}")

        prompt = build_critic_prompt(
            errors=errors,
            stderr=stderr,
            stdout=stdout,
            memory_matches=memory_matches,
            file_list=file_list,
            attempt=attempt,
            file_contents=file_contents,
        )

        max_attempts = int(os.getenv("MODEL_MAX_RETRIES", "3"))
        last_error = ""

        strategy: Optional[CriticStrategy] = None

        for model_attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "Critic model call attempt %s/%s",
                    model_attempt,
                    max_attempts,
                )

                if self.use_openai_compatible:
                    raw_response = self._query_openai_compatible(prompt, CRITIC_SYSTEM_PROMPT)
                else:
                    raw_response = self._query_ollama(prompt, CRITIC_SYSTEM_PROMPT)
                data = self._extract_json(raw_response)
                strategy = self._to_strategy(data)
                break

            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Critic Agent model attempt %s/%s failed: %s",
                    model_attempt,
                    max_attempts,
                    last_error,
                )

                if model_attempt < max_attempts:
                    time.sleep(2)

        if strategy is None:
            logger.error("Critic Agent failed after all retries: %s", last_error)
            strategy = self._fallback_strategy_from_errors(errors, stderr, file_list=file_list)

        # ── Agentic Strategy Validation ───────────────────────────────────────────
        strategy = self._validate_affected_files(strategy, file_list)
        strategy = self._ensure_instructions_not_vague(strategy, errors, stderr, file_list)
        strategy = self._enrich_with_file_list(strategy, file_list)
        # ─────────────────────────────────────────────────────────────────

        return strategy

    def _fallback_strategy_from_errors(
        self,
        errors: List[RuntimeErrorInfo],
        stderr: str,
        file_list: Optional[List[str]] = None,
    ) -> CriticStrategy:
        combined_text = " ".join([err.message for err in errors]) + " " + stderr
        combined_lower = combined_text.lower()

        affected_files = self._guess_affected_files(errors)

        # --- Express-validator CJS named import error ---
        if "named export" in combined_lower and "express-validator" in combined_lower:
            return CriticStrategy(
                root_cause="express-validator is a CommonJS module and cannot be imported using named ESM syntax.",
                affected_files=affected_files or ["app.js"],
                fixing_strategy="Replace named ESM imports from express-validator with CJS default import. For simple CRUD APIs, remove express-validator entirely and do inline validation.",
                instructions_for_code_agent="Find any line like `import { validate, body } from 'express-validator'`. Replace with `import pkg from 'express-validator'; const { body, validationResult } = pkg;`. Or if no validation is needed, remove the import and any usage of validate middleware entirely.",
                confidence=1.0,
            )

        if "named export" in combined_lower and "cors" in combined_lower and "express" in combined_lower:
            return CriticStrategy(
                root_cause="The code incorrectly imports cors as a named export from express. cors is a separate package, and middleware/errorHandler.js should not import cors or json at all.",
                affected_files=affected_files or ["middleware/errorHandler.js"],
                fixing_strategy="Remove invalid imports from middleware/errorHandler.js. The error handler should not import json or cors. It should only export the errorHandler function.",
                instructions_for_code_agent="Patch middleware/errorHandler.js only. Remove import { json, cors } from 'express'. Keep only a named export: export const errorHandler = (err, req, res, next) => {...}.",
                confidence=1.0,
            )

        if "cannot find module" in combined_lower or "err_module_not_found" in combined_lower:
            # --- Entity mismatch detection ---
            module_match = re.search(r"cannot find module ['\"](.*?)['\"]", combined_lower)
            if not module_match:
                module_match = re.search(r"err_module_not_found.*?['\"](.*?)['\"]", combined_lower, re.DOTALL)
                
            if module_match and file_list:
                missing_path = module_match.group(1).replace("\\", "/").lstrip("./")
                missing_basename = os.path.basename(missing_path)
                
                if missing_basename:
                    import difflib
                    basenames = {os.path.basename(f): f for f in file_list if f.endswith('.js') or f.endswith('.json')}
                    closest = difflib.get_close_matches(missing_basename, basenames.keys(), n=1, cutoff=0.5)
                    
                    if closest:
                        correct_basename = closest[0]
                        correct_full_path = basenames[correct_basename]
                        
                        # If the name is wrong or the directory is wrong
                        if missing_basename != correct_basename or missing_path not in correct_full_path:
                            return CriticStrategy(
                                root_cause=f"The code incorrectly imports '{missing_path}' but the actual existing file is '{correct_full_path}'.",
                                affected_files=affected_files or ["app.js"],
                                fixing_strategy=f"Replace the invalid import '{missing_path}' with the correct file path '{correct_full_path}'.",
                                instructions_for_code_agent=f"Search for any import statement referencing '{missing_path}' or '{missing_basename}' and update it to strictly use '{correct_full_path}'. Ensure case sensitivity matches exactly.",
                                confidence=0.95,
                            )

            return CriticStrategy(
                root_cause="A local import path or external dependency cannot be resolved.",
                affected_files=affected_files or ["app.js", "package.json"],
                fixing_strategy="Check the missing module. If it is a local file, update the import path to an existing file. If it is an npm package, ensure package.json includes it.",
                instructions_for_code_agent="Use only existing project files for local imports. Do not invent new files. Add missing dependency only if it is an external package.",
                confidence=0.65,
            )

        if "does not provide an export named" in combined_lower or "export default" in combined_lower:
            return CriticStrategy(
                root_cause="The generated app or module does not export the value expected by the test or importing file.",
                affected_files=affected_files or ["app.js"],
                fixing_strategy="Update the affected file so that its exports match the import usage. If Supertest imports app from app.js, app.js must export default app.",
                instructions_for_code_agent="Patch only the affected file. Ensure app.js exports default app and does not start the server during NODE_ENV=test.",
                confidence=0.65,
            )

        if "syntaxerror" in combined_lower or "unexpected token" in combined_lower:
            return CriticStrategy(
                root_cause="The generated JavaScript contains invalid syntax.",
                affected_files=affected_files or ["app.js"],
                fixing_strategy="Patch the syntax error in the affected file while preserving existing behavior.",
                instructions_for_code_agent="Fix only the syntax issue. Do not regenerate the whole project.",
                confidence=0.6,
            )

        # Route.get/post/etc requires a callback — controller function not imported or undefined
        if ("route" in combined_lower and "requires a callback" in combined_lower) or \
           "route_callback_error" in combined_lower:
            return CriticStrategy(
                root_cause="A route handler is referencing a controller function that is undefined, missing, or imported incorrectly. Route.get/post/put/delete requires a valid callback function.",
                affected_files=affected_files or ["routes"],
                fixing_strategy="Verify that all controller functions imported in the route file are actually exported by the controller file. Ensure named import names exactly match the exported function names.",
                instructions_for_code_agent="In the failing route file: check every imported controller function name against the controller file's exports. Fix any mismatched names. Ensure `import { getAll..., create..., update..., delete... } from '../controllers/...'` names match exactly.",
                confidence=1.0,
            )

        # Cannot use import statement outside a module — package.json missing type:module
        if "cannot use import statement" in combined_lower or "cannot use import statement outside a module" in combined_lower:
            return CriticStrategy(
                root_cause='package.json is missing \"type\": \"module\". Node.js requires this field to use ES module import/export syntax.',
                affected_files=["package.json"],
                fixing_strategy='Add \"type\": \"module\" to the top-level fields of package.json to enable ES Module support.',
                instructions_for_code_agent='In package.json, add the field \"type\": \"module\" at the top level. Do not change any other field.',
                confidence=1.0,
            )

        return CriticStrategy(
            root_cause="The Testing Agent reported a runtime or Jest/Supertest failure.",
            affected_files=affected_files or ["app.js"],
            fixing_strategy="Use the test error log to identify the affected file and apply the smallest possible fix.",
            instructions_for_code_agent="Patch only the affected file. Do not regenerate the full project.",
            confidence=0.4,
        )

    def _to_strategy(self, data: Dict[str, Any]) -> CriticStrategy:
        affected_files = data.get("affected_files", [])

        if isinstance(affected_files, str):
            affected_files = [affected_files]

        return CriticStrategy(
            root_cause=data.get("root_cause", "Unknown root cause"),
            affected_files=affected_files,
            fixing_strategy=data.get("fixing_strategy", "No strategy generated"),
            instructions_for_code_agent=data.get(
                "instructions_for_code_agent",
                "Use the error log to apply a minimal fix.",
            ),
            confidence=float(data.get("confidence", 0.0)),
        )

    def _guess_affected_files(self, errors: List[RuntimeErrorInfo]) -> List[str]:
        files = []

        for err in errors:
            if err.file and err.file not in files:
                files.append(err.file)

        return files

    # ─────────────────────────────────────────────────────────────────
    # Agentic Strategy Validation Methods
    # ─────────────────────────────────────────────────────────────────

    def _validate_affected_files(
        self, strategy: CriticStrategy, file_list: List[str]
    ) -> CriticStrategy:
        """
        For each file in affected_files, verify it actually exists in the project.
        If not, use difflib to find the closest real file and substitute it.
        """
        if not file_list:
            return strategy

        import difflib
        corrected = []
        for af in strategy.affected_files:
            if af in file_list:
                corrected.append(af)
            else:
                closest = difflib.get_close_matches(af, file_list, n=1, cutoff=0.5)
                if closest:
                    logger.info(
                        "[critic] Corrected affected_file '%s' → '%s' (actual file)",
                        af, closest[0]
                    )
                    corrected.append(closest[0])
                else:
                    # Keep the original; CodeGen will get the ground truth file_list anyway
                    corrected.append(af)
        strategy.affected_files = corrected
        return strategy

    _VAGUE_INSTRUCTION_PATTERNS = [
        r"verify\s+(if|that)\s+the\s+(file|path)",
        r"ensure\s+that\s+the\s+(correct|file|path)",
        r"scan\s+the\s+directory",
        r"check\s+if\s+the\s+file\s+exists",
        r"make\s+sure\s+the\s+path\s+is\s+correct",
    ]

    def _ensure_instructions_not_vague(
        self,
        strategy: CriticStrategy,
        errors: List[RuntimeErrorInfo],
        stderr: str,
        file_list: List[str],
    ) -> CriticStrategy:
        """
        Detect vague instructions like 'verify that the file exists' and replace the
        strategy with one from the deterministic fallback engine which always provides
        exact file paths.
        """
        instructions = strategy.instructions_for_code_agent or ""
        fixing_str   = strategy.fixing_strategy or ""

        combined = (instructions + " " + fixing_str).lower()
        is_vague = any(
            re.search(p, combined, re.IGNORECASE)
            for p in self._VAGUE_INSTRUCTION_PATTERNS
        )

        if is_vague:
            logger.warning(
                "[critic] Vague instructions detected ('%s'). Replacing with deterministic fallback.",
                instructions[:120]
            )
            fallback = self._fallback_strategy_from_errors(errors, stderr, file_list=file_list)
            # Merge: keep the LLM root_cause if it looks reasonable, override instructions
            strategy.fixing_strategy              = fallback.fixing_strategy
            strategy.instructions_for_code_agent = fallback.instructions_for_code_agent
            strategy.confidence                   = max(strategy.confidence, fallback.confidence)

        return strategy

    def _enrich_with_file_list(
        self, strategy: CriticStrategy, file_list: List[str]
    ) -> CriticStrategy:
        """
        Append the real file_list to instructions so CodeGen always has ground truth,
        regardless of what the Critic said.
        """
        if not file_list:
            return strategy

        appendix = (
            "\n\n[SYSTEM GROUND TRUTH] Actual files in the project:\n"
            + "\n".join(f"  - {f}" for f in sorted(file_list))
            + "\nOnly import from paths in this list."
        )
        strategy.instructions_for_code_agent = (
            (strategy.instructions_for_code_agent or "") + appendix
        )
        return strategy

    def _query_ollama(self, prompt: str, system_prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_ctx": 2048,
                "num_predict": 1024,
            },
        }

        response = requests.post(
            f"{self.ollama_url}/api/generate",
            json=payload,
            timeout=int(os.getenv("MODEL_TIMEOUT", "240")),
        )

        response.raise_for_status()
        data = response.json()

        if "response" not in data:
            raise ValueError("Ollama returned no response field")

        return data["response"]

    def _query_openai_compatible(self, prompt: str, system_prompt: str) -> str:
        if not self.openai_compatible_url:
            raise ValueError("OPENAI_COMPATIBLE_URL is not set")

        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Koshigawarman/R26-SE-029",
            "X-Title": "AI Backend Builder",
        }
        if self.openai_compatible_api_key:
            headers["Authorization"] = f"Bearer {self.openai_compatible_api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1024,
        }

        response = requests.post(
            self.openai_compatible_url,
            headers=headers,
            json=payload,
            timeout=int(os.getenv("MODEL_TIMEOUT", "240")),
        )
        response.raise_for_status()

        data = response.json()
        return data["choices"][0]["message"]["content"]

    def _extract_json(self, raw_response: str) -> Dict[str, Any]:
        if not raw_response:
            raise ValueError("Empty model response")

        text = raw_response.strip()

        json_match = re.search(
            r"```(?:json)?\s*(.*?)\s*```",
            text,
            re.DOTALL | re.IGNORECASE,
        )

        if json_match:
            text = json_match.group(1).strip()
        else:
            start = text.find("{")
            end = text.rfind("}")

            if start != -1 and end != -1 and end > start:
                text = text[start : end + 1]

        return json.loads(text)
