"""
Experiment 2: Coding Agent AST Compilation Rate Evaluation
===========================================================
Takes 30 blueprints from approved_plans.json + generated ones,
runs the Coding Agent on each, then checks every .js file with
`node --check` to verify AST syntax validity.

Run from python-backend directory:
    python eval/eval_codegen.py

Outputs:
  - eval/results/codegen_eval_results.json
  - eval/results/codegen_eval_summary.txt
"""

import json
import os
import sys
import shutil
import logging
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.codegen_agent import CodeGenAgent
from schema import FileSpec, CodeGenContext, Architecture, Entity, Feature, EntityField

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL      = os.getenv("CODEGEN_MODEL", "qwen2.5-coder:3b")

USE_OPENAI_COMPATIBLE  = os.getenv("USE_OPENAI_COMPATIBLE", "false").lower() == "true"
OPENAI_COMPATIBLE_URL  = os.getenv("OPENAI_COMPATIBLE_URL", "")
OPENAI_COMPATIBLE_KEY  = os.getenv("OPENAI_COMPATIBLE_API_KEY", "")
OPENAI_COMPATIBLE_PROV = os.getenv("OPENAI_COMPATIBLE_PROVIDER", "modal")

DATASET_PATH = Path(__file__).parent.parent / "datasets" / "approved_plans.json"

# Files to skip AST check on (non-JS files)
SKIP_AST_EXTENSIONS = {".json", ".env", ".md", ".txt", ".yaml", ".yml"}

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_blueprints(n: int = 15) -> list[dict]:
    """Load up to n blueprints from the dataset."""
    raw = json.loads(DATASET_PATH.read_text())
    return raw[:n]


def blueprint_to_codegen_context(plan_dict: dict) -> tuple[list[FileSpec], CodeGenContext]:
    """Convert a plan JSON dict to a CodeGenContext for the CodeGen Agent."""
    arch_data = plan_dict.get("architecture", {})
    arch = Architecture(
        stack=arch_data.get("stack", "node-express-mongoose"),
        pattern=arch_data.get("pattern", "mvc"),
        language=arch_data.get("language", "javascript"),
        moduleSystem=arch_data.get("moduleSystem", "esm"),
        database=arch_data.get("database", "mongodb"),
        orm=arch_data.get("orm", "mongoose"),
    )

    entities = []
    for e in plan_dict.get("entities", []):
        fields = [EntityField(**f) for f in e.get("fields", [])]
        entities.append(Entity(name=e["name"], fields=fields, description=e.get("description", "")))

    features = [
        Feature(name=f["name"], description=f.get("description", ""))
        for f in plan_dict.get("features", [])
    ]

    files = [FileSpec(path=f["path"], description=f["description"]) for f in plan_dict.get("files", [])]

    context = CodeGenContext(
        projectName=plan_dict["projectName"],
        architecture=arch,
        entities=entities,
        features=features,
        allFiles=files,
        existingFileContents={},
        styleProfile={},
    )
    return files, context


def check_ast(js_file_path: Path) -> tuple[bool, str]:
    """Run `node --check` on a .js file. Returns (passed, error_output)."""
    try:
        result = subprocess.run(
            ["node", "--check", str(js_file_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "AST check timed out"
    except FileNotFoundError:
        return False, "node not found in PATH"
    except Exception as e:
        return False, str(e)


def run_codegen_for_blueprint(blueprint: dict, agent: CodeGenAgent) -> dict:
    """
    Runs CodeGen Agent for all files in a single blueprint.
    Returns a dict with per-file AST results.
    """
    project_name = blueprint.get("projectName", "unknown")
    print(f"\n  Project: {project_name}")

    try:
        files, context = blueprint_to_codegen_context(blueprint)
    except Exception as e:
        print(f"    ⚠ Failed to parse blueprint: {e}")
        return {"project": project_name, "error": str(e), "files": []}

    file_results = []
    existing_contents = {}

    with tempfile.TemporaryDirectory(prefix="eval-codegen-") as tmp_dir:
        project_dir = Path(tmp_dir) / project_name
        project_dir.mkdir(parents=True, exist_ok=True)

        # Sort files by generation priority (models first, then controllers, then routes)
        priority_order = ["models/", "repositories/", "services/", "controllers/", "routes/", "middleware/", "config/", "app.js", "package.json"]

        def file_priority(f: FileSpec) -> int:
            for i, prefix in enumerate(priority_order):
                if f.path.startswith(prefix) or f.path == prefix:
                    return i
            return len(priority_order)

        sorted_files = sorted(files, key=file_priority)

        for file_spec in sorted_files:
            # Skip non-JS files from AST check
            ext = Path(file_spec.path).suffix
            is_js = ext == ".js"

            print(f"    → Generating: {file_spec.path}", end=" ", flush=True)

            # Update context with existing contents
            context.existingFileContents = existing_contents

            try:
                generated = agent.execute(file_spec, context)
            except Exception as e:
                print(f"❌ EXCEPTION: {e}")
                file_results.append({
                    "file": file_spec.path,
                    "generated": False,
                    "ast_checked": False,
                    "ast_passed": False,
                    "error": str(e),
                })
                continue

            if not generated or not generated.content or generated.status == "error":
                msg = generated.errorMessage if generated else "no output"
                print(f"❌ GENERATION FAILED: {msg}")
                file_results.append({
                    "file": file_spec.path,
                    "generated": False,
                    "ast_checked": False,
                    "ast_passed": False,
                    "error": msg,
                })
                continue

            # Write to temp dir
            full_path = project_dir / file_spec.path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(generated.content, encoding="utf-8")
            existing_contents[file_spec.path] = generated.content

            # AST check only for .js files
            if is_js:
                ast_passed, ast_error = check_ast(full_path)
                status_str = "✅ AST PASS" if ast_passed else f"❌ AST FAIL"
                print(f"→ {status_str}")
                file_results.append({
                    "file": file_spec.path,
                    "generated": True,
                    "ast_checked": True,
                    "ast_passed": ast_passed,
                    "ast_error": ast_error if not ast_passed else "",
                })
            else:
                print(f"→ ✅ Generated (no AST check)")
                file_results.append({
                    "file": file_spec.path,
                    "generated": True,
                    "ast_checked": False,
                    "ast_passed": None,  # N/A for non-JS
                    "ast_error": "",
                })

    return {"project": project_name, "files": file_results}


def main():
    output_dir = Path(__file__).parent / "results"
    output_dir.mkdir(parents=True, exist_ok=True)

    agent = CodeGenAgent(
        ollama_url=OLLAMA_URL,
        model=MODEL,
        use_openai_compatible=USE_OPENAI_COMPATIBLE,
        openai_compatible_url=OPENAI_COMPATIBLE_URL,
        openai_compatible_api_key=OPENAI_COMPATIBLE_KEY,
        openai_compatible_provider=OPENAI_COMPATIBLE_PROV,
    )

    blueprints_raw = load_blueprints(n=15)

    # Parse plan_json strings into dicts
    blueprints = []
    for item in blueprints_raw:
        try:
            plan = json.loads(item["plan_json"])
            blueprints.append(plan)
        except Exception as e:
            print(f"⚠ Failed to parse blueprint from dataset: {e}")

    total_projects = len(blueprints)

    print(f"\n{'='*60}")
    print(f"  Coding Agent AST Evaluation — {total_projects} blueprints")
    print(f"  Model: {MODEL}")
    print(f"  OpenAI-Compatible: {USE_OPENAI_COMPATIBLE}")
    print(f"{'='*60}")

    all_project_results = []
    total_js_files      = 0
    total_ast_passed    = 0

    for i, blueprint in enumerate(blueprints, 1):
        print(f"\n[{i:02d}/{total_projects}]", end="")
        result = run_codegen_for_blueprint(blueprint, agent)
        all_project_results.append(result)

        # Tally JS file AST results
        for f in result.get("files", []):
            if f.get("ast_checked"):
                total_js_files += 1
                if f.get("ast_passed"):
                    total_ast_passed += 1

    # ── Metrics ──
    ast_pass_rate = (total_ast_passed / total_js_files * 100) if total_js_files > 0 else 0.0

    projects_fully_passing = sum(
        1 for r in all_project_results
        if all(
            f.get("ast_passed", True)
            for f in r.get("files", [])
            if f.get("ast_checked")
        ) and len(r.get("files", [])) > 0
    )
    project_pass_rate = (projects_fully_passing / total_projects * 100) if total_projects > 0 else 0.0

    summary = f"""
Coding Agent AST Compilation Rate Evaluation
=============================================
Date:              {datetime.now().strftime('%Y-%m-%d %H:%M')}
Model:             {MODEL}
Total Projects:    {total_projects}

File-Level AST Results:
  JS files evaluated:   {total_js_files}
  AST pass:             {total_ast_passed}
  AST fail:             {total_js_files - total_ast_passed}
  AST Pass Rate:        {ast_pass_rate:.1f}%

Project-Level (all files pass):
  Fully passing:        {projects_fully_passing} / {total_projects}
  Project Pass Rate:    {project_pass_rate:.1f}%

--- For TABLE II (System Evaluation) ---
Coding Agent | AST First-Pass Compilation Rate | {ast_pass_rate:.1f}%

--- For TABLE III (Baseline Comparison) ---
Proposed Framework | Code Compiles | {ast_pass_rate:.1f}%
(For zero-shot baseline, re-run after disabling style_profile in CodeGenAgent)
"""

    raw_output = {
        "timestamp": datetime.now().isoformat(),
        "model": MODEL,
        "total_projects": total_projects,
        "total_js_files": total_js_files,
        "total_ast_passed": total_ast_passed,
        "ast_pass_rate": round(ast_pass_rate, 1),
        "projects_fully_passing": projects_fully_passing,
        "project_pass_rate": round(project_pass_rate, 1),
        "project_results": all_project_results,
    }

    json_path = output_dir / "codegen_eval_results.json"
    txt_path  = output_dir / "codegen_eval_summary.txt"

    json_path.write_text(json.dumps(raw_output, indent=2))
    txt_path.write_text(summary)

    print(summary)
    print(f"Results saved to:")
    print(f"  {json_path}")
    print(f"  {txt_path}")


if __name__ == "__main__":
    main()
