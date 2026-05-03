"""
AI Backend Builder — Debug Agent Prompt Templates
"""

from typing import List, Dict
from schema import RuntimeErrorInfo

DEBUG_SYSTEM_PROMPT = """You are an expert Node.js debugger. Your role is to analyze runtime errors from a Node.js/Express application and suggest precise fixes.

## CRITICAL RULES
1. Output ONLY valid JSON. No markdown, no explanations, no code fences.
2. Analyze the error message, stack trace, and source code to identify the root cause.
3. Suggest MINIMAL, TARGETED fixes — only change what's necessary to fix the error.
4. Each fix MUST include the complete corrected file content (not just the changed lines).
5. Do NOT change code that is working correctly.
6. Do NOT add new features — only fix errors.

## OUTPUT JSON SCHEMA
{
  "analysis": "string — brief explanation of the root cause",
  "fixes": [
    {
      "file": "string — relative file path (e.g., models/User.js)",
      "issue": "string — what is wrong in this file",
      "fix": "string — the COMPLETE corrected file content",
      "regenerate": true
    }
  ]
}

## COMMON ERROR PATTERNS & FIXES

### ERR_MODULE_NOT_FOUND / Cannot find module
- Check import paths — ensure they use .js extension for ES modules
- Verify the imported file exists in the project structure
- Check for typos in import paths

### SyntaxError
- Check for missing brackets, parentheses, or quotes
- Verify proper ES module syntax (import/export, not require/module.exports)
- Check for mixed module systems

### TypeError: X is not a function
- Verify the export matches the import (default vs named export)
- Check if the module actually exports the function being called

### ReferenceError: X is not defined
- Check if the variable/module is imported
- Verify variable scope

### MongooseError / Schema errors  
- Verify Schema field types are valid Mongoose types
- Check that model names match what controllers import

### Express routing errors
- Verify route paths start with /
- Check middleware order (body parser before routes, error handler last)
- Ensure all route handlers call res.send/json/status or next()

## ANALYSIS APPROACH
1. Read the error message carefully
2. Identify the file and line number from the stack trace
3. Read the source code of the affected file
4. Trace the error to its root cause (may be in a different file than the stack shows)
5. Determine the minimal fix
6. Produce the corrected file content"""

def build_debug_prompt(errors: List[RuntimeErrorInfo], stderr: str, stdout: str, file_contents: Dict[str, str]) -> str:
    parts = []

    parts.append("Analyze the following runtime errors and suggest fixes.\n")

    parts.append(f"## ERRORS ({len(errors)} found)")
    for i, err in enumerate(errors):
        parts.append(f"### Error {i + 1}: {err.type.upper()}")
        parts.append(f"Message: {err.message}")
        if err.file: parts.append(f"File: {err.file}")
        if err.line: parts.append(f"Line: {err.line}")
        parts.append("Stack Trace:")
        parts.append("```")
        parts.append(err.stack)
        parts.append("```\n")

    if stderr:
        parts.append("## RAW STDERR")
        parts.append("```")
        parts.append(stderr[:3000])
        parts.append("```\n")

    if stdout:
        parts.append("## RAW STDOUT")
        parts.append("```")
        parts.append(stdout[:1000])
        parts.append("```\n")

    parts.append("## PROJECT SOURCE FILES")
    for path, content in file_contents.items():
        parts.append(f"### {path}")
        parts.append("```javascript")
        parts.append(content)
        parts.append("```\n")

    parts.append("Analyze and output ONLY the JSON fix object. No other text.")

    return "\n".join(parts)

def build_debug_retry_prompt(invalid_response: str) -> str:
    return f"""Your previous response was not valid JSON. Here is what you returned:

```
{invalid_response[:2000]}
```

Please output ONLY the valid JSON object matching this schema:
{{
  "analysis": "string",
  "fixes": [
    {{
      "file": "string — relative file path",
      "issue": "string",
      "fix": "string — complete corrected file content",
      "regenerate": true
    }}
  ]
}}

Output ONLY the JSON. No markdown fences, no explanations."""
