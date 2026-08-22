import logging
import os
import time
from typing import Dict, List, Optional, TypedDict, Any
from langgraph.graph import StateGraph, END

from schema import (
    BuildRequest,
    CodeGenContext,
    CriticStrategy,
    RuntimeErrorInfo,
    OrchestrationAttempt,
    TestResults,
)
from services.research_artifact_recorder import ResearchArtifactRecorder

logger = logging.getLogger(__name__)

class OrchestrationState(TypedDict):
    request: BuildRequest
    session: Any  # BuildSession
    agent: Any  # OrchestratorAgent
    start_time: float
    project_root: str
    
    # State accumulated
    project_path: str
    session_project_path: Optional[str]
    plan: Optional[Any]  # PlannerOutput
    artifact_recorder: Optional[ResearchArtifactRecorder]
    plan_rejection_count: int
    plan_action: Optional[str]
    
    existing_contents: Dict[str, str]
    context: Optional[CodeGenContext]
    files_generated: int
    
    # Debug / Critic state
    debug_attempt_count: int
    latest_errors: List[RuntimeErrorInfo]
    latest_stderr: str
    latest_stdout: str
    attempts: List[OrchestrationAttempt]
    memory_matches: List[Any]
    critic_strategy: Optional[CriticStrategy]
    previous_failed_errors: Optional[List[RuntimeErrorInfo]]
    previous_critic_strategy: Optional[CriticStrategy]
    previous_fixed_files: List[str]
    final_test_results: Optional[TestResults]
    
    error_message: Optional[str]
    final_outcome: Optional[str]
    pre_debug_action: Optional[str]
    fix_action: Optional[str]
    exhausted_action: Optional[str]

def status(session, message: str, progress: int, state: str):
    logger.info(f"STATE -> {state}: {message}")
    session.emit("status", {"message": message, "progress": progress, "state": state})

# ─────────────────────────────────────────────────────────────────────────────
# Graph Nodes
# ─────────────────────────────────────────────────────────────────────────────

def node_plan_project(state: OrchestrationState) -> dict:
    session = state["session"]
    agent = state["agent"]
    request = state["request"]
    
    status(session, "🧠 STATE → PLANNING: Planning project architecture...", 5, "PLANNING")
    
    plan = agent._retry_operation(
        operation_name="Planner Agent model call",
        operation=lambda: agent.planner_agent.execute(request.prompt, cancel_token=lambda: not session.active),
        session=session,
        progress=5,
        state="PLANNING_RETRY",
    )
    
    project_path = os.path.join(state["project_root"], plan.projectName)
    artifact_recorder = ResearchArtifactRecorder(state["project_root"], plan.projectName)
    artifact_recorder.record_planner(
        user_prompt=request.prompt,
        trace=agent.planner_agent.last_request_trace,
        planner_output=plan.model_dump(),
    )
    
    logger.info("📋 Plan generated: %s", plan.projectName)
    status(session, f"📋 STATE → PLAN_READY: Plan ready with {len(plan.files)} files", 10, "PLAN_READY")
    
    return {
        "plan": plan,
        "project_path": project_path,
        "artifact_recorder": artifact_recorder
    }

def node_await_plan_approval(state: OrchestrationState) -> dict:
    session = state["session"]
    plan = state["plan"]
    rejection_count = state.get("plan_rejection_count", 0)
    
    options = ["approve", "reject", "cancel"] if rejection_count < 2 else ["approve", "cancel"]
    msg = f"📋 Revised Plan: {plan.projectName}" if rejection_count > 0 else f"📋 Plan ready: {plan.projectName}"
    
    action = session.wait_for_approval(
        "plan",
        {
            "message": msg,
            "projectName": plan.projectName,
            "entities": [entity.model_dump() for entity in plan.entities],
            "features": [feature.model_dump() for feature in plan.features],
            "files": [file_spec.model_dump() for file_spec in plan.files],
            "options": options,
            "revisionAttempt": rejection_count
        },
    )
    return {"plan_action": action}

def node_replan_project(state: OrchestrationState) -> dict:
    session = state["session"]
    agent = state["agent"]
    request = state["request"]
    rejection_count = state.get("plan_rejection_count", 0) + 1
    
    user_feedback = session.approval_data.get("feedback", "") if session.approval_data else ""
    status(session, "🔄 STATE → RE_PLANNING: Re-planning with user feedback...", 10, "RE_PLANNING")
    
    if user_feedback:
        updated_prompt = f"{request.prompt}\n\nUser feedback on previous plan:\n{user_feedback}\n\nRegenerate the backend project plan by applying this feedback. Keep the output strictly valid JSON."
    else:
        updated_prompt = f"{request.prompt}\n\nThe user rejected the previous plan but did not provide detailed feedback. Regenerate a simpler and clearer backend project plan. Keep the output strictly valid JSON."
        
    plan = agent._retry_operation(
        operation_name=f"Planner Agent re-plan attempt {rejection_count}",
        operation=lambda: agent.planner_agent.execute(updated_prompt, cancel_token=lambda: not session.active),
        session=session,
        progress=10,
        state="RE_PLANNING_RETRY",
    )
    
    status(session, f"📋 STATE → REVISED_PLAN_READY: Revised plan ready with {len(plan.files)} files", 11, "REVISED_PLAN_READY")
    
    return {
        "plan": plan,
        "plan_rejection_count": rejection_count,
        "project_path": os.path.join(state["project_root"], plan.projectName)
    }

def node_create_structure(state: OrchestrationState) -> dict:
    session = state["session"]
    agent = state["agent"]
    plan = state["plan"]
    
    status(session, "📁 STATE → CREATING_STRUCTURE: Creating project structure...", 15, "CREATING_STRUCTURE")
    
    session_project_path = state.get("session_project_path")
    if session_project_path is None:
        session_project_path = agent._create_fresh_project_path(state["project_root"], plan.projectName)
        
    project_path = session_project_path
    os.makedirs(project_path, exist_ok=True)
    
    for file_spec in plan.files:
        dirname = os.path.dirname(os.path.join(project_path, file_spec.path))
        if dirname:
            os.makedirs(dirname, exist_ok=True)
            
    return {"session_project_path": session_project_path, "project_path": project_path}

def node_generate_files(state: OrchestrationState) -> dict:
    session = state["session"]
    agent = state["agent"]
    plan = state["plan"]
    project_path = state["project_path"]
    existing_contents = state["existing_contents"]
    files_generated = state["files_generated"]
    artifact_recorder = state["artifact_recorder"]
    
    status(session, "⚙️ STATE → GENERATING: Generating backend files...", 20, "GENERATING")
    
    total_files = max(len(plan.files), 1)
    sorted_files = sorted(plan.files, key=lambda f: agent._file_priority(f.path))
    
    context = CodeGenContext(
        projectName=plan.projectName,
        entities=plan.entities,
        features=plan.features,
        allFiles=plan.files,
        existingFileContents=existing_contents,
    )
    
    for i, file_spec in enumerate(sorted_files):
        if not session.active: return {"error_message": "Session inactive during generation"}
        
        progress_pct = 20 + int((i / total_files) * 45)
        status(session, f"⚙️ STATE → GENERATING: ({i + 1}/{total_files}) {file_spec.path}", progress_pct, "GENERATING")
        
        generated = None
        file_generation_success = False
        
        for file_attempt in range(1, 4):
            status(session, f"⚙️ STATE → GENERATING: {file_spec.path} generation attempt {file_attempt}/3", progress_pct, "GENERATING_FILE_RETRY")
            try:
                generated = agent._retry_operation(
                    operation_name=f"CodeGen Agent generate {file_spec.path}",
                    operation=lambda: agent.codegen_agent.execute(file_spec, context, cancel_token=lambda: not session.active),
                    session=session,
                    progress=progress_pct,
                    state="GENERATING_MODEL_RETRY",
                    allow_user_fallback=False,
                )
            except Exception as exc:
                generated = None
                
            if generated and generated.status == "generated" and generated.content:
                agent._write_project_file(project_path, generated.path, generated.content)
                existing_contents[generated.path] = generated.content
                context.existingFileContents = existing_contents
                files_generated += 1
                file_generation_success = True
                
                session.emit("file_generated", {"path": generated.path, "status": "success", "chars": len(generated.content), "index": i + 1, "total": total_files, "content": generated.content})
                status(session, f"✅ Generated file: {generated.path}", progress_pct, "FILE_GENERATED")
                break
            if file_attempt < 3: time.sleep(2)
            
        if not file_generation_success:
            action = session.wait_for_approval("codegen_error", {"message": f"❌ Failed generating {file_spec.path}", "file": file_spec.path, "options": ["skip", "cancel"]})
            if action == "cancel": return {"error_message": f"Cancelled during generation of {file_spec.path}"}
            
    return {"existing_contents": existing_contents, "context": context, "files_generated": files_generated}

def node_consistency_check(state: OrchestrationState) -> dict:
    session = state["session"]
    agent = state["agent"]
    project_path = state["project_path"]
    
    status(session, "🧾 STATE → CONSISTENCY_CHECK: Checking imports and package dependencies...", 68, "CONSISTENCY_CHECK")
    
    validation_result = agent._retry_operation(
        operation_name="Project consistency validation",
        operation=lambda: agent.project_validator.validate(project_path),
        max_attempts=2,
        session=session,
        progress=68,
        state="CONSISTENCY_CHECK_RETRY",
    )
    
    if validation_result["missing_dependencies"]:
        added = agent._retry_operation(
            operation_name="Sync missing package dependencies",
            operation=lambda: agent.project_validator.sync_package_dependencies(project_path, validation_result["missing_dependencies"]),
            max_attempts=2,
            session=session,
            progress=69,
            state="DEPENDENCY_SYNC_RETRY",
        )
        if added:
            status(session, f"📦 Added missing dependencies: {', '.join(added)}", 69, "DEPENDENCY_SYNC")
            
    action = session.wait_for_approval("pre_debug", {"message": "Proceed to Debugging Phase?", "options": ["approve", "cancel"]})
    return {"pre_debug_action": action}

def node_run_debug(state: OrchestrationState) -> dict:
    session = state["session"]
    agent = state["agent"]
    project_path = state["project_path"]
    
    debug_attempt_count = state["debug_attempt_count"] + 1
    status(session, f"🧪 STATE → TESTING: Running Debug Agent (Attempt {debug_attempt_count}/{agent.max_retries})...", 75, "TESTING")
    
    debug_result = agent.debug_agent.execute(project_path)
    
    if debug_result.success:
        status(session, "✅ STATE → SUCCESS: Debug Agent verified the project successfully.", 100, "SUCCESS")
        return {"debug_attempt_count": debug_attempt_count, "final_test_results": getattr(debug_result, 'testResults', None), "final_outcome": "success"}
        
    status(session, f"🐞 STATE → DEBUG_FAILED: Tests failed. {len(debug_result.errors)} errors found.", 80, "DEBUG_FAILED")
    
    if debug_attempt_count >= agent.max_retries:
        return {"debug_attempt_count": debug_attempt_count, "final_outcome": "exhausted", "latest_errors": debug_result.errors, "latest_stderr": debug_result.stderr}
        
    return {"debug_attempt_count": debug_attempt_count, "latest_errors": debug_result.errors, "latest_stderr": debug_result.stderr, "latest_stdout": debug_result.stdout, "final_outcome": "needs_fix"}

def node_retrieve_memory(state: OrchestrationState) -> dict:
    agent = state["agent"]
    matches = agent.episodic_memory.retrieve_similar(state["latest_errors"], state["latest_stderr"])
    return {"memory_matches": matches}

def node_run_critic(state: OrchestrationState) -> dict:
    agent = state["agent"]
    session = state["session"]
    
    status(session, "🕵️ STATE → CRITIC_ANALYSIS: Critic Agent analyzing errors...", 82, "CRITIC_ANALYSIS")
    strategy = agent.critic_agent.execute(
        errors=state["latest_errors"],
        stderr=state["latest_stderr"],
        stdout=state["latest_stdout"],
        memory_matches=state["memory_matches"],
        file_list=list(state["existing_contents"].keys()),
        attempt=state["debug_attempt_count"],
        file_contents=state["existing_contents"],
    )
    
    action = session.wait_for_approval("debug_fix", {
        "message": "Critic Agent identified a fix strategy.",
        "strategy": strategy.fixing_strategy,
        "instructions": strategy.instructions_for_code_agent,
        "affectedFiles": strategy.affected_files,
        "errors": [f"{e.file}: {e.message}" for e in state["latest_errors"]],
        "options": ["approve", "skip", "cancel"]
    })
    
    return {"critic_strategy": strategy, "fix_action": action}

def node_apply_fixes(state: OrchestrationState) -> dict:
    agent = state["agent"]
    session = state["session"]
    
    status(session, "🔧 STATE → CODE_FIXING: CodeGen Agent applying fixes...", 85, "CODE_FIXING")
    files_to_fix = agent._choose_affected_files(state["critic_strategy"], state["latest_errors"], state["existing_contents"], state["project_path"])
    
    existing_contents = dict(state["existing_contents"])
    for f in files_to_fix:
        original = existing_contents.get(f, agent._read_project_file(state["project_path"], f))
        fixed = agent.codegen_agent.fix_file_with_strategy(
            file_path=f, original_content=original, error_log=agent._build_error_log(state["latest_errors"], state["latest_stderr"]),
            critic_strategy=state["critic_strategy"].fixing_strategy, instructions_for_code_agent=state["critic_strategy"].instructions_for_code_agent,
            file_list=list(existing_contents.keys())
        )
        if fixed and fixed.status == "fixed" and fixed.content:
            agent._write_project_file(state["project_path"], fixed.path, fixed.content)
            existing_contents[fixed.path] = fixed.content
            
    return {"existing_contents": existing_contents}

def node_cancelled(state: OrchestrationState) -> dict:
    state["agent"]._emit_complete(state["session"], False, state["plan"].projectName if state.get("plan") else "Project", state["project_path"], state["files_generated"], state["debug_attempt_count"], ["Build Cancelled"], state["start_time"])
    return {}

def node_success(state: OrchestrationState) -> dict:
    state["agent"]._emit_complete(state["session"], True, state["plan"].projectName, state["project_path"], state["files_generated"], state["debug_attempt_count"], [], state["start_time"])
    return {}

def node_exhausted(state: OrchestrationState) -> dict:
    session = state["session"]
    action = session.wait_for_approval("debug_exhausted", {
        "message": f"Testing failed after {state['debug_attempt_count']} retries.",
        "errors": [e.message for e in state["latest_errors"]]
    })
    
    if action == "retry":
        state["agent"].max_retries += 2
        
    return {"exhausted_action": action}

def node_end_failed(state: OrchestrationState) -> dict:
    state["agent"]._emit_complete(state["session"], False, state["plan"].projectName if state.get("plan") else "Project", state["project_path"], state["files_generated"], state["debug_attempt_count"], [e.message for e in state["latest_errors"]], state["start_time"])
    return {}

# ─────────────────────────────────────────────────────────────────────────────
# Edge Conditionals
# ─────────────────────────────────────────────────────────────────────────────

def edge_after_plan_approval(state: OrchestrationState) -> str:
    action = state.get("plan_action")
    if action == "cancel": return "cancelled"
    elif action == "reject":
        return "replan" if state.get("plan_rejection_count", 0) < 2 else "cancelled"
    return "create_structure"

def edge_after_generate(state: OrchestrationState) -> str:
    if state.get("error_message"): return "cancelled"
    return "consistency_check"

def edge_after_consistency(state: OrchestrationState) -> str:
    if state.get("pre_debug_action") == "cancel": return "cancelled"
    return "run_debug"

def edge_after_debug(state: OrchestrationState) -> str:
    if state["final_outcome"] == "success": return "success"
    if state["final_outcome"] == "exhausted": return "exhausted"
    return "retrieve_memory"

def edge_after_critic(state: OrchestrationState) -> str:
    if state["fix_action"] == "cancel": return "cancelled"
    if state["fix_action"] == "skip": return "run_debug"
    return "apply_fixes"

def edge_after_exhausted(state: OrchestrationState) -> str:
    if state.get("exhausted_action") == "retry":
        return "retrieve_memory"
    return "end_failed"

# ─────────────────────────────────────────────────────────────────────────────
# Graph Builder
# ─────────────────────────────────────────────────────────────────────────────

def build_orchestrator_graph() -> StateGraph:
    builder = StateGraph(OrchestrationState)
    
    # Add Nodes
    builder.add_node("plan_project", node_plan_project)
    builder.add_node("await_plan_approval", node_await_plan_approval)
    builder.add_node("replan_project", node_replan_project)
    builder.add_node("create_structure", node_create_structure)
    builder.add_node("generate_files", node_generate_files)
    builder.add_node("consistency_check", node_consistency_check)
    builder.add_node("run_debug", node_run_debug)
    builder.add_node("retrieve_memory", node_retrieve_memory)
    builder.add_node("run_critic", node_run_critic)
    builder.add_node("apply_fixes", node_apply_fixes)
    
    # Terminal nodes
    builder.add_node("cancelled", node_cancelled)
    builder.add_node("success", node_success)
    builder.add_node("end_failed", node_end_failed)
    builder.add_node("exhausted", node_exhausted)
    
    # Add Edges
    builder.set_entry_point("plan_project")
    builder.add_edge("plan_project", "await_plan_approval")
    builder.add_conditional_edges("await_plan_approval", edge_after_plan_approval, {
        "create_structure": "create_structure",
        "replan": "replan_project",
        "cancelled": "cancelled"
    })
    builder.add_edge("replan_project", "await_plan_approval")
    builder.add_edge("create_structure", "generate_files")
    
    builder.add_conditional_edges("generate_files", edge_after_generate, {
        "consistency_check": "consistency_check",
        "cancelled": "cancelled"
    })
    
    builder.add_conditional_edges("consistency_check", edge_after_consistency, {
        "run_debug": "run_debug",
        "cancelled": "cancelled"
    })
    
    builder.add_conditional_edges("run_debug", edge_after_debug, {
        "success": "success",
        "exhausted": "exhausted",
        "retrieve_memory": "retrieve_memory"
    })
    
    builder.add_edge("retrieve_memory", "run_critic")
    
    builder.add_conditional_edges("run_critic", edge_after_critic, {
        "apply_fixes": "apply_fixes",
        "run_debug": "run_debug",
        "cancelled": "cancelled"
    })
    
    builder.add_edge("apply_fixes", "run_debug")
    
    builder.add_conditional_edges("exhausted", edge_after_exhausted, {
        "retrieve_memory": "retrieve_memory",
        "end_failed": "end_failed"
    })
    
    builder.add_edge("end_failed", END)
    builder.add_edge("cancelled", END)
    builder.add_edge("success", END)
    
    return builder.compile()
