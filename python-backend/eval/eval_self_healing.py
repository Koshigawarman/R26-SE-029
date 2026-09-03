"""
Experiment 3: Self-Healing Recovery Rate Evaluation (Code-Level)
=================================================================
Injects deliberate syntax/import errors into 15 generated projects,
runs the Critic Agent + CodeGen fix loop, and measures whether the
code-level error is corrected within the 3-retry FSM limit.

Uses `node --check` (AST validation) — does NOT require npm install
or Docker, making this fast, reproducible, and infrastructure-free.

Academic framing: "Code-Level Self-Healing Rate" — measures whether
the multi-agent Critic+Codegen loop can autonomously identify and
correct injected source-code faults within 3 attempts.

Run from python-backend directory:
    export $(grep -v '^#' .env | xargs) && python eval/eval_self_healing.py

Outputs:
  - eval/results/self_healing_eval_results.json
  - eval/results/self_healing_eval_summary.txt
"""

import json
import os
import sys
import shutil
import random
import logging
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.codegen_agent import CodeGenAgent
from agents.critic_agent import CriticAgent
from schema import FileSpec, CodeGenContext, Architecture, Entity, Feature, EntityField

logging.basicConfig(level=logging.WARNING)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

CODEGEN_MODEL = os.getenv("CODEGEN_MODEL", "qwen2.5-coder:3b")
CRITIC_MODEL  = os.getenv("CRITIC_MODEL",  "qwen2.5-coder:7b")

USE_OPENAI_COMPATIBLE  = os.getenv("USE_OPENAI_COMPATIBLE", "false").lower() == "true"
OPENAI_COMPATIBLE_URL  = os.getenv("OPENAI_COMPATIBLE_URL", "")
OPENAI_COMPATIBLE_KEY  = os.getenv("OPENAI_COMPATIBLE_API_KEY", "")
OPENAI_COMPATIBLE_PROV = os.getenv("OPENAI_COMPATIBLE_PROVIDER", "modal")

MAX_RETRIES  = 3
DATASET_PATH = Path(__file__).parent.parent / "datasets" / "approved_plans.json"
OUTPUT_DIR   = Path(__file__).parent / "results"

RANDOM_SEED = 42
random.seed(RANDOM_SEED)

# ─────────────────────────────────────────────────────────────────────────────
# Error injectors (code-level only — no runtime needed)
# ─────────────────────────────────────────────────────────────────────────────

def _corrupt_first_local_import(content: str) -> str:
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if ("from './" in line or 'from "./' in line) and "express" not in line:
            lines[i] = line.replace("from './", "from './BROKEN_PATH/")
            break
    return "\n".join(lines)

def _remove_default_export(content: str) -> str:
    return content.replace("export default app", "// REMOVED_EXPORT default app")

def _inject_syntax_error(content: str) -> str:
    return content + "\n// INJECTED SYNTAX ERROR\nconst _broken = {"

def _inject_missing_import(content: str) -> str:
    lines = content.split("\n")
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("import "):
            insert_at = i
            break
    lines.insert(insert_at, "import _nonExistent from './missing/nonExistentModule.js';")
    return "\n".join(lines)

def _corrupt_module_type(content: str) -> str:
    """Remove 'type': 'module' from package.json to break ESM."""
    return content.replace('"type": "module"', '"type": "commonjs"')

ERROR_INJECTORS = [
    {"name": "syntax_error",     "description": "Inject unclosed brace syntax error",         "inject": _inject_syntax_error,       "target": "app.js",       "is_js": True},
    {"name": "wrong_import",     "description": "Corrupt a local import path",                 "inject": _corrupt_first_local_import,"target": "app.js",       "is_js": True},
    {"name": "missing_export",   "description": "Remove the default export from app.js",       "inject": _remove_default_export,     "target": "app.js",       "is_js": True},
    {"name": "missing_import",   "description": "Inject reference to a non-existent module",  "inject": _inject_missing_import,     "target": "app.js",       "is_js": True},
    {"name": "module_type",      "description": "Break ESM by changing package.json type",     "inject": _corrupt_module_type,       "target": "package.json", "is_js": False},
]

# ─────────────────────────────────────────────────────────────────────────────
# AST check (no npm install needed)
# ─────────────────────────────────────────────────────────────────────────────

def ast_passes(file_path: Path) -> tuple[bool, str]:
    """Run node --check. Returns (passed, error)."""
    if not file_path.exists():
        return False, "File does not exist"
    if file_path.suffix != ".js":
        return True, "Non-JS file — skipping AST"
    try:
        r = subprocess.run(["node", "--check", str(file_path)],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0, r.stderr.strip()
    except Exception as e:
        return False, str(e)

def has_export_default(content: str) -> bool:
    return "export default" in content and "// REMOVED_EXPORT" not in content

def project_is_healthy(project_path: Path, injector: dict) -> bool:
    """
    Determine if the project has been healed at the code level.
    Uses AST check for JS files and heuristic pattern checks.
    """
    target = project_path / injector["target"]
    if not target.exists():
        return False

    content = target.read_text(encoding="utf-8")

    # Check 1: AST must pass for JS files
    if injector["is_js"]:
        passed, _ = ast_passes(target)
        if not passed:
            return False

    # Check 2: injector-specific pattern checks
    name = injector["name"]
    if name == "syntax_error":
        return "const _broken = {" not in content
    if name == "wrong_import":
        return "BROKEN_PATH" not in content
    if name == "missing_export":
        return has_export_default(content)
    if name == "missing_import":
        return "_nonExistent" not in content
    if name == "module_type":
        return '"type": "module"' in content

    return True

# ─────────────────────────────────────────────────────────────────────────────
# Project generation helpers
# ─────────────────────────────────────────────────────────────────────────────

def blueprint_to_context(plan_dict: dict):
    arch_data = plan_dict.get("architecture", {})
    arch = Architecture(
        stack=arch_data.get("stack", "node-express-mongoose"),
        pattern=arch_data.get("pattern", "mvc"),
        language=arch_data.get("language", "javascript"),
        moduleSystem=arch_data.get("moduleSystem", "esm"),
        database=arch_data.get("database", "mongodb"),
        orm=arch_data.get("orm", "mongoose"),
    )
    entities = [Entity(name=e["name"], fields=[EntityField(**f) for f in e.get("fields", [])],
                       description=e.get("description", "")) for e in plan_dict.get("entities", [])]
    features = [Feature(name=f["name"], description=f.get("description", ""))
                for f in plan_dict.get("features", [])]
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


def generate_project(blueprint: dict, codegen: CodeGenAgent, project_path: Path) -> bool:
    project_path.mkdir(parents=True, exist_ok=True)
    try:
        files, context = blueprint_to_context(blueprint)
    except Exception as e:
        return False

    existing = {}
    for file_spec in sorted(files, key=lambda f: f.path):
        context.existingFileContents = existing
        try:
            g = codegen.execute(file_spec, context)
        except Exception:
            continue
        if g and g.content and g.status != "error":
            fp = project_path / file_spec.path
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(g.content, encoding="utf-8")
            existing[file_spec.path] = g.content
    return True

# ─────────────────────────────────────────────────────────────────────────────
# Self-healing loop
# ─────────────────────────────────────────────────────────────────────────────

def self_heal(project_path: Path, injector: dict,
              codegen: CodeGenAgent, critic: CriticAgent,
              max_retries: int) -> dict:
    target_rel  = injector["target"]
    target_file = project_path / target_rel

    existing = {}
    for f in project_path.rglob("*.js"):
        if "node_modules" not in f.parts:
            try:
                existing[str(f.relative_to(project_path))] = f.read_text(encoding="utf-8")
            except Exception:
                pass
    # Also read package.json
    pkg = project_path / "package.json"
    if pkg.exists():
        existing["package.json"] = pkg.read_text(encoding="utf-8")

    attempts = []

    for attempt in range(1, max_retries + 1):
        healthy = project_is_healthy(project_path, injector)
        status = "✅ HEALED" if healthy else "❌ FAULT PRESENT"
        print(f"      Attempt {attempt}/{max_retries}: {status}")

        if healthy:
            attempts.append({"attempt": attempt, "outcome": "healed"})
            return {"healed": True, "healed_on_attempt": attempt, "attempts": attempts}

        attempts.append({"attempt": attempt, "outcome": "fault_present"})

        if attempt >= max_retries:
            break

        # Read current broken content for error description
        if not target_file.exists():
            break
        broken_content = target_file.read_text(encoding="utf-8")

        # Build error description from AST check
        if injector["is_js"]:
            _, ast_err = ast_passes(target_file)
            error_log = ast_err or f"Injected fault: {injector['description']}"
        else:
            error_log = f"Injected fault: {injector['description']}"

        # Fake a minimal error list for Critic
        from schema import RuntimeErrorInfo
        errors = [RuntimeErrorInfo(
            message=error_log[:200],
            file=target_rel,
            line=None, column=None,
            stack=error_log[:400],
            type="SyntaxError" if "syntax" in injector["name"] else "ModuleNotFoundError",
        )]

        # Critic Agent
        print(f"      → Calling Critic Agent...", end=" ", flush=True)
        try:
            strategy = critic.execute(
                errors=errors,
                stderr=error_log,
                stdout="",
                memory_matches=[],
                file_list=list(existing.keys()),
                attempt=attempt,
                file_contents=existing,
            )
            print(f"Strategy: {strategy.fixing_strategy[:60]}...")
        except Exception as e:
            print(f"⚠ Critic failed: {e}")
            continue

        # CodeGen fix
        original = existing.get(target_rel, broken_content)
        print(f"      → Fixing: {target_rel}...", end=" ", flush=True)
        try:
            fixed = codegen.fix_file_with_strategy(
                file_path=target_rel,
                original_content=original,
                error_log=error_log,
                critic_strategy=strategy.fixing_strategy,
                instructions_for_code_agent=strategy.instructions_for_code_agent,
                file_list=list(existing.keys()),
                architecture={"stack": "node-express-mongoose", "pattern": "mvc",
                              "language": "javascript", "moduleSystem": "esm",
                              "database": "mongodb", "orm": "mongoose"},
            )
        except Exception as e:
            print(f"⚠ Fix failed: {e}")
            continue

        if fixed and fixed.content and fixed.status == "fixed":
            target_file.write_text(fixed.content, encoding="utf-8")
            existing[target_rel] = fixed.content
            print(f"✅ Fixed")
        else:
            print(f"❌ Nothing generated")

    # Final check
    healed = project_is_healthy(project_path, injector)
    return {"healed": healed, "healed_on_attempt": None, "attempts": attempts}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    codegen = CodeGenAgent(
        ollama_url=OLLAMA_URL, model=CODEGEN_MODEL,
        use_openai_compatible=USE_OPENAI_COMPATIBLE,
        openai_compatible_url=OPENAI_COMPATIBLE_URL,
        openai_compatible_api_key=OPENAI_COMPATIBLE_KEY,
        openai_compatible_provider=OPENAI_COMPATIBLE_PROV,
    )
    critic = CriticAgent(
        ollama_url=OLLAMA_URL, model=CRITIC_MODEL,
        use_openai_compatible=USE_OPENAI_COMPATIBLE,
        openai_compatible_url=OPENAI_COMPATIBLE_URL,
        openai_compatible_api_key=OPENAI_COMPATIBLE_KEY,
        openai_compatible_provider=OPENAI_COMPATIBLE_PROV,
    )

    raw_items = json.loads(DATASET_PATH.read_text())
    blueprints = []
    for item in raw_items[:15]:
        try:
            blueprints.append(json.loads(item["plan_json"]))
        except Exception:
            pass

    total = len(blueprints)

    print(f"\n{'='*60}")
    print(f"  Self-Healing Evaluation (Code-Level AST) — {total} projects")
    print(f"  Critic Model: {CRITIC_MODEL}")
    print(f"  Max Retries: {MAX_RETRIES}")
    print(f"  Method: node --check (no npm install / Docker needed)")
    print(f"{'='*60}\n")

    all_results = []
    healed_count = 0
    total_trials = 0
    retry_dist = {1: 0, 2: 0, 3: 0, "failed": 0}

    for i, blueprint in enumerate(blueprints, 1):
        project_name = blueprint.get("projectName", f"project-{i}")
        print(f"\n[{i:02d}/{total}] {project_name}")

        with tempfile.TemporaryDirectory(prefix="eval-heal-") as tmp:
            project_path = Path(tmp) / project_name

            print(f"  Generating...", end=" ", flush=True)
            ok = generate_project(blueprint, codegen, project_path)
            if not ok:
                print(f"⚠ Skipping — generation failed")
                continue

            # Check app.js exists and is initially healthy
            app_js = project_path / "app.js"
            if not app_js.exists():
                print(f"⚠ Skipping — no app.js generated")
                continue

            initial_pass, _ = ast_passes(app_js)
            if not initial_pass:
                print(f"⚠ Skipping — initial AST failed (model error, not our fault)")
                continue
            print(f"✅ Generated and verified")

            # Pick injector
            injector = random.choice(ERROR_INJECTORS)
            print(f"  Injecting: [{injector['name']}] {injector['description']}")

            target = project_path / injector["target"]
            if not target.exists():
                print(f"  ⚠ Target file missing, skipping")
                continue

            original_content = target.read_text(encoding="utf-8")
            broken_content   = injector["inject"](original_content)

            if broken_content == original_content:
                print(f"  ⚠ Injection had no effect, skipping")
                continue

            target.write_text(broken_content, encoding="utf-8")

            # Verify injection worked
            if project_is_healthy(project_path, injector):
                print(f"  ⚠ Error already self-corrected by content — skipping")
                continue

            total_trials += 1
            result = self_heal(project_path, injector, codegen, critic, MAX_RETRIES)
            result["project"] = project_name
            result["error_injected"] = injector["name"]
            all_results.append(result)

            if result["healed"]:
                healed_count += 1
                on = result["healed_on_attempt"]
                if on in retry_dist:
                    retry_dist[on] += 1
            else:
                retry_dist["failed"] += 1

            label = f"✅ HEALED (attempt {result['healed_on_attempt']})" if result["healed"] else "❌ NOT HEALED"
            print(f"  → {label}")

    # ── Metrics ──
    rate = (healed_count / total_trials * 100) if total_trials > 0 else 0.0
    cum1 = retry_dist[1] / total_trials * 100 if total_trials else 0
    cum2 = (retry_dist[1] + retry_dist[2]) / total_trials * 100 if total_trials else 0
    cum3 = rate

    summary = f"""
Self-Healing Recovery Rate Evaluation (Code-Level)
====================================================
Date:              {datetime.now().strftime('%Y-%m-%d %H:%M')}
Critic Model:      {CRITIC_MODEL}
Method:            Code-level AST validation (node --check)
Total trials:      {total_trials}
Max retries (FSM): {MAX_RETRIES}

Results:
  Healed:            {healed_count} / {total_trials}
  Recovery Rate:     {rate:.1f}%
  Not Healed:        {total_trials - healed_count}

Retry Distribution:
  Healed on attempt 1: {retry_dist[1]}
  Healed on attempt 2: {retry_dist[2]}
  Healed on attempt 3: {retry_dist[3]}
  Failed all attempts:  {retry_dist['failed']}

--- For TABLE II (System Evaluation) ---
Orchestration Engine | Autonomous Code-Level Self-Healing Rate | {rate:.1f}%

--- For TABLE III (Baseline Comparison) ---
Zero-Shot / Linear MAS  | Self-Heals | No
Proposed Framework      | Self-Heals | {rate:.1f}%

--- For Fig. 2 (Convergence Chart) ---
Retry 1 cumulative: {cum1:.1f}%
Retry 2 cumulative: {cum2:.1f}%
Retry 3 cumulative: {cum3:.1f}%
"""

    raw_output = {
        "timestamp": datetime.now().isoformat(),
        "critic_model": CRITIC_MODEL,
        "method": "code_level_ast",
        "total_trials": total_trials,
        "healed_count": healed_count,
        "recovery_rate": round(rate, 1),
        "retry_distribution": retry_dist,
        "trial_results": all_results,
    }

    (OUTPUT_DIR / "self_healing_eval_results.json").write_text(json.dumps(raw_output, indent=2))
    (OUTPUT_DIR / "self_healing_eval_summary.txt").write_text(summary)

    print(summary)
    print(f"Results saved to: {OUTPUT_DIR}/self_healing_eval_summary.txt")


if __name__ == "__main__":
    main()
