import subprocess
import os
import time
import logging

logger = logging.getLogger(__name__)

class ProcessRunner:
    @staticmethod
    def run_npm_install(project_root: str) -> dict:
        try:
            logger.info(f"Running npm install in {project_root}")
            result = subprocess.run(
                ["npm", "install"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=120
            )
            return {
                "exitCode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timedOut": False
            }
        except subprocess.TimeoutExpired as e:
            return {
                "exitCode": None,
                "stdout": e.stdout.decode() if e.stdout else "",
                "stderr": e.stderr.decode() if e.stderr else "",
                "timedOut": True
            }
        except Exception as e:
            return {
                "exitCode": -1,
                "stdout": "",
                "stderr": str(e),
                "timedOut": False
            }

    @staticmethod
    def run_node(project_root: str, script: str, timeout_ms: int = 10000) -> dict:
        try:
            logger.info(f"Running node {script} in {project_root} (timeout: {timeout_ms}ms)")
            # Node processes that start a server run indefinitely.
            # We start it, wait for timeout_ms, then terminate it.
            # If it dies before timeout, we capture the error.
            
            proc = subprocess.Popen(
                ["node", script],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PORT": "3000"}
            )
            
            try:
                # Wait for the specified timeout
                stdout, stderr = proc.communicate(timeout=timeout_ms / 1000.0)
                exit_code = proc.returncode
                timed_out = False
            except subprocess.TimeoutExpired:
                # The process is still running after timeout -> success for a server!
                proc.terminate()
                stdout, stderr = proc.communicate(timeout=2.0)
                exit_code = None
                timed_out = True

            return {
                "exitCode": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "timedOut": timed_out
            }

        except Exception as e:
            return {
                "exitCode": -1,
                "stdout": "",
                "stderr": str(e),
                "timedOut": False
            }

    @staticmethod
    def is_server_start_success(result: dict) -> bool:
        # If it timed out, it means it stayed alive for the whole duration, which is good for a server
        if result["timedOut"]:
            return True
            
        # Check stdout for typical success messages
        success_indicators = [
            "server running",
            "listening on port",
            "connected to mongodb"
        ]
        
        lower_stdout = result["stdout"].lower()
        if any(indicator in lower_stdout for indicator in success_indicators) and result["exitCode"] == 0:
            return True
            
        return False
