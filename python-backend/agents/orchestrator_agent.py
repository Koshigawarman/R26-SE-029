import os
import json
import time
import logging
from typing import Dict, Generator, List, Optional

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

logger = logging.getLogger(__name__)

class OrchestratorAgent:
    def __init__(self, ollama_url: str, models: dict, max_retries: int = 3, use_openrouter: bool = False, openrouter_api_key: str = ""):
        self.ollama_url = ollama_url
        self.planner_agent = PlannerAgent(ollama_url, models.get("planner"), use_openrouter=use_openrouter, openrouter_api_key=openrouter_api_key)
        self.codegen_agent = CodeGenAgent(ollama_url, models.get("codegen"), use_openrouter=use_openrouter, openrouter_api_key=openrouter_api_key)
        self.debug_agent = DebugAgent(ollama_url, models.get("debug"), use_openrouter=use_openrouter, openrouter_api_key=openrouter_api_key)
        self.critic_agent = CriticAgent(ollama_url, models.get("critic"), use_openrouter=use_openrouter, openrouter_api_key=openrouter_api_key)
        self.max_retries = max_retries

        self.episodic_memory = EpisodicMemory(
            memory_path=os.getenv("EPISODIC_MEMORY_PATH", "memory/episodic_memory.json")
        )

        # For PP1: seed initial curated cases if available.
        self.episodic_memory.seed_from_dataset(
            os.getenv("ERROR_FIX_DATASET_PATH", "datasets/error_fix_cases.json")
        )

    def execute_stream(self, request: BuildRequest) -> Generator[str, None, None]:
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

        def yield_event(event_type: str, data: dict):
            payload = json.dumps({"type": event_type, "data": data})
            return f"data: {payload}\n\n"

        def status(message: str, progress: int, state: str):
            logger.info(f"STATE -> {state}: {message}")
            return yield_event(
                "status",
                {
                    "message": message,
                    "progress": progress,
                    "state": state,
                }
            )
        
        try:
            # Phase 1: Planning

            yield status("🧠 STATE → PLANNING: Planning project architecture...", 5, "PLANNING")

            plan = self.planner_agent.execute(request.prompt)
            project_path = os.path.join(project_root, plan.projectName)
            
            yield status(
                f"📋 STATE → PLAN_READY: Plan ready with {len(plan.files)} files",
                10,
                "PLAN_READY"
            )

            # PP1 Human-in-the-loop checkpoint placeholder.
            # For now, auto-approve but show it in logs.
            yield status(
                "👤 STATE → PLAN_APPROVAL: Human approval checkpoint reached. Auto-approved for PP1 demo.",
                12,
                "PLAN_APPROVAL"
            )


            # Phase 2: Create Base Structure
            yield status("📁 STATE → CREATING_STRUCTURE: Creating project structure...", 15, "CREATING_STRUCTURE")
            
            os.makedirs(project_path, exist_ok=True)
            
            for file_spec in plan.files:
                dirname = os.path.dirname(os.path.join(project_path, file_spec.path))
                if dirname:
                    os.makedirs(dirname, exist_ok=True)

            # Phase 3: Generate All Files
            yield status("⚙️ STATE → GENERATING: Generating backend files...", 20, "GENERATING")

            total_files = len(plan.files)
            sorted_files = sorted(plan.files, key=lambda f: self._file_priority(f.path))

            context = CodeGenContext(
                projectName=plan.projectName,
                entities=plan.entities,
                features=plan.features,
                allFiles=plan.files,
                existingFileContents=existing_contents
            )

            for i, file_spec in enumerate(sorted_files):
                progress_pct = 20 + int((i / total_files) * 45)
                yield status(
                    f"⚙️ STATE → GENERATING: ({i + 1}/{total_files}) {file_spec.path}",
                    progress_pct,
                    "GENERATING"
                )                
                generated = self.codegen_agent.execute(file_spec, context)
                
                if generated.status == 'generated' and generated.content:
                    self._write_project_file(project_path, generated.path, generated.content)
                    existing_contents[generated.path] = generated.content
                    context.existingFileContents = existing_contents
                    files_generated += 1
                else:
                    logger.warning(
                        f"File generation failed for {file_spec.path}: {generated.errorMessage}"
                    )


            # Phase 4: Debug Loop
            debug_success = False
            
            for attempt in range(1, self.max_retries + 1):
                debug_attempt_count = attempt
                progress_pct = 70 + int((attempt / self.max_retries) * 25)
                yield status(
                    f"🔍 STATE → TESTING: Debug attempt {attempt}/{self.max_retries}",
                    progress_pct,
                    "TESTING"
                )
                
                debug_result = self.debug_agent.execute(project_path)
                
                if debug_result.success:
                    debug_success = True
                    yield status(
                        "✅ STATE → VERIFIED: Generated backend started successfully",
                        95,
                        "VERIFIED"
                    )

                    if previous_failed_errors and previous_critic_strategy:
                        self.episodic_memory.store_success_case(
                            errors=previous_failed_errors,
                            critic_strategy=previous_critic_strategy,
                            fixed_files=previous_fixed_files,
                        )

                        yield status(
                            "🧠 STATE → MEMORY_UPDATE: Successful error-to-fix case stored in episodic memory",
                            97,
                            "MEMORY_UPDATE"
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
                    
                latest_errors = [e.message for e in debug_result.errors]
                
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

                yield status(
                    f"❌ STATE → ERROR_RECEIVED: {len(debug_result.errors)} error(s) captured",
                    progress_pct,
                    "ERROR_RECEIVED"
                )

                # Stop if retry budget is exhausted.
                if attempt == self.max_retries:
                    yield status(
                        "🛑 STATE → FAILED_SAFE_EXIT: Retry budget exhausted",
                        100,
                        "FAILED_SAFE_EXIT"
                    )
                    break

                    
                # ─────────────────────────────────────────────────────────
                # Memory Retrieval
                # ─────────────────────────────────────────────────────────
                yield status(
                    "🧠 STATE → MEMORY_RETRIEVAL: Retrieving similar past error-fix cases...",
                    progress_pct + 2,
                    "MEMORY_RETRIEVAL"
                )

                memory_matches = self.episodic_memory.retrieve_similar(
                    errors=debug_result.errors,
                    stderr=debug_result.stderr,
                    top_k=3,
                )

                # ─────────────────────────────────────────────────────────
                # Critic Strategy Generation
                # ─────────────────────────────────────────────────────────
                yield status(
                    "🧩 STATE → CRITIC_ANALYSIS: Critic Agent generating fixing strategy...",
                    progress_pct + 4,
                    "CRITIC_ANALYSIS"
                )

                file_list = self._list_project_files(project_path)

                critic_strategy = self.critic_agent.execute(
                    errors=debug_result.errors,
                    stderr=debug_result.stderr,
                    stdout=debug_result.stdout,
                    memory_matches=memory_matches,
                    file_list=file_list,
                    attempt=attempt,
                )

                yield status(
                    f"🧩 Critic Strategy: {critic_strategy.fixing_strategy[:180]}",
                    progress_pct + 6,
                    "CRITIC_ANALYSIS"
                )

                # ─────────────────────────────────────────────────────────
                # Code Agent applies the strategy
                # ─────────────────────────────────────────────────────────
                yield status(
                    "🔧 STATE → CODE_FIXING: CodeGen Agent applying Critic strategy...",
                    progress_pct + 8,
                    "CODE_FIXING"
                )

                affected_files = self._choose_affected_files(
                    critic_strategy=critic_strategy,
                    errors=debug_result.errors,
                    existing_contents=existing_contents,
                    project_path=project_path,
                )

                fixed_files: List[str] = []

                for affected_file in affected_files:
                    original_content = self._read_project_file(project_path, affected_file)

                    if not original_content:
                        logger.warning(f"Skipping fix because file not found/readable: {affected_file}")
                        continue

                    error_log = self._build_error_log(debug_result.errors, debug_result.stderr)

                    fixed_result = self.codegen_agent.fix_file_with_strategy(
                        file_path=affected_file,
                        original_content=original_content,
                        error_log=error_log,
                        critic_strategy=critic_strategy.fixing_strategy,
                        instructions_for_code_agent=critic_strategy.instructions_for_code_agent,
                    )

                    if fixed_result.status == "fixed" and fixed_result.content:
                        self._write_project_file(project_path, fixed_result.path, fixed_result.content)
                        existing_contents[fixed_result.path] = fixed_result.content
                        context.existingFileContents = existing_contents
                        fixed_files.append(fixed_result.path)

                        yield status(
                            f"🔧 Fixed file generated: {fixed_result.path}",
                            progress_pct + 10,
                            "CODE_FIXING"
                        )
                    else:
                        logger.warning(
                            f"CodeGen failed to fix {affected_file}: {fixed_result.errorMessage}"
                        )

                # Save attempt record for report/evidence.
                attempts[-1].memory_matches_count = len(memory_matches)
                attempts[-1].critic_strategy = critic_strategy
                attempts[-1].fixed_files = fixed_files
                attempts[-1].state = "FIX_APPLIED" if fixed_files else "FIX_FAILED"

                previous_failed_errors = debug_result.errors
                previous_critic_strategy = critic_strategy
                previous_fixed_files = fixed_files

                if not fixed_files:
                    yield status(
                        "⚠️ STATE → FIX_FAILED: No file was fixed. Retrying may fail.",
                        progress_pct + 10,
                        "FIX_FAILED"
                    )
                else:
                    yield status(
                        "🔁 STATE → RETESTING: Fix applied. Retesting in next loop...",
                        progress_pct + 12,
                        "RETESTING"
                    )

            # ─────────────────────────────────────────────────────────────
            # Final Response
            # ─────────────────────────────────────────────────────────────
            duration = time.time() - start_time

            response = BuildResponse(
                success=debug_success,
                projectName=plan.projectName,
                projectRoot=project_path,
                filesGenerated=len(existing_contents),
                debugAttempts=debug_attempt_count,
                errors=latest_errors if not debug_success else [],
                duration=duration,
            )

            if debug_success:
                yield status("✅ Build complete!", 100, "COMPLETE")
            else:
                yield status("❌ Build failed after max retries", 100, "FAILED")

            yield yield_event("complete", response.model_dump())

        except Exception as e:
            logger.exception("Fatal error during orchestration")

            duration = time.time() - start_time

            response = BuildResponse(
                success=False,
                projectName="unknown",
                projectRoot=project_path,
                filesGenerated=files_generated,
                debugAttempts=debug_attempt_count,
                errors=[str(e)],
                duration=duration,
            )

            yield status(f"❌ Fatal Error: {str(e)}", 100, "FATAL_ERROR")
            yield yield_event("complete", response.model_dump())

    # ─────────────────────────────────────────────────────────────────────
    # Helper methods
    # ─────────────────────────────────────────────────────────────────────

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
        os.makedirs(os.path.dirname(file_path), exist_ok=True) if os.path.dirname(file_path) else None

        with open(file_path, "w", encoding="utf-8") as file:
            file.write(content)

    def _read_project_file(self, project_path: str, relative_path: str) -> str:
        file_path = os.path.join(project_path, relative_path)

        if not os.path.exists(file_path):
            return ""

        try:
            with open(file_path, "r", encoding="utf-8") as file:
                return file.read()
        except Exception as e:
            logger.warning(f"Failed to read {relative_path}: {e}")
            return ""

    def _list_project_files(self, project_path: str) -> List[str]:
        files: List[str] = []

        for root, dirs, filenames in os.walk(project_path):
            # Ignore heavy/generated folders.
            dirs[:] = [d for d in dirs if d not in ["node_modules", ".git"]]

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
        1. Critic Agent affected files
        2. Files detected from error stack
        3. app.js fallback
        """

        candidates: List[str] = []

        for file in critic_strategy.affected_files:
            normalized = self._normalize_file_path(file, existing_contents, project_path)
            if normalized and normalized not in candidates:
                candidates.append(normalized)

        for err in errors:
            if err.file:
                normalized = self._normalize_file_path(err.file, existing_contents, project_path)
                if normalized and normalized not in candidates:
                    candidates.append(normalized)

        if not candidates:
            fallback = self._normalize_file_path("app.js", existing_contents, project_path)
            if fallback:
                candidates.append(fallback)

        # For PP1, patch max 2 files per attempt to keep model usage small.
        return candidates[:2]

    def _normalize_file_path(
        self,
        file_path: str,
        existing_contents: Dict[str, str],
        project_path: str,
    ) -> Optional[str]:
        """
        Convert model/stack returned file paths into actual relative project paths.
        """

        if not file_path:
            return None

        cleaned = file_path.replace("\\", "/").strip()
        cleaned = cleaned.lstrip("./")

        # Direct match in current generated contents.
        if cleaned in existing_contents:
            return cleaned

        # Direct match on disk.
        if os.path.exists(os.path.join(project_path, cleaned)):
            return cleaned

        # Sometimes stack gives only app.js or controller file name.
        basename = os.path.basename(cleaned)

        for known_path in existing_contents.keys():
            if os.path.basename(known_path) == basename:
                return known_path

        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in ["node_modules", ".git"]]

            for filename in files:
                if filename == basename:
                    full_path = os.path.join(root, filename)
                    return os.path.relpath(full_path, project_path).replace("\\", "/")

        return None