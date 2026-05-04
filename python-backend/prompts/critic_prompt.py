"""
AI Backend Builder — Critic Agent Prompt Templates

The Critic Agent does NOT generate fixed code.
It only creates a fixing strategy for the Code Generation Agent.
"""

from typing import List
from schema import RuntimeErrorInfo, MemoryMatch


CRITIC_SYSTEM_PROMPT = """
You are a Diagnostic Critic Agent in a multi-agent backend code generation system.

Your job is to analyze runtime errors and generate a fixing strategy for the Code Generation Agent.

IMPORTANT RULES:
1. Do NOT write fixed source code.
2. Do NOT return complete corrected files.
3. Do NOT regenerate files.
4. Only diagnose the error and produce a repair strategy.
5. Use episodic memory cases if they are relevant.
6. Suggest minimal and targeted changes only.
7. Output ONLY valid JSON. No markdown. No explanations outside JSON.

OUTPUT JSON SCHEMA:
{
  "root_cause": "string",
  "affected_files": ["string"],
  "fixing_strategy": "string",
  "instructions_for_code_agent": "string",
  "confidence": 0.0
}

COMMON NODE.JS ERROR PATTERNS:

1. Cannot find module / MODULE_NOT_FOUND
- Usually caused by wrong import path, missing file, missing dependency, or filename mismatch.
- Strategy: check actual generated file names and update the import path.

2. SyntaxError
- Usually caused by invalid JavaScript syntax, missing brackets, invalid import/export, or malformed JSON.
- Strategy: patch only the affected syntax area.

3. ReferenceError
- Usually caused by using a variable or module that was not imported or declared.
- Strategy: add the missing import or correct the variable name.

4. TypeError
- Usually caused by wrong function export/import, calling undefined value, or incorrect middleware usage.
- Strategy: verify export/import style and object usage.

5. Express routing errors
- Usually caused by unregistered routes, wrong route exports, or middleware order issues.
- Strategy: check route registration in app.js and route module exports.

6. EADDRINUSE
- Usually caused by port already being used.
- Strategy: change the port or stop the existing process.
"""


def build_critic_prompt(
    errors: List[RuntimeErrorInfo],
    stderr: str,
    stdout: str,
    memory_matches: List[MemoryMatch],
    file_list: List[str],
    attempt: int
) -> str:
    parts = []

    parts.append("Analyze the following backend runtime failure.")
    parts.append("Your task is to create a fixing strategy for the Code Agent.")
    parts.append("Do NOT write fixed code.\n")

    parts.append("## RETRY ATTEMPT")
    parts.append(str(attempt))
    parts.append("")

    parts.append("## PARSED ERRORS")
    if errors:
        for i, err in enumerate(errors, start=1):
            parts.append(f"### Error {i}")
            parts.append(f"Type: {err.type}")
            parts.append(f"Message: {err.message}")

            if err.file:
                parts.append(f"File: {err.file}")

            if err.line:
                parts.append(f"Line: {err.line}")

            if err.column:
                parts.append(f"Column: {err.column}")

            parts.append("Stack:")
            parts.append(err.stack[:1500])
            parts.append("")
    else:
        parts.append("No parsed errors were available.")
        parts.append("")

    if stderr:
        parts.append("## RAW STDERR")
        parts.append(stderr[:2000])
        parts.append("")

    if stdout:
        parts.append("## RAW STDOUT")
        parts.append(stdout[:800])
        parts.append("")

    parts.append("## PROJECT FILE LIST")
    for file in file_list:
        parts.append(f"- {file}")
    parts.append("")

    parts.append("## SIMILAR EPISODIC MEMORY CASES")
    if memory_matches:
        for i, match in enumerate(memory_matches, start=1):
            case = match.case

            parts.append(f"### Memory Case {i}")
            parts.append(f"Matched Pattern: {match.matched_pattern}")
            parts.append(f"Similarity Score: {match.score}")
            parts.append(f"Error Pattern: {case.error_pattern}")
            parts.append(f"Root Cause: {case.root_cause}")
            parts.append(f"Previous Fix Strategy: {case.fix_strategy}")
            parts.append(f"Affected Files: {case.affected_files}")
            parts.append("")
    else:
        parts.append("No similar memory cases found.")
        parts.append("")

    parts.append("""
Return ONLY valid JSON using this exact schema:
{
  "root_cause": "brief root cause",
  "affected_files": ["relative/path.js"],
  "fixing_strategy": "high level repair strategy",
  "instructions_for_code_agent": "specific instruction for the Code Agent, but no source code",
  "confidence": 0.0
}
""")

    return "\n".join(parts)


def build_critic_retry_prompt(invalid_response: str) -> str:
    return f"""
Your previous response was not valid JSON.

Previous response:
{invalid_response[:2000]}

Return ONLY one valid JSON object using this exact schema:
{{
  "root_cause": "brief root cause",
  "affected_files": ["relative/path.js"],
  "fixing_strategy": "high level repair strategy",
  "instructions_for_code_agent": "specific instruction for the Code Agent, but no source code",
  "confidence": 0.0
}}

Do NOT include markdown.
Do NOT include source code.
Do NOT include explanations outside JSON.
"""