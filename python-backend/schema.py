from typing import List, Optional, Dict
from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# Planner Agent Schemas
# ─────────────────────────────────────────────────────────────────────────────

class EntityField(BaseModel):
    name: str
    type: str
    required: bool
    unique: Optional[bool] = False
    default: Optional[str] = None

class Entity(BaseModel):
    name: str
    fields: List[EntityField]
    description: Optional[str] = None

class Feature(BaseModel):
    name: str
    description: str

class FileSpec(BaseModel):
    path: str
    description: str

class PlannerOutput(BaseModel):
    projectName: str
    entities: List[Entity]
    features: List[Feature]
    files: List[FileSpec]

# ─────────────────────────────────────────────────────────────────────────────
# Code Generator Agent Schemas
# ─────────────────────────────────────────────────────────────────────────────

class GeneratedFile(BaseModel):
    path: str
    content: str
    status: str = 'pending' # 'pending', 'generated', 'error', 'fixed'
    errorMessage: Optional[str] = None

class CodeGenContext(BaseModel):
    projectName: str
    entities: List[Entity]
    features: List[Feature]
    allFiles: List[FileSpec]
    existingFileContents: Dict[str, str]

class CodeFixRequest(BaseModel):
    """
    Used when the Orchestrator sends the Critic Agent's fixing strategy
    to the Code Agent.

    The Code Agent should use this to generate corrected code.
    """
    file_path: str
    original_content: str
    error_log: str
    critic_strategy: str
    instructions_for_code_agent: str


class CodeFixResult(BaseModel):
    """
    Output from Code Agent after applying the Critic Agent's strategy.
    """
    file: str
    fixed_code: str
    explanation: Optional[str] = None
# ─────────────────────────────────────────────────────────────────────────────
# Debug Agent Schemas
# ─────────────────────────────────────────────────────────────────────────────

class RuntimeErrorInfo(BaseModel):
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    stack: str
    type: str # 'syntax', 'runtime', 'module', 'connection', 'unknown'

class FixSuggestion(BaseModel):
    file: str
    issue: str
    fix: str
    regenerate: bool

class DebugResult(BaseModel):
    success: bool
    errors: List[RuntimeErrorInfo] = Field(default_factory=list)
    suggestions: List[FixSuggestion] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exitCode: Optional[int] = None



# ─────────────────────────────────────────────────────────────────────────────
# Episodic Memory Schemas
# ─────────────────────────────────────────────────────────────────────────────

class MemoryCase(BaseModel):
    """
    One error-to-success memory record.

    For PP1 this will be stored in JSON.
    Final version can move this into ChromaDB / FAISS.
    """
    error_pattern: str
    root_cause: str
    fix_strategy: str
    affected_files: List[str] = Field(default_factory=list)
    success: bool = True
    usage_count: int = 0
    created_at: Optional[str] = None


class MemoryMatch(BaseModel):
    """
    One retrieved similar memory case.
    """
    case: MemoryCase
    score: float = 0.0
    matched_pattern: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Critic Agent Schemas
# ─────────────────────────────────────────────────────────────────────────────

class CriticStrategy(BaseModel):
    """
    Output from Diagnostic Critic Agent.

    Important:
    The Critic Agent must NOT generate fixed source code.
    It only diagnoses the error and gives a fixing strategy.
    """
    root_cause: str
    affected_files: List[str] = Field(default_factory=list)
    fixing_strategy: str
    instructions_for_code_agent: str
    confidence: float = 0.0


class CriticInput(BaseModel):
    """
    Input given to the Critic Agent by the Orchestrator.
    """
    error_log: str
    errors: List[RuntimeErrorInfo] = Field(default_factory=list)
    memory_matches: List[MemoryMatch] = Field(default_factory=list)
    file_list: List[str] = Field(default_factory=list)
    attempt: int = 1


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration / Retry Tracking Schemas
# ─────────────────────────────────────────────────────────────────────────────

class OrchestrationAttempt(BaseModel):
    """
    Used to record each retry attempt for PP1 evidence and debug-report.md.
    """
    attempt: int
    state: str
    success: bool = False
    errors: List[str] = Field(default_factory=list)
    memory_matches_count: int = 0
    critic_strategy: Optional[CriticStrategy] = None
    fixed_files: List[str] = Field(default_factory=list)





# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator Request / Response
# ─────────────────────────────────────────────────────────────────────────────

class BuildRequest(BaseModel):
    prompt: str
    workspace_uri: str
    planner_model: Optional[str] = None
    codegen_model: Optional[str] = None
    debug_model: Optional[str] = None
    critic_model: Optional[str] = None

class BuildResponse(BaseModel):
    success: bool
    projectName: str
    projectRoot: str
    filesGenerated: int
    debugAttempts: int
    errors: List[str]
    duration: float
