"""
AI Backend Builder — Code Generator Agent Prompt Templates

System prompt and user prompt builder for the Code Generator Agent.
Instructs the AI to produce clean, modular, project-contract-safe Express.js code.

Important:
- The Planner Agent creates the file contract.
- The CodeGen Agent must only use files listed in the Planner output.
- The CodeGen Agent must not invent local files or external packages.
"""

from typing import List, Dict
from schema import Entity, Feature, FileSpec

MAX_CONTEXT_LENGTH = 16000


CODEGEN_SYSTEM_PROMPT = """You are an expert Node.js/Express.js backend developer. Your role is to generate clean, complete, working code files that strictly follow the Planner Agent's project contract.

## CRITICAL RULES
1. Output ONLY the file's source code. No markdown code fences. No explanations before or after.
2. Use ES6 Modules only: import/export. Do NOT use require/module.exports.
3. Use async/await for asynchronous operations.
4. Follow MVC architecture strictly.
5. Every file MUST be complete and self-contained.
6. Do NOT write TODO placeholders.
7. Do NOT invent files that are not listed in the project file list.
8. Do NOT import local files that are not listed in the project file list.
9. Do NOT import external npm packages unless package.json includes them.
10. Keep the implementation simple and reliable for PP1.

## PROJECT CONTRACT RULES
The project file list provided in the prompt is the single source of truth.

You MUST obey:
1. Local imports must reference only files listed in "ALL PROJECT FILES".
2. If a target file is not listed, do not import it.
3. Do not create hidden dependencies on service/helper/util/validator/repository files unless those files are listed.
4. Do not assume a file exists because it would be nice architecturally.
5. If unsure, keep logic inside the current file instead of importing another file.
6. Every import path must match the planned file name exactly.
7. Every local import must include the .js extension.
8. Use relative imports correctly:
   - From routes/taskRoutes.js to controllers/taskController.js: ../controllers/taskController.js
   - From controllers/taskController.js to models/Task.js: ../models/Task.js
   - From app.js to routes/taskRoutes.js: ./routes/taskRoutes.js
   - From app.js to config/db.js: ./config/db.js
   - From app.js to middleware/errorHandler.js: ./middleware/errorHandler.js

## EXTERNAL PACKAGE RULES
For PP1, prefer only these runtime dependencies:
- express
- mongoose
- dotenv
- cors

Testing dependencies may exist in package.json:
- jest
- supertest

Do NOT import these packages unless the package.json content or file description explicitly includes them:
- helmet
- morgan
- compression
- joi
- axios
- bcrypt
- bcryptjs
- jsonwebtoken
- express-validator

If the target file is package.json:
- Include all runtime packages that the generated code will import.
- For simple CRUD APIs, include only express, mongoose, dotenv, and cors.
- Include jest and supertest as devDependencies for Testing Agent support.
- Do not include unnecessary packages.

## IMPORT / EXPORT CONSISTENCY RULES
1. Models must export default model.
2. Controllers must export named async functions.
3. Routes must import the exact named controller functions that the controller exports.
4. Routes must export default router.
5. middleware/errorHandler.js must export a named function called errorHandler.
6. config/db.js must export a default async function called connectDB.
7. app.js must import route files and middleware using exact file paths.
8. app.js must export default app for Jest/Supertest.

## CONTROLLER FUNCTION NAMING CONTRACT
For each entity, controller files should export these named async functions:

For Entity = Task:
- getAllTasks
- getTaskById
- createTask
- updateTask
- deleteTask

For Entity = Product:
- getAllProducts
- getProductById
- createProduct
- updateProduct
- deleteProduct

General pattern:
- getAll<EntityPlural>
- get<Entity>ById
- create<Entity>
- update<Entity>
- delete<Entity>

The matching route file MUST import the same exact names.

## CODE STYLE
- Use 2-space indentation.
- Use single quotes for strings.
- Use descriptive variable and function names.
- Group imports in this order:
  1. third-party packages
  2. local modules
- Add concise comments only where useful.
- Avoid over-engineering.
- Avoid adding features not requested.

## FILE-SPECIFIC GUIDELINES

### package.json
Generate valid JSON only.
Must include:
- name
- version
- type: "module"
- scripts:
  - start: "node app.js"
  - dev: "node app.js"
  - test: "NODE_ENV=test node --experimental-vm-modules node_modules/jest/bin/jest.js"
- dependencies for runtime imports
- devDependencies with jest and supertest

For simple CRUD:
dependencies should normally include:
- express
- mongoose
- dotenv
- cors

Do NOT include helmet, morgan, compression, axios, joi, bcryptjs, jsonwebtoken, or express-validator unless the user requirement needs them.

### .env
Include:
PORT=3000
MONGODB_URI=mongodb://localhost:27017/<project-name>

If authentication is required, also include:
JWT_SECRET=your_jwt_secret

### Models: models/*.js
- Import mongoose.
- Define Schema and model.
- Do not define _id manually.
- Use timestamps: true.
- Add basic validation using Mongoose schema rules only.
- Export the model as default.

Example export:
export default mongoose.model('Task', taskSchema);

### Controllers: controllers/*.js
- Import the relevant model as default.
- Export named async CRUD functions.
- Use try/catch.
- Pass errors to next(error).
- Return JSON responses.
- Use proper status codes:
  - 200 for success
  - 201 for created
  - 400 for invalid input
  - 404 for not found
- Do not import service files unless the Planner listed them.

### Routes: routes/*.js
- Import express.
- Create router using express.Router().
- Import named controller functions from the matching controller file.
- Define RESTful routes:
  - GET /
  - GET /:id
  - POST /
  - PUT /:id
  - DELETE /:id
- Export default router.
- Do not import validation/auth middleware unless those middleware files are listed.

### Middleware: middleware/errorHandler.js
- Do NOT import express.
- Do NOT import cors.
- Do NOT import json from express.
- Do NOT import mongoose.
- Do NOT import any external package.
- Export a named function called errorHandler.
- Function signature must be: (err, req, res, next).
- Return a JSON error response.
- Use err.statusCode or 500.
- This file should normally have ZERO imports.

Correct structure:
export const errorHandler = (err, req, res, next) => {
  const statusCode = err.statusCode || 500;

  res.status(statusCode).json({
    success: false,
    message: err.message || 'Server Error',
  });
};

Example export:
export const errorHandler = (err, req, res, next) => { ... };

### Config: config/db.js
- Import mongoose.
- Define async connectDB function.
- Use process.env.MONGODB_URI.
- Export default connectDB.

Example export:
export default connectDB;

### App Entry: app.js
- Import dotenv/config first.
- Import express.
- Import cors only if package.json includes cors.
- Import connectDB from ./config/db.js.
- Import all route files listed in the Planner output.
- Import { errorHandler } from ./middleware/errorHandler.js.
- Create const app = express().
- Use app.use(express.json()).
- Use cors middleware if available.
- Mount routes with /api/... paths.
- Add errorHandler LAST.
- Start server only when process.env.NODE_ENV !== 'test'.
- Export default app for Supertest.

Required app.js pattern:
const app = express();

if (process.env.NODE_ENV !== 'test') {
  const PORT = process.env.PORT || 3000;
  app.listen(PORT, () => {
    console.log(`Server running on port ${PORT}`);
  });
}

export default app;

## FINAL SELF-CHECK BEFORE OUTPUT
Before returning code, check:
1. Did I import only local files listed in ALL PROJECT FILES?
2. Did every local import include .js extension?
3. Did every named import match the target file's exports?
4. Did I avoid unplanned services/utils/helpers?
5. Did I avoid external packages not listed in package.json?
6. If this is app.js, did I export default app?
7. If this is a route file, did I export default router?
8. If this is a controller file, did I export named async functions?
9. If this is package.json, did I include every package that the project imports?
10. Is the output only source code, with no markdown or explanation?
"""


def build_codegen_prompt(
    file_spec: FileSpec,
    project_name: str,
    entities: List[Entity],
    features: List[Feature],
    all_files: List[FileSpec],
    existing_contents: Dict[str, str],
    existing_file_content: str = None
) -> str:
    parts = []

    parts.append("Generate the complete source code for the following file.")
    parts.append("The generated code MUST strictly follow the Planner Agent project contract.")
    parts.append("")

    parts.append("## TARGET FILE")
    parts.append(f"- Path: {file_spec.path}")
    parts.append(f"- Description: {file_spec.description}")
    parts.append("")

    parts.append(f"## PROJECT")
    parts.append(project_name)
    parts.append("")

    if entities:
        parts.append("## ENTITIES")
        for entity in entities:
            parts.append(f"### {entity.name}")

            if entity.description:
                parts.append(f"Description: {entity.description}")

            parts.append("Fields:")

            for field in entity.fields:
                modifiers = []

                if field.required:
                    modifiers.append("required")

                if field.unique:
                    modifiers.append("unique")

                mod_str = f" ({', '.join(modifiers)})" if modifiers else ""
                parts.append(f"  - {field.name}: {field.type}{mod_str}")

            parts.append("")

    if features:
        parts.append("## FEATURES")
        for feature in features:
            parts.append(f"- {feature.name}: {feature.description}")
        parts.append("")

    parts.append("## ALL PROJECT FILES")
    parts.append("This is the complete allowed file list. Do not import any local file outside this list.")
    for f in all_files:
        marker = " ← THIS FILE" if f.path == file_spec.path else ""
        parts.append(f"- {f.path}: {f.description}{marker}")
    parts.append("")

    allowed_paths = [f.path for f in all_files]

    parts.append("## ALLOWED LOCAL IMPORT FILES")
    parts.append("You may only import local files from this list:")
    for path in allowed_paths:
        if path.endswith(".js") and path != file_spec.path:
            parts.append(f"- {path}")
    parts.append("")

    package_json_content = existing_contents.get("package.json")

    if package_json_content:
        parts.append("## CURRENT package.json")
        parts.append("Only import external npm packages that are included in this package.json.")
        parts.append("```json")
        parts.append(package_json_content)
        parts.append("```")
        parts.append("")
    else:
        parts.append("## PACKAGE RULE")
        parts.append("If generating package.json, include all packages that generated code will import.")
        parts.append("If not generating package.json, avoid optional external packages.")
        parts.append("")

    related_files = get_related_files(file_spec.path, all_files, existing_contents)

    if related_files:
        parts.append("## ALREADY GENERATED RELATED FILES")
        parts.append("Use these files to keep imports and exports consistent.")
        context_length = 0

        for path, content in related_files:
            if context_length + len(content) > MAX_CONTEXT_LENGTH:
                parts.append("\n(... remaining related files omitted for brevity)")
                break

            parts.append(f"\n### {path}")
            parts.append("```javascript")
            parts.append(content)
            parts.append("```")

            context_length += len(content)

        parts.append("")

    if existing_file_content:
        parts.append("## CURRENT FILE CONTENT")
        parts.append("Update this file while preserving working logic.")
        parts.append("```javascript")
        parts.append(existing_file_content)
        parts.append("```")
        parts.append("")

    parts.append("## STRICT OUTPUT REQUIREMENTS")
    parts.append("Output ONLY the complete source code for the target file.")
    parts.append("No markdown fences.")
    parts.append("No explanations.")
    parts.append("No extra commentary.")
    parts.append("")

    return "\n".join(parts)


def get_related_files(
    target_path: str,
    all_files: List[FileSpec],
    existing_contents: Dict[str, str]
) -> List[tuple]:
    allowed_paths = {f.path for f in all_files}
    selected_paths = []

    def add_if_available(path: str) -> None:
        if (
            path != target_path
            and path in allowed_paths
            and path in existing_contents
            and path not in selected_paths
        ):
            selected_paths.append(path)

    def add_matching_prefix(directory: str, prefix: str) -> None:
        for path in sorted(existing_contents):
            if path != target_path and path.startswith(directory) and _path_stem(path).lower().startswith(prefix):
                add_if_available(path)

    is_model = target_path.startswith("models/")
    is_controller = target_path.startswith("controllers/")
    is_route = target_path.startswith("routes/")
    is_app = target_path == "app.js"
    is_middleware = target_path.startswith("middleware/")
    is_config = target_path.startswith("config/")

    if target_path != "package.json":
        add_if_available("package.json")

    if is_controller:
        entity_prefix = _path_stem(target_path).replace("Controller", "").lower()
        add_matching_prefix("models/", entity_prefix)

    elif is_route:
        entity_prefix = _path_stem(target_path).replace("Routes", "").lower()
        add_matching_prefix("controllers/", entity_prefix)

        # Route files may need auth/validation middleware, but not the global
        # error handler. Keep this narrow to avoid sending unrelated middleware.
        for path in sorted(existing_contents):
            if path.startswith("middleware/") and not path.endswith("errorHandler.js"):
                add_if_available(path)

    elif is_app:
        add_if_available("config/db.js")

        for path in sorted(existing_contents):
            if path.startswith("routes/") or path == "middleware/errorHandler.js":
                add_if_available(path)

    elif is_model or is_middleware or is_config:
        # package.json is enough for external dependency consistency.
        pass

    return [(path, existing_contents[path]) for path in selected_paths]


def _path_stem(path: str) -> str:
    filename = path.rsplit("/", 1)[-1]
    return filename.rsplit(".", 1)[0]


def build_code_fix_prompt(
    file_path: str,
    original_content: str,
    error_log: str,
    critic_strategy: str,
    instructions_for_code_agent: str
) -> str:
    """
    Builds a prompt for the CodeGenAgent to patch one affected file
    using the Critic Agent's fixing strategy.

    Important:
    - The Critic Agent gives strategy only.
    - The CodeGenAgent generates the corrected source code.
    - The fix must not invent new files or packages.
    """

    parts = []

    parts.append("You are fixing one file in a generated Node.js/Express backend project.")
    parts.append("Use the Critic Agent's repair strategy to produce the corrected file content.")
    parts.append("")

    parts.append("## PRIORITY ORDER")
    parts.append("1. First, read the RAW ERROR LOG and identify the exact file, line, and import/export problem.")
    parts.append("2. Then read the Critic Agent strategy.")
    parts.append("3. If the Critic strategy points to a different file than the raw stack trace, trust the raw stack trace.")
    parts.append("4. Generate the corrected content for the target file only.")
    parts.append("")
    
    parts.append("## IMPORTANT RULES")
    parts.append("1. Output ONLY the complete corrected source code for the target file.")
    parts.append("2. Do NOT include markdown code fences.")
    parts.append("3. Do NOT include explanations before or after the code.")
    parts.append("4. Modify only what is necessary to fix the error.")
    parts.append("5. Do NOT add new features.")
    parts.append("6. Preserve existing working logic.")
    parts.append("7. Use ES6 modules: import/export only. Do NOT use require/module.exports.")
    parts.append("")

    parts.append("## IMPORTANT IMPORT REPAIR RULES")
    parts.append("1. If the raw error log and Critic strategy conflict, prioritize the raw error log and stack trace.")
    parts.append("2. Fix the file shown in the stack trace if it is available.")
    parts.append("3. If the error is caused by a missing local file, do not invent a new import.")
    parts.append("4. Prefer changing the import to an existing planned file or removing unnecessary abstraction.")
    parts.append("5. Do not import from services/, utils/, helpers/, validators/, or repositories/ unless that file already exists.")
    parts.append("6. If a named import does not exist, update the import or usage to match the actual exports.")
    parts.append("7. If app.js is missing a default export, add export default app without breaking server startup.")
    parts.append("8. If an external package is missing, avoid importing it unless package.json includes it.")
    parts.append("9. Prefer simple Express code with fewer external dependencies.")
    parts.append("10. All local imports must include .js extension.")
    parts.append("11. middleware/errorHandler.js must not import express, cors, json, mongoose, or any external package.")
    parts.append("12. middleware/errorHandler.js should only export named function errorHandler.")
    parts.append("")

    parts.append("## TARGET FILE")
    parts.append(file_path)
    parts.append("")

    parts.append("## CURRENT FILE CONTENT")
    parts.append("```javascript")
    parts.append(original_content)
    parts.append("```")
    parts.append("")

    parts.append("## ERROR LOG")
    parts.append("```")
    parts.append(error_log[:3000])
    parts.append("```")
    parts.append("")

    parts.append("## CRITIC AGENT FIXING STRATEGY")
    parts.append(critic_strategy)
    parts.append("")

    parts.append("## SPECIFIC INSTRUCTIONS FOR CODE AGENT")
    parts.append(instructions_for_code_agent)
    parts.append("")

    parts.append("Return raw JavaScript code only. No markdown. No explanation.")

    return "\n".join(parts)
