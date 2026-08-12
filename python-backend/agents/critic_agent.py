import json
import logging
import os
import re
import time
from typing import Any, Dict, List

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
        )

        max_attempts = int(os.getenv("MODEL_MAX_RETRIES", "3"))
        last_error = ""

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

                return self._to_strategy(data)

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

        logger.error("Critic Agent failed after all retries: %s", last_error)

        return self._fallback_strategy_from_errors(errors, stderr)

    def _fallback_strategy_from_errors(
        self,
        errors: List[RuntimeErrorInfo],
        stderr: str,
    ) -> CriticStrategy:
        combined_text = " ".join([err.message for err in errors]) + " " + stderr
        combined_lower = combined_text.lower()

        affected_files = self._guess_affected_files(errors)
        
        if "named export" in combined_lower and "cors" in combined_lower and "express" in combined_lower:
            return CriticStrategy(
                root_cause="The code incorrectly imports cors as a named export from express. cors is a separate package, and middleware/errorHandler.js should not import cors or json at all.",
                affected_files=affected_files or ["middleware/errorHandler.js"],
                fixing_strategy="Remove invalid imports from middleware/errorHandler.js. The error handler should not import json or cors. It should only export the errorHandler function.",
                instructions_for_code_agent="Patch middleware/errorHandler.js only. Remove import { json, cors } from 'express'. Keep only a named export: export const errorHandler = (err, req, res, next) => {...}.",
                confidence=0.9,
            )
            
        if "does not provide an export named" in combined_lower or "export default" in combined_lower:
            return CriticStrategy(
                root_cause="The generated app or module does not export the value expected by the test or importing file.",
                affected_files=affected_files or ["app.js"],
                fixing_strategy="Update the affected file so that its exports match the import usage. If Supertest imports app from app.js, app.js must export default app.",
                instructions_for_code_agent="Patch only the affected file. Ensure app.js exports default app and does not start the server during NODE_ENV=test.",
                confidence=0.65,
            )

        if "cannot find module" in combined_lower or "err_module_not_found" in combined_lower:
            return CriticStrategy(
                root_cause="A local import path or external dependency cannot be resolved.",
                affected_files=affected_files or ["app.js", "package.json"],
                fixing_strategy="Check the missing module. If it is a local file, update the import path to an existing file. If it is an npm package, ensure package.json includes it.",
                instructions_for_code_agent="Use only existing project files for local imports. Do not invent new files. Add missing dependency only if it is an external package.",
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

        if "route" in combined_lower and "callback" in combined_lower:
            return CriticStrategy(
                root_cause="A route is referencing a controller function that is missing or not imported correctly.",
                affected_files=affected_files or ["routes"],
                fixing_strategy="Make route imports match the named exports from the controller file.",
                instructions_for_code_agent="Patch the route file or controller export names so they match exactly.",
                confidence=0.6,
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
