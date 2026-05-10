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
)
from agents.planner_agent import PlannerAgent
from agents.codegen_agent import CodeGenAgent
from agents.debug_agent import DebugAgent
from agents.critic_agent import CriticAgent
from services.episodic_memory import EpisodicMemory
from services.project_consistency_validator import ProjectConsistencyValidator

logger = logging.getLogger(__name__)

DEFAULT_OPERATION_RETRIES = int(os.getenv("ORCHESTRATOR_OPERATION_RETRIES", "3"))
DEFAULT_RETRY_DELAY_SECONDS = float(os.getenv("ORCHESTRATOR_RETRY_DELAY_SECONDS", "2"))



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
        use_openrouter: bool = False,
        openrouter_api_key: str = "",
    ):
        self.ollama_url = ollama_url
        self.project_validator = ProjectConsistencyValidator()

        self.planner_agent = PlannerAgent(
            ollama_url,
            models.get("planner"),
            use_openrouter=use_openrouter,
            openrouter_api_key=openrouter_api_key,
        )

        self.codegen_agent = CodeGenAgent(
            ollama_url,
            models.get("codegen"),
            use_openrouter=use_openrouter,
            openrouter_api_key=openrouter_api_key,
        )

        self.debug_agent = DebugAgent(
            ollama_url=ollama_url,
            model=models.get("debug") or "qwen2.5-coder:1.5b",
            debug_timeout=int(os.getenv("DEBUG_TIMEOUT", "10000")),
            use_openrouter=use_openrouter,
            openrouter_api_key=openrouter_api_key,
        )

        self.critic_agent = CriticAgent(
            ollama_url,
            models.get("critic"),
        )

        self.max_retries = max_retries

        self.episodic_memory = EpisodicMemory(
            memory_path=os.getenv("EPISODIC_MEMORY_PATH", "memory/episodic_memory.json")
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
        project_path = project_root

        existing_contents: Dict[str, str] = {}
        attempts: List[OrchestrationAttempt] = []

        latest_errors: List[str] = []
        debug_attempt_count = 0
        files_generated = 0

        previous_failed_errors: Optional[List[RuntimeErrorInfo]] = None
        previous_critic_strategy: Optional[CriticStrategy] = None
        previous_fixed_files: List[str] = []

        def status(message: str, progress: int, state: str) -> None:
            logger.info("STATE -> %s: %s", state, message)
            session.emit(
                "status",
                {
                    "message": message,
                    "progress": progress,
                    "state": state,
                },
            )

        try:
            logger.info("=" * 70)
            logger.info("🚀 ORCHESTRATION STARTED")
            logger.info("Workspace: %s", project_root)
            logger.info("Max retries: %s", self.max_retries)
            logger.info("=" * 70)

            # Phase 1: Planning
            status("🧠 STATE → PLANNING: Planning project architecture...", 5, "PLANNING")

            plan = self._retry_operation(
                operation_name="Planner Agent model call",
                operation=lambda: self.planner_agent.execute(request.prompt),
                session=session,
                progress=5,
                state="PLANNING_RETRY",
            )
            project_path = os.path.join(project_root, plan.projectName)

            logger.info("📋 Plan generated: %s", plan.projectName)
            logger.info("📁 Planned file count: %s", len(plan.files))

            status(
                f"📋 STATE → PLAN_READY: Plan ready with {len(plan.files)} files",
                10,
                "PLAN_READY",
            )

            # Human-in-the-loop checkpoint: plan approval.
            action = session.wait_for_approval(
                "plan",
                {
                    "message": f"📋 Plan ready: {plan.projectName}",
                    "projectName": plan.projectName,
                    "entities": [entity.model_dump() for entity in plan.entities],
                    "features": [feature.model_dump() for feature in plan.features],
                    "files": [file_spec.model_dump() for file_spec in plan.files],
                    "options": ["approve", "cancel"],
                },
            )

            if action == "cancel":
                status("❌ STATE → CANCELLED: Build cancelled during planning", 100, "CANCELLED")
                self._emit_complete(
                    session=session,
                    success=False,
                    name=plan.projectName,
                    root=project_path,
                    files=files_generated,
                    attempts=debug_attempt_count,
                    errors=["Cancelled by user during plan approval"],
                    start=start_time,
                )
                return

            status(
                "👤 STATE → PLAN_APPROVAL: Human approval checkpoint approved.",
                12,
                "PLAN_APPROVAL",
            )

            # Phase 2: Create base structure
            status("📁 STATE → CREATING_STRUCTURE: Creating project structure...", 15, "CREATING_STRUCTURE")

            os.makedirs(project_path, exist_ok=True)

            for file_spec in plan.files:
                dirname = os.path.dirname(os.path.join(project_path, file_spec.path))
                if dirname:
                    os.makedirs(dirname, exist_ok=True)

            logger.info("📁 Project structure created at: %s", project_path)

            # Phase 3: Generate all files
            status("⚙️ STATE → GENERATING: Generating backend files...", 20, "GENERATING")

            total_files = max(len(plan.files), 1)
            sorted_files = sorted(plan.files, key=lambda f: self._file_priority(f.path))

            context = CodeGenContext(
                projectName=plan.projectName,
                entities=plan.entities,
                features=plan.features,
                allFiles=plan.files,
                existingFileContents=existing_contents,
            )

            for i, file_spec in enumerate(sorted_files):
                if not session.active:
                    logger.warning("Session became inactive during file generation")
                    return

                progress_pct = 20 + int((i / total_files) * 45)

                status(
                    f"⚙️ STATE → GENERATING: ({i + 1}/{total_files}) {file_spec.path}",
                    progress_pct,
                    "GENERATING",
                )

                generated = None
                file_generation_success = False
                file_generation_error = ""

                # Updated logic from the first file: retry controlled by Orchestrator.
                for file_attempt in range(1, 4):
                    status(
                        f"⚙️ STATE → GENERATING: {file_spec.path} generation attempt {file_attempt}/3",
                        progress_pct,
                        "GENERATING_FILE_RETRY",
                    )

                    try:
                        generated = self._retry_operation(
                            operation_name=f"CodeGen Agent generate {file_spec.path}",
                            operation=lambda: self.codegen_agent.execute(file_spec, context),
                            session=session,
                            progress=progress_pct,
                            state="GENERATING_MODEL_RETRY",
                        )
                    except Exception as exc:
                        generated = None
                        file_generation_error = str(exc)

                    if generated and generated.status == "generated" and generated.content:
                        self._write_project_file(project_path, generated.path, generated.content)

                        existing_contents[generated.path] = generated.content
                        context.existingFileContents = existing_contents
                        files_generated += 1
                        file_generation_success = True

                        session.emit(
                            "file_generated",
                            {
                                "path": generated.path,
                                "status": "success",
                                "chars": len(generated.content),
                                "index": i + 1,
                                "total": total_files,
                            },
                        )

                        status(f"✅ Generated file: {generated.path}", progress_pct, "FILE_GENERATED")
                        break

                    file_generation_error = generated.errorMessage if generated else "Unknown generation error"

                    logger.warning(
                        "File generation attempt %s/3 failed for %s: %s",
                        file_attempt,
                        file_spec.path,
                        file_generation_error,
                    )

                    if file_attempt < 3:
                        time.sleep(2)

                if not file_generation_success:
                    logger.warning(
                        "File generation failed after 3 attempts for %s: %s",
                        file_spec.path,
                        file_generation_error,
                    )

                    # Session logic from the second file: user decides after automatic retries fail.
                    action = session.wait_for_approval(
                        "codegen_error",
                        {
                            "message": f"❌ Failed generating {file_spec.path}",
                            "file": file_spec.path,
                            "error": file_generation_error,
                            "options": ["retry", "skip", "cancel"],
                        },
                    )

                    if action == "retry":
                        status(
                            f"🔁 STATE → GENERATING: User requested retry for {file_spec.path}",
                            progress_pct,
                            "GENERATING_FILE_RETRY",
                        )

                        try:
                            regenerated = self._retry_operation(
                                operation_name=f"CodeGen Agent user retry {file_spec.path}",
                                operation=lambda: self.codegen_agent.execute(file_spec, context),
                                session=session,
                                progress=progress_pct,
                                state="GENERATING_USER_RETRY",
                            )
                        except Exception as exc:
                            regenerated = None
                            logger.warning("User retry failed for %s: %s", file_spec.path, exc)

                        if regenerated and regenerated.status == "generated" and regenerated.content:
                            self._write_project_file(project_path, regenerated.path, regenerated.content)

                            existing_contents[regenerated.path] = regenerated.content
                            context.existingFileContents = existing_contents
                            files_generated += 1

                            session.emit(
                                "file_generated",
                                {
                                    "path": regenerated.path,
                                    "status": "success",
                                    "chars": len(regenerated.content),
                                    "index": i + 1,
                                    "total": total_files,
                                },
                            )
                        else:
                            status(
                                f"⚠️ File generation still failed after user retry: {file_spec.path}",
                                progress_pct,
                                "FILE_GENERATION_FAILED",
                            )

                    elif action == "cancel":
                        status("❌ STATE → CANCELLED: Build cancelled during generation", 100, "CANCELLED")
                        self._emit_complete(
                            session=session,
                            success=False,
                            name=plan.projectName,
                            root=project_path,
                            files=files_generated,
                            attempts=debug_attempt_count,
                            errors=[f"Cancelled during generation of {file_spec.path}"],
                            start=start_time,
                        )
                        return

                    else:
                        status(
                            f"⚠️ STATE → FILE_SKIPPED: Skipped failed file {file_spec.path}",
                            progress_pct,
                            "FILE_SKIPPED",
                        )

            # Phase 3.5: Project Consistency Validation
            status(
                "🧾 STATE → CONSISTENCY_CHECK: Checking imports and package dependencies...",
                68,
                "CONSISTENCY_CHECK",
            )

            validation_result = self._retry_operation(
                operation_name="Project consistency validation",
                operation=lambda: self.project_validator.validate(project_path),
                max_attempts=2,
                session=session,
                progress=68,
                state="CONSISTENCY_CHECK_RETRY",
            )

            if validation_result["missing_dependencies"]:
                added_packages = self._retry_operation(
                    operation_name="Sync missing package dependencies",
                    operation=lambda: self.project_validator.sync_package_dependencies(
                        project_path=project_path,
                        packages=validation_result["missing_dependencies"],
                    ),
                    max_attempts=2,
                    session=session,
                    progress=69,
                    state="DEPENDENCY_SYNC_RETRY",
                )

                if added_packages:
                    status(
                        f"📦 Added missing dependencies to package.json: {', '.join(added_packages)}",
                        69,
                        "DEPENDENCY_SYNC",
                    )

                    package_json_path = os.path.join(project_path, "package.json")
                    if os.path.exists(package_json_path):
                        with open(package_json_path, "r", encoding="utf-8") as file:
                            existing_contents["package.json"] = file.read()

            if validation_result["issues"]:
                issue_preview = validation_result["issues"][:3]

                for issue in issue_preview:
                    status(
                        f"⚠️ Consistency issue: {issue['message']}",
                        69,
                        "CONSISTENCY_WARNING",
                    )

            # Approval Gate — Before Debug
            action = session.wait_for_approval(
                "pre_debug",
                {
                    "message": "✅ File generation and consistency check complete. Proceed to debug?",
                    "filesGenerated": files_generated,
                    "fileList": list(existing_contents.keys()),
                    "options": ["approve", "cancel"],
                },
            )

            if action == "cancel":
                status("❌ STATE → CANCELLED: Build cancelled before debug", 100, "CANCELLED")
                self._emit_complete(
                    session=session,
                    success=False,
                    name=plan.projectName,
                    root=project_path,
                    files=files_generated,
                    attempts=debug_attempt_count,
                    errors=["Cancelled before debug"],
                    start=start_time,
                )
                return

            # Phase 4: Debug Loop
            debug_success = False

            for attempt in range(1, self.max_retries + 1):
                if not session.active:
                    logger.warning("Session became inactive during debug loop")
                    return

                debug_attempt_count = attempt
                progress_pct = 70 + int((attempt / self.max_retries) * 25)

                status(
                    f"🔍 STATE → TESTING: Debug attempt {attempt}/{self.max_retries}",
                    progress_pct,
                    "TESTING",
                )

                debug_result = self._retry_operation(
                    operation_name=f"Debug Agent execution attempt {attempt}",
                    operation=lambda: self.debug_agent.execute(project_path),
                    session=session,
                    progress=progress_pct,
                    state="TESTING_RETRY",
                )

                if debug_result.success:
                    debug_success = True

                    status("✅ STATE → VERIFIED: Generated backend started successfully", 95, "VERIFIED")

                    if previous_failed_errors and previous_critic_strategy:
                        self._retry_operation(
                            operation_name="Store successful case in episodic memory",
                            operation=lambda: self.episodic_memory.store_success_case(
                                errors=previous_failed_errors,
                                critic_strategy=previous_critic_strategy,
                                fixed_files=previous_fixed_files,
                            ),
                            max_attempts=2,
                            session=session,
                            progress=97,
                            state="MEMORY_UPDATE_RETRY",
                        )

                        status(
                            "🧠 STATE → MEMORY_UPDATE: Successful error-to-fix case stored in episodic memory",
                            97,
                            "MEMORY_UPDATE",
                        )

                    attempts.append(
                        OrchestrationAttempt(
                            attempt=attempt,
                            state="VERIFIED",
                            success=True,
                            errors=[],
                            memory_matches_count=0,
                            critic_strategy=None,
                            fixed_files=[],
                        )
                    )

                    break

                latest_errors = [error.message for error in debug_result.errors]

                attempts.append(
                    OrchestrationAttempt(
                        attempt=attempt,
                        state="ERROR_RECEIVED",
                        success=False,
                        errors=latest_errors,
                        memory_matches_count=0,
                        critic_strategy=None,
                        fixed_files=[],
                    )
                )

                status(
                    f"❌ STATE → ERROR_RECEIVED: {len(debug_result.errors)} error(s) captured",
                    progress_pct,
                    "ERROR_RECEIVED",
                )

                if attempt == self.max_retries:
                    status("🛑 STATE → FAILED_SAFE_EXIT: Retry budget exhausted", 100, "FAILED_SAFE_EXIT")

                    report_path = self._retry_operation(
                        operation_name="Generate debug report",
                        operation=lambda: self._generate_debug_report(
                            project_path=project_path,
                            user_prompt=request.prompt,
                            max_retries=self.max_retries,
                            attempts=attempts,
                            final_errors=latest_errors,
                        ),
                        max_attempts=2,
                        session=session,
                        progress=100,
                        state="DEBUG_REPORT_RETRY",
                    )

                    status(f"📄 Debug report generated: {report_path}", 100, "DEBUG_REPORT_GENERATED")
                    break

                # Memory Retrieval
                status(
                    "🧠 STATE → MEMORY_RETRIEVAL: Retrieving similar past error-fix cases...",
                    progress_pct + 2,
                    "MEMORY_RETRIEVAL",
                )

                memory_matches = self._retry_operation(
                    operation_name="Episodic memory retrieval",
                    operation=lambda: self.episodic_memory.retrieve_similar(
                        errors=debug_result.errors,
                        stderr=debug_result.stderr,
                        top_k=3,
                    ),
                    max_attempts=2,
                    session=session,
                    progress=progress_pct + 2,
                    state="MEMORY_RETRIEVAL_RETRY",
                )

                # Critic Strategy Generation
                status(
                    "🧩 STATE → CRITIC_ANALYSIS: Critic Agent generating fixing strategy...",
                    progress_pct + 4,
                    "CRITIC_ANALYSIS",
                )

                file_list = self._retry_operation(
                    operation_name="List project files for critic context",
                    operation=lambda: self._list_project_files(project_path),
                    max_attempts=2,
                    session=session,
                    progress=progress_pct + 3,
                    state="FILE_LIST_RETRY",
                )

                critic_strategy = self._retry_operation(
                    operation_name=f"Critic Agent strategy generation attempt {attempt}",
                    operation=lambda: self.critic_agent.execute(
                    errors=debug_result.errors,
                    stderr=debug_result.stderr,
                    stdout=debug_result.stdout,
                    memory_matches=memory_matches,
                    file_list=file_list,
                        attempt=attempt,
                    ),
                    session=session,
                    progress=progress_pct + 4,
                    state="CRITIC_ANALYSIS_RETRY",
                )

                status(
                    f"🧩 Critic Strategy: {critic_strategy.fixing_strategy[:180]}",
                    progress_pct + 6,
                    "CRITIC_ANALYSIS",
                )

                # Approval Gate — Apply Fixes
                action = session.wait_for_approval(
                    "debug_fix",
                    {
                        "message": f"🔧 Apply AI fixes for debug attempt {attempt}?",
                        "strategy": critic_strategy.fixing_strategy,
                        "instructions": critic_strategy.instructions_for_code_agent,
                        "affectedFiles": critic_strategy.affected_files,
                        "errors": latest_errors,
                        "options": ["approve", "skip", "cancel"],
                    },
                )

                if action == "cancel":
                    status("❌ STATE → CANCELLED: Build cancelled during debug fixing", 100, "CANCELLED")
                    self._emit_complete(
                        session=session,
                        success=False,
                        name=plan.projectName,
                        root=project_path,
                        files=files_generated,
                        attempts=debug_attempt_count,
                        errors=latest_errors,
                        start=start_time,
                    )
                    return

                if action == "skip":
                    attempts[-1].memory_matches_count = len(memory_matches)
                    attempts[-1].critic_strategy = critic_strategy
                    attempts[-1].state = "FIX_SKIPPED"
                    status(
                        "⏭️ STATE → FIX_SKIPPED: User skipped the fix. Retrying test loop if budget remains.",
                        progress_pct + 8,
                        "FIX_SKIPPED",
                    )
                    continue

                # Code Agent applies the strategy
                status(
                    "🔧 STATE → CODE_FIXING: CodeGen Agent applying Critic strategy...",
                    progress_pct + 8,
                    "CODE_FIXING",
                )

                affected_files = self._retry_operation(
                    operation_name="Choose affected files",
                    operation=lambda: self._choose_affected_files(
                        critic_strategy=critic_strategy,
                        errors=debug_result.errors,
                        existing_contents=existing_contents,
                        project_path=project_path,
                    ),
                    max_attempts=2,
                    session=session,
                    progress=progress_pct + 7,
                    state="AFFECTED_FILES_RETRY",
                )

                fixed_files: List[str] = []

                for affected_file in affected_files:
                    original_content = self._retry_operation(
                        operation_name=f"Read affected file {affected_file}",
                        operation=lambda: self._read_project_file(project_path, affected_file),
                        max_attempts=2,
                    )

                    if not original_content:
                        logger.warning("Skipping fix because file not found/readable: %s", affected_file)
                        continue

                    error_log = self._retry_operation(
                        operation_name="Build error log for code fixing",
                        operation=lambda: self._build_error_log(debug_result.errors, debug_result.stderr),
                        max_attempts=2,
                    )

                    fixed_result = None
                    fix_success = False
                    fix_error = ""

                    # Updated logic from the first file: orchestrator-level fix retry.
                    for fix_attempt in range(1, 4):
                        status(
                            f"🔧 STATE → CODE_FIXING: Fixing {affected_file} attempt {fix_attempt}/3",
                            progress_pct + 8,
                            "CODE_FIXING_RETRY",
                        )

                        try:
                            fixed_result = self._retry_operation(
                                operation_name=f"CodeGen Agent fix {affected_file}",
                                operation=lambda: self.codegen_agent.fix_file_with_strategy(
                                    file_path=affected_file,
                                    original_content=original_content,
                                    error_log=error_log,
                                    critic_strategy=critic_strategy.fixing_strategy,
                                    instructions_for_code_agent=critic_strategy.instructions_for_code_agent,
                                ),
                                session=session,
                                progress=progress_pct + 8,
                                state="CODE_FIXING_MODEL_RETRY",
                            )
                        except Exception as exc:
                            fixed_result = None
                            fix_error = str(exc)

                        if fixed_result and fixed_result.status == "fixed" and fixed_result.content:
                            self._write_project_file(project_path, fixed_result.path, fixed_result.content)

                            existing_contents[fixed_result.path] = fixed_result.content
                            context.existingFileContents = existing_contents
                            fixed_files.append(fixed_result.path)
                            fix_success = True

                            session.emit(
                                "fix_applied",
                                {
                                    "file": fixed_result.path,
                                    "type": "critic_fix",
                                    "attempt": attempt,
                                    "fixAttempt": fix_attempt,
                                },
                            )

                            status(
                                f"🔧 Fixed file generated: {fixed_result.path}",
                                progress_pct + 10,
                                "CODE_FIXING",
                            )

                            break

                        if fixed_result:
                            fix_error = fixed_result.errorMessage or "Unknown fix error"
                        elif not fix_error:
                            fix_error = "Unknown fix error"

                        logger.warning(
                            "CodeGen fix attempt %s/3 failed for %s: %s",
                            fix_attempt,
                            affected_file,
                            fix_error,
                        )

                        if fix_attempt < 3:
                            time.sleep(2)

                    if not fix_success:
                        logger.warning("CodeGen failed to fix %s after 3 attempts: %s", affected_file, fix_error)

                attempts[-1].memory_matches_count = len(memory_matches)
                attempts[-1].critic_strategy = critic_strategy
                attempts[-1].fixed_files = fixed_files
                attempts[-1].state = "FIX_APPLIED" if fixed_files else "FIX_FAILED"

                previous_failed_errors = debug_result.errors
                previous_critic_strategy = critic_strategy
                previous_fixed_files = fixed_files

                if not fixed_files:
                    status(
                        "⚠️ STATE → FIX_FAILED: No file was fixed. Retrying may fail.",
                        progress_pct + 10,
                        "FIX_FAILED",
                    )
                else:
                    status(
                        "🔁 STATE → RETESTING: Fix applied. Retesting in next loop...",
                        progress_pct + 12,
                        "RETESTING",
                    )

            if debug_success:
                status("✅ Build complete!", 100, "COMPLETE")
            else:
                status("❌ Build failed after max retries", 100, "FAILED")

            self._emit_complete(
                session=session,
                success=debug_success,
                name=plan.projectName,
                root=project_path,
                files=len(existing_contents),
                attempts=debug_attempt_count,
                errors=latest_errors if not debug_success else [],
                start=start_time,
            )

        except Exception as exc:
            logger.exception("Fatal error during orchestration")

            status(f"❌ Fatal Error: {str(exc)}", 100, "FATAL_ERROR")

            self._emit_complete(
                session=session,
                success=False,
                name="unknown",
                root=project_path,
                files=files_generated,
                attempts=debug_attempt_count,
                errors=[str(exc)],
                start=start_time,
            )

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
    ) -> Any:
        """
        Retry wrapper for unstable orchestration operations.

        Use this for model/API calls and other operations that can fail because of
        temporary connection issues, timeout errors, incomplete model responses, or
        file-system race conditions. The orchestrator still owns the workflow-level
        retry budget; this helper protects each individual operation.
        """

        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
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
                            },
                        )

                    time.sleep(delay_seconds * (2 ** (attempt - 2)))

                result = operation()

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

                if attempt == max_attempts:
                    logger.error(
                        "%s failed after %s attempt(s)",
                        operation_name,
                        max_attempts,
                    )
                    raise

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
        )

        session.emit("complete", response.model_dump())
        session.active = False

        logger.info("=" * 70)
        logger.info("🏁 ORCHESTRATION COMPLETE")
        logger.info("Success: %s", success)
        logger.info("Project: %s", name)
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
