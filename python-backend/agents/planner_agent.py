import json
import logging
import re
from typing import Dict, Any
import requests

from schema import PlannerOutput, FileSpec
from prompts.planner_prompt import PLANNER_SYSTEM_PROMPT, build_planner_prompt

logger = logging.getLogger(__name__)

MANDATORY_FILES = [
    'package.json',
    'app.js',
    'config/db.js',
]

class PlannerAgent:
    MAX_JSON_RETRIES = 2

    def __init__(self, ollama_url: str, model: str):
        self.ollama_url = ollama_url
        self.model = model

    def execute(self, user_prompt: str) -> PlannerOutput:
        logger.info("Starting project planning...")
        plan = None
        last_error = None

        for attempt in range(self.MAX_JSON_RETRIES + 1):
            try:
                if attempt == 0:
                    prompt = build_planner_prompt(user_prompt)
                else:
                    prompt = self._build_retry_prompt(user_prompt, str(last_error))

                logger.info(f"Querying AI (attempt {attempt + 1})...")
                raw_response = self._query_ollama(prompt, PLANNER_SYSTEM_PROMPT)

                plan = self._parse_and_validate(raw_response)
                break
            except Exception as e:
                last_error = e
                logger.warning(f"Attempt {attempt + 1} failed: {e}")
                if attempt == self.MAX_JSON_RETRIES:
                    logger.error("All planning attempts failed")
                    raise RuntimeError(f"Planner Agent failed after {self.MAX_JSON_RETRIES + 1} attempts: {e}")

        if not plan:
            raise RuntimeError("Planner Agent produced no output")

        plan = self._ensure_mandatory_files(plan)
        logger.info(f"Planning complete: '{plan.projectName}' — {len(plan.files)} files")
        return plan

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

    def _parse_and_validate(self, raw_response: str) -> PlannerOutput:
        # Extract JSON if the model wrapped it in markdown
        json_match = re.search(r'```json\s*(.*?)\s*```', raw_response, re.DOTALL)
        if json_match:
            raw_response = json_match.group(1)
        else:
            # Fallback to finding the first { and last }
            start = raw_response.find('{')
            end = raw_response.rfind('}')
            if start != -1 and end != -1:
                raw_response = raw_response[start:end+1]

        data = json.loads(raw_response)
        
        # Pydantic validation
        parsed = PlannerOutput(**data)
        
        # Sanitize project name
        parsed.projectName = re.sub(r'[^a-z0-9]+', '-', parsed.projectName.lower()).strip('-')

        # Clean paths
        for file in parsed.files:
            file.path = re.sub(r'^\.?/', '', file.path)
            
        return parsed

    def _ensure_mandatory_files(self, plan: PlannerOutput) -> PlannerOutput:
        existing_paths = {f.path for f in plan.files}
        
        for mandatory_path in MANDATORY_FILES:
            if mandatory_path not in existing_paths:
                description = self._get_default_description(mandatory_path, plan.projectName)
                plan.files.append(FileSpec(path=mandatory_path, description=description))
                
        if '.env' not in existing_paths:
            plan.files.append(FileSpec(
                path='.env', 
                description=f"Environment variables: PORT, MONGODB_URI for {plan.projectName}, NODE_ENV, JWT_SECRET"
            ))
            
        if 'middleware/errorHandler.js' not in existing_paths:
            plan.files.append(FileSpec(
                path='middleware/errorHandler.js',
                description='Centralized Express error handling middleware that catches all errors and returns formatted JSON responses'
            ))

        return plan

    def _get_default_description(self, path: str, project_name: str) -> str:
        descriptions = {
            'app.js': f"Main Express application entry point for {project_name}. Imports dotenv/config, sets up Express middleware (json, cors), connects to MongoDB, mounts all route files, adds error handling middleware, and starts the server on PORT from environment.",
            'package.json': f"NPM package manifest for {project_name}. Sets type to 'module' for ES modules, lists dependencies: express, mongoose, dotenv, cors, bcryptjs, jsonwebtoken. Includes start script.",
            'config/db.js': "MongoDB connection configuration. Exports an async connectDB function that uses mongoose.connect() with MONGODB_URI from process.env. Logs success/failure.",
        }
        return descriptions.get(path, f"Configuration file for {project_name}")

    def _build_retry_prompt(self, user_prompt: str, error_message: str) -> str:
        return f"""Your previous response was not valid JSON. Error: {error_message}

Please try again. Analyze this requirement and output ONLY valid JSON matching the schema in your system prompt.

## USER REQUIREMENT
{user_prompt}

Remember: Output ONLY the JSON object. No markdown fences, no explanations, no extra text."""
