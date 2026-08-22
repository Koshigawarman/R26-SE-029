import re
from typing import Any, Dict, Optional


class PromptBudgetManager:
    """Calculates prompt token usage and context-window fit without trimming."""

    DEFAULT_CONTEXT_LIMIT = 32768
    DEFAULT_RESERVED_OUTPUT_TOKENS = 4096

    @classmethod
    def analyze(
        cls,
        system_prompt: str,
        built_prompt: str,
        raw_output: str = "",
        model: Optional[str] = None,
        context_limit: int = DEFAULT_CONTEXT_LIMIT,
        reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS,
    ) -> Dict[str, Any]:
        system_count = cls.count_tokens(system_prompt, model=model)
        built_count = cls.count_tokens(built_prompt, model=model)
        output_count = cls.count_tokens(raw_output, model=model)

        input_total = system_count["tokens"] + built_count["tokens"]
        request_plus_reserved = input_total + reserved_output_tokens
        request_plus_output = input_total + output_count["tokens"]

        exact = system_count["exact"] and built_count["exact"] and output_count["exact"]
        method = system_count["method"] if exact else "mixed_or_estimated"

        return {
            "context_limit": context_limit,
            "reserved_output_tokens": reserved_output_tokens,
            "available_input_tokens": max(context_limit - reserved_output_tokens, 0),
            "method": method,
            "exact": exact,
            "note": cls._note(exact),
            "system_prompt_tokens": system_count["tokens"],
            "built_prompt_tokens": built_count["tokens"],
            "input_total_tokens": input_total,
            "output_tokens": output_count["tokens"],
            "request_plus_reserved_tokens": request_plus_reserved,
            "request_plus_output_tokens": request_plus_output,
            "fits_context_with_reserved_output": request_plus_reserved <= context_limit,
            "fits_context_with_actual_output": request_plus_output <= context_limit,
            "overflow_tokens_with_reserved_output": max(request_plus_reserved - context_limit, 0),
            "overflow_tokens_with_actual_output": max(request_plus_output - context_limit, 0),
            "details": {
                "system_prompt": system_count,
                "built_prompt": built_count,
                "raw_output": output_count,
            },
        }

    @classmethod
    def count_tokens(cls, text: str, model: Optional[str] = None) -> Dict[str, Any]:
        if not text:
            return {
                "tokens": 0,
                "method": "empty",
                "exact": True,
                "model": model,
            }

        exact_count = cls._count_with_tiktoken(text, model)
        if exact_count is not None:
            return {
                "tokens": exact_count,
                "method": "tiktoken",
                "exact": True,
                "model": model,
            }

        word_like_tokens = len(re.findall(r"\w+|[^\w\s]", text, re.UNICODE))
        char_based_tokens = max(1, len(text) // 4)

        return {
            "tokens": max(word_like_tokens, char_based_tokens),
            "method": "approximate_chars_and_words",
            "exact": False,
            "model": model,
        }

    @staticmethod
    def _count_with_tiktoken(text: str, model: Optional[str]) -> Optional[int]:
        try:
            import tiktoken
        except ImportError:
            return None

        try:
            encoding = tiktoken.encoding_for_model(model or "gpt-4")
        except Exception:
            encoding = tiktoken.get_encoding("cl100k_base")

        return len(encoding.encode(text))

    @staticmethod
    def _note(exact: bool) -> str:
        if exact:
            return "Exact token count from tiktoken for a compatible tokenizer."

        return (
            "Estimated count. Install tiktoken for exact OpenAI-compatible tokenization. "
            "For Ollama models, exact counts require that model's tokenizer."
        )
