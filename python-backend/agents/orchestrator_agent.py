import os
import json
import time
import logging
from typing import Generator

from schema import BuildRequest, BuildResponse, CodeGenContext
from agents.planner_agent import PlannerAgent
from agents.codegen_agent import CodeGenAgent
from agents.debug_agent import DebugAgent

logger = logging.getLogger(__name__)

class OrchestratorAgent:
    def __init__(self, ollama_url: str, models: dict, max_retries: int = 3, use_openrouter: bool = False, openrouter_api_key: str = ""):
        self.ollama_url = ollama_url
        self.planner_agent = PlannerAgent(ollama_url, models.get("planner"), use_openrouter=use_openrouter, openrouter_api_key=openrouter_api_key)
        self.codegen_agent = CodeGenAgent(ollama_url, models.get("codegen"), use_openrouter=use_openrouter, openrouter_api_key=openrouter_api_key)
        self.debug_agent = DebugAgent(ollama_url, models.get("debug"), use_openrouter=use_openrouter, openrouter_api_key=openrouter_api_key)
        self.max_retries = max_retries

    def execute_stream(self, request: BuildRequest) -> Generator[str, None, None]:
        start_time = time.time()
        project_root = request.workspace_uri
        
        def yield_event(event_type: str, data: dict):
            payload = json.dumps({"type": event_type, "data": data})
            return f"data: {payload}\n\n"

        yield yield_event("status", {"message": "🧠 Planning project architecture...", "progress": 5})

        try:
            # Phase 1: Planning
            logger.info("\n" + "="*50)
            logger.info(f"🚀 ORCHESTRATOR PHASE 1: PLANNING")
            logger.info(f"Target Project Root: {project_root}")
            logger.info("="*50)
            
            plan = self.planner_agent.execute(request.prompt)
            project_path = os.path.join(project_root, plan.projectName)
            
            logger.info(f"📋 Plan generated successfully! Project Name: '{plan.projectName}'")
            logger.info(f"📋 Total Entities: {len(plan.entities)} | Total Features: {len(plan.features)} | Total Files to Generate: {len(plan.files)}")
            
            yield yield_event("status", {"message": f"📋 Plan ready: {len(plan.files)} files", "progress": 10})

            # Phase 2: Create Base Structure
            logger.info("\n" + "="*50)
            logger.info(f"📁 ORCHESTRATOR PHASE 2: SCAFFOLDING")
            logger.info(f"Creating project directory at: {project_path}")
            logger.info("="*50)
            
            yield yield_event("status", {"message": "📁 Creating project structure...", "progress": 15})
            os.makedirs(project_path, exist_ok=True)
            for file_spec in plan.files:
                dirname = os.path.dirname(os.path.join(project_path, file_spec.path))
                if dirname:
                    os.makedirs(dirname, exist_ok=True)

            # Phase 3: Generate All Files
            logger.info("\n" + "="*50)
            logger.info(f"⚙️ ORCHESTRATOR PHASE 3: CODE GENERATION")
            logger.info(f"Generating {len(plan.files)} files sequentially...")
            logger.info("="*50)
            total_files = len(plan.files)
            existing_contents = {}

            # Sort files conceptually: models first, then controllers, etc.
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
                existingFileContents=existing_contents
            )

            for i, file_spec in enumerate(sorted_files):
                progress_pct = 20 + int((i / total_files) * 50)
                yield yield_event("status", {"message": f"⚙️ Generating ({i+1}/{total_files}): {file_spec.path}", "progress": progress_pct})
                
                logger.info(f"→ Dispatching Codegen for [{i+1}/{total_files}]: {file_spec.path}")
                generated = self.codegen_agent.execute(file_spec, context)
                
                if generated.status == 'generated' and generated.content:
                    file_path = os.path.join(project_path, file_spec.path)
                    with open(file_path, "w") as f:
                        f.write(generated.content)
                    existing_contents[file_spec.path] = generated.content
                    logger.info(f"✓ Saved file: {file_path}")
                else:
                    logger.error(f"✗ Failed to generate file: {file_spec.path}")

            # Phase 4: Debug Loop
            logger.info("\n" + "="*50)
            logger.info(f"🔍 ORCHESTRATOR PHASE 4: DEBUG LOOP")
            logger.info(f"Max configured retries: {self.max_retries}")
            logger.info("="*50)
            
            debug_success = False
            errors = []
            
            for attempt in range(1, self.max_retries + 1):
                progress_pct = 70 + int((attempt / self.max_retries) * 25)
                yield yield_event("status", {"message": f"🔍 Debug attempt {attempt}/{self.max_retries}...", "progress": progress_pct})
                
                logger.info(f"→ Initiating Debug Cycle [{attempt}/{self.max_retries}]")
                debug_result = self.debug_agent.execute(project_path, existing_contents)
                
                if debug_result.success:
                    logger.info(f"✅ Debug Cycle [{attempt}] succeeded! Zero runtime errors.")
                    debug_success = True
                    break
                    
                errors = [e.message for e in debug_result.errors]
                logger.warning(f"⚠️ Debug Cycle [{attempt}] found {len(errors)} errors.")
                
                if attempt == self.max_retries:
                    logger.error(f"❌ Max debug retries ({self.max_retries}) reached without success.")
                    break
                    
                if debug_result.suggestions:
                    logger.info(f"🔧 Orchestrator applying {len(debug_result.suggestions)} proposed fixes...")
                    yield yield_event("status", {"message": f"🔧 Applying {len(debug_result.suggestions)} fixes...", "progress": progress_pct + 5})
                    
                    for fix in debug_result.suggestions:
                        if fix.fix and len(fix.fix.strip()) > 10:
                            file_path = os.path.join(project_path, fix.file)
                            with open(file_path, "w") as f:
                                f.write(fix.fix)
                            existing_contents[fix.file] = fix.fix
                            logger.info(f"   ✓ Applied direct replacement fix to: {fix.file}")
                        elif fix.regenerate:
                            logger.info(f"   ↻ Initiating full regeneration for: {fix.file} (Issue: {fix.issue})")
                            f_spec = next((f for f in plan.files if f.path == fix.file), None)
                            if f_spec:
                                modified_spec = FileSpec(path=f_spec.path, description=f"{f_spec.description}. FIX NEEDED: {fix.issue}")
                                generated = self.codegen_agent.execute(modified_spec, context)
                                if generated.status == 'generated' and generated.content:
                                    file_path = os.path.join(project_path, fix.file)
                                    with open(file_path, "w") as f:
                                        f.write(generated.content)
                                    existing_contents[fix.file] = generated.content
                                    logger.info(f"   ✓ Regeneration successful for: {fix.file}")
                                else:
                                    logger.error(f"   ✗ Regeneration failed for: {fix.file}")

            duration = time.time() - start_time
            response = BuildResponse(
                success=debug_success,
                projectName=plan.projectName,
                projectRoot=project_path,
                filesGenerated=len(existing_contents),
                debugAttempts=attempt,
                errors=errors,
                duration=duration
            )

            if debug_success:
                yield yield_event("status", {"message": "✅ Build complete!", "progress": 100})
            else:
                yield yield_event("status", {"message": "❌ Build failed after max retries", "progress": 100})

            yield yield_event("complete", response.model_dump())

        except Exception as e:
            logger.error(f"Fatal error during build: {str(e)}")
            duration = time.time() - start_time
            response = BuildResponse(
                success=False,
                projectName="unknown",
                projectRoot=project_root,
                filesGenerated=0,
                debugAttempts=0,
                errors=[str(e)],
                duration=duration
            )
            yield yield_event("status", {"message": f"❌ Fatal Error: {str(e)}", "progress": 100})
            yield yield_event("complete", response.model_dump())