"""
AI Backend Builder — Interactive Orchestrator Agent

Coordinates the multi-agent pipeline with human-in-the-loop approval gates.
Uses a BuildSession (queue + threading.Event) so the async SSE stream can
deliver events while the orchestrator blocks at approval checkpoints.
"""

import os
import json
import time
import logging
import threading
import queue
import uuid
from typing import Dict, Optional

from schema import BuildRequest, BuildResponse, CodeGenContext, FileSpec
from agents.planner_agent import PlannerAgent
from agents.codegen_agent import CodeGenAgent
from agents.debug_agent import DebugAgent

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Build Session — shared state between the orchestrator thread and the
# async SSE endpoint so the user can approve / reject at each gate.
# ─────────────────────────────────────────────────────────────────────────────

class BuildSession:
    """Thread-safe session for a single build run."""

    def __init__(self):
        self.id: str = str(uuid.uuid4())
        self.event_queue: queue.Queue = queue.Queue()
        self.approval_event: threading.Event = threading.Event()
        self.approval_action: Optional[str] = None   # "approve" | "skip" | "retry" | "cancel"
        self.approval_data: Optional[dict] = None     # extra data from the user
        self.active: bool = True

    # ── helpers used by the orchestrator (runs in its own thread) ──

    def emit(self, event_type: str, data: dict):
        """Push an event onto the queue for the SSE consumer."""
        self.event_queue.put({"type": event_type, "data": data})

    def wait_for_approval(self, step: str, details: dict, timeout: int = 600) -> str:
        """
        Emit an 'approval_needed' event, then block until the user responds
        via the /api/build/<id>/approve endpoint.
        Returns the user's action string.
        """
        self.approval_event.clear()
        self.approval_action = None
        self.emit("approval_needed", {"step": step, **details})

        logger.info(f"⏸️  Waiting for user approval at step: {step}")
        self.approval_event.wait(timeout=timeout)

        action = self.approval_action or "cancel"
        logger.info(f"▶️  User responded with: {action}")
        return action


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator Agent
# ─────────────────────────────────────────────────────────────────────────────

class OrchestratorAgent:
    def __init__(self, ollama_url: str, models: dict, max_retries: int = 3,
                 use_openrouter: bool = False, openrouter_api_key: str = ""):
        self.ollama_url = ollama_url
        self.planner_agent = PlannerAgent(ollama_url, models.get("planner"),
                                          use_openrouter=use_openrouter,
                                          openrouter_api_key=openrouter_api_key)
        self.codegen_agent = CodeGenAgent(ollama_url, models.get("codegen"),
                                          use_openrouter=use_openrouter,
                                          openrouter_api_key=openrouter_api_key)
        self.debug_agent   = DebugAgent(ollama_url, models.get("debug"),
                                        use_openrouter=use_openrouter,
                                        openrouter_api_key=openrouter_api_key)
        self.max_retries = max_retries

    # ── legacy non-interactive stream (kept for backward compat) ──────────
    def execute_stream(self, request: BuildRequest):
        """Original fire-and-forget generator (no approval gates)."""
        session = BuildSession()
        self._run(request, session)
        while not session.event_queue.empty():
            yield self._format_sse(session.event_queue.get())

    # ── interactive entry point (called from the background thread) ───────
    def execute_interactive(self, request: BuildRequest, session: BuildSession):
        """Run the full pipeline with approval gates via *session*."""
        self._run(request, session)

    # ── core pipeline ─────────────────────────────────────────────────────
    def _run(self, request: BuildRequest, session: BuildSession):
        start_time = time.time()
        project_root = request.workspace_uri

        session.emit("status", {"message": "🧠 Planning project architecture...", "progress": 5})

        try:
            # ═══════════════════════════════════════════════════════════════
            # PHASE 1 — PLANNING
            # ═══════════════════════════════════════════════════════════════
            logger.info("\n" + "="*50)
            logger.info("🚀 ORCHESTRATOR PHASE 1: PLANNING")
            logger.info(f"Target Project Root: {project_root}")
            logger.info("="*50)

            plan = self.planner_agent.execute(request.prompt)
            project_path = os.path.join(project_root, plan.projectName)

            logger.info(f"📋 Plan ready — '{plan.projectName}' | "
                         f"{len(plan.entities)} entities, {len(plan.features)} features, {len(plan.files)} files")

            # ── APPROVAL GATE 1: Review the plan ──────────────────────────
            action = session.wait_for_approval("plan", {
                "message": f"📋 Plan ready: {plan.projectName}",
                "projectName": plan.projectName,
                "entities": [e.model_dump() for e in plan.entities],
                "features": [f.model_dump() for f in plan.features],
                "files": [f.model_dump() for f in plan.files],
            })

            if action == "cancel":
                session.emit("status", {"message": "❌ Build cancelled by user at planning stage.", "progress": 100})
                self._emit_complete(session, False, "unknown", project_root, 0, 0, ["Cancelled by user"], start_time)
                return

            session.emit("status", {"message": f"📋 Plan approved — {len(plan.files)} files", "progress": 10})

            # ═══════════════════════════════════════════════════════════════
            # PHASE 2 — SCAFFOLDING
            # ═══════════════════════════════════════════════════════════════
            logger.info("\n" + "="*50)
            logger.info("📁 ORCHESTRATOR PHASE 2: SCAFFOLDING")
            logger.info("="*50)

            session.emit("status", {"message": "📁 Creating project structure...", "progress": 15})
            os.makedirs(project_path, exist_ok=True)
            for file_spec in plan.files:
                dirname = os.path.dirname(os.path.join(project_path, file_spec.path))
                if dirname:
                    os.makedirs(dirname, exist_ok=True)

            # ═══════════════════════════════════════════════════════════════
            # PHASE 3 — CODE GENERATION
            # ═══════════════════════════════════════════════════════════════
            logger.info("\n" + "="*50)
            logger.info("⚙️ ORCHESTRATOR PHASE 3: CODE GENERATION")
            logger.info("="*50)

            total_files = len(plan.files)
            existing_contents: Dict[str, str] = {}

            def file_priority(path: str):
                if path.startswith('models/'): return 0
                if path.startswith('middleware/'): return 1
                if path.startswith('controllers/'): return 2
                if path.startswith('routes/'): return 3
                if path == 'app.js': return 4
                return 5

            sorted_files = sorted(plan.files, key=lambda f: file_priority(f.path))
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

                progress_pct = 20 + int((i / total_files) * 50)
                session.emit("status", {
                    "message": f"⚙️ Generating ({i+1}/{total_files}): {file_spec.path}",
                    "progress": progress_pct,
                })

                logger.info(f"→ Dispatching CodeGen [{i+1}/{total_files}]: {file_spec.path}")
                generated = self.codegen_agent.execute(file_spec, context)

                if generated.status == 'generated' and generated.content:
                    fp = os.path.join(project_path, file_spec.path)
                    with open(fp, "w") as f:
                        f.write(generated.content)
                    existing_contents[file_spec.path] = generated.content
                    logger.info(f"✓ Saved: {fp}")

                    # Notify panel with per-file result
                    session.emit("file_generated", {
                        "path": file_spec.path,
                        "status": "success",
                        "chars": len(generated.content),
                        "index": i + 1,
                        "total": total_files,
                    })
                else:
                    error_msg = generated.errorMessage or "Unknown error"
                    logger.error(f"✗ Failed: {file_spec.path} — {error_msg}")

                    # ── APPROVAL GATE 2: File generation error ────────────
                    action = session.wait_for_approval("codegen_error", {
                        "message": f"❌ Failed to generate: {file_spec.path}",
                        "file": file_spec.path,
                        "error": error_msg,
                        "explanation": (
                            f"The AI model could not produce valid code for '{file_spec.path}'. "
                            f"This may be caused by a timeout, an overloaded model, or an issue "
                            f"with the prompt context. Error: {error_msg}"
                        ),
                        "options": ["retry", "skip", "cancel"],
                    })

                    if action == "retry":
                        logger.info(f"⟳ User chose to retry: {file_spec.path}")
                        generated = self.codegen_agent.execute(file_spec, context)
                        if generated.status == 'generated' and generated.content:
                            fp = os.path.join(project_path, file_spec.path)
                            with open(fp, "w") as f:
                                f.write(generated.content)
                            existing_contents[file_spec.path] = generated.content
                            session.emit("file_generated", {
                                "path": file_spec.path, "status": "success",
                                "chars": len(generated.content),
                                "index": i + 1, "total": total_files,
                            })
                        else:
                            session.emit("file_generated", {
                                "path": file_spec.path, "status": "failed",
                                "error": generated.errorMessage,
                                "index": i + 1, "total": total_files,
                            })
                    elif action == "cancel":
                        session.emit("status", {"message": "❌ Build cancelled by user.", "progress": 100})
                        self._emit_complete(session, False, plan.projectName, project_path,
                                            len(existing_contents), 0, ["Cancelled by user"], start_time)
                        return
                    else:
                        # skip
                        session.emit("file_generated", {
                            "path": file_spec.path, "status": "skipped",
                            "index": i + 1, "total": total_files,
                        })

            # ── APPROVAL GATE 3: Confirm before debugging ─────────────────
            action = session.wait_for_approval("pre_debug", {
                "message": f"✅ Code generation complete — {len(existing_contents)} files generated. Proceed to debug?",
                "filesGenerated": len(existing_contents),
                "fileList": list(existing_contents.keys()),
                "options": ["approve", "cancel"],
            })

            if action == "cancel":
                session.emit("status", {"message": "❌ Build cancelled before debug phase.", "progress": 100})
                self._emit_complete(session, False, plan.projectName, project_path,
                                    len(existing_contents), 0, ["Cancelled before debug"], start_time)
                return

            # ═══════════════════════════════════════════════════════════════
            # PHASE 4 — DEBUG LOOP
            # ═══════════════════════════════════════════════════════════════
            logger.info("\n" + "="*50)
            logger.info("🔍 ORCHESTRATOR PHASE 4: DEBUG LOOP")
            logger.info("="*50)

            debug_success = False
            errors = []
            attempt = 0

            for attempt in range(1, self.max_retries + 1):
                if not session.active:
                    return

                progress_pct = 70 + int((attempt / self.max_retries) * 25)
                session.emit("status", {
                    "message": f"🔍 Debug attempt {attempt}/{self.max_retries}...",
                    "progress": progress_pct,
                })

                logger.info(f"→ Debug Cycle [{attempt}/{self.max_retries}]")
                debug_result = self.debug_agent.execute(project_path, existing_contents)

                if debug_result.success:
                    logger.info(f"✅ Debug Cycle [{attempt}] succeeded!")
                    debug_success = True
                    break

                errors = [e.message for e in debug_result.errors]
                logger.warning(f"⚠️ Debug Cycle [{attempt}] found {len(errors)} errors.")

                if attempt == self.max_retries:
                    logger.error(f"❌ Max retries ({self.max_retries}) reached.")
                    break

                if debug_result.suggestions:
                    # ── APPROVAL GATE 4: Review debug fixes ───────────────
                    fix_details = []
                    for fix in debug_result.suggestions:
                        fix_details.append({
                            "file": fix.file,
                            "issue": fix.issue,
                            "hasDirectFix": bool(fix.fix and len(fix.fix.strip()) > 10),
                            "willRegenerate": fix.regenerate,
                        })

                    action = session.wait_for_approval("debug_fix", {
                        "message": f"🔧 Debug found {len(errors)} errors with {len(debug_result.suggestions)} proposed fixes.",
                        "attempt": attempt,
                        "maxAttempts": self.max_retries,
                        "errors": errors[:5],
                        "fixes": fix_details,
                        "explanation": (
                            f"The debug agent ran the generated project and detected "
                            f"{len(errors)} runtime error(s). It has proposed "
                            f"{len(debug_result.suggestions)} fix(es). "
                            f"Approve to apply them, or cancel to stop."
                        ),
                        "options": ["approve", "skip", "cancel"],
                    })

                    if action == "cancel":
                        session.emit("status", {"message": "❌ Build cancelled during debug phase.", "progress": 100})
                        self._emit_complete(session, False, plan.projectName, project_path,
                                            len(existing_contents), attempt, errors, start_time)
                        return
                    elif action == "skip":
                        continue

                    # Apply fixes
                    session.emit("status", {
                        "message": f"🔧 Applying {len(debug_result.suggestions)} fixes...",
                        "progress": progress_pct + 5,
                    })

                    for fix in debug_result.suggestions:
                        if fix.fix and len(fix.fix.strip()) > 10:
                            fp = os.path.join(project_path, fix.file)
                            with open(fp, "w") as f:
                                f.write(fix.fix)
                            existing_contents[fix.file] = fix.fix
                            logger.info(f"   ✓ Direct fix applied: {fix.file}")
                            session.emit("fix_applied", {
                                "file": fix.file, "type": "direct",
                                "issue": fix.issue,
                            })
                        elif fix.regenerate:
                            logger.info(f"   ↻ Regenerating: {fix.file}")
                            f_spec = next((f for f in plan.files if f.path == fix.file), None)
                            if f_spec:
                                modified_spec = FileSpec(
                                    path=f_spec.path,
                                    description=f"{f_spec.description}. FIX NEEDED: {fix.issue}"
                                )
                                generated = self.codegen_agent.execute(modified_spec, context)
                                if generated.status == 'generated' and generated.content:
                                    fp = os.path.join(project_path, fix.file)
                                    with open(fp, "w") as f:
                                        f.write(generated.content)
                                    existing_contents[fix.file] = generated.content
                                    logger.info(f"   ✓ Regeneration OK: {fix.file}")
                                    session.emit("fix_applied", {
                                        "file": fix.file, "type": "regenerated",
                                        "issue": fix.issue,
                                    })

            # ═══════════════════════════════════════════════════════════════
            # DONE
            # ═══════════════════════════════════════════════════════════════
            if debug_success:
                session.emit("status", {"message": "✅ Build complete!", "progress": 100})
            else:
                session.emit("status", {"message": "❌ Build failed after max retries", "progress": 100})

            self._emit_complete(session, debug_success, plan.projectName, project_path,
                                len(existing_contents), attempt, errors, start_time)

        except Exception as e:
            logger.error(f"Fatal error during build: {str(e)}")
            session.emit("status", {"message": f"❌ Fatal Error: {str(e)}", "progress": 100})
            self._emit_complete(session, False, "unknown", project_root, 0, 0, [str(e)], start_time)

    # ── helpers ───────────────────────────────────────────────────────────

    def _emit_complete(self, session, success, name, root, files, attempts, errors, start):
        duration = time.time() - start
        response = BuildResponse(
            success=success, projectName=name, projectRoot=root,
            filesGenerated=files, debugAttempts=attempts,
            errors=errors, duration=duration,
        )
        session.emit("complete", response.model_dump())
        session.active = False

    @staticmethod
    def _format_sse(event: dict) -> str:
        return f"data: {json.dumps(event)}\n\n"