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

    def __init__(
        self, 
        ollama_url: str = "http://localhost:11434", 
        model: str = "qwen7b-planner",  # Updated default to 7B fine-tuned model
        use_openrouter: bool = False, 
        openrouter_api_key: str = ""  
    ):
        self.ollama_url = ollama_url
        self.model = model
        self.use_openrouter = use_openrouter
        self.openrouter_api_key = openrouter_api_key
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"

    def execute(self, user_prompt: str) -> PlannerOutput:
        logger.info(f"Starting project planning with model: {self.model}...")
        plan = None
        last_error = None

        for attempt in range(self.MAX_JSON_RETRIES + 1):
            try:
                if attempt == 0:
                    prompt = build_planner_prompt(user_prompt)
                else:
                    prompt = self._build_retry_prompt(user_prompt, str(last_error))

                logger.info(f"Querying AI (attempt {attempt + 1})...")
                if self.use_openrouter:
                    raw_response = self._query_openrouter(prompt, PLANNER_SYSTEM_PROMPT)
                else:
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
            "format": "json",  # Force strict JSON output from Ollama
            "stream": False,
            "options": {
                "temperature": 0.2,  # Low temperature for deterministic, structured schema
                "num_predict": 4096,
            }
        }
        resp = requests.post(
            f"{self.ollama_url}/api/generate",
            json=payload,
            timeout=240
        )
        resp.raise_for_status()
        data = resp.json()
        if "response" not in data:
            raise ValueError("Ollama returned no response")
        return data["response"]

    def _query_openrouter(self, prompt: str, system_prompt: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Koshigawarman/R26-SE-029",
            "X-Title": "AI Backend Builder",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "max_tokens": 4096,
        }
        resp = requests.post(self.openrouter_url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        return data['choices'][0]['message']['content']

    def _parse_and_validate(self, raw_response: str) -> PlannerOutput:
        # Extract JSON if wrapped in markdown blocks
        json_match = re.search(r'```json\s*(.*?)\s*```', raw_response, re.DOTALL)
        if json_match:
            raw_response = json_match.group(1)
        else:
            # Fallback to slice between the first { and last }
            start = raw_response.find('{')
            end = raw_response.rfind('}')
            if start != -1 and end != -1:
                raw_response = raw_response[start:end+1]

        data = json.loads(raw_response)
        
        # Pydantic validation against PlannerOutput schema
        parsed = PlannerOutput(**data)
        
        # Sanitize project name
        parsed.projectName = re.sub(r'[^a-z0-9]+', '-', parsed.projectName.lower()).strip('-')

        # Clean paths (remove leading ./ or /)
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


# =====================================================================
# Direct Execution / Standalone Test Example
# =====================================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    agent = PlannerAgent(
        ollama_url="http://localhost:11434",
        model="qwen7b-planner",
        use_openrouter=False,
    )
    
    sample_request = "Build an E-Commerce backend with User, Product, Category, and Order management."
    result = agent.execute(sample_request)
    print("\n--- Plan Generated Successfully ---")
    print(json.dumps(result.model_dump() if hasattr(result, "model_dump") else result.dict(), indent=2))