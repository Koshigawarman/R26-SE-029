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

## CRITICAL MERMAID SYNTAX RULES
1. Output ONLY the Mermaid diagram code inside a ```mermaid code block.
2. The diagram MUST start with `classDiagram` on line 1.
3. Read the entities and features from the plan JSON.
4. Generics syntax: NEVER use angle brackets like `<Type>`. In Mermaid v10+, generics MUST use tildes: `List~String~`, `Promise~void~`, `Array~Order~`.
5. Method syntax: NEVER put a colon before return types. Use `+methodName(param: type) ReturnType` or simply `+methodName(param)`.
6. Comments: If you use comments, use ONLY `%%` comment syntax. NEVER use `//` or `/* */`.
7. Attribute syntax: `+Type fieldName` or `-Type fieldName` (e.g., `+String email`, `-Number count`, `+Date createdAt`). NEVER put colons between field names and types.
8. Relationships format: `ClassA "1" --> "*" ClassB : description` or `ClassA -- ClassB`.
9. DO NOT output any conversational text, explanations, or markdown outside the mermaid block.

Example:
```mermaid
classDiagram
    class User {
        +String id
        +String email
        +String password
        +register(data) Boolean
        +login(credentials) String
    }
    class Order {
        +String id
        +String userId
        +Number totalAmount
        +createOrder() Order
    }
    User "1" --> "*" Order : places
```
"""

import yaml

class MergeDuplicateLoader(yaml.SafeLoader):
    pass

def construct_mapping_merge(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        val = loader.construct_object(value_node, deep=deep)
        if key in mapping and isinstance(mapping[key], dict) and isinstance(val, dict):
            mapping[key].update(val)
        else:
            mapping[key] = val
    return mapping

MergeDuplicateLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_mapping_merge
)

USECASE_DIAGRAM_PROMPT = """You are an expert software architect. Given the JSON representation of a project plan, generate a detailed Mermaid Use Case diagram using a Left-to-Right flowchart (`flowchart LR`).

## CRITICAL MERMAID SYNTAX RULES
1. Output ONLY the Mermaid diagram code inside a ```mermaid code block.
2. The diagram MUST start with `flowchart LR` on line 1.
3. Use Mermaid syntax for actors: `Actor_ID((Actor Name))` with NO spaces in the ID (e.g., `Customer((Customer))`, `Admin_User((Admin User))`).
4. Use Mermaid syntax for use cases: `UC_ID(["Use Case Name"])` ALWAYS wrapped in double quotes inside `([" ... "])` to allow special characters and parentheses (e.g., `UC1(["Register & Login"])`).
5. Connect actors to use cases: `Customer --> UC1`.
6. Subgraphs MUST be quoted and closed with `end`:
   subgraph "Authentication & Profile"
       UC1(["Login"])
       UC2(["Register"])
   end
7. Base the actors and use cases on the provided plan JSON features.
8. DO NOT output any conversational text, explanations, or markdown outside the mermaid block.

Example:
```mermaid
flowchart LR
    subgraph "User Management"
        UC1(["Register Account"])
        UC2(["Login & Authenticate"])
    end
    subgraph "Order System"
        UC3(["Browse Catalog"])
        UC4(["Place Order"])
    end
    User((User)) --> UC1
    User --> UC2
    User --> UC3
    User --> UC4
```
"""

SWAGGER_PROMPT = """You are an expert software architect. Given the JSON representation of a project plan and the ACTUAL generated source code of the backend, generate a complete OpenAPI 3.0 specification in YAML format.

## CRITICAL RULES
1. Output ONLY the YAML code inside a ```yaml code block.
2. The specification MUST be valid OpenAPI 3.0.
3. Every endpoint path MUST appear only ONCE under `paths:`. Group all HTTP methods (get, post, put, delete) for that route under that single path key.
4. The paths, methods, and schemas MUST strictly reflect the actual implementation provided in the Codebase Context.
5. Include realistic tags, request bodies, and responses.
6. DO NOT output any conversational text, explanations, or markdown outside the yaml block.
"""

MOCK_DATA_PROMPT = """You are an expert QA engineer. Given the JSON representation of a project plan and the ACTUAL generated source code of the backend, generate realistic mock JSON data for the entities.

## CRITICAL RULES
1. Output ONLY the JSON code inside a ```json code block.
2. The output MUST be a valid JSON object where each key is an entity name (e.g., "User", "Review", "Product"), and the value is a JSON array of dummy objects.
3. Generate EXACTLY {count} objects per entity.
4. The properties of the objects MUST strictly reflect the actual Mongoose schemas defined in the Codebase Context.
5. Use realistic dummy data (e.g., proper emails, MongoDB ObjectIDs like "65a1234567890abcdef12345" for references, ISO dates).
6. DO NOT leave trailing commas in JSON.
7. DO NOT output any conversational text, explanations, or markdown outside the json block.
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
        
    def _sanitize_mermaid(self, code: str, artifact_type: str) -> str:
        code = code.strip()
        if artifact_type == "class":
            # Strip any text before classDiagram
            if "classDiagram" in code:
                code = code[code.find("classDiagram"):]
            # Replace C++/Java style comments // with Mermaid %%
            code = re.sub(r'//(.*)', r'%%\1', code)
            # Replace angle bracket generics like List<Type> or Promise<void> with ~Type~
            code = re.sub(r'<([a-zA-Z0-9_, ]+)>', r'~\1~', code)
            # Fix method colons: +method(): ReturnType -> +method() ReturnType
            code = re.sub(r'(\)\s*):(\s*[a-zA-Z0-9_~]+)', r'\1 \2', code)
            # Fix attribute colons: +name: Type -> +Type name (only at start of line)
            code = re.sub(r'(?m)^([ \t]*)([+\-#~])[ \t]*([a-zA-Z0-9_]+)[ \t]*:[ \t]*([a-zA-Z0-9_~]+)[ \t]*$', r'\1\2\4 \3', code)
        elif artifact_type == "usecase":
            # Strip any text before flowchart
            if "flowchart" in code:
                code = code[code.find("flowchart"):]
            code = re.sub(r'//(.*)', r'%%\1', code)
            # Ensure subgraphs are quoted even if containing &, /, -, spaces
            def quote_subgraph(m):
                content = m.group(1).strip()
                if (content.startswith('"') and content.endswith('"')) or ('[' in content and ']' in content):
                    return f'subgraph {content}\n'
                clean = content.strip('"\'')
                return f'subgraph "{clean}"\n'
            code = re.sub(r'subgraph[ \t]+([^\r\n]+)[\r\n]+', quote_subgraph, code)
            # Fix actor IDs with spaces before (( on the SAME line, never matching 'end'
            code = re.sub(r'(?m)^([ \t]*)(?!end\b)([a-zA-Z0-9_]+)[ \t]+([a-zA-Z0-9_]+)[ \t]*\(\(', r'\1\2_\3((', code)
        return code

    def _clean_swagger_yaml(self, raw_yaml: str) -> str:
        try:
            spec = yaml.load(raw_yaml, Loader=MergeDuplicateLoader)
            if isinstance(spec, dict):
                if "openapi" not in spec and "swagger" not in spec:
                    spec["openapi"] = "3.0.0"
                return yaml.dump(spec, sort_keys=False, allow_unicode=True)
        except Exception as e:
            logger.warning(f"Could not merge duplicate keys in Swagger YAML: {e}")
        return raw_yaml

    def _clean_mock_json(self, raw_json: str) -> str:
        raw_json = raw_json.strip()
        # Remove markdown code block if present
        raw_json = re.sub(r'^```json\s*', '', raw_json, flags=re.IGNORECASE)
        raw_json = re.sub(r'\s*```$', '', raw_json)
        # Remove trailing commas
        cleaned = re.sub(r',\s*([}\]])', r'\1', raw_json)
        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                data = {"Items": data}
            elif isinstance(data, dict):
                for k, v in list(data.items()):
                    if not isinstance(v, list):
                        data[k] = [v]
            return json.dumps(data, indent=2)
        except Exception:
            start = cleaned.find('{')
            end = cleaned.rfind('}')
            if start != -1 and end != -1:
                try:
                    sub = re.sub(r',\s*([}\]])', r'\1', cleaned[start:end+1])
                    data = json.loads(sub)
                    return json.dumps(data, indent=2)
                except Exception:
                    pass
        return cleaned

    def _extract_code(self, raw_response: str, artifact_type: str) -> str:
        extracted = ""
        # Check for specific language blocks
        if artifact_type in ("class", "usecase"):
            match = re.search(r'```(?:mermaid)?\s*\n(.*?)\n\s*```', raw_response, re.DOTALL | re.IGNORECASE)
            if match:
                extracted = match.group(1).strip()
            elif "classDiagram" in raw_response or "flowchart" in raw_response:
                extracted = raw_response.strip().strip('`')
            else:
                extracted = raw_response.strip()
            return self._sanitize_mermaid(extracted, artifact_type)
                
        elif artifact_type == "swagger":
            match = re.search(r'```(?:yaml)?\s*\n(.*?)\n\s*```', raw_response, re.DOTALL | re.IGNORECASE)
            if match:
                extracted = match.group(1).strip()
            else:
                extracted = raw_response.strip()
            return self._clean_swagger_yaml(extracted)
                
        elif artifact_type == "mock_data":
            match = re.search(r'```(?:json)?\s*\n(.*?)\n\s*```', raw_response, re.DOTALL | re.IGNORECASE)
            if match:
                extracted = match.group(1).strip()
            else:
                extracted = raw_response.strip()
            return self._clean_mock_json(extracted)

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
