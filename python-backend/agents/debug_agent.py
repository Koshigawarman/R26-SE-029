import logging
import re
import json
import requests
from typing import Dict, Any, List

from schema import DebugResult, RuntimeErrorInfo, FixSuggestion
from prompts.debug_prompt import DEBUG_SYSTEM_PROMPT, build_debug_prompt, build_debug_retry_prompt
from services.process_runner import ProcessRunner

logger = logging.getLogger(__name__)

ERROR_PATTERNS = {
    "SYNTAX_ERROR": re.compile(r"SyntaxError: (.*?)(?:\n|$)"),
    "REFERENCE_ERROR": re.compile(r"ReferenceError: (.*?)(?:\n|$)"),
    "TYPE_ERROR": re.compile(r"TypeError: (.*?)(?:\n|$)"),
    "MODULE_NOT_FOUND": re.compile(r"Cannot find module '(.*?)'"),
    "ESM_ERROR": re.compile(r"Warning: To load an ES module.*?|SyntaxError: Cannot use import statement outside a module"),
    "MONGO_CONNECTION": re.compile(r"Mongo(?:Network)?Error: (.*?)(?:\n|$)"),
    "PORT_IN_USE": re.compile(r"EADDRINUSE.*?(\d+)"),
    "STACK_FILE_LINE": re.compile(r"at .*? \((.*?):(\d+):(\d+)\)"),
    "STACK_FILE_LINE_ALT": re.compile(r"at (.*?):(\d+):(\d+)")
}

ENVIRONMENT_ERRORS = [
    re.compile(r"ECONNREFUSED"),
    re.compile(r"EADDRINUSE"),
    re.compile(r"MongoNetworkError")
]

class DebugAgent:
    def __init__(self, ollama_url: str, model: str, debug_timeout: int = 10000):
        self.ollama_url = ollama_url
        self.model = model
        self.debug_timeout = debug_timeout
        self.process_runner = ProcessRunner()

    def execute(self, project_root: str, file_contents: Dict[str, str]) -> DebugResult:
        logger.info(f"Debugging project at: {project_root}")

        logger.info("Running npm install...")
        install_result = self.process_runner.run_npm_install(project_root)

        if install_result["exitCode"] != 0 and 'npm warn' not in install_result["stderr"]:
            has_real_error = any(
                line.startswith('npm error') or line.startswith('npm ERR!') 
                for line in install_result["stderr"].split('\n')
            )
            
            if has_real_error:
                logger.error("npm install failed")
                return DebugResult(
                    success=False,
                    errors=[RuntimeErrorInfo(
                        message='npm install failed: ' + install_result["stderr"][:500],
                        stack=install_result["stderr"],
                        type='runtime'
                    )],
                    suggestions=[FixSuggestion(
                        file='package.json',
                        issue='npm install failed — check dependencies',
                        fix='',
                        regenerate=True
                    )],
                    stdout=install_result["stdout"],
                    stderr=install_result["stderr"],
                    exitCode=install_result["exitCode"]
                )

        logger.info(f"Running node app.js (timeout: {self.debug_timeout}ms)...")
        run_result = self.process_runner.run_node(project_root, 'app.js', self.debug_timeout)

        if self.process_runner.is_server_start_success(run_result):
            logger.info("✓ Application started successfully!")
            return DebugResult(
                success=True,
                errors=[],
                suggestions=[],
                stdout=run_result["stdout"],
                stderr=run_result["stderr"],
                exitCode=run_result["exitCode"]
            )

        errors = self._parse_errors(run_result)
        logger.info(f"Found {len(errors)} errors")

        env_errors = [e for e in errors if self._is_environment_error(e)]
        code_errors = [e for e in errors if not self._is_environment_error(e)]

        if not code_errors and env_errors:
            logger.info("Only environment errors detected (e.g., MongoDB not running) — treating as success")
            return DebugResult(
                success=True,
                errors=env_errors,
                suggestions=[],
                stdout=run_result["stdout"],
                stderr=run_result["stderr"],
                exitCode=run_result["exitCode"]
            )

        if not code_errors:
            logger.warning("Process failed but no clear errors found in output")
            return DebugResult(
                success=False,
                errors=[RuntimeErrorInfo(
                    message=f"Process exited with code {run_result['exitCode']}",
                    stack=run_result["stderr"] or run_result["stdout"],
                    type='unknown'
                )],
                suggestions=[],
                stdout=run_result["stdout"],
                stderr=run_result["stderr"],
                exitCode=run_result["exitCode"]
            )

        logger.info("Analyzing errors with AI...")
        suggestions = self._get_fix_suggestions(code_errors, run_result, file_contents)

        return DebugResult(
            success=False,
            errors=code_errors,
            suggestions=suggestions,
            stdout=run_result["stdout"],
            stderr=run_result["stderr"],
            exitCode=run_result["exitCode"]
        )

    def _parse_errors(self, result: dict) -> List[RuntimeErrorInfo]:
        errors = []
        output = result["stderr"] or result["stdout"]
        
        if not output:
            return errors

        syntax_match = ERROR_PATTERNS["SYNTAX_ERROR"].search(output)
        if syntax_match:
            errors.append(self._build_error('syntax', syntax_match.group(1), output))

        ref_match = ERROR_PATTERNS["REFERENCE_ERROR"].search(output)
        if ref_match:
            errors.append(self._build_error('runtime', ref_match.group(1), output))

        type_match = ERROR_PATTERNS["TYPE_ERROR"].search(output)
        if type_match:
            errors.append(self._build_error('runtime', type_match.group(1), output))

        module_match = ERROR_PATTERNS["MODULE_NOT_FOUND"].search(output)
        if module_match:
            errors.append(self._build_error('module', f"Cannot find module '{module_match.group(1)}'", output))

        esm_match = ERROR_PATTERNS["ESM_ERROR"].search(output)
        if esm_match:
            errors.append(self._build_error('module', esm_match.group(0), output))

        mongo_match = ERROR_PATTERNS["MONGO_CONNECTION"].search(output)
        if mongo_match:
            errors.append(self._build_error('connection', mongo_match.group(0), output))

        port_match = ERROR_PATTERNS["PORT_IN_USE"].search(output)
        if port_match:
            errors.append(self._build_error('connection', f"Port {port_match.group(1)} already in use", output))

        if not errors and result["exitCode"] != 0:
            lines = output.split('\n')
            errors.append(RuntimeErrorInfo(
                message=lines[0] if lines else "Unknown error",
                stack=output,
                type='unknown'
            ))

        return errors

    def _build_error(self, type_str: str, message: str, full_output: str) -> RuntimeErrorInfo:
        error = RuntimeErrorInfo(message=message, stack=full_output, type=type_str)

        file_match = ERROR_PATTERNS["STACK_FILE_LINE"].search(full_output) or ERROR_PATTERNS["STACK_FILE_LINE_ALT"].search(full_output)
        if file_match:
            file_path = file_match.group(1)
            segments = file_path.split('/')
            known_dirs = ['models', 'controllers', 'routes', 'middleware', 'config']
            dir_index = next((i for i, s in enumerate(segments) if s in known_dirs), -1)
            
            if dir_index != -1:
                error.file = '/'.join(segments[dir_index:])
            else:
                error.file = segments[-1]
                
            error.line = int(file_match.group(2))
            error.column = int(file_match.group(3))

        return error

    def _is_environment_error(self, error: RuntimeErrorInfo) -> bool:
        if error.type == 'connection':
            return True
        for pattern in ENVIRONMENT_ERRORS:
            if pattern.search(error.message) or pattern.search(error.stack):
                return True
        return False

    def _get_fix_suggestions(self, errors: List[RuntimeErrorInfo], process_result: dict, file_contents: Dict[str, str]) -> List[FixSuggestion]:
        prompt = build_debug_prompt(errors, process_result["stderr"], process_result["stdout"], file_contents)
        try:
            raw_response = self._query_ollama(prompt, DEBUG_SYSTEM_PROMPT)
            return self._parse_fix_suggestions(raw_response)
        except Exception as e:
            logger.error(f"Failed to get AI fix suggestions: {e}")
            return []

    def _parse_fix_suggestions(self, raw_response: str) -> List[FixSuggestion]:
        try:
            data = self._extract_json(raw_response)
            if "analysis" in data:
                logger.info(f"AI Analysis: {data['analysis']}")
                
            if "fixes" in data and isinstance(data["fixes"], list):
                fixes = []
                for fix in data["fixes"]:
                    if not fix.get("file") or not fix.get("fix"):
                        continue
                    fix["file"] = re.sub(r'^\.?/', '', fix["file"])
                    fix["regenerate"] = fix.get("regenerate", True)
                    fixes.append(FixSuggestion(**fix))
                return fixes
            return []
        except:
            logger.warning("First parse failed, retrying with fix prompt...")
            try:
                retry_prompt = build_debug_retry_prompt(raw_response)
                retry_response = self._query_ollama(retry_prompt, DEBUG_SYSTEM_PROMPT)
                data = self._extract_json(retry_response)
                fixes = []
                for fix in data.get("fixes", []):
                    if not fix.get("file") or not fix.get("fix"):
                        continue
                    fix["file"] = re.sub(r'^\.?/', '', fix["file"])
                    fix["regenerate"] = fix.get("regenerate", True)
                    fixes.append(FixSuggestion(**fix))
                return fixes
            except Exception as e:
                logger.error(f"Failed to parse fix suggestions after retry: {e}")
                return []

    def _extract_json(self, raw_response: str) -> dict:
        json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', raw_response, re.DOTALL | re.IGNORECASE)
        if json_match:
            raw_response = json_match.group(1).strip()
        else:
            start = raw_response.find('{')
            end = raw_response.rfind('}')
            if start != -1 and end != -1:
                raw_response = raw_response[start:end+1]
        return json.loads(raw_response)

    def _query_ollama(self, prompt: str, system_prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "options": {
                "temperature": 0.3,
                "num_predict": 4096,
            }
        }
        resp = requests.post(
            f"{self.ollama_url}/api/generate",
            json=payload,
            timeout=120
        )
        resp.raise_for_status()
        data = resp.json()
        if "response" not in data:
            raise ValueError("Ollama returned no response")
        return data["response"]
