import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from services.prompt_budget_manager import PromptBudgetManager


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
        architecture = self._extract_architecture(planner_output, enriched_trace)
        payload = {
            "agent": "planner",
            "user_prompt": user_prompt,
            "architecture": architecture,
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
                "Architecture": architecture,
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
        architecture = self._extract_architecture({}, enriched_trace)
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
            "architecture": architecture,
            "model_request": enriched_trace,
            "output_validation": enriched_trace.get("output_validation", {}),
            "generated_content": generated_content,
            "recorded_at": time.time(),
        }
        self.write_json(f"codegen/{artifact_name}.json", payload)
        self.write_markdown(
            f"codegen/{artifact_name}.md",
            f"CodeGen Agent Trace: {file_path}",
            {
                "Target File": file_path,
                "Architecture": architecture,
                "Token Counts": enriched_trace.get("token_counts", {}),
                "System Prompt": enriched_trace.get("system_prompt", ""),
                "Built Prompt": enriched_trace.get("built_prompt", ""),
                "Raw Model Output": enriched_trace.get("raw_output", ""),
                "Generated Code File": generated_content,
                "Output Validation": enriched_trace.get("output_validation", {}),
                "Status": {"status": status, "error": error},
            },
        )

    def record_validation(self, name: str, validation_result: Dict[str, Any]) -> None:
        safe_name = self._safe_name(name)
        payload = {
            "name": name,
            "validation_result": validation_result,
            "recorded_at": time.time(),
        }
        self.write_json(f"validation/{safe_name}.json", payload)
        self.write_markdown(
            f"validation/{safe_name}.md",
            f"Validation: {name}",
            {"Validation Result": validation_result},
        )

    def read_validation_operations(self) -> list:
        path = self.root / "validation" / "planner_contract.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            result = data.get("validation_result", {})
            operations = result.get("operations", [])
            return operations if isinstance(operations, list) else []
        except Exception:
            return []

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
        model = trace.get("model")

        budget = PromptBudgetManager.analyze(
            system_prompt=system_prompt,
            built_prompt=built_prompt,
            raw_output=raw_output,
            model=str(model) if model else None,
        )

        enriched = dict(trace)
        enriched["token_counts"] = budget
        return enriched

    @staticmethod
    def _extract_architecture(primary: Dict[str, Any], trace: Dict[str, Any]) -> Dict[str, Any]:
        architecture = primary.get("architecture") if isinstance(primary, dict) else None
        if not architecture:
            architecture = trace.get("architecture") if isinstance(trace, dict) else None
        if not isinstance(architecture, dict):
            architecture = {}

        return {
            "stack": architecture.get("stack", "node-express-mongoose"),
            "pattern": architecture.get("pattern", "mvc"),
            "language": architecture.get("language", "javascript"),
            "moduleSystem": architecture.get("moduleSystem", "esm"),
            "database": architecture.get("database", "mongodb"),
            "orm": architecture.get("orm", "mongoose"),
        }
