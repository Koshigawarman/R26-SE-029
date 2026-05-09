import json
import logging
import re
from typing import List, Dict, Any

import requests

from schema import RuntimeErrorInfo, MemoryMatch, CriticStrategy
from prompts.critic_prompt import (
    CRITIC_SYSTEM_PROMPT,
    build_critic_prompt,
    build_critic_retry_prompt,
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

    def __init__(self, ollama_url: str, model: str, use_openrouter: bool = False, openrouter_api_key: str = ""):
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.use_openrouter = use_openrouter
        self.openrouter_api_key = openrouter_api_key
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"

    def execute(
        self,
        errors: List[RuntimeErrorInfo],
        stderr: str,
        stdout: str,
        memory_matches: List[MemoryMatch],
        file_list: List[str],
        attempt: int
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

        try:
            if self.use_openrouter:
                raw_response = self._query_openrouter(prompt, CRITIC_SYSTEM_PROMPT)
            else:
                raw_response = self._query_ollama(prompt, CRITIC_SYSTEM_PROMPT)
            data = self._extract_json(raw_response)
            return self._to_strategy(data)

        except Exception as first_error:
            logger.warning(f"Critic Agent first response failed: {first_error}")

            try:
                retry_prompt = build_critic_retry_prompt(str(first_error))
                if self.use_openrouter:
                    retry_response = self._query_openrouter(retry_prompt, CRITIC_SYSTEM_PROMPT)
                else:
                    retry_response = self._query_ollama(retry_prompt, CRITIC_SYSTEM_PROMPT)
                data = self._extract_json(retry_response)
                return self._to_strategy(data)

            except Exception as retry_error:
                logger.error(f"Critic Agent failed after retry: {retry_error}")

                return CriticStrategy(
                    root_cause="Critic model failed to produce valid analysis.",
                    affected_files=self._guess_affected_files(errors),
                    fixing_strategy="Use the runtime error log to identify the affected file and apply the smallest possible fix.",
                    instructions_for_code_agent="Patch only the affected file. Do not regenerate the full project.",
                    confidence=0.3,
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
                "Use the error log to apply a minimal fix."
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
            timeout=120,
        )

        response.raise_for_status()
        data = response.json()

        if "response" not in data:
            raise ValueError("Ollama returned no response field")

        return data["response"]

    def _query_openrouter(self, prompt: str, system_prompt: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Koshigawarman/R26-SE-029",
            "X-Title": "AI Backend Builder",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 1024,
        }
        resp = requests.post(self.openrouter_url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        return data['choices'][0]['message']['content']

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
                text = text[start:end + 1]

        return json.loads(text)