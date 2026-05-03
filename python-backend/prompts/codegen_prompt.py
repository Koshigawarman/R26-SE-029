"""
AI Backend Builder — Code Generator Agent Prompt Templates

System prompt and user prompt builder for the Code Generator Agent.
Instructs the AI to produce clean, modular, production-ready Express.js code.
"""

from typing import List, Dict
from schema import Entity, Feature, FileSpec

MAX_CONTEXT_LENGTH = 16000

CODEGEN_SYSTEM_PROMPT = """You are an expert Node.js/Express.js backend developer. Your role is to generate production-ready, clean, modular code files.

## CRITICAL RULES
1. Output ONLY the file's source code. No markdown code fences, no explanations before or after.
2. Use ES6 Modules (import/export). Do NOT use require/module.exports.
3. Use async/await for all asynchronous operations. No raw promises or callbacks.
4. Follow MVC architecture strictly.
5. Include meaningful inline comments explaining complex logic.
6. Handle all errors with try/catch and pass errors to Express error middleware via next(error).
7. Use destructuring, template literals, and modern JavaScript features.
8. Every file MUST be complete and self-contained — no TODO placeholders or incomplete code.

## TECHNOLOGY REQUIREMENTS
- Express.js for routing and middleware
- Mongoose for MongoDB models (use Schema, model)
- dotenv for environment variables (import 'dotenv/config' in app.js only)
- bcryptjs for password hashing
- jsonwebtoken for JWT authentication
- express-validator for input validation (when applicable)

## CODE STYLE
- Use 2-space indentation
- Use single quotes for strings
- Add JSDoc comments for exported functions
- Use descriptive variable and function names
- Group imports: built-in → third-party → local modules
- Export at the end of the file or use named exports

## FILE-SPECIFIC GUIDELINES

### Models (models/*.js)
- Import mongoose, define Schema with validators
- Add timestamps: true to schema options
- Export the model as default

### Controllers (controllers/*.js)
- Import the relevant model
- Export named async functions: getAll, getById, create, update, delete
- Use req.params, req.body, req.query appropriately
- Return proper HTTP status codes (200, 201, 400, 404, 500)
- Use try/catch with next(error) for error handling

### Routes (routes/*.js)
- Import express Router
- Import controller functions
- Import auth middleware if authentication is required
- Define RESTful routes: GET /, GET /:id, POST /, PUT /:id, DELETE /:id
- Export the router as default

### Middleware (middleware/*.js)
- Export named functions
- Authentication: verify JWT from Authorization header (Bearer token)
- Error handler: catch-all (err, req, res, next) with proper error response

### Config (config/*.js)
- Database: connect to MongoDB using mongoose.connect() with MONGODB_URI from env
- Export the connection function

### App Entry (app.js)
- Import dotenv/config first
- Import express, cors, helmet (if available)
- Import database connection
- Import all route files
- Set up middleware: json parser, cors, etc.
- Mount routes with versioned paths (/api/...)
- Add error handling middleware LAST
- Start server on PORT from env
- Log "Server running on port..." message"""

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

    parts.append("Generate the complete source code for the following file.\n")
    parts.append("## TARGET FILE")
    parts.append(f"- Path: {file_spec.path}")
    parts.append(f"- Description: {file_spec.description}\n")

    parts.append(f"## PROJECT: {project_name}\n")

    if entities:
        parts.append("## ENTITIES")
        for entity in entities:
            parts.append(f"### {entity.name}")
            if entity.description:
                parts.append(f"Description: {entity.description}")
            parts.append("Fields:")
            for field in entity.fields:
                modifiers = []
                if field.required: modifiers.append("required")
                if field.unique: modifiers.append("unique")
                mod_str = f" ({', '.join(modifiers)})" if modifiers else ""
                parts.append(f"  - {field.name}: {field.type}{mod_str}")
            parts.append("")

    if features:
        parts.append("## FEATURES")
        for feature in features:
            parts.append(f"- {feature.name}: {feature.description}")
        parts.append("")

    parts.append("## ALL PROJECT FILES")
    for f in all_files:
        marker = " ← (THIS FILE)" if f.path == file_spec.path else ""
        parts.append(f"- {f.path}: {f.description}{marker}")
    parts.append("")

    related_files = get_related_files(file_spec.path, all_files, existing_contents)
    if related_files:
        parts.append("## ALREADY GENERATED FILES (for reference)")
        context_length = 0
        for path, content in related_files:
            if context_length + len(content) > MAX_CONTEXT_LENGTH:
                parts.append("\n(... remaining files omitted for brevity)")
                break
            parts.append(f"\n### {path}")
            parts.append("```javascript")
            parts.append(content)
            parts.append("```")
            context_length += len(content)
        parts.append("")

    if existing_file_content:
        parts.append("## CURRENT FILE CONTENT (update this file)")
        parts.append("```javascript")
        parts.append(existing_file_content)
        parts.append("```\n")
        parts.append("Update the above file to incorporate the project requirements. Preserve existing functionality while adding new features.")

    parts.append("\nOutput ONLY the complete source code. No markdown fences, no explanations.")
    
    return "\n".join(parts)

def get_related_files(target_path: str, all_files: List[FileSpec], existing_contents: Dict[str, str]) -> List[tuple]:
    related = []

    is_model = target_path.startswith('models/')
    is_controller = target_path.startswith('controllers/')
    is_route = target_path.startswith('routes/')
    is_app = target_path == 'app.js'
    is_middleware = target_path.startswith('middleware/')

    for path, content in existing_contents.items():
        if path == target_path:
            continue

        is_relevant = False

        if is_controller and path.startswith('models/'): is_relevant = True
        if is_route and (path.startswith('controllers/') or path.startswith('middleware/')): is_relevant = True
        if is_app and (path.startswith('routes/') or path.startswith('config/') or path.startswith('middleware/')): is_relevant = True
        if is_middleware and path.startswith('models/'): is_relevant = True
        if is_app and path.startswith('config/'): is_relevant = True

        if is_relevant:
            related.append((path, content))

    return related
