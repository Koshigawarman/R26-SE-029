"""
AI Backend Builder — Interactive Orchestrator Agent

Merged version:
- Keeps the updated orchestration logic from the newer OrchestratorAgent
- Adds BuildSession-based interactive execution
- Adds queue-based event streaming for FastAPI/SSE
- Adds human-in-the-loop approval gates
- Keeps ProjectConsistencyValidator
- Keeps retry-budget debug loop, Critic Agent, Episodic Memory, and debug-report generation
"""

import json
import logging
import os
import queue
import threading
import time
import uuid
from typing import Any, Callable, Dict, Generator, List, Optional

from schema import (
    BuildRequest,
    BuildResponse,
    CodeGenContext,
    CriticStrategy,
    RuntimeErrorInfo,
    OrchestrationAttempt,
    TestResults,
)
from agents.planner_agent import PlannerAgent
from agents.codegen_agent import CodeGenAgent
from agents.debug_agent import DebugAgent
from agents.critic_agent import CriticAgent
from services.episodic_memory import EpisodicMemory
from services.planner_contract_validator import PlannerContractValidator
from services.project_consistency_validator import ProjectConsistencyValidator
from services.research_artifact_recorder import ResearchArtifactRecorder

from agents.orchestrator_graph import build_orchestrator_graph, OrchestrationState
logger = logging.getLogger(__name__)

DEFAULT_OPERATION_RETRIES = int(os.getenv("ORCHESTRATOR_OPERATION_RETRIES", "3"))
DEFAULT_RETRY_DELAY_SECONDS = float(os.getenv("ORCHESTRATOR_RETRY_DELAY_SECONDS", "2"))


class AbortBuildException(Exception):
    """Raised when a build is manually aborted by the user during an interactive fallback."""
    pass

# ─────────────────────────────────────────────────────────────────────────────
# Build Session
# ─────────────────────────────────────────────────────────────────────────────


class BuildSession:
    """Thread-safe session for a single build run."""

    def __init__(self):
        self.id: str = str(uuid.uuid4())
        self.event_queue: queue.Queue = queue.Queue()
        self.approval_event: threading.Event = threading.Event()
        self.approval_action: Optional[str] = None
        self.approval_data: Optional[dict] = None
        self.active: bool = True

    def emit(self, event_type: str, data: dict) -> None:
        self.event_queue.put(
            {
                "type": event_type,
                "data": data,
            }
        )

    def wait_for_approval(
        self,
        step: str,
        details: dict,
        timeout: int = 600,
    ) -> str:
        self.approval_event.clear()
        self.approval_action = None
        self.approval_data = None

        self.emit(
            "approval_needed",
            {
                "step": step,
                **details,
            },
        )

        logger.info("⏸️ Waiting for approval at: %s", step)
        self.approval_event.wait(timeout=timeout)

        action = self.approval_action or "cancel"
        logger.info("▶️ User response for %s: %s", step, action)

        return action


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator Agent
# ─────────────────────────────────────────────────────────────────────────────


class OrchestratorAgent:
    def __init__(
        self,
        ollama_url: str,
        models: dict,
        max_retries: int = 3,
        use_openai_compatible: bool = False,
        openai_compatible_url: str = "",
        openai_compatible_api_key: str = "",
        openai_compatible_provider: str = "openai-compatible",
    ):
        self.ollama_url = ollama_url
        self.project_validator = ProjectConsistencyValidator()
        self.planner_contract_validator = PlannerContractValidator()

        self.planner_agent = PlannerAgent(
            ollama_url,
            models.get("planner"),
            use_openai_compatible=use_openai_compatible,
            openai_compatible_url=openai_compatible_url,
            openai_compatible_api_key=openai_compatible_api_key,
            openai_compatible_provider=openai_compatible_provider,
        )

        self.codegen_agent = CodeGenAgent(
            ollama_url,
            models.get("codegen"),
            use_openai_compatible=use_openai_compatible,
            openai_compatible_url=openai_compatible_url,
            openai_compatible_api_key=openai_compatible_api_key,
            openai_compatible_provider=openai_compatible_provider,
        )

        self.debug_agent = DebugAgent(
            ollama_url=ollama_url,
            model=models.get("debug") or os.getenv("DEBUG_MODEL", "qwen2.5-coder:1.5b"),
            debug_timeout=int(os.getenv("DEBUG_TIMEOUT", "10000")),
            use_openai_compatible=use_openai_compatible,
            openai_compatible_url=openai_compatible_url,
            openai_compatible_api_key=openai_compatible_api_key,
            openai_compatible_provider=openai_compatible_provider,
        )

        self.critic_agent = CriticAgent(
            ollama_url,
            models.get("critic"),
            use_openai_compatible=use_openai_compatible,
            openai_compatible_url=openai_compatible_url,
            openai_compatible_api_key=openai_compatible_api_key,
            openai_compatible_provider=openai_compatible_provider,
        )

        self.max_retries = max_retries

        self.episodic_memory = EpisodicMemory(
            memory_dir=os.getenv("EPISODIC_MEMORY_DIR", "memory/chroma_db")
        )

        # For PP1: seed initial curated cases if available.
        self._retry_operation(
            operation_name="Seed episodic memory dataset",
            operation=lambda: self.episodic_memory.seed_from_dataset(
                os.getenv("ERROR_FIX_DATASET_PATH", "datasets/error_fix_cases.json")
            ),
            max_attempts=2,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Legacy stream support
    # ─────────────────────────────────────────────────────────────────────

    def execute_stream(self, request: BuildRequest) -> Generator[str, None, None]:
        """
        Backward-compatible SSE generator.

        New FastAPI code can create a BuildSession and call execute_interactive()
        directly in a background thread. This method exists for older code that
        still expects execute_stream().
        """

        session = BuildSession()

        thread = threading.Thread(
            target=self.execute_interactive,
            args=(request, session),
            daemon=True,
        )
        thread.start()

        while session.active or not session.event_queue.empty():
            try:
                event = session.event_queue.get(timeout=1)
                yield self._format_sse(event)
            except queue.Empty:
                continue

    # ─────────────────────────────────────────────────────────────────────
    # Interactive entry point
    # ─────────────────────────────────────────────────────────────────────

    def execute_interactive(self, request: BuildRequest, session: BuildSession) -> None:
        self._run(request, session)

    # ─────────────────────────────────────────────────────────────────────
    # Core pipeline
    # ─────────────────────────────────────────────────────────────────────

    def _run(self, request: BuildRequest, session: BuildSession) -> None:
        start_time = time.time()
        project_root = request.workspace_uri
        
        initial_state = OrchestrationState(
            request=request,
            session=session,
            agent=self,
            start_time=start_time,
            project_root=project_root,
            project_path=project_root,
            session_project_path=None,
            plan=None,
            artifact_recorder=None,
            plan_rejection_count=0,
            plan_action=None,
            existing_contents={},
            context=None,
            files_generated=0,
            debug_attempt_count=0,
            latest_errors=[],
            latest_stderr="",
            latest_stdout="",
            attempts=[],
            memory_matches=[],
            critic_strategy=None,
            previous_failed_errors=None,
            previous_critic_strategy=None,
            previous_fixed_files=[],
            final_test_results=None,
            error_message=None,
            final_outcome=None,
            pre_debug_action=None,
            fix_action=None,
            exhausted_action=None
        )
        
        graph = build_orchestrator_graph()
        
        try:
            logger.info("=" * 70)
            logger.info("🚀 ORCHESTRATION STARTED (LANGGRAPH MODE)")
            logger.info("Workspace: %s", project_root)
            logger.info("Max retries: %s", self.max_retries)
            logger.info("=" * 70)
            
            # Execute the LangGraph State Machine
            final_state = graph.invoke(initial_state, config={"recursion_limit": 100})
            
        except AbortBuildException:
            logger.warning("Build aborted by user.")
            self._emit_complete(session, False, "Project", project_root, 0, 0, ["Aborted by user"], start_time)
        except Exception as e:
            logger.error(f"Unexpected error in LangGraph orchestration: {e}")
            self._emit_complete(session, False, "Project", project_root, 0, 0, [str(e)], start_time)

    def _create_fresh_project_path(self, project_root: str, project_name: str) -> str:
        """
        Always return a folder path that is safe to write a NEW project into.

        Rules:
        - If project_root/project_name does NOT exist → use it directly.
        - If it exists but is EMPTY (only blank subdirectories, no code files) → reuse it.
        - If it exists AND contains real files → auto-increment suffix:
            project-name-2, project-name-3, … until a free (or empty) slot is found.

        This ensures:
        ✓ Every new build never overwrites an existing completed project.
        ✓ The user doesn't end up with a stale empty folder ghost sitting next to a real build.
        """
        import re as _re

        def _has_real_files(path: str) -> bool:
            """Return True if path contains at least one non-directory file."""
            for root, dirs, files in os.walk(path):
                # Skip node_modules
                dirs[:] = [d for d in dirs if d != "node_modules"]
                if files:
                    return True
            return False

        base_path = os.path.join(project_root, project_name)

        # Case 1: doesn't exist yet → fresh, use it
        if not os.path.isdir(base_path):
            logger.info("[orchestrator] Fresh project folder: %s", base_path)
            return base_path

        # Case 2: exists but empty → reuse (avoids ghost folders)
        if not _has_real_files(base_path):
            logger.info("[orchestrator] Reusing empty project folder: %s", base_path)
            return base_path

        # Case 3: exists with code → find next free numbered slot
        counter = 2
        while True:
            candidate = os.path.join(project_root, f"{project_name}-{counter}")
            if not os.path.isdir(candidate):
                logger.info(
                    "[orchestrator] Existing folder has code. Creating numbered slot: %s", candidate
                )
                return candidate
            if not _has_real_files(candidate):
                logger.info(
                    "[orchestrator] Reusing empty numbered slot: %s", candidate
                )
                return candidate
            counter += 1

    # ─────────────────────────────────────────────────────────────────────
    # Agentic Post-Fix Verification Helpers
    # ─────────────────────────────────────────────────────────────────────

    def _verify_fix_applied(
        self,
        original_errors: list,
        fixed_code: str,
    ) -> bool:
        """
        Programmatically verify that the fix actually removed the error-causing
        pattern from the code. Does NOT call any LLM.

        Returns True if the fix is likely effective, False if suspicious.
        """
        if not original_errors or not fixed_code:
            return True  # Cannot verify; optimistically proceed

        for err in original_errors:
            msg = getattr(err, "message", "") or ""

            # For MODULE_NOT_FOUND: check the bad import path is gone
            import re as _re
            mod_match = _re.search(r"Cannot find module ['\"]([^'\"]+)['\"]", msg, _re.IGNORECASE)
            if mod_match:
                bad_module = mod_match.group(1)
                # Extract just the filename part for a loose match
                bad_name = os.path.basename(bad_module).replace(".js", "")
                if bad_name and bad_name.lower() in fixed_code.lower():
                    logger.warning(
                        "[orchestrator] Bad module '%s' still appears in fixed code", bad_name
                    )
                    return False

            # For SyntaxError: check the bad line reference is gone (heuristic)
            # We can't run JS, but a significantly changed file is a good sign

        return True

    def _cross_check_project_imports(
        self, existing_contents: dict
    ) -> list:
        """
        Scan every JS file in the project. For each `import ... from '...'`,
        resolve the path relative to the file and verify the target exists in
        existing_contents. Return a list of human-readable warning strings for
        any missing targets.

        This is pure Python — no LLM involved.
        """
        import re as _re

        _import_re = _re.compile(r"""import\s+[^'"]+from\s+['"](\.[^'"]+)['"]""")
        warnings = []

        for file_path, code in existing_contents.items():
            if not file_path.endswith(".js"):
                continue

            file_dir = os.path.dirname(file_path)

            for m in _import_re.finditer(code):
                raw_import = m.group(1)

                # Resolve to project-root-relative path
                resolved = os.path.normpath(
                    os.path.join(file_dir, raw_import)
                ).replace("\\", "/").lstrip("./")

                # Ensure .js extension for lookup
                if not resolved.endswith(".js") and not resolved.endswith(".json"):
                    resolved_js = resolved + ".js"
                else:
                    resolved_js = resolved

                if resolved_js not in existing_contents and resolved not in existing_contents:
                    warnings.append(
                        f"{file_path}: imports '{raw_import}' → resolves to '{resolved_js}' "
                        f"which does not exist in the project"
                    )

        if warnings:
            logger.warning(
                "[orchestrator] Cross-check found %d unresolved import(s): %s",
                len(warnings), warnings[:3]
            )

        return warnings

    # ─────────────────────────────────────────────────────────────────────
    # Retry helper
    # ─────────────────────────────────────────────────────────────────────

    def _retry_operation(
        self,
        operation_name: str,
        operation: Callable[[], Any],
        max_attempts: int = DEFAULT_OPERATION_RETRIES,
        delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
        session: Optional[BuildSession] = None,
        progress: Optional[int] = None,
        state: str = "OPERATION_RETRY",
        allow_user_fallback: bool = True,
    ) -> Any:
        """
        Retry wrapper for unstable orchestration operations.

        Use this for model/API calls and other operations that can fail because of
        temporary connection issues, timeout errors, incomplete model responses, or
        file-system race conditions. The orchestrator still owns the workflow-level
        retry budget; this helper protects each individual operation.
        """

        last_error: Optional[Exception] = None

        while True:
            for attempt in range(1, max_attempts + 1):
                if session and not session.active:
                    raise AbortBuildException("Session cancelled by user")
                    
                try:
                    if attempt > 1:
                        logger.warning(
                            "Retrying %s (%s/%s)",
                            operation_name,
                            attempt,
                            max_attempts,
                        )

                        if session and progress is not None:
                            session.emit(
                                "status",
                                {
                                    "message": (
                                        f"🔁 Retrying {operation_name} "
                                        f"({attempt}/{max_attempts})"
                                    ),
                                    "progress": progress,
                                    "state": state,
                                }
                            )

                        time.sleep(delay_seconds * (2 ** (attempt - 2)))

                    result = operation()

                    if session and not session.active:
                        raise AbortBuildException("Session cancelled by user")

                    if attempt > 1:
                        logger.info("%s succeeded on retry %s", operation_name, attempt)

                    return result

                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "%s failed on attempt %s/%s: %s",
                        operation_name,
                        attempt,
                        max_attempts,
                        exc,
                    )

            logger.error(
                "%s failed after %s automatic attempt(s)",
                operation_name,
                max_attempts,
            )

            if session and allow_user_fallback:
                action = session.wait_for_approval(
                    "agent_error",
                    {
                        "agent_name": operation_name,
                        "error": str(last_error),
                    }
                )
                
                if action == "retry":
                    logger.info("User requested manual retry for %s", operation_name)
                    continue
                elif action == "cancel":
                    logger.warning("User cancelled orchestration during %s failure", operation_name)
                    raise AbortBuildException(f"Aborted by user during {operation_name} failure.")
                else:
                    raise RuntimeError(f"{operation_name} failed after user chose {action}: {last_error}")
            
            # If no session or fallback not allowed
            raise RuntimeError(f"{operation_name} failed: {last_error}")

    # ─────────────────────────────────────────────────────────────────────
    # Helper methods
    # ─────────────────────────────────────────────────────────────────────

    def _emit_complete(
        self,
        session: BuildSession,
        success: bool,
        name: str,
        root: str,
        files: int,
        attempts: int,
        errors: List[str],
        start: float,
        test_results: Optional[TestResults] = None,
        architecture: Optional[Dict[str, Any]] = None,
    ) -> None:
        duration = time.time() - start

        response = BuildResponse(
            success=success,
            projectName=name,
            projectRoot=root,
            filesGenerated=files,
            debugAttempts=attempts,
            errors=errors,
            duration=duration,
            testResults=test_results,
        )

        payload = response.model_dump()
        if architecture:
            payload["architecture"] = architecture

        session.emit("complete", payload)
        session.active = False

        logger.info("=" * 70)
        logger.info("🏁 ORCHESTRATION COMPLETE")
        logger.info("Success: %s", success)
        logger.info("Project: %s", name)
        if architecture:
            logger.info("Architecture: %s", architecture.get("pattern", "mvc"))
        logger.info("Files generated: %s", files)
        logger.info("Debug attempts: %s", attempts)
        logger.info("Duration: %.2fs", duration)
        logger.info("=" * 70)

    @staticmethod
    def _format_sse(event: dict) -> str:
        return f"data: {json.dumps(event)}\n\n"

    def _file_priority(self, path: str) -> int:
        if path == "package.json":
            return 0
        if path == ".env":
            return 1
        if path.startswith("config/"):
            return 2
        if path.startswith("models/"):
            return 3
        if path.startswith("middleware/"):
            return 4
        if path.startswith("services/"):
            return 5
        if path.startswith("controllers/"):
            return 6
        if path.startswith("routes/"):
            return 7
        if path == "app.js":
            return 8
        return 9

    def _write_project_file(self, project_path: str, relative_path: str, content: str) -> None:
        file_path = os.path.join(project_path, relative_path)

        if os.path.dirname(file_path):
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)

    def _read_project_file(self, project_path: str, relative_path: str) -> str:
        file_path = os.path.join(project_path, relative_path)

        if not os.path.exists(file_path):
            return ""

        try:
            with open(file_path, "r", encoding="utf-8") as file:
                return file.read()
        except Exception as exc:
            logger.warning("Failed to read %s: %s", relative_path, exc)
            return ""

    def _list_project_files(self, project_path: str) -> List[str]:
        files: List[str] = []

        for root, dirs, filenames in os.walk(project_path):
            dirs[:] = [directory for directory in dirs if directory not in ["node_modules", ".git"]]

            for filename in filenames:
                if filename.endswith((".js", ".json", ".env")):
                    full_path = os.path.join(root, filename)
                    relative_path = os.path.relpath(full_path, project_path).replace("\\", "/")
                    files.append(relative_path)

        return sorted(files)

    def _build_error_log(self, errors: List[RuntimeErrorInfo], stderr: str) -> str:
        parts: List[str] = []

        if stderr:
            parts.append(stderr[:3000])

        for err in errors:
            parts.append(f"Type: {err.type}")
            parts.append(f"Message: {err.message}")

            if err.file:
                parts.append(f"File: {err.file}")

            if err.line:
                parts.append(f"Line: {err.line}")

            parts.append("Stack:")
            parts.append(err.stack[:1500])

        return "\n".join(parts)

    def _choose_affected_files(
        self,
        critic_strategy: CriticStrategy,
        errors: List[RuntimeErrorInfo],
        existing_contents: Dict[str, str],
        project_path: str,
    ) -> List[str]:
        """
        Select files for CodeGenAgent to patch.

        Priority:
        1. Files detected directly from Debug Agent error stack
        2. Critic Agent affected files
        3. app.js fallback

        Reason:
        The raw stack trace is usually more reliable than the Critic Agent.
        If Critic strategy is wrong, this prevents fixing the wrong file.
        """

        candidates: List[str] = []

        for err in errors:
            if err.file:
                normalized = self._normalize_file_path(err.file, existing_contents, project_path)

                if normalized and normalized not in candidates:
                    candidates.append(normalized)

        for file in critic_strategy.affected_files:
            normalized = self._normalize_file_path(file, existing_contents, project_path)

            if normalized and normalized not in candidates:
                candidates.append(normalized)

        if not candidates:
            fallback = self._normalize_file_path("app.js", existing_contents, project_path)

            if fallback:
                candidates.append(fallback)

        return candidates[:2]

    def _normalize_file_path(
        self,
        file_path: str,
        existing_contents: Dict[str, str],
        project_path: str,
    ) -> Optional[str]:
        """Convert model/stack returned file paths into actual relative project paths."""

        if not file_path:
            return None

        cleaned = file_path.replace("\\", "/").strip()
        cleaned = cleaned.lstrip("./")

        if cleaned in existing_contents:
            return cleaned

        if os.path.exists(os.path.join(project_path, cleaned)):
            return cleaned

        basename = os.path.basename(cleaned)

        for known_path in existing_contents.keys():
            if os.path.basename(known_path) == basename:
                return known_path

        for root, dirs, files in os.walk(project_path):
            dirs[:] = [directory for directory in dirs if directory not in ["node_modules", ".git"]]

            for filename in files:
                if filename == basename:
                    full_path = os.path.join(root, filename)
                    return os.path.relpath(full_path, project_path).replace("\\", "/")

        if cleaned.endswith(('.js', '.json', '.env', '.ts', '.py')):
            return cleaned

        return None

    def _generate_debug_report(
        self,
        project_path: str,
        user_prompt: str,
        max_retries: int,
        attempts: List[OrchestrationAttempt],
        final_errors: List[str],
    ) -> str:
        """
        Generate debug-report.md when retry budget is exhausted.

        This gives PP1 evidence for:
        - retry budget control
        - safe exit handling
        - traceability
        - human escalation
        """

        report_lines: List[str] = []

        report_lines.append("# Debug Report")
        report_lines.append("")
        report_lines.append("## Final Status")
        report_lines.append("FAILED_SAFE_EXIT")
        report_lines.append("")
        report_lines.append("The system stopped because the retry budget was exhausted.")
        report_lines.append("")
        report_lines.append("## User Prompt")
        report_lines.append("```")
        report_lines.append(user_prompt)
        report_lines.append("```")
        report_lines.append("")
        report_lines.append("## Retry Budget")
        report_lines.append(f"Maximum retries: {max_retries}")
        report_lines.append("")
        report_lines.append("## Final Errors")

        if final_errors:
            for error in final_errors:
                report_lines.append(f"- {error}")
        else:
            report_lines.append("- No final error message available.")

        report_lines.append("")
        report_lines.append("## Attempt History")

        if not attempts:
            report_lines.append("No attempts were recorded.")
        else:
            for attempt in attempts:
                report_lines.append("")
                report_lines.append(f"### Attempt {attempt.attempt}")
                report_lines.append("")
                report_lines.append(f"- State: {attempt.state}")
                report_lines.append(f"- Success: {attempt.success}")
                report_lines.append(f"- Memory matches retrieved: {attempt.memory_matches_count}")

                report_lines.append("")
                report_lines.append("#### Errors")

                if attempt.errors:
                    for error in attempt.errors:
                        report_lines.append(f"- {error}")
                else:
                    report_lines.append("- No errors recorded.")

                report_lines.append("")
                report_lines.append("#### Critic Strategy")

                if attempt.critic_strategy:
                    report_lines.append(f"- Root cause: {attempt.critic_strategy.root_cause}")
                    report_lines.append(f"- Fixing strategy: {attempt.critic_strategy.fixing_strategy}")
                    report_lines.append(
                        f"- Instructions for Code Agent: {attempt.critic_strategy.instructions_for_code_agent}"
                    )
                    report_lines.append(f"- Confidence: {attempt.critic_strategy.confidence}")
                else:
                    report_lines.append("- No critic strategy generated.")

                report_lines.append("")
                report_lines.append("#### Fixed Files")

                if attempt.fixed_files:
                    for file in attempt.fixed_files:
                        report_lines.append(f"- {file}")
                else:
                    report_lines.append("- No files were fixed in this attempt.")

        report_lines.append("")
        report_lines.append("## Human Action Required")
        report_lines.append(
            "Review the final error logs, affected files, and Critic Agent strategies above. "
            "The system stopped safely instead of continuing an infinite self-debugging loop."
        )
        report_lines.append("")
        report_lines.append("## PP1 Prototype Note")
        report_lines.append(
            "This report was generated by the Adaptive Orchestrator as evidence of retry-budget "
            "control, safe exit handling, and traceable self-debugging behavior."
        )
        report_lines.append("")

        report_content = "\n".join(report_lines)
        report_path = os.path.join(project_path, "debug-report.md")

        try:
            with open(report_path, "w", encoding="utf-8") as report_file:
                report_file.write(report_content)

            logger.info("Debug report generated at: %s", report_path)

        except Exception as exc:
            logger.error("Failed to generate debug report: %s", exc)

        return report_path
