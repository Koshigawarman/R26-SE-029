#!/usr/bin/env python3
"""
Generate synthetic Planner Agent fine-tuning records with Gemini.

Output JSONL record:
{
  "id": "...",
  "messages": [
    {"role": "system", "content": "planner system prompt"},
    {"role": "user", "content": "Build ..."},
    {"role": "assistant", "content": "{... strict planner JSON ...}"}
  ],
  "metadata": {...}
}

Setup:
  pip install google-genai
  export GEMINI_API_KEY="..."

Examples:
  python scripts/generate_planner_dataset_with_gemini.py \
    --output datasets/planner_synthetic.jsonl \
    --count 200 \
    --model gemini-2.5-flash

  python scripts/generate_planner_dataset_with_gemini.py \
    --seed-prompts datasets/planner_seed_prompts.txt \
    --output datasets/planner_synthetic.jsonl \
    --count 400
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None


PLANNER_SYSTEM_PROMPT = """You are the Planner Agent for an AI backend generator.
Your task is to convert a natural-language backend request into a strict project contract JSON object.

Return ONLY valid JSON. No markdown fences. No explanation.

Required JSON shape:
{
  "projectName": "kebab-case-project-name",
  "architecture": {
    "stack": "node-express-mongoose",
    "pattern": "mvc | service-repository | clean-architecture | modular-monolith",
    "language": "javascript",
    "moduleSystem": "esm",
    "database": "mongodb",
    "orm": "mongoose"
  },
  "entities": [
    {
      "name": "PascalCaseEntity",
      "fields": [
        {
          "name": "camelCaseField",
          "type": "String | Number | Boolean | Date | ObjectId | [String] | [ObjectId]",
          "required": true,
          "unique": false
        }
      ],
      "description": "Short entity purpose."
    }
  ],
  "features": [
    {
      "name": "Feature Name",
      "description": "Short implementation-oriented feature description."
    }
  ],
  "files": [
    {
      "path": "package.json",
      "description": "Defines dependencies and scripts."
    }
  ]
}

Planning rules:
1. Always include package.json, .env, app.js, config/db.js, and middleware/errorHandler.js.
2. Choose exactly one architecture.pattern:
   - mvc: default for simple CRUD and normal REST APIs.
   - service-repository: richer business logic and database separation.
   - clean-architecture: explicit domain/application/infrastructure/interface separation.
   - modular-monolith: large systems organized by business modules in one deployable backend.
3. For every entity, include files according to architecture.pattern:
   mvc:
   - models/<Entity>.js
   - controllers/<entity>Controller.js
   - routes/<entity>Routes.js
   service-repository:
   - models/<Entity>.js
   - repositories/<entity>Repository.js
   - services/<entity>Service.js
   - controllers/<entity>Controller.js
   - routes/<entity>Routes.js
   clean-architecture:
   - domain/entities/<Entity>.js
   - application/use-cases/<entity>UseCases.js
   - infrastructure/database/<Entity>Model.js
   - infrastructure/repositories/<entity>Repository.js
   - interfaces/controllers/<entity>Controller.js
   - interfaces/routes/<entity>Routes.js
   modular-monolith:
   - modules/<entity>/model.js
   - modules/<entity>/repository.js
   - modules/<entity>/service.js
   - modules/<entity>/controller.js
   - modules/<entity>/routes.js
4. If authentication, login, registration, JWT, roles, or protected routes are requested, include:
   - middleware/auth.js
   - controllers/authController.js
   - routes/authRoutes.js
5. Use Express.js, MongoDB, Mongoose, and ES modules.
6. Do not include frontend files.
7. Keep the plan implementable by a code generation agent. Do not over-plan huge enterprise systems unless requested.
8. Use MONGODB_URI, not MONGO_URI.
9. Use clear file descriptions that tell the CodeGen Agent what belongs in the file.
10. Do not duplicate schema definitions across controllers or routes."""


DOMAINS = [
    "e-commerce marketplace",
    "inventory management",
    "hotel booking",
    "clinic appointment",
    "school management",
    "restaurant ordering",
    "blog publishing",
    "task management",
    "event ticketing",
    "car rental",
    "real estate listing",
    "learning management",
    "support ticketing",
    "warehouse logistics",
    "subscription billing",
]

FEATURE_SETS = [
    ["CRUD APIs", "search and filtering", "pagination"],
    ["JWT authentication", "role-based access control", "protected routes"],
    ["inventory tracking", "low stock alerts", "status updates"],
    ["booking availability", "date range validation", "cancellation workflow"],
    ["reviews and ratings", "moderation", "public listing search"],
    ["order lifecycle", "payment status tracking", "shipping status tracking"],
    ["admin reporting", "aggregation summaries", "export-friendly endpoints"],
    ["user profiles", "password hashing", "account activation status"],
    ["nested categories", "slug fields", "unique constraints"],
    ["assignment workflow", "priority levels", "audit timestamps"],
]

COMPLEXITIES = ["simple", "medium", "advanced"]


def main() -> int:
    args = parse_args()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    api_key = args.api_key or os.getenv("GEMINI_API_KEY")
    if not api_key and not args.dry_run:
        raise SystemExit("Missing Gemini API key. Set GEMINI_API_KEY or pass --api-key.")

    if not args.dry_run and genai is None:
        raise SystemExit("Missing google-genai package. Install it with: pip install google-genai")

    client = None if args.dry_run else genai.Client(api_key=api_key)
    completed_ids = load_completed_ids(output_path) if args.resume else set()
    seed_prompts = load_seed_prompts(Path(args.seed_prompts)) if args.seed_prompts else []

    written = 0
    attempted = 0

    with output_path.open("a", encoding="utf-8") as out:
        while written < args.count:
            user_prompt = choose_prompt(seed_prompts, args)
            record_id = make_record_id(user_prompt)
            if record_id in completed_ids:
                continue

            attempted += 1

            if args.dry_run:
                planner_output = dry_run_plan(user_prompt)
                usage = {}
            else:
                planner_output, usage = generate_plan_with_retry(
                    client=client,
                    model=args.model,
                    user_prompt=user_prompt,
                    temperature=args.temperature,
                    max_output_tokens=args.max_output_tokens,
                    retries=args.retries,
                )

            validation = validate_plan(planner_output)
            if not validation["passed"] and args.skip_invalid:
                print(f"[skip-invalid] {record_id}: {validation['issues']}", file=sys.stderr)
                continue

            dataset_record = {
                "id": record_id,
                "messages": [
                    {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": json.dumps(planner_output, ensure_ascii=False)},
                ],
                "metadata": {
                    "validation": validation,
                    "gemini_model": args.model,
                    "gemini_usage": usage,
                    "generated_at": time.time(),
                },
            }

            out.write(json.dumps(dataset_record, ensure_ascii=False) + "\n")
            out.flush()

            completed_ids.add(record_id)
            written += 1
            print(f"[ok] {record_id} -> {planner_output.get('projectName', 'unknown')} ({written}/{args.count})")

            if args.sleep > 0 and not args.dry_run:
                time.sleep(args.sleep)

    print(f"Done. attempted={attempted}, wrote={written}, output={output_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synthetic Planner Agent dataset with Gemini.")
    parser.add_argument("--output", required=True, help="Output JSONL path.")
    parser.add_argument("--count", type=int, default=100, help="Number of records to write.")
    parser.add_argument("--seed-prompts", default="", help="Optional txt/json/jsonl file containing prompts.")
    parser.add_argument("--api-key", default="", help="Gemini API key. Defaults to GEMINI_API_KEY.")
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--no-resume", action="store_false", dest="resume")
    parser.add_argument("--skip-invalid", action="store_true", default=True)
    parser.add_argument("--keep-invalid", action="store_false", dest="skip_invalid")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-entities", type=int, default=3)
    parser.add_argument("--max-entities", type=int, default=8)
    return parser.parse_args()


def choose_prompt(seed_prompts: List[str], args: argparse.Namespace) -> str:
    if seed_prompts and random.random() < 0.65:
        return random.choice(seed_prompts)
    return synthesize_user_prompt(args.min_entities, args.max_entities)


def synthesize_user_prompt(min_entities: int, max_entities: int) -> str:
    domain = random.choice(DOMAINS)
    feature_set = random.choice(FEATURE_SETS)
    complexity = random.choice(COMPLEXITIES)
    entity_count = random.randint(min_entities, max_entities)

    modifiers = {
        "simple": "clean and maintainable",
        "medium": "production-ready",
        "advanced": "scalable and modular",
    }
    architecture_hint = {
        "simple": "Use the simplest suitable architecture pattern.",
        "medium": "Choose either mvc or service-repository depending on the business logic.",
        "advanced": "Choose service-repository, clean-architecture, or modular-monolith if the requirements justify it.",
    }

    return (
        f"Build a {modifiers[complexity]} {domain} backend API with about {entity_count} core entities. "
        f"Include {', '.join(feature_set)}. "
        "Use Node.js, Express, MongoDB, Mongoose, ES modules, environment configuration, and global error handling. "
        f"{architecture_hint[complexity]}"
    )


def generate_plan_with_retry(
    client: Any,
    model: str,
    user_prompt: str,
    temperature: float,
    max_output_tokens: int,
    retries: int,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=PLANNER_SYSTEM_PROMPT,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    response_mime_type="application/json",
                ),
            )
            text = strip_code_fences(response.text or "")
            plan = json.loads(text)
            usage = {}
            if getattr(response, "usage_metadata", None):
                usage = response.usage_metadata.model_dump(exclude_none=True)
            return plan, usage
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"[retry] Gemini planner attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
            time.sleep(min(30, 2**attempt))

    raise RuntimeError(f"Planner generation failed after {retries} attempt(s): {last_error}")


def validate_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    issues: List[Dict[str, str]] = []

    required_top = ["projectName", "architecture", "entities", "features", "files"]
    for key in required_top:
        if key not in plan:
            issues.append({"severity": "error", "code": "missing_top_level_key", "message": f"Missing {key}."})

    files = plan.get("files") if isinstance(plan.get("files"), list) else []
    paths = [str(item.get("path", "")) for item in files if isinstance(item, dict)]
    architecture = plan.get("architecture") if isinstance(plan.get("architecture"), dict) else {}
    pattern = str(architecture.get("pattern") or "mvc")
    if pattern not in {"mvc", "service-repository", "clean-architecture", "modular-monolith"}:
        issues.append({"severity": "error", "code": "unsupported_architecture_pattern", "message": f"Unsupported architecture pattern: {pattern}."})

    for required_path in ["package.json", ".env", "app.js", "config/db.js", "middleware/errorHandler.js"]:
        if required_path not in paths:
            issues.append({"severity": "error", "code": "missing_required_file", "message": f"Missing {required_path}."})

    entities = plan.get("entities") if isinstance(plan.get("entities"), list) else []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "")
        if not re.match(r"^[A-Z][A-Za-z0-9]*$", name):
            issues.append({"severity": "warning", "code": "bad_entity_name", "message": f"Entity name should be PascalCase: {name}"})
            continue

        entity_var = name[0].lower() + name[1:]
        expected = expected_entity_files(name, entity_var, pattern)
        for path in expected:
            if path not in paths:
                issues.append({"severity": "warning", "code": "missing_entity_file", "message": f"Missing expected file {path}."})

        fields = entity.get("fields")
        if not isinstance(fields, list) or not fields:
            issues.append({"severity": "error", "code": "entity_missing_fields", "message": f"Entity {name} has no fields."})

    prompt_mentions_auth = any(
        feature in json.dumps(plan.get("features", []), ensure_ascii=False).lower()
        for feature in ["jwt", "auth", "login", "role"]
    )
    if prompt_mentions_auth:
        for path in ["middleware/auth.js", "controllers/authController.js", "routes/authRoutes.js"]:
            if path not in paths:
                issues.append({"severity": "warning", "code": "auth_file_missing", "message": f"Auth feature present but missing {path}."})

    return {
        "passed": not any(issue["severity"] == "error" for issue in issues),
        "issues": issues,
    }


def load_seed_prompts(path: Path) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(path)

    if path.suffix == ".jsonl":
        prompts = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                item = json.loads(line)
                prompt = item.get("prompt") or item.get("user_prompt")
                if prompt:
                    prompts.append(str(prompt))
        return prompts

    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(item.get("prompt") or item.get("user_prompt") or item) for item in data]
        if isinstance(data, dict):
            return [str(data.get("prompt") or data.get("user_prompt") or "")]

    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_completed_ids(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()
    completed = set()
    with output_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_id = item.get("id")
            if item_id:
                completed.add(str(item_id))
    return completed


def strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if match:
        return match.group(1).strip()
    return cleaned


def make_record_id(user_prompt: str) -> str:
    raw = f"{user_prompt}:{time.time_ns()}:{random.random()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def dry_run_plan(user_prompt: str) -> Dict[str, Any]:
    return {
        "projectName": "dry-run-backend",
        "architecture": {
            "stack": "node-express-mongoose",
            "pattern": "service-repository",
            "language": "javascript",
            "moduleSystem": "esm",
            "database": "mongodb",
            "orm": "mongoose",
        },
        "entities": [
            {
                "name": "User",
                "fields": [
                    {"name": "name", "type": "String", "required": True, "unique": False},
                    {"name": "email", "type": "String", "required": True, "unique": True},
                ],
                "description": "Represents application users.",
            },
            {
                "name": "Product",
                "fields": [
                    {"name": "title", "type": "String", "required": True, "unique": False},
                    {"name": "price", "type": "Number", "required": True, "unique": False},
                ],
                "description": "Represents products.",
            },
        ],
        "features": [{"name": "CRUD APIs", "description": "Create, read, update, and delete core resources."}],
        "files": [
            {"path": "package.json", "description": "Defines dependencies and scripts."},
            {"path": ".env", "description": "Contains PORT and MONGODB_URI."},
            {"path": "app.js", "description": "Initializes Express app and mounts routes."},
            {"path": "config/db.js", "description": "Connects to MongoDB using Mongoose."},
            {"path": "middleware/errorHandler.js", "description": "Global error handling middleware."},
            {"path": "models/User.js", "description": "Mongoose model for User."},
            {"path": "repositories/userRepository.js", "description": "Repository logic for User persistence."},
            {"path": "services/userService.js", "description": "Service logic for User business operations."},
            {"path": "controllers/userController.js", "description": "Controller logic for User CRUD."},
            {"path": "routes/userRoutes.js", "description": "Thin routes for User endpoints."},
            {"path": "models/Product.js", "description": "Mongoose model for Product."},
            {"path": "repositories/productRepository.js", "description": "Repository logic for Product persistence."},
            {"path": "services/productService.js", "description": "Service logic for Product business operations."},
            {"path": "controllers/productController.js", "description": "Controller logic for Product CRUD."},
            {"path": "routes/productRoutes.js", "description": "Thin routes for Product endpoints."},
        ],
    }


def expected_entity_files(name: str, entity_var: str, pattern: str) -> List[str]:
    if pattern == "service-repository":
        return [
            f"models/{name}.js",
            f"repositories/{entity_var}Repository.js",
            f"services/{entity_var}Service.js",
            f"controllers/{entity_var}Controller.js",
            f"routes/{entity_var}Routes.js",
        ]
    if pattern == "clean-architecture":
        return [
            f"domain/entities/{name}.js",
            f"application/use-cases/{entity_var}UseCases.js",
            f"infrastructure/database/{name}Model.js",
            f"infrastructure/repositories/{entity_var}Repository.js",
            f"interfaces/controllers/{entity_var}Controller.js",
            f"interfaces/routes/{entity_var}Routes.js",
        ]
    if pattern == "modular-monolith":
        return [
            f"modules/{entity_var}/model.js",
            f"modules/{entity_var}/repository.js",
            f"modules/{entity_var}/service.js",
            f"modules/{entity_var}/controller.js",
            f"modules/{entity_var}/routes.js",
        ]
    return [
        f"models/{name}.js",
        f"controllers/{entity_var}Controller.js",
        f"routes/{entity_var}Routes.js",
    ]


if __name__ == "__main__":
    raise SystemExit(main())
