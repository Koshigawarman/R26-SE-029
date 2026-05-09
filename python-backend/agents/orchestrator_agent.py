"""
AI Backend Builder — Interactive Orchestrator Agent

Coordinates the multi-agent pipeline with human-in-the-loop approval gates.
Combines:
- Interactive approval system from HEAD
- Critic + Episodic Memory system from origin/main
- Advanced debugging + fixing workflow
- SSE streaming support
"""

import os
import json
import time
import logging
import threading
import queue
import uuid

from typing import Dict, Generator, List, Optional

from schema import (
    BuildRequest,
    BuildResponse,
    CodeGenContext,
    FileSpec,
    CriticStrategy,
    RuntimeErrorInfo,
    OrchestrationAttempt,
)

from agents.planner_agent import PlannerAgent
from agents.codegen_agent import CodeGenAgent
from agents.debug_agent import DebugAgent
from agents.critic_agent import CriticAgent
from services.episodic_memory import EpisodicMemory

logger = logging.getLogger(__name__)


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

    def emit(self, event_type: str, data: dict):
        self.event_queue.put({
            "type": event_type,
            "data": data
        })

    def wait_for_approval(
        self,
        step: str,
        details: dict,
        timeout: int = 600
    ) -> str:

        self.approval_event.clear()
        self.approval_action = None

        self.emit("approval_needed", {
            "step": step,
            **details
        })

        logger.info(f"⏸️ Waiting for approval at: {step}")

        self.approval_event.wait(timeout=timeout)

        action = self.approval_action or "cancel"

        logger.info(f"▶️ User response: {action}")

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
        use_openrouter: bool = False,
        openrouter_api_key: str = ""
    ):

        self.ollama_url = ollama_url

        self.planner_agent = PlannerAgent(
            ollama_url,
            models.get("planner"),
            use_openrouter=use_openrouter,
            openrouter_api_key=openrouter_api_key
        )

        self.codegen_agent = CodeGenAgent(
            ollama_url,
            models.get("codegen"),
            use_openrouter=use_openrouter,
            openrouter_api_key=openrouter_api_key
        )

        self.debug_agent = DebugAgent()

        self.critic_agent = CriticAgent(
            ollama_url,
            models.get("critic"),
            use_openrouter=use_openrouter,
            openrouter_api_key=openrouter_api_key
        )

        self.max_retries = max_retries

        # Episodic Memory
        self.episodic_memory = EpisodicMemory(
            memory_path=os.getenv(
                "EPISODIC_MEMORY_PATH",
                "memory/episodic_memory.json"
            )
        )

        self.episodic_memory.seed_from_dataset(
            os.getenv(
                "ERROR_FIX_DATASET_PATH",
                "datasets/error_fix_cases.json"
            )
        )

    # ─────────────────────────────────────────────────────────────────────
    # Legacy Stream Support
    # ─────────────────────────────────────────────────────────────────────

    def execute_stream(self, request: BuildRequest):
        session = BuildSession()

        thread = threading.Thread(
            target=self.execute_interactive,
            args=(request, session),
            daemon=True
        )

        thread.start()

        while session.active or not session.event_queue.empty():

            try:
                event = session.event_queue.get(timeout=1)

                yield self._format_sse(event)

            except queue.Empty:
                continue

    # ─────────────────────────────────────────────────────────────────────
    # Interactive Entry Point
    # ─────────────────────────────────────────────────────────────────────

    def execute_interactive(
        self,
        request: BuildRequest,
        session: BuildSession
    ):

        self._run(request, session)

    # ─────────────────────────────────────────────────────────────────────
    # Core Pipeline
    # ─────────────────────────────────────────────────────────────────────

    def _run(
        self,
        request: BuildRequest,
        session: BuildSession
    ):

        start_time = time.time()

        project_root = request.workspace_uri
        project_path = project_root

        existing_contents: Dict[str, str] = {}
        attempts: List[OrchestrationAttempt] = []

        latest_errors: List[str] = []
        debug_attempt_count = 0
        files_generated = 0

        previous_failed_errors: Optional[List[RuntimeErrorInfo]] = None
        previous_critic_strategy: Optional[CriticStrategy] = None
        previous_fixed_files: List[str] = []

        try:

            # ═══════════════════════════════════════════════════════════
            # PHASE 1 — PLANNING
            # ═══════════════════════════════════════════════════════════

            session.emit("status", {
                "message": "🧠 Planning project architecture...",
                "progress": 5,
                "state": "PLANNING"
            })

            logger.info("=" * 60)
            logger.info("🚀 PHASE 1 — PLANNING")
            logger.info("=" * 60)

            plan = self.planner_agent.execute(request.prompt)

            project_path = os.path.join(
                project_root,
                plan.projectName
            )

            logger.info(
                f"📋 Plan generated: {plan.projectName}"
            )

            # ─────────────────────────────────────────────────────────
            # Approval Gate — Plan Review
            # ─────────────────────────────────────────────────────────

            action = session.wait_for_approval(
                "plan",
                {
                    "message": f"📋 Plan ready: {plan.projectName}",
                    "projectName": plan.projectName,
                    "entities": [
                        e.model_dump() for e in plan.entities
                    ],
                    "features": [
                        f.model_dump() for f in plan.features
                    ],
                    "files": [
                        f.model_dump() for f in plan.files
                    ],
                    "options": ["approve", "cancel"]
                }
            )

            if action == "cancel":

                session.emit("status", {
                    "message": "❌ Build cancelled during planning.",
                    "progress": 100
                })

                self._emit_complete(
                    session,
                    False,
                    "unknown",
                    project_root,
                    0,
                    0,
                    ["Cancelled by user"],
                    start_time
                )

                return

            # ═══════════════════════════════════════════════════════════
            # PHASE 2 — PROJECT SCAFFOLDING
            # ═══════════════════════════════════════════════════════════

            session.emit("status", {
                "message": "📁 Creating project structure...",
                "progress": 15,
                "state": "CREATING_STRUCTURE"
            })

            os.makedirs(project_path, exist_ok=True)

            for file_spec in plan.files:

                dirname = os.path.dirname(
                    os.path.join(project_path, file_spec.path)
                )

                if dirname:
                    os.makedirs(dirname, exist_ok=True)

            # ═══════════════════════════════════════════════════════════
            # PHASE 3 — FILE GENERATION
            # ═══════════════════════════════════════════════════════════

            session.emit("status", {
                "message": "⚙️ Generating backend files...",
                "progress": 20,
                "state": "GENERATING"
            })

            total_files = len(plan.files)

            sorted_files = sorted(
                plan.files,
                key=lambda f: self._file_priority(f.path)
            )

            context = CodeGenContext(
                projectName=plan.projectName,
                entities=plan.entities,
                features=plan.features,
                allFiles=plan.files,
                existingFileContents=existing_contents,
            )

            for i, file_spec in enumerate(sorted_files):

                if not session.active:
                    return

                progress_pct = 20 + int(
                    (i / total_files) * 45
                )

                session.emit("status", {
                    "message": f"⚙️ Generating ({i+1}/{total_files}) {file_spec.path}",
                    "progress": progress_pct,
                    "state": "GENERATING"
                })

                generated = self.codegen_agent.execute(
                    file_spec,
                    context
                )

                if generated.status == "generated" and generated.content:

                    self._write_project_file(
                        project_path,
                        generated.path,
                        generated.content
                    )

                    existing_contents[generated.path] = generated.content

                    context.existingFileContents = existing_contents

                    files_generated += 1

                    session.emit("file_generated", {
                        "path": generated.path,
                        "status": "success",
                        "chars": len(generated.content),
                        "index": i + 1,
                        "total": total_files,
                    })

                else:

                    error_msg = generated.errorMessage or "Unknown error"

                    logger.error(
                        f"Failed generating {file_spec.path}: {error_msg}"
                    )

                    action = session.wait_for_approval(
                        "codegen_error",
                        {
                            "message": f"❌ Failed generating {file_spec.path}",
                            "file": file_spec.path,
                            "error": error_msg,
                            "options": ["retry", "skip", "cancel"]
                        }
                    )

                    if action == "retry":

                        regenerated = self.codegen_agent.execute(
                            file_spec,
                            context
                        )

                        if regenerated.status == "generated":

                            self._write_project_file(
                                project_path,
                                regenerated.path,
                                regenerated.content
                            )

                            existing_contents[regenerated.path] = regenerated.content

                            session.emit("file_generated", {
                                "path": regenerated.path,
                                "status": "success",
                                "chars": len(regenerated.content),
                                "index": i + 1,
                                "total": total_files,
                            })

                    elif action == "cancel":

                        self._emit_complete(
                            session,
                            False,
                            plan.projectName,
                            project_path,
                            files_generated,
                            0,
                            ["Cancelled during generation"],
                            start_time
                        )

                        return

            # ─────────────────────────────────────────────────────────
            # Approval Gate — Before Debug
            # ─────────────────────────────────────────────────────────

            action = session.wait_for_approval(
                "pre_debug",
                {
                    "message": "✅ File generation complete. Proceed to debug?",
                    "filesGenerated": files_generated,
                    "fileList": list(existing_contents.keys()),
                    "options": ["approve", "cancel"]
                }
            )

            if action == "cancel":

                self._emit_complete(
                    session,
                    False,
                    plan.projectName,
                    project_path,
                    files_generated,
                    0,
                    ["Cancelled before debug"],
                    start_time
                )

                return

            # ═══════════════════════════════════════════════════════════
            # PHASE 4 — DEBUG LOOP
            # ═══════════════════════════════════════════════════════════

            debug_success = False

            for attempt in range(1, self.max_retries + 1):

                debug_attempt_count = attempt

                progress_pct = 70 + int(
                    (attempt / self.max_retries) * 25
                )

                session.emit("status", {
                    "message": f"🔍 Debug attempt {attempt}/{self.max_retries}",
                    "progress": progress_pct,
                    "state": "TESTING"
                })

                debug_result = self.debug_agent.execute(
                    project_path
                )

                # ─────────────────────────────────────────────────────
                # SUCCESS
                # ─────────────────────────────────────────────────────

                if debug_result.success:

                    debug_success = True

                    session.emit("status", {
                        "message": "✅ Backend verified successfully",
                        "progress": 95,
                        "state": "VERIFIED"
                    })

                    # Store successful fix case
                    if previous_failed_errors and previous_critic_strategy:

                        self.episodic_memory.store_success_case(
                            errors=previous_failed_errors,
                            critic_strategy=previous_critic_strategy,
                            fixed_files=previous_fixed_files,
                        )

                    break

                # ─────────────────────────────────────────────────────
                # ERRORS DETECTED
                # ─────────────────────────────────────────────────────

                latest_errors = [
                    e.message for e in debug_result.errors
                ]

                session.emit("status", {
                    "message": f"❌ {len(debug_result.errors)} runtime errors found",
                    "progress": progress_pct,
                    "state": "ERROR_RECEIVED"
                })

                if attempt == self.max_retries:
                    break

                # ─────────────────────────────────────────────────────
                # MEMORY RETRIEVAL
                # ─────────────────────────────────────────────────────

                memory_matches = self.episodic_memory.retrieve_similar(
                    errors=debug_result.errors,
                    stderr=debug_result.stderr,
                    top_k=3,
                )

                # ─────────────────────────────────────────────────────
                # CRITIC ANALYSIS
                # ─────────────────────────────────────────────────────

                critic_strategy = self.critic_agent.execute(
                    errors=debug_result.errors,
                    stderr=debug_result.stderr,
                    stdout=debug_result.stdout,
                    memory_matches=memory_matches,
                    file_list=self._list_project_files(project_path),
                    attempt=attempt,
                )

                # ─────────────────────────────────────────────────────
                # Approval Gate — Apply Fixes
                # ─────────────────────────────────────────────────────

                action = session.wait_for_approval(
                    "debug_fix",
                    {
                        "message": f"🔧 Apply AI fixes for attempt {attempt}?",
                        "strategy": critic_strategy.fixing_strategy,
                        "affectedFiles": critic_strategy.affected_files,
                        "options": ["approve", "skip", "cancel"]
                    }
                )

                if action == "cancel":

                    self._emit_complete(
                        session,
                        False,
                        plan.projectName,
                        project_path,
                        files_generated,
                        debug_attempt_count,
                        latest_errors,
                        start_time
                    )

                    return

                elif action == "skip":
                    continue

                # ─────────────────────────────────────────────────────
                # APPLY FIXES
                # ─────────────────────────────────────────────────────

                affected_files = self._choose_affected_files(
                    critic_strategy,
                    debug_result.errors,
                    existing_contents,
                    project_path,
                )

                fixed_files: List[str] = []

                for affected_file in affected_files:

                    original_content = self._read_project_file(
                        project_path,
                        affected_file
                    )

                    if not original_content:
                        continue

                    error_log = self._build_error_log(
                        debug_result.errors,
                        debug_result.stderr
                    )

                    fixed_result = self.codegen_agent.fix_file_with_strategy(
                        file_path=affected_file,
                        original_content=original_content,
                        error_log=error_log,
                        critic_strategy=critic_strategy.fixing_strategy,
                        instructions_for_code_agent=critic_strategy.instructions_for_code_agent,
                    )

                    if fixed_result.status == "fixed" and fixed_result.content:

                        self._write_project_file(
                            project_path,
                            fixed_result.path,
                            fixed_result.content
                        )

                        existing_contents[fixed_result.path] = fixed_result.content

                        fixed_files.append(fixed_result.path)

                        session.emit("fix_applied", {
                            "file": fixed_result.path,
                            "type": "critic_fix"
                        })

                previous_failed_errors = debug_result.errors
                previous_critic_strategy = critic_strategy
                previous_fixed_files = fixed_files

                attempts.append(
                    OrchestrationAttempt(
                        attempt=attempt,
                        state="FIX_APPLIED",
                        success=False,
                        errors=latest_errors,
                        memory_matches_count=len(memory_matches),
                        critic_strategy=critic_strategy,
                        fixed_files=fixed_files,
                    )
                )

            # ═══════════════════════════════════════════════════════════
            # FINAL RESPONSE
            # ═══════════════════════════════════════════════════════════

            if debug_success:

                session.emit("status", {
                    "message": "✅ Build complete!",
                    "progress": 100,
                    "state": "COMPLETE"
                })

            else:

                session.emit("status", {
                    "message": "❌ Build failed after max retries",
                    "progress": 100,
                    "state": "FAILED"
                })

            self._emit_complete(
                session,
                debug_success,
                plan.projectName,
                project_path,
                len(existing_contents),
                debug_attempt_count,
                latest_errors if not debug_success else [],
                start_time
            )

        except Exception as e:

            logger.exception("Fatal orchestration error")

            session.emit("status", {
                "message": f"❌ Fatal Error: {str(e)}",
                "progress": 100,
                "state": "FATAL_ERROR"
            })

            self._emit_complete(
                session,
                False,
                "unknown",
                project_root,
                files_generated,
                debug_attempt_count,
                [str(e)],
                start_time
            )

    # ─────────────────────────────────────────────────────────────────────
    # Helper Methods
    # ─────────────────────────────────────────────────────────────────────

    def _emit_complete(
        self,
        session,
        success,
        name,
        root,
        files,
        attempts,
        errors,
        start
    ):

        duration = time.time() - start

        response = BuildResponse(
            success=success,
            projectName=name,
            projectRoot=root,
            filesGenerated=files,
            debugAttempts=attempts,
            errors=errors,
            duration=duration,
        )

        session.emit("complete", response.model_dump())

        session.active = False

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

    def _write_project_file(
        self,
        project_path: str,
        relative_path: str,
        content: str
    ):

        file_path = os.path.join(
            project_path,
            relative_path
        )

        directory = os.path.dirname(file_path)

        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)

    def _read_project_file(
        self,
        project_path: str,
        relative_path: str
    ) -> str:

        file_path = os.path.join(
            project_path,
            relative_path
        )

        if not os.path.exists(file_path):
            return ""

        try:

            with open(file_path, "r", encoding="utf-8") as file:
                return file.read()

        except Exception as e:

            logger.warning(
                f"Failed reading {relative_path}: {e}"
            )

            return ""

    def _list_project_files(
        self,
        project_path: str
    ) -> List[str]:

        files: List[str] = []

        for root, dirs, filenames in os.walk(project_path):

            dirs[:] = [
                d for d in dirs
                if d not in ["node_modules", ".git"]
            ]

            for filename in filenames:

                if filename.endswith((".js", ".json", ".env")):

                    full_path = os.path.join(root, filename)

                    relative_path = os.path.relpath(
                        full_path,
                        project_path
                    ).replace("\\", "/")

                    files.append(relative_path)

        return sorted(files)

    def _build_error_log(
        self,
        errors: List[RuntimeErrorInfo],
        stderr: str
    ) -> str:

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

        candidates: List[str] = []

        for file in critic_strategy.affected_files:

            normalized = self._normalize_file_path(
                file,
                existing_contents,
                project_path
            )

            if normalized and normalized not in candidates:
                candidates.append(normalized)

        for err in errors:

            if err.file:

                normalized = self._normalize_file_path(
                    err.file,
                    existing_contents,
                    project_path
                )

                if normalized and normalized not in candidates:
                    candidates.append(normalized)

        if not candidates:

            fallback = self._normalize_file_path(
                "app.js",
                existing_contents,
                project_path
            )

            if fallback:
                candidates.append(fallback)

        return candidates[:2]

    def _normalize_file_path(
        self,
        file_path: str,
        existing_contents: Dict[str, str],
        project_path: str,
    ) -> Optional[str]:

        if not file_path:
            return None

        cleaned = file_path.replace("\\", "/").strip()
        cleaned = cleaned.lstrip("./")

        if cleaned in existing_contents:
            return cleaned

        if os.path.exists(
            os.path.join(project_path, cleaned)
        ):
            return cleaned

        basename = os.path.basename(cleaned)

        for known_path in existing_contents.keys():

            if os.path.basename(known_path) == basename:
                return known_path

        for root, dirs, files in os.walk(project_path):

            dirs[:] = [
                d for d in dirs
                if d not in ["node_modules", ".git"]
            ]

            for filename in files:

                if filename == basename:

                    full_path = os.path.join(root, filename)

                    return os.path.relpath(
                        full_path,
                        project_path
                    ).replace("\\", "/")

        return None