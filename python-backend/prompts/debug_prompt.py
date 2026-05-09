"""
AI Backend Builder — Testing Agent Prompt Templates

This prompt is used by the PP1 DebugAgent / TestingAgent.

Responsibility:
- Generate Jest + Supertest test cases.
- Do NOT fix source code.
- Do NOT generate repair strategy.
- Only generate test code or structured testing diagnostics.
"""

from typing import Dict, List


TESTING_AGENT_SYSTEM_PROMPT = """
You are an autonomous Testing Agent for a multi-agent backend code generation system.

Your role is to generate functional tests for a generated Node.js/Express backend.

IMPORTANT RULES:
1. Do NOT generate fixed source code.
2. Do NOT suggest repair strategies.
3. Do NOT modify application business logic.
4. Generate Jest + Supertest tests only.
5. Assume the app entry file is app.js.
6. Prefer importing the app using: import app from '../app.js';
7. Output ONLY the complete JavaScript test file content.
8. Do NOT include markdown fences.
9. Do NOT include explanations.
10. Keep tests simple and fast for automated sandbox execution.

Test generation goals:
- Validate that the Express app can be imported.
- Validate detected REST endpoints.
- Generate basic GET/POST/PUT/PATCH/DELETE tests.
- Use flexible status-code expectations for PP1 because generated APIs may return 200, 201, 400, 404, or 500.
- The purpose is to detect runtime/import/routing errors, not to fully verify business logic yet.
"""


def build_model_testcase_prompt(
    file_contents: Dict[str, str],
    detected_routes: List[dict],
) -> str:
    parts: List[str] = []

    parts.append("Generate a Jest + Supertest test file for this generated Node.js/Express backend.")
    parts.append("")
    parts.append("IMPORTANT:")
    parts.append("- Output ONLY raw JavaScript test code.")
    parts.append("- No markdown.")
    parts.append("- No explanation.")
    parts.append("- Do not fix app code.")
    parts.append("- Use: import request from 'supertest';")
    parts.append("- Use: import app from '../app.js';")
    parts.append("- Keep tests simple.")
    parts.append("- Use flexible status code checks for PP1.")
    parts.append("")

    parts.append("Detected REST routes:")
    if detected_routes:
        for route in detected_routes:
            parts.append(f"- {route.get('method')} {route.get('path')} from {route.get('file')}")
    else:
        parts.append("- No clear routes detected. Create a basic app import/root test.")
    parts.append("")

    parts.append("Project source files:")
    for path, content in file_contents.items():
        parts.append(f"\n### {path}")
        parts.append("```javascript")
        parts.append(content[:3500])
        parts.append("```")

    parts.append("""
Return ONLY a complete tests/api.test.js file.

Example style:
import request from 'supertest';
import app from '../app.js';

describe('Generated API validation tests', () => {
  test('App should respond with a valid HTTP response', async () => {
    const res = await request(app).get('/');
    expect([200, 201, 204, 400, 401, 403, 404, 500]).toContain(res.statusCode);
  });
});
""")

    return "\n".join(parts)


def build_testing_diagnostic_summary_prompt(stdout: str, stderr: str, exit_code: int) -> str:
    return f"""
Convert this Jest/Supertest execution result into a structured diagnostic summary.

Do NOT suggest fixes.
Do NOT write corrected code.
Only summarize the testing failure.

STDOUT:
{stdout[:3000]}

STDERR:
{stderr[:3000]}

Exit code:
{exit_code}

Return JSON only:
{{
  "stage": "docker_jest_supertest",
  "main_error": "",
  "error_type": "",
  "affected_file": "",
  "summary": ""
}}
"""