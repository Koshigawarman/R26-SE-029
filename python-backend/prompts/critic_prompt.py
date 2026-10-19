"""
AI Backend Builder — Critic Agent Prompt Templates

The Critic Agent does NOT generate fixed code.
It only creates a fixing strategy for the Code Generation Agent.
"""

from typing import Dict, List, Optional
from schema import RuntimeErrorInfo, MemoryMatch, CriticStrategy


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
9. IF YOU ARE PROVIDED A PREVIOUS FAILED STRATEGY, DO NOT SUGGEST IT AGAIN. Find a different approach or fix the root cause instead of the symptom.

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
    previous_strategy: Optional[CriticStrategy] = None,
    previous_errors: Optional[List[RuntimeErrorInfo]] = None,
) -> str:
    parts = []

    parts.append("Analyze the following backend runtime failure.")
    parts.append("Your task is to create a fixing strategy for the Code Agent.")
    parts.append("Do NOT write fixed code.\n")

    parts.append("## RETRY ATTEMPT")
    parts.append(str(attempt))
    parts.append("")

    if previous_strategy and previous_errors:
        parts.append("## PREVIOUS FAILED ATTEMPT (WARNING: DO NOT REPEAT THIS MISTAKE)")
        parts.append("You (or another Critic) already tried the following strategy on the previous attempt, BUT IT FAILED.")
        parts.append("You MUST provide a DIFFERENT strategy this time.")
        parts.append(f"Previous Root Cause: {previous_strategy.root_cause}")
        parts.append(f"Previous Strategy: {previous_strategy.fixing_strategy}")
        parts.append(f"Previous Instructions: {previous_strategy.instructions_for_code_agent}")
        parts.append("Previous Errors that led to it:")
        for err in previous_errors:
            parts.append(f"- {err.type}: {err.message}")
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
        # Dynamically determine the most relevant files based on the actual errors
        relevant_files = set()
        
        # 1. Always include files directly mentioned in parsed errors
        for err in errors:
            if err.file and err.file in file_contents:
                relevant_files.add(err.file)
                
        # 2. Extract any project file paths mentioned in stderr or stack trace
        combined_err_text = stderr + " " + " ".join(err.stack for err in errors if err.stack)
        for f in file_list:
            if f in combined_err_text and f in file_contents:
                relevant_files.add(f)
                
        # 3. For any route file, also include its related controller/model if available
        for f in list(relevant_files):
            if f.startswith("routes/") and f.endswith("Routes.js"):
                base = f[len("routes/"): -len("Routes.js")]
                ctrl = f"controllers/{base}Controller.js"
                if ctrl in file_contents:
                    relevant_files.add(ctrl)
                model = f"models/{base.capitalize()}.js"
                if model in file_contents:
                    relevant_files.add(model)
            elif f.startswith("controllers/") and f.endswith("Controller.js"):
                base = f[len("controllers/"): -len("Controller.js")]
                route = f"routes/{base}Routes.js"
                if route in file_contents:
                    relevant_files.add(route)
                    
        # 4. If relevant files are few, include app.js
        if "app.js" in file_contents and len(relevant_files) < 4:
            relevant_files.add("app.js")
            
        # Display up to 5 most relevant files
        displayed_count = 0
        for key_file in sorted(relevant_files):
            if displayed_count >= 5:
                break
            code_snippet = file_contents[key_file]
            parts.append(f"## ACTUAL CONTENT OF {key_file}")
            parts.append("```javascript")
            parts.append(code_snippet[:3000])
            parts.append("```")
            parts.append("")
            displayed_count += 1

    parts.append("## CRITICAL SOURCE CODE ACCURACY RULES")
    parts.append("1. The files above contain the REAL, CURRENT source code of the project.")
    parts.append("2. You must inspect the actual lines of code provided above before proposing any fix.")
    parts.append("3. DO NOT hallucinate nonexistent function names, variables, or imports.")
    parts.append("4. NEVER instruct to 'remove' code unless that code actually exists in the provided file content.")
    parts.append("5. Specify the exact line number, function, or statement to be changed.")
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

