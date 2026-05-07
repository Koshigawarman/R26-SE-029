import logging
import re
import json
import requests
from typing import Dict, Any

from schema import FileSpec, CodeGenContext, GeneratedFile
from prompts.codegen_prompt import CODEGEN_SYSTEM_PROMPT, build_codegen_prompt

logger = logging.getLogger(__name__)

class CodeGenAgent:
    MAX_RETRIES = 3          # Maximum retry attempts per file
    RETRY_DELAY_BASE = 5     # Base delay in seconds (doubles each retry)

    def __init__(self, ollama_url: str, model: str, use_openrouter: bool = False, openrouter_api_key: str = ""):
        self.ollama_url = ollama_url
        self.model = model
        self.use_openrouter = use_openrouter
        self.openrouter_api_key = openrouter_api_key
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"

    def execute(self, file_spec: FileSpec, context: CodeGenContext, existing_content: str = None) -> GeneratedFile:
        logger.info(f"Generating: {file_spec.path}")

        # Handle special files deterministically (no retry needed)
        if file_spec.path == '.env':
            return self._generate_env_file(file_spec, context)
        if file_spec.path == 'package.json':
            return self._generate_package_json(file_spec, context)

        last_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                if attempt > 1:
                    import time
                    delay = self.RETRY_DELAY_BASE * (2 ** (attempt - 2))
                    logger.warning(f"⟳ Retry {attempt}/{self.MAX_RETRIES} for {file_spec.path} (waiting {delay}s)...")
                    time.sleep(delay)

                prompt = build_codegen_prompt(
                    file_spec=file_spec,
                    project_name=context.projectName,
                    entities=context.entities,
                    features=context.features,
                    all_files=context.allFiles,
                    existing_contents=context.existingFileContents,
                    existing_file_content=existing_content
                )

                if self.use_openrouter:
                    raw_response = self._query_openrouter(prompt, CODEGEN_SYSTEM_PROMPT)
                else:
                    raw_response = self._query_ollama(prompt, CODEGEN_SYSTEM_PROMPT)

                code = self._extract_code(raw_response)

                if not code or len(code.strip()) < 10:
                    raise ValueError(f"Generated code is too short ({len(code)} chars)")

                self._validate_code(file_spec.path, code)

                logger.info(f"✓ Generated: {file_spec.path} ({len(code)} chars)" + (f" [attempt {attempt}]" if attempt > 1 else ""))

                return GeneratedFile(
                    path=file_spec.path,
                    content=code,
                    status='generated'
                )

            except Exception as e:
                last_error = e
                logger.error(f"✗ Attempt {attempt}/{self.MAX_RETRIES} failed for {file_spec.path}: {str(e)}")

        # All retries exhausted
        logger.error(f"✗ All {self.MAX_RETRIES} attempts failed for {file_spec.path}: {str(last_error)}")
        return GeneratedFile(
            path=file_spec.path,
            content='',
            status='error',
            errorMessage=f"Failed after {self.MAX_RETRIES} attempts: {str(last_error)}"
        )

    def _query_ollama(self, prompt: str, system_prompt: str) -> str:
        logger.info("\n" + "="*50)
        logger.info(f"OLLAMA REQUEST [CodeGenAgent] | Model: {self.model}")
        logger.info(f"--- SYSTEM PROMPT ---\n{system_prompt}")
        logger.info(f"--- USER PROMPT ---\n{prompt[:1000]}{'...' if len(prompt) > 1000 else ''}")
        logger.info("="*50)

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
            
        response_text = data["response"]
        logger.info("\n" + "="*50)
        logger.info(f"OLLAMA RESPONSE [CodeGenAgent] | Length: {len(response_text)}")
        logger.info(f"--- CONTENT ---\n{response_text[:1000]}{'...' if len(response_text) > 1000 else ''}")
        logger.info("="*50)
        
        return response_text

    def _query_openrouter(self, prompt: str, system_prompt: str) -> str:
        logger.info("\n" + "="*50)
        logger.info(f"OPENROUTER REQUEST [CodeGenAgent] | Model: {self.model}")
        logger.info(f"--- SYSTEM PROMPT ---\n{system_prompt}")
        logger.info(f"--- USER PROMPT ---\n{prompt[:1000]}{'...' if len(prompt) > 1000 else ''}")
        logger.info("="*50)

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
            "temperature": 0.3,
            "max_tokens": 4096,
        }
        resp = requests.post(self.openrouter_url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        
        response_text = data['choices'][0]['message']['content']
        logger.info("\n" + "="*50)
        logger.info(f"OPENROUTER RESPONSE [CodeGenAgent] | Length: {len(response_text)}")
        logger.info(f"--- CONTENT ---\n{response_text[:1000]}{'...' if len(response_text) > 1000 else ''}")
        logger.info("="*50)
        
        return response_text

    def _extract_code(self, raw_response: str) -> str:
        """Extracts code block from markdown if present."""
        code_match = re.search(r'```(?:javascript|js)?\s*(.*?)\s*```', raw_response, re.DOTALL | re.IGNORECASE)
        if code_match:
            return code_match.group(1).strip()
        return raw_response.strip()

    def _generate_env_file(self, file_spec: FileSpec, context: CodeGenContext) -> GeneratedFile:
        db_name = re.sub(r'[^a-z0-9]', '-', context.projectName.lower()).strip('-')
        has_auth = any('auth' in f.name.lower() for f in context.features)

        lines = [
            f"# {context.projectName} Environment Variables",
            "PORT=3000",
            f"MONGODB_URI=mongodb://localhost:27017/{db_name}",
            "NODE_ENV=development",
        ]

        if has_auth:
            lines.append("JWT_SECRET=your_jwt_secret_key_change_in_production")
            lines.append("JWT_EXPIRE=7d")

        lines.append("")
        content = "\n".join(lines)
        
        logger.info(f"✓ Generated: {file_spec.path} (env file — deterministic)")
        return GeneratedFile(path=file_spec.path, content=content, status='generated')

    def _generate_package_json(self, file_spec: FileSpec, context: CodeGenContext) -> GeneratedFile:
        has_auth = any('auth' in f.name.lower() for f in context.features)
        has_validation = any('valid' in f.name.lower() for f in context.features)

        pkg = {
            "name": context.projectName,
            "version": "1.0.0",
            "description": f"{context.projectName} — Generated by AI Backend Builder",
            "type": "module",
            "main": "app.js",
            "scripts": {
                "start": "node app.js",
                "dev": "node --watch app.js"
            },
            "dependencies": {
                "express": "^4.18.2",
                "mongoose": "^8.0.0",
                "dotenv": "^16.3.1",
                "cors": "^2.8.5"
            }
        }

        if has_auth:
            pkg["dependencies"]["bcryptjs"] = "^2.4.3"
            pkg["dependencies"]["jsonwebtoken"] = "^9.0.2"
        if has_validation:
            pkg["dependencies"]["express-validator"] = "^7.0.1"

        content = json.dumps(pkg, indent=2) + "\n"
        
        logger.info(f"✓ Generated: {file_spec.path} (package.json — deterministic)")
        return GeneratedFile(path=file_spec.path, content=content, status='generated')

    def _validate_code(self, path: str, code: str):
        warnings = []
        
        if 'require(' in code and 'import ' in code:
            warnings.append("Mixed require() and import statements detected")
        if 'require(' in code and 'import ' not in code:
            warnings.append("Using require() instead of ES module imports")
        if 'module.exports' in code:
            warnings.append("Using module.exports instead of ES module exports")
        if '// TODO' in code or '/* TODO' in code:
            warnings.append("Contains TODO placeholders")

        import_regex = re.compile(r"from\s+['\"](\.\/.+?)['\"]")
        for match in import_regex.finditer(code):
            import_path = match.group(1)
            if not import_path.endswith('.js') and not import_path.endswith('.json'):
                warnings.append(f"Import '{import_path}' missing .js extension")

        for w in warnings:
            logger.warning(f"{path}: {w}")