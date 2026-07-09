"""
AI Backend Builder — Critic Agent Prompt Templates

The Critic Agent does NOT generate fixed code.
It only creates a fixing strategy for the Code Generation Agent.
"""

from typing import Dict, List, Optional
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
8. NEVER tell the Code Agent to "verify if a file exists" or "scan the directory". YOU must scan the PROJECT FILE LIST provided to you and explicitly provide the EXACT correct file path or import statement in your instructions.

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
- CRITICAL RULE: First check if the missing module name matches ANY file in the PROJECT FILE LIST.
- If it does NOT match (e.g., error says '../controllers/MenuItemController.js' but only '../controllers/menuItemController.js' exists):
  - YOU must find the closest matching file name in the PROJECT FILE LIST.
  - The fix is to UPDATE THE IMPORT in the file that contains the wrong import (usually a route file or app.js).
  - The affected_files must be the FILE CONTAINING THE BAD IMPORT, NOT the missing module itself.
  - instructions_for_code_agent must explicitly provide the EXACT string replacement. NEVER say "Verify if the file exists". ALWAYS say "Change the import from 'X' to 'Y' because Y is the actual existing file."
- If the missing module IS an npm package: add it to package.json.
- If the missing module is a legitimately missing local file: create it.

2. SyntaxError: Named export not found (e.g., from 'express-validator')
- express-validator is a CommonJS module and CANNOT be imported using named ESM imports.
- Strategy: Replace `import { validate } from 'express-validator'` with `import pkg from 'express-validator'; const { validate } = pkg;`
- OR for simple CRUD: remove express-validator usage entirely and validate inline.
- affected_files: the file containing the bad import.

3. SyntaxError
- Usually caused by invalid JavaScript syntax, missing brackets, invalid import/export, or malformed JSON.
- Strategy: patch only the affected syntax area.

4. ReferenceError
- Usually caused by using a variable or module that was not imported or declared.
- Strategy: add the missing import or correct the variable name.

5. TypeError
- Usually caused by wrong function export/import, calling undefined value, or incorrect middleware usage.
- Strategy: verify export/import style and object usage.

6. Express routing errors
- Usually caused by unregistered routes, wrong route exports, or middleware order issues.
- Strategy: check route registration in app.js and route module exports.

7. EADDRINUSE
- Usually caused by port already being used.
- Strategy: change the port or stop the existing process.
"""


def build_critic_prompt(
    errors: List[RuntimeErrorInfo],
    stderr: str,
    stdout: str,
    memory_matches: List[MemoryMatch],
    file_list: List[str],
    attempt: int,
    file_contents: Optional[Dict[str, str]] = None,
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
    parts.append("These are the ACTUAL files that exist in the project. Use this to determine correct import targets.")
    for file in file_list:
        parts.append(f"- {file}")
    parts.append("")

    if file_contents:
        key_files = ["app.js", "routes/carRoutes.js", "routes/taskRoutes.js", "routes/productRoutes.js"]
        for key_file in key_files:
            if key_file in file_contents:
                parts.append(f"## CONTENT OF {key_file} (for import analysis)")
                parts.append(file_contents[key_file][:2000])
                parts.append("")
                break  # Only include the most relevant entry file

    parts.append("## ENTITY ANALYSIS")
    parts.append("If the error is MODULE_NOT_FOUND for a local model file:")
    parts.append("1. Identify the WRONG model name mentioned in the error.")
    parts.append("2. Look in the PROJECT FILE LIST for model files that DO exist (files in models/ directory).")
    parts.append("3. If the error mentions '../models/Task.js' but only '../models/Car.js' exists:")
    parts.append("   - The app.js (or route file) imported the WRONG entity name.")
    parts.append("   - affected_files MUST be the file containing the wrong import (app.js).")
    parts.append("   - instructions must explicitly say to replace the wrong entity name with the correct one.")
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

