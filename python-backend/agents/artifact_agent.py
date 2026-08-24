import json
import logging
import os
import re
import requests
from typing import Optional, Dict, Any

from services.http_settings import get_ssl_verify_setting
from services.openai_compatible_http import build_provider_headers, raise_for_provider_error

logger = logging.getLogger(__name__)

CLASS_DIAGRAM_PROMPT = """You are an expert software architect. Given the JSON representation of a project plan, generate a detailed Mermaid classDiagram.

## CRITICAL RULES
1. Output ONLY the Mermaid diagram code inside a ```mermaid code block.
2. The diagram MUST start with `classDiagram`.
3. Read the entities and features from the plan JSON.
4. Include relationships between classes (e.g., `User "1" -- "*" Post : has`).
5. Include properties with data types based on the plan.
6. Include methods based on expected CRUD and business rules.
7. DO NOT output any conversational text, explanations, or markdown outside the mermaid block.
"""

USECASE_DIAGRAM_PROMPT = """You are an expert software architect. Given the JSON representation of a project plan, generate a detailed Mermaid Use Case diagram using a Left-to-Right flowchart (`flowchart LR`).

## CRITICAL RULES
1. Output ONLY the Mermaid diagram code inside a ```mermaid code block.
2. The diagram MUST start with `flowchart LR`.
3. Use Mermaid syntax for actors: `ActorName((ActorName))` (e.g., `User((User))`).
4. Use Mermaid syntax for use cases: `UC_ID([Use Case Name])` (e.g., `UC1([Login])`).
5. Connect actors to use cases: `User --> UC1`.
6. Base the actors and use cases on the provided plan JSON features.
7. Group related use cases inside subgraphs if appropriate (e.g., `subgraph Auth System`).
8. DO NOT output any conversational text, explanations, or markdown outside the mermaid block.
"""

SWAGGER_PROMPT = """You are an expert software architect. Given the JSON representation of a project plan and the ACTUAL generated source code of the backend, generate a complete OpenAPI 3.0 specification in YAML format.

## CRITICAL RULES
1. Output ONLY the YAML code inside a ```yaml code block.
2. The specification MUST be valid OpenAPI 3.0.
3. The paths, methods, and schemas MUST strictly reflect the actual implementation provided in the Codebase Context.
4. Include realistic tags, request bodies, and responses.
5. DO NOT output any conversational text, explanations, or markdown outside the yaml block.
"""

MOCK_DATA_PROMPT = """You are an expert QA engineer. Given the JSON representation of a project plan and the ACTUAL generated source code of the backend, generate realistic mock JSON data for the entities.

## CRITICAL RULES
1. Output ONLY the JSON code inside a ```json code block.
2. The output MUST be a JSON object where each key is an entity name (e.g., "User", "Post"), and the value is an array of dummy objects.
3. Generate EXACTLY {count} objects per entity.
4. The properties of the objects MUST strictly reflect the actual Mongoose schemas defined in the Codebase Context.
5. Use realistic dummy data (e.g., proper emails, MongoDB ObjectIDs for references, coherent text).
6. DO NOT output any conversational text, explanations, or markdown outside the json block.
"""

class ArtifactAgent:
    def __init__(
        self,
        ollama_url: str,
        model: str,
        use_openai_compatible: bool = False,
        openai_compatible_url: str = "",
        openai_compatible_api_key: str = "",
        openai_compatible_provider: str = "openai-compatible",
    ):
        self.ollama_url = ollama_url
        self.model = model
        self.use_openai_compatible = use_openai_compatible
        self.openai_compatible_api_key = openai_compatible_api_key
        self.openai_compatible_url = openai_compatible_url
        self.openai_compatible_provider = openai_compatible_provider

    def generate(self, artifact_type: str, plan_json: Dict[str, Any], count: int = 5, codebase: Optional[str] = None) -> str:
        logger.info(f"Generating {artifact_type} artifact...")
        
        context_str = f"Plan JSON:\n```json\n{json.dumps(plan_json, indent=2)}\n```\n"
        if codebase:
            context_str += f"\nCodebase Context (Generated Source Code):\n```javascript\n{codebase}\n```\n"
            
        if artifact_type == "class":
            system_prompt = CLASS_DIAGRAM_PROMPT
            prompt = context_str + "\nGenerate the mermaid diagram."
        elif artifact_type == "usecase":
            system_prompt = USECASE_DIAGRAM_PROMPT
            prompt = context_str + "\nGenerate the mermaid diagram."
        elif artifact_type == "swagger":
            system_prompt = SWAGGER_PROMPT
            prompt = context_str + "\nGenerate the Swagger YAML."
        elif artifact_type == "mock_data":
            system_prompt = MOCK_DATA_PROMPT.format(count=count)
            prompt = context_str + "\nGenerate the mock data JSON."
        else:
            raise ValueError(f"Unknown artifact type: {artifact_type}")

        if self.use_openai_compatible:
            raw_response = self._query_openai_compatible(prompt, system_prompt)
        else:
            raw_response = self._query_ollama(prompt, system_prompt)
            
        return self._extract_code(raw_response, artifact_type)
        
    def _extract_code(self, raw_response: str, artifact_type: str) -> str:
        # Check for specific language blocks
        if artifact_type in ("class", "usecase"):
            match = re.search(r'```(?:mermaid)?\s*\n(.*?)\n\s*```', raw_response, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()
            # Fallback
            if "classDiagram" in raw_response or "flowchart" in raw_response:
                return raw_response.strip().strip('`')
                
        elif artifact_type == "swagger":
            match = re.search(r'```(?:yaml)?\s*\n(.*?)\n\s*```', raw_response, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()
                
        elif artifact_type == "mock_data":
            match = re.search(r'```(?:json)?\s*\n(.*?)\n\s*```', raw_response, re.DOTALL | re.IGNORECASE)
            if match:
                return match.group(1).strip()
            # Fallback
            start = raw_response.find('{')
            end = raw_response.rfind('}')
            if start != -1 and end != -1:
                return raw_response[start:end+1]

        return raw_response.strip()

    def _query_ollama(self, prompt: str, system_prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "options": {
                "temperature": 0.2,
                "num_predict": 4096,
            }
        }
        resp = requests.post(
            f"{self.ollama_url}/api/generate",
            json=payload,
            timeout=int(os.getenv("MODEL_TIMEOUT", "120")),
        )
        resp.raise_for_status()
        return resp.json().get("response", "")

    def _query_openai_compatible(self, prompt: str, system_prompt: str) -> str:
        headers = build_provider_headers(self.openai_compatible_api_key)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "max_tokens": 4096,
            "stream": False
        }
        resp = requests.post(
            self.openai_compatible_url,
            headers=headers,
            json=payload,
            timeout=int(os.getenv("MODEL_TIMEOUT", "120")),
            verify=get_ssl_verify_setting(),
        )
        raise_for_provider_error(resp, self.openai_compatible_provider, self.openai_compatible_url)
        return resp.json()['choices'][0]['message']['content']
