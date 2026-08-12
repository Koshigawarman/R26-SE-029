import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional


class ResearchArtifactRecorder:
    """Stores per-project agent prompts, model requests, and outputs."""

    def __init__(self, workspace_root: str, project_name: str):
        safe_name = self._safe_name(project_name)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        self.root = Path(workspace_root) / "_research_artifacts" / f"{safe_name}-{timestamp}"
        self.root.mkdir(parents=True, exist_ok=True)
        self._codegen_counts: Dict[str, int] = {}

    def write_json(self, relative_path: str, data: Dict[str, Any]) -> str:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def write_markdown(self, relative_path: str, title: str, sections: Dict[str, Any]) -> str:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = [f"# {title}", ""]
        for heading, content in sections.items():
            lines.append(f"## {heading}")
            lines.append("")
            if isinstance(content, (dict, list)):
                lines.append("```json")
                lines.append(json.dumps(content, indent=2, ensure_ascii=False))
                lines.append("```")
            else:
                lines.append("```")
                lines.append(str(content or ""))
                lines.append("```")
            lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)

    def record_planner(
        self,
        user_prompt: str,
        trace: Dict[str, Any],
        planner_output: Dict[str, Any],
    ) -> None:
        enriched_trace = self._with_token_counts(trace)
        payload = {
            "agent": "planner",
            "user_prompt": user_prompt,
            "model_request": enriched_trace,
            "planner_output": planner_output,
            "recorded_at": time.time(),
        }
        self.write_json("planner/planner_trace.json", payload)
        self.write_markdown(
            "planner/planner_trace.md",
            "Planner Agent Trace",
            {
                "User Input Prompt": user_prompt,
                "Token Counts": enriched_trace.get("token_counts", {}),
                "System Prompt": enriched_trace.get("system_prompt", ""),
                "Built Prompt": enriched_trace.get("built_prompt", ""),
                "Raw Model Output": enriched_trace.get("raw_output", ""),
                "Parsed Planner Output": planner_output,
            },
        )

    def record_codegen(
        self,
        file_path: str,
        trace: Dict[str, Any],
        generated_content: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        enriched_trace = self._with_token_counts(trace)
        safe_file_name = self._safe_path(file_path)
        self._codegen_counts[safe_file_name] = self._codegen_counts.get(safe_file_name, 0) + 1
        attempt_no = self._codegen_counts[safe_file_name]
        artifact_name = f"{safe_file_name}__attempt-{attempt_no:02d}"
        payload = {
            "agent": "codegen",
            "target_file": file_path,
            "artifact_attempt": attempt_no,
            "status": status,
            "error": error,
            "model_request": enriched_trace,
            "generated_content": generated_content,
            "recorded_at": time.time(),
        }
        self.write_json(f"codegen/{artifact_name}.json", payload)
        self.write_markdown(
            f"codegen/{artifact_name}.md",
            f"CodeGen Agent Trace: {file_path}",
            {
                "Target File": file_path,
                "Token Counts": enriched_trace.get("token_counts", {}),
                "System Prompt": enriched_trace.get("system_prompt", ""),
                "Built Prompt": enriched_trace.get("built_prompt", ""),
                "Raw Model Output": enriched_trace.get("raw_output", ""),
                "Generated Code File": generated_content,
                "Status": {"status": status, "error": error},
            },
        )

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
        return cleaned or "generated-project"

    @classmethod
    def _safe_path(cls, value: str) -> str:
        return cls._safe_name(value.replace("/", "__").replace("\\", "__"))

    def _with_token_counts(self, trace: Dict[str, Any]) -> Dict[str, Any]:
        system_prompt = str(trace.get("system_prompt") or "")
        built_prompt = str(trace.get("built_prompt") or "")
        raw_output = str(trace.get("raw_output") or "")

        system_tokens = self._estimate_tokens(system_prompt)
        built_prompt_tokens = self._estimate_tokens(built_prompt)
        output_tokens = self._estimate_tokens(raw_output)

        enriched = dict(trace)
        enriched["token_counts"] = {
            "method": "approximate_chars_and_words",
            "note": "Use provider tokenizer metadata or a model tokenizer for exact counts.",
            "system_prompt_tokens": system_tokens,
            "built_prompt_tokens": built_prompt_tokens,
            "input_total_tokens": system_tokens + built_prompt_tokens,
            "output_tokens": output_tokens,
            "request_plus_output_tokens": system_tokens + built_prompt_tokens + output_tokens,
        }
        return enriched

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        if not text:
            return 0

        word_like_tokens = len(re.findall(r"\w+|[^\w\s]", text, re.UNICODE))
        char_based_tokens = max(1, len(text) // 4)
        return max(word_like_tokens, char_based_tokens)
