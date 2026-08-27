from prompts.codegen_profiles.base import (
    AUTH_CONTROLLER_PROMPT,
    AUTH_MIDDLEWARE_PROMPT,
    AUTH_ROUTE_PROMPT,
    BASE_PROFILE_RULES,
    CONFIG_PROMPT,
    ENV_PROMPT,
    ERROR_MIDDLEWARE_PROMPT,
    GENERIC_PROMPT,
    PACKAGE_PROMPT,
)


ARCHITECTURE_RULES = """
## ARCHITECTURE PATTERN: Service Repository
Dependency direction:
- routes -> controllers
- controllers -> services
- services -> repositories
- repositories -> models
- app -> routes/config/middleware

Controllers must not import models directly.
Services must not use req, res, next, or Express APIs.
Repositories must not use req, res, next, or HTTP response logic."""


MODEL_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Model
1. Import mongoose.
2. Define exactly one Mongoose schema for the target entity.
3. Use fields from ENTITIES only.
4. Use timestamps: true.
5. Export default mongoose model.
6. Do not import repositories, services, controllers, routes, middleware, or app.js.
7. Do not add pre('save') or pre('updateOne') timestamp hooks when using timestamps: true; Mongoose manages createdAt and updatedAt automatically."""


REPOSITORY_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Repository
1. Import the matching model as a default import.
2. Export named database access functions only. Do not export a repository class/object unless the planner explicitly asks for it.
3. Repository functions may call Model.find, findById, create, findByIdAndUpdate, findByIdAndDelete, save, aggregate, or countDocuments.
4. Do not use req, res, next, status, or json.
5. Do not import express.
6. Do not contain route definitions or controller logic.
7. Do not perform HTTP response formatting."""


SERVICE_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Service
1. Import named functions from the matching repository file using import { functionName } from '../repositories/<entity>Repository.js'.
2. Export named business logic functions.
3. Services may validate business rules and compose repository calls.
4. Do not import Mongoose models directly.
5. Do not use req, res, next, status, or json.
6. Do not import express.
7. Do not define routes or schemas.
8. Do not import a default repository object/class unless the repository file has a default export."""


CONTROLLER_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Controller
1. Import functions from the matching service file.
2. Export named async HTTP handler functions.
3. Read req.params, req.query, and req.body only in controllers.
4. Use try/catch and next(error).
5. Return JSON responses with proper status codes.
6. Do not import Mongoose models directly.
7. Do not call Model.find/create/update/delete.
8. Do not define schemas, repositories, services, or routes.
9. Every called service function must be imported by the exact same name from the service file.
10. Do not invent *Service suffix function names unless those exact names are exported by the service file.
11. Do not import next from 'next'. next is the Express callback parameter in (req, res, next), not a package import.
12. Do not define schema middleware hooks such as EntitySchema.pre('save') or EntitySchema.pre('updateOne') in controllers."""


ROUTE_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Route
1. Import express.
2. Create one router with express.Router().
3. Import named controller functions from the matching controller file.
4. Define route mappings only.
5. Export default router.
6. Do not import models, repositories, or services.
7. Do not write inline async business logic or try/catch blocks.
8. Every middleware used in a route, such as protect, must be imported in this file."""


APP_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: App Entry
1. Import dotenv/config first.
2. Import express and cors if package.json includes cors.
3. Import connectDB from ./config/db.js.
4. Import all planned route files using the exact file paths and casing from ALL PROJECT FILES.
5. Mount routes under /api/<resource>.
6. Add errorHandler last.
7. Start server only when process.env.NODE_ENV !== 'test'.
8. Export default app."""


PROMPTS = {
    "package": PACKAGE_PROMPT,
    "env": ENV_PROMPT,
    "app": APP_PROMPT,
    "config": CONFIG_PROMPT,
    "model": MODEL_PROMPT,
    "repository": REPOSITORY_PROMPT,
    "service": SERVICE_PROMPT,
    "controller": CONTROLLER_PROMPT,
    "route": ROUTE_PROMPT,
    "auth_controller": AUTH_CONTROLLER_PROMPT,
    "auth_route": AUTH_ROUTE_PROMPT,
    "middleware": ERROR_MIDDLEWARE_PROMPT,
    "auth_middleware": AUTH_MIDDLEWARE_PROMPT,
    "error_middleware": ERROR_MIDDLEWARE_PROMPT,
    "generic": GENERIC_PROMPT,
}
