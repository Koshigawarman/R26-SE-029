#!/usr/bin/env python3
"""
Generate synthetic CodeGen fine-tuning records from Planner Agent datasets.

Input shape supported:
1. One JSON file:
   {
     "prompt": "Build ...",
     "response": {
       "projectName": "...",
       "entities": [...],
       "features": [...],
       "files": [{"path": "models/User.js", "description": "..."}]
     }
   }

2. A directory containing many .json or .jsonl files with the same records.

Output:
JSONL records in chat fine-tuning style:
{
  "id": "...",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "generated code"}
  ],
  "metadata": {...}
}

Setup:
  pip install google-genai
  export GEMINI_API_KEY="..."

Example:
  python scripts/generate_codegen_dataset_with_gemini.py \
    --input datasets/planner_outputs \
    --record-dir datasets/codegen_dataset \
    --model gemini-3.6-flash \
    --sleep 1.0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import requests
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

CURRENT_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = CURRENT_DIR.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agents.codegen_agent import CodeGenAgent
from schema import CodeGenContext, FileSpec, PlannerOutput
from prompts.codegen_prompt import build_codegen_prompt as build_runtime_codegen_prompt
from prompts.codegen_prompt_factory import get_architecture_codegen_system_prompt
from services.architecture_profile_registry import detect_file_type, normalize_architecture

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None


FILE_PRIORITY = {
    "package.json": 0,
    "README.md": 1,
    ".env": 2,
    "config/": 3,
    "models/": 4,
    "domain/": 4,
    "infrastructure/database/": 4,
    "middleware/": 5,
    "repositories/": 6,
    "infrastructure/repositories/": 6,
    "services/": 7,
    "application/use-cases/": 7,
    "controllers/": 8,
    "interfaces/controllers/": 8,
    "routes/": 9,
    "interfaces/routes/": 9,
    "modules/": 6,
    "app.js": 10,
}

DETERMINISTIC_FILES = {"package.json", ".env", "README.md"}


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else None
    record_dir = Path(args.record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    provider = args.provider.strip().lower()
    api_key = args.api_key or os.getenv("GEMINI_API_KEY")
    if provider in {"modal", "openai-compatible"}:
        api_key = args.api_key or os.getenv("OPENAI_COMPATIBLE_API_KEY", "")

    if provider == "gemini" and not api_key and not args.dry_run:
        raise SystemExit("Missing Gemini API key. Set GEMINI_API_KEY or pass --api-key.")

    if provider == "gemini" and not args.dry_run and genai is None:
        raise SystemExit("Missing google-genai package. Install it with: pip install google-genai")

    if provider in {"modal", "openai-compatible"} and not args.openai_url and not args.dry_run:
        raise SystemExit("Missing OpenAI-compatible URL. Pass --openai-url or set OPENAI_COMPATIBLE_URL.")

    client = None
    if not args.dry_run and provider == "gemini":
        client = genai.Client(api_key=api_key)
    completed_ids = set()
    if args.resume:
        completed_ids.update(load_completed_ids_from_dir(record_dir))
        if output_path:
            completed_ids.update(load_completed_ids(output_path))

    planner_records = list(load_planner_records(input_path))
    if args.shuffle:
        random.shuffle(planner_records)

    total_written = 0
    total_seen = 0

    out = output_path.open("a", encoding="utf-8") if output_path else None
    try:
        for source_path, record_index, planner_record in planner_records:
            plan, user_prompt = extract_plan_and_prompt(planner_record)
            plan = ensure_current_plan_contract(plan, user_prompt)

            if not isinstance(plan, dict) or not isinstance(plan.get("files"), list):
                print(f"[skip] {source_path}:{record_index} missing response.files", file=sys.stderr)
                continue

            project_name = str(plan.get("projectName") or plan.get("project_name") or "generated-backend")
            architecture = normalize_architecture(plan.get("architecture")).model_dump()
            existing_contents: Dict[str, str] = {}

            for file_spec in sorted(plan["files"], key=lambda item: file_priority(str(item.get("path", "")))):
                file_path = str(file_spec.get("path") or "").strip()
                if not file_path:
                    continue
                if file_path in DETERMINISTIC_FILES and not args.include_deterministic_files:
                    existing_contents[file_path] = deterministic_code_for(file_path, plan, existing_contents)
                    continue

                total_seen += 1
                example_id = make_example_id(source_path, record_index, project_name, file_path)
                if example_id in completed_ids:
                    continue

                system_prompt = get_architecture_codegen_system_prompt(file_path, architecture)
                user_codegen_prompt = build_current_codegen_prompt(
                    user_prompt=user_prompt,
                    plan=plan,
                    target_file=file_spec,
                    existing_contents=existing_contents,
                    max_related_chars=args.max_related_chars,
                    architecture=architecture,
                )

                if file_path in DETERMINISTIC_FILES:
                    code = deterministic_code_for(file_path, plan, existing_contents)
                    usage = {}
                elif args.dry_run:
                    code = dry_run_code_for(file_path)
                    usage = {}
                else:
                    code, usage = generate_code_with_retry(
                        client=client,
                        provider=provider,
                        model=args.model,
                        system_prompt=system_prompt,
                        user_prompt=user_codegen_prompt,
                        openai_url=args.openai_url or os.getenv("OPENAI_COMPATIBLE_URL", ""),
                        api_key=api_key,
                        max_output_tokens=args.max_output_tokens,
                        temperature=args.temperature,
                        retries=args.retries,
                    )

                code = strip_code_fences(code)
                validation = validate_generated_code(file_path, code, architecture)

                dataset_record = {
                    "id": example_id,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_codegen_prompt},
                        {"role": "assistant", "content": code},
                    ],
                    "metadata": {
                        "source_file": str(source_path),
                        "source_record_index": record_index,
                        "project_name": project_name,
                        "architecture": architecture,
                        "target_file": file_path,
                        "planner_prompt": user_prompt,
                        "validation": validation,
                        "provider": provider,
                        "model": args.model,
                        "usage": usage,
                    },
                }

                write_record_json(record_dir, dataset_record)
                if out:
                    out.write(json.dumps(dataset_record, ensure_ascii=False) + "\n")
                    out.flush()

                existing_contents[file_path] = code
                completed_ids.add(example_id)
                total_written += 1

                print(f"[ok] {example_id} -> {file_path} ({len(code)} chars)")

                if args.limit and total_written >= args.limit:
                    print(f"Limit reached. Wrote {total_written} new examples.")
                    return 0

                if args.sleep > 0 and not args.dry_run:
                    time.sleep(args.sleep)

    finally:
        if out:
            out.close()

    output_text = str(output_path) if output_path else "(json files only)"
    print(f"Done. Seen={total_seen}, wrote_new={total_written}, record_dir={record_dir}, output={output_text}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synthetic CodeGen dataset records using Gemini or an OpenAI-compatible endpoint.")
    parser.add_argument("--input", required=True, help="Planner dataset JSON/JSONL file or directory.")
    parser.add_argument("--output", default="", help="Optional output JSONL path. If omitted, only per-record JSON files are written.")
    parser.add_argument("--record-dir", default="datasets/codegen_dataset", help="Directory where each dataset record is written as a separate JSON file.")
    parser.add_argument("--provider", default="gemini", choices=["gemini", "modal", "openai-compatible"], help="Generation provider.")
    parser.add_argument("--openai-url", default="", help="OpenAI-compatible /v1/chat/completions URL. Defaults to OPENAI_COMPATIBLE_URL.")
    parser.add_argument("--api-key", default="", help="Gemini API key or OpenAI-compatible bearer token. Defaults to GEMINI_API_KEY or OPENAI_COMPATIBLE_API_KEY.")
    parser.add_argument("--model", default="gemini-2.5-flash", help="Gemini model name.")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--max-related-chars", type=int, default=12000)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=15.0, help="Delay between Gemini calls. Gemini free tier for some models is 5 RPM, so 15s is a safe default.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum new examples to write.")
    parser.add_argument("--resume", action="store_true", default=True, help="Skip IDs already in output file.")
    parser.add_argument("--no-resume", action="store_false", dest="resume")
    parser.add_argument("--shuffle", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Write placeholder records without calling Gemini.")
    parser.add_argument(
        "--include-deterministic-files",
        action="store_true",
        help="Also write package.json, .env, and README.md records using the runtime deterministic templates.",
    )
    return parser.parse_args()


def load_planner_records(path: Path) -> Iterable[Tuple[Path, int, Dict[str, Any]]]:
    if path.is_dir():
        files = sorted([*path.rglob("*.json"), *path.rglob("*.jsonl")])
    else:
        files = [path]

    for file_path in files:
        if file_path.suffix == ".jsonl":
            yield from load_jsonl_records(file_path)
        else:
            yield from load_json_records(file_path)


def load_json_records(path: Path) -> Iterable[Tuple[Path, int, Dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        for index, item in enumerate(data):
            if isinstance(item, dict):
                yield path, index, item
    elif isinstance(data, dict):
        yield path, 0, data


def extract_plan_and_prompt(record: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    plan = record.get("response") or record.get("planner_output")
    user_prompt = record.get("prompt") or record.get("user_prompt") or ""

    messages = record.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if message.get("role") == "user" and not user_prompt:
                user_prompt = str(message.get("content") or "")
            if message.get("role") == "assistant" and not plan:
                content = str(message.get("content") or "").strip()
                if content:
                    try:
                        plan = json.loads(strip_code_fences(content))
                    except json.JSONDecodeError:
                        plan = {}

    return plan if isinstance(plan, dict) else {}, str(user_prompt)


def load_jsonl_records(path: Path) -> Iterable[Tuple[Path, int, Dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as fh:
        for index, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[skip] {path}:{index} invalid JSONL: {exc}", file=sys.stderr)
                continue
            if isinstance(item, dict):
                yield path, index, item


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


def load_completed_ids_from_dir(record_dir: Path) -> set[str]:
    if not record_dir.exists():
        return set()

    completed = set()
    for path in record_dir.rglob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        item_id = item.get("id")
        if item_id:
            completed.add(str(item_id))
    return completed


def write_record_json(record_dir: Path, record: Dict[str, Any]) -> Path:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    project_name = safe_filename(str(metadata.get("project_name") or "unknown-project"))
    target_file = safe_filename(str(metadata.get("target_file") or "unknown-file"))
    record_id = safe_filename(str(record.get("id") or hashlib.sha256(json.dumps(record, sort_keys=True).encode("utf-8")).hexdigest()[:24]))

    project_dir = record_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)
    output_path = project_dir / f"{target_file}__{record_id}.json"
    output_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return output_path


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
    cleaned = cleaned.strip("._")
    return cleaned[:120] or "record"


def build_codegen_prompt(
    original_user_prompt: str,
    plan: Dict[str, Any],
    target_file: Dict[str, Any],
    existing_contents: Dict[str, str],
    max_related_chars: int,
    architecture: Dict[str, Any],
) -> str:
    target_path = str(target_file.get("path") or "")
    parts = [
        "Generate the complete source code for the target file using the Planner Agent contract.",
        "",
        "## ORIGINAL USER REQUEST",
        original_user_prompt.strip(),
        "",
        "## TARGET FILE",
        f"- Path: {target_path}",
        f"- Description: {target_file.get('description', '')}",
        "",
        "## PROJECT NAME",
        str(plan.get("projectName") or "generated-backend"),
        "",
        "## ARCHITECTURE",
        f"- stack: {architecture.get('stack', 'node-express-mongoose')}",
        f"- pattern: {architecture.get('pattern', 'mvc')}",
        f"- language: {architecture.get('language', 'javascript')}",
        f"- moduleSystem: {architecture.get('moduleSystem', 'esm')}",
        f"- database: {architecture.get('database', 'mongodb')}",
        f"- orm: {architecture.get('orm', 'mongoose')}",
        "",
    ]

    entities = plan.get("entities") or []
    if entities:
        parts.append("## ENTITIES")
        for entity in entities:
            parts.append(f"### {entity.get('name', 'Entity')}")
            description = entity.get("description")
            if description:
                parts.append(f"Description: {description}")
            parts.append("Fields:")
            for field in entity.get("fields", []):
                modifiers = []
                if field.get("required"):
                    modifiers.append("required")
                if field.get("unique"):
                    modifiers.append("unique")
                mod_text = f" ({', '.join(modifiers)})" if modifiers else ""
                parts.append(f"- {field.get('name')}: {field.get('type')}{mod_text}")
            parts.append("")

    features = plan.get("features") or []
    if features:
        parts.append("## FEATURES")
        for feature in features:
            parts.append(f"- {feature.get('name')}: {feature.get('description')}")
        parts.append("")

    files = plan.get("files") or []
    parts.append("## ALL PROJECT FILES")
    parts.append("This is the complete local file contract. Do not import local files outside this list.")
    for file_item in files:
        marker = " <- TARGET" if file_item.get("path") == target_path else ""
        parts.append(f"- {file_item.get('path')}{marker}")
    parts.append("")

    related = related_files_for(target_path, existing_contents, architecture.get("pattern", "mvc"))
    if related:
        parts.append("## ALREADY GENERATED RELATED FILES")
        total_chars = 0
        for path, content in related:
            if total_chars + len(content) > max_related_chars:
                parts.append("(... related files omitted for length ...)")
                break
            parts.append(f"### {path}")
            parts.append("```javascript")
            parts.append(content)
            parts.append("```")
            total_chars += len(content)
        parts.append("")

    parts.extend(
        [
            "## OUTPUT REQUIREMENTS",
            "Return ONLY the complete source code for the target file.",
            "No markdown fences.",
            "No explanation.",
            "No extra commentary.",
        ]
    )
    return "\n".join(parts)


def build_current_codegen_prompt(
    user_prompt: str,
    plan: Dict[str, Any],
    target_file: Dict[str, Any],
    existing_contents: Dict[str, str],
    max_related_chars: int,
    architecture: Dict[str, Any],
) -> str:
    try:
        planner_output = PlannerOutput.model_validate(plan)
        file_spec = FileSpec.model_validate(target_file)
        return build_runtime_codegen_prompt(
            file_spec=file_spec,
            project_name=planner_output.projectName,
            entities=planner_output.entities,
            features=planner_output.features,
            all_files=planner_output.files,
            existing_contents=existing_contents,
            existing_file_content=None,
            architecture=architecture,
            style_profile={},
        )
    except Exception as exc:  # noqa: BLE001 - keep dataset generation resilient.
        print(f"[warn] Falling back to legacy prompt builder for {target_file.get('path')}: {exc}", file=sys.stderr)
        return build_codegen_prompt(
            original_user_prompt=user_prompt,
            plan=plan,
            target_file=target_file,
            existing_contents=existing_contents,
            max_related_chars=max_related_chars,
            architecture=architecture,
        )


def ensure_current_plan_contract(plan: Dict[str, Any], user_prompt: str = "") -> Dict[str, Any]:
    if not isinstance(plan, dict):
        return {}

    normalized = dict(plan)
    normalized["projectName"] = str(
        normalized.get("projectName") or normalized.get("project_name") or project_name_from_prompt(user_prompt)
    )
    normalized["architecture"] = normalize_architecture(normalized.get("architecture")).model_dump()
    normalized.setdefault("entities", [])
    normalized.setdefault("features", [])

    files = normalized.get("files")
    if not isinstance(files, list):
        files = []
    normalized["files"] = [item for item in files if isinstance(item, dict) and item.get("path")]

    required_descriptions = {
        "package.json": "NPM package manifest with ES module metadata, start/dev/test scripts, runtime dependencies, and nodemon devDependency.",
        "README.md": "Project documentation with overview, architecture, setup commands, environment variables, scripts, and API route summary.",
        ".env": "Environment variables using PORT, MONGODB_URI, NODE_ENV, and JWT placeholders only when authentication is required.",
        "app.js": "Express app entry point that loads dotenv, connects MongoDB, configures middleware, mounts routes, adds errorHandler last, conditionally starts server, and exports app.",
        "config/db.js": "MongoDB connection configuration exporting default connectDB using mongoose.connect and process.env.MONGODB_URI.",
        "middleware/errorHandler.js": "Centralized Express error handling middleware exporting default errorHandler.",
    }

    paths = {str(item.get("path")) for item in normalized["files"]}
    for path, description in required_descriptions.items():
        if path not in paths:
            normalized["files"].append({"path": path, "description": description})
            paths.add(path)

    if requires_auth(normalized, user_prompt):
        auth_files = {
            "middleware/auth.js": "JWT authentication middleware. Verifies Bearer token with JWT_SECRET, attaches decoded payload to req.user, and exports named protect middleware.",
            "controllers/authController.js": "Authentication controller exporting registerUser, loginUser, and getProfile. Uses bcryptjs, jsonwebtoken, and User model.",
            "routes/authRoutes.js": "Authentication router mapping POST /register, POST /login, and protected GET /profile.",
        }
        for path, description in auth_files.items():
            if path not in paths:
                normalized["files"].append({"path": path, "description": description})
                paths.add(path)

    normalized["files"] = sorted(normalized["files"], key=lambda item: file_priority(str(item.get("path", ""))))
    return normalized


def deterministic_code_for(file_path: str, plan: Dict[str, Any], existing_contents: Dict[str, str]) -> str:
    planner_output = PlannerOutput.model_validate(plan)
    context = CodeGenContext(
        projectName=planner_output.projectName,
        architecture=planner_output.architecture,
        entities=planner_output.entities,
        features=planner_output.features,
        allFiles=planner_output.files,
        existingFileContents=existing_contents,
        styleProfile={},
    )
    agent = CodeGenAgent("http://localhost:11434", "deterministic-template")
    generated = agent.execute(FileSpec(path=file_path, description="Deterministic project support file."), context)
    return generated.content


def requires_auth(plan: Dict[str, Any], user_prompt: str = "") -> bool:
    haystack = " ".join(
        [
            user_prompt,
            json.dumps(plan.get("features", []), ensure_ascii=False),
            json.dumps(plan.get("entities", []), ensure_ascii=False),
        ]
    ).lower()
    return any(term in haystack for term in ["auth", "login", "register", "jwt", "password", "protected", "role-based", "rbac"])


def project_name_from_prompt(user_prompt: str) -> str:
    words = re.findall(r"[a-zA-Z0-9]+", user_prompt.lower())[:4]
    return "-".join(words) if words else "generated-backend"


def related_files_for(target_path: str, existing_contents: Dict[str, str], pattern: str = "mvc") -> List[Tuple[str, str]]:
    selected: List[str] = []

    def add(path: str) -> None:
        if path in existing_contents and path not in selected and path != target_path:
            selected.append(path)

    add("package.json")

    pattern = (pattern or "mvc").strip().lower()

    if pattern == "service-repository":
        add_service_repository_related(target_path, existing_contents, add)
    elif pattern == "clean-architecture":
        add_clean_architecture_related(target_path, existing_contents, add)
    elif pattern == "modular-monolith":
        add_modular_monolith_related(target_path, existing_contents, add)
    else:
        add_mvc_related(target_path, existing_contents, add)

    if target_path == "app.js":
        add("config/db.js")
        for path in sorted(existing_contents):
            if is_route_file(path, pattern) or path == "middleware/errorHandler.js":
                add(path)

    return [(path, existing_contents[path]) for path in selected]


def add_mvc_related(target_path: str, existing_contents: Dict[str, str], add) -> None:
    if target_path.startswith("controllers/"):
        stem = path_stem(target_path).replace("Controller", "").lower()
        for path in sorted(existing_contents):
            if path.startswith("models/") and path_stem(path).lower().startswith(stem):
                add(path)

    elif target_path.startswith("routes/"):
        stem = path_stem(target_path).replace("Routes", "").lower()
        for path in sorted(existing_contents):
            if path.startswith("controllers/") and path_stem(path).lower().startswith(stem):
                add(path)
        add_non_error_middleware(existing_contents, add)


def add_service_repository_related(target_path: str, existing_contents: Dict[str, str], add) -> None:
    if target_path.startswith("repositories/"):
        stem = path_stem(target_path).replace("Repository", "").lower()
        add_matching(existing_contents, add, "models/", stem)
    elif target_path.startswith("services/"):
        stem = path_stem(target_path).replace("Service", "").lower()
        add_matching(existing_contents, add, "repositories/", stem)
    elif target_path.startswith("controllers/"):
        stem = path_stem(target_path).replace("Controller", "").lower()
        add_matching(existing_contents, add, "services/", stem)
    elif target_path.startswith("routes/"):
        stem = path_stem(target_path).replace("Routes", "").lower()
        add_matching(existing_contents, add, "controllers/", stem)
        add_non_error_middleware(existing_contents, add)


def add_clean_architecture_related(target_path: str, existing_contents: Dict[str, str], add) -> None:
    if target_path.startswith("application/use-cases/"):
        stem = path_stem(target_path).replace("UseCases", "").lower()
        add_matching(existing_contents, add, "domain/entities/", stem)
        add_matching(existing_contents, add, "infrastructure/repositories/", stem)
    elif target_path.startswith("infrastructure/repositories/"):
        stem = path_stem(target_path).replace("Repository", "").lower()
        add_matching(existing_contents, add, "infrastructure/database/", stem)
    elif target_path.startswith("interfaces/controllers/"):
        stem = path_stem(target_path).replace("Controller", "").lower()
        add_matching(existing_contents, add, "application/use-cases/", stem)
    elif target_path.startswith("interfaces/routes/"):
        stem = path_stem(target_path).replace("Routes", "").lower()
        add_matching(existing_contents, add, "interfaces/controllers/", stem)
        add_non_error_middleware(existing_contents, add)


def add_modular_monolith_related(target_path: str, existing_contents: Dict[str, str], add) -> None:
    module = module_name(target_path)
    if not module:
        return
    if target_path.endswith("/repository.js"):
        add(f"modules/{module}/model.js")
    elif target_path.endswith("/service.js"):
        add(f"modules/{module}/repository.js")
    elif target_path.endswith("/controller.js"):
        add(f"modules/{module}/service.js")
    elif target_path.endswith("/routes.js"):
        add(f"modules/{module}/controller.js")
        add_non_error_middleware(existing_contents, add)


def add_matching(existing_contents: Dict[str, str], add, directory: str, stem: str) -> None:
    for path in sorted(existing_contents):
        if path.startswith(directory) and path_stem(path).lower().startswith(stem):
            add(path)


def add_non_error_middleware(existing_contents: Dict[str, str], add) -> None:
    for path in sorted(existing_contents):
        if path.startswith("middleware/") and not path.endswith("errorHandler.js"):
            add(path)


def is_route_file(path: str, pattern: str) -> bool:
    if pattern == "clean-architecture":
        return path.startswith("interfaces/routes/")
    if pattern == "modular-monolith":
        return path.startswith("modules/") and path.endswith("/routes.js")
    return path.startswith("routes/")


def module_name(path: str) -> str:
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "modules":
        return parts[1]
    return ""


def generate_code_with_retry(
    client: Any,
    provider: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    openai_url: str,
    api_key: str,
    max_output_tokens: int,
    temperature: float,
    retries: int,
) -> Tuple[str, Dict[str, Any]]:
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            if provider == "gemini":
                response = client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                    ),
                )
                text = response.text or ""
                usage = {}
                if getattr(response, "usage_metadata", None):
                    usage = response.usage_metadata.model_dump(exclude_none=True)
                return text, usage

            return generate_openai_compatible_code(
                url=openai_url,
                api_key=api_key,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            )
        except Exception as exc:  # noqa: BLE001 - dataset generation should keep running.
            last_error = exc
            wait_seconds = retry_delay_seconds(exc, attempt)
            print(f"[retry] {provider} call failed attempt {attempt}/{retries}: {exc}", file=sys.stderr)
            print(f"[retry] Waiting {wait_seconds:.1f}s before retrying.", file=sys.stderr)
            time.sleep(wait_seconds)

    raise RuntimeError(f"{provider} generation failed after {retries} attempt(s): {last_error}")


def generate_openai_compatible_code(
    url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int,
    temperature: float,
) -> Tuple[str, Dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    response = requests.post(
        url,
        headers=headers,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
            "stream": False,
        },
        timeout=300,
    )

    if response.status_code >= 400:
        body = response.text[:1000] if response.text else "(empty)"
        raise RuntimeError(f"HTTP {response.status_code} from OpenAI-compatible endpoint. Body: {body}")

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenAI-compatible response had no choices: {data}")

    message = choices[0].get("message") or {}
    content = message.get("content") or choices[0].get("text") or ""
    usage = data.get("usage") or {}
    return str(content), usage


def retry_delay_seconds(exc: Exception, attempt: int) -> float:
    message = str(exc)
    match = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+(?:\.\d+)?)s", message)
    if match:
        return float(match.group(1)) + 3.0

    match = re.search(r"Please retry in\s+(\d+(?:\.\d+)?)s", message, re.IGNORECASE)
    if match:
        return float(match.group(1)) + 3.0

    if "RESOURCE_EXHAUSTED" in message or "quota" in message.lower() or "rate" in message.lower():
        return 65.0

    return float(min(60, 2**attempt))


def validate_generated_code(file_path: str, code: str, architecture: Dict[str, Any]) -> Dict[str, Any]:
    issues: List[Dict[str, str]] = []
    pattern = architecture.get("pattern", "mvc")

    if len(code.strip()) < 10:
        issues.append({"severity": "error", "code": "too_short", "message": "Generated output is too short."})

    if "```" in code:
        issues.append({"severity": "warning", "code": "markdown_fence", "message": "Output contains markdown fence."})

    if file_path.endswith(".js") and re.search(r"\brequire\s*\(|module\.exports", code):
        issues.append({"severity": "warning", "code": "commonjs_used", "message": "Expected ES modules."})

    if detect_file_type(file_path, pattern) == "route" and re.search(r"mongoose\.Schema|mongoose\.model|new\s+Schema", code):
        issues.append({"severity": "error", "code": "route_contains_schema", "message": "Route contains schema/model code."})

    if detect_file_type(file_path, pattern) == "controller" and "express.Router" in code:
        issues.append({"severity": "error", "code": "controller_contains_router", "message": "Controller contains router code."})

    return {
        "architecture_pattern": pattern,
        "passed": not any(issue["severity"] == "error" for issue in issues),
        "issues": issues,
    }


def strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    fence_match = re.match(r"^```(?:[a-zA-Z0-9_.-]+)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    return cleaned


def file_priority(path: str) -> Tuple[int, str]:
    if path in FILE_PRIORITY:
        return FILE_PRIORITY[path], path
    for prefix, priority in FILE_PRIORITY.items():
        if prefix.endswith("/") and path.startswith(prefix):
            return priority, path
    return 99, path


def make_example_id(source_path: Path, record_index: int, project_name: str, file_path: str) -> str:
    raw = f"{source_path}:{record_index}:{project_name}:{file_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def path_stem(path: str) -> str:
    return Path(path).name.rsplit(".", 1)[0]


def dry_run_code_for(file_path: str) -> str:
    if file_path == "package.json":
        return '{"name":"dry-run","version":"1.0.0","type":"module"}'
    if file_path == ".env":
        return "PORT=3000\nMONGODB_URI=mongodb://localhost:27017/dry-run\nNODE_ENV=development"
    return f"// Dry run placeholder for {file_path}\nexport default {{}};"


if __name__ == "__main__":
    raise SystemExit(main())
