import logging
import os
import re
import json
import requests
from typing import Dict, Any

from schema import FileSpec, CodeGenContext, GeneratedFile
from prompts.codegen_prompt import (
    CODEGEN_SYSTEM_PROMPT,
    build_codegen_prompt,
    build_code_fix_prompt,
)
logger = logging.getLogger(__name__)

class CodeGenAgent:
    def __init__(self, ollama_url: str, model: str, use_openrouter: bool = False, openrouter_api_key: str = ""):
        self.ollama_url = ollama_url
        self.model = model
        self.use_openrouter = use_openrouter
        self.openrouter_api_key = openrouter_api_key
        self.openrouter_url = "https://openrouter.ai/api/v1/chat/completions"
        self.last_request_trace: Dict[str, Any] = {}

    def execute(self, file_spec: FileSpec, context: CodeGenContext, existing_content: str = None) -> GeneratedFile:
        logger.info(f"Generating: {file_spec.path}")

        try:
            # Handle special files deterministically
            if file_spec.path == '.env':
                return self._generate_env_file(file_spec, context)
            if file_spec.path == 'package.json':
                return self._generate_package_json(file_spec, context)

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
                provider = "openrouter"
            else:
                raw_response = self._query_ollama(prompt, CODEGEN_SYSTEM_PROMPT)
                provider = "ollama"

            self.last_request_trace = {
                "agent": "codegen",
                "mode": "generate",
                "provider": provider,
                "model": self.model,
                "target_file": file_spec.path,
                "system_prompt": CODEGEN_SYSTEM_PROMPT,
                "built_prompt": prompt,
                "raw_output": raw_response,
            }

            code = self._extract_code(raw_response)

            if not code or len(code.strip()) < 10:
                raise ValueError(f"Generated code is too short ({len(code)} chars)")

            self._validate_code(file_spec.path, code)

            logger.info(f"✓ Generated: {file_spec.path} ({len(code)} chars)")

            return GeneratedFile(
                path=file_spec.path,
                content=code,
                status='generated'
            )

        except Exception as e:
            logger.error(f"✗ Failed to generate {file_spec.path}: {str(e)}")
            return GeneratedFile(
                path=file_spec.path,
                content='',
                status='error',
                errorMessage=str(e)
            )
            
    def fix_file_with_strategy(
        self,
        file_path: str,
        original_content: str,
        error_log: str,
        critic_strategy: str,
        instructions_for_code_agent: str
    ) -> GeneratedFile:
        """
        Applies the Critic Agent's fixing strategy to one affected file.

        This method belongs to the CodeGenAgent because:
        - CriticAgent diagnoses and creates strategy only.
        - CodeGenAgent generates the actual fixed code.
        """

        logger.info(f"Applying critic strategy to fix: {file_path}")

        try:
            prompt = build_code_fix_prompt(
                file_path=file_path,
                original_content=original_content,
                error_log=error_log,
                critic_strategy=critic_strategy,
                instructions_for_code_agent=instructions_for_code_agent,
            )

            if self.use_openrouter:
                raw_response = self._query_openrouter(prompt, CODEGEN_SYSTEM_PROMPT)
                provider = "openrouter"
            else:
                raw_response = self._query_ollama(prompt, CODEGEN_SYSTEM_PROMPT)
                provider = "ollama"

            self.last_request_trace = {
                "agent": "codegen",
                "mode": "fix",
                "provider": provider,
                "model": self.model,
                "target_file": file_path,
                "system_prompt": CODEGEN_SYSTEM_PROMPT,
                "built_prompt": prompt,
                "raw_output": raw_response,
            }
           
            fixed_code = self._extract_code(raw_response)

            if not fixed_code or len(fixed_code.strip()) < 10:
                raise ValueError("Generated fixed code is too short")

            self._validate_code(file_path, fixed_code)

            logger.info(f"✓ Fixed file generated: {file_path} ({len(fixed_code)} chars)")

            return GeneratedFile(
                path=file_path,
                content=fixed_code,
                status="fixed"
            )

        except Exception as e:
            logger.error(f"✗ Failed to fix {file_path}: {str(e)}")

            return GeneratedFile(
                path=file_path,
                content="",
                status="error",
                errorMessage=str(e)
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
            timeout=int(os.getenv("MODEL_TIMEOUT", "240"))
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
        self.last_request_trace = {
            "agent": "codegen",
            "mode": "deterministic",
            "provider": "local-template",
            "model": None,
            "target_file": file_spec.path,
            "system_prompt": "",
            "built_prompt": "Deterministic .env template generated from project name and features.",
            "raw_output": content,
        }
        
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
        self.last_request_trace = {
            "agent": "codegen",
            "mode": "deterministic",
            "provider": "local-template",
            "model": None,
            "target_file": file_spec.path,
            "system_prompt": "",
            "built_prompt": "Deterministic package.json template generated from project name and features.",
            "raw_output": content,
        }
        
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
