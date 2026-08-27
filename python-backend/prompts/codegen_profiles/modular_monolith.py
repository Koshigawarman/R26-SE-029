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
## ARCHITECTURE PATTERN: Modular Monolith
Dependency direction inside each module:
- modules/<module>/routes.js -> modules/<module>/controller.js
- modules/<module>/controller.js -> modules/<module>/service.js
- modules/<module>/service.js -> modules/<module>/repository.js
- modules/<module>/repository.js -> modules/<module>/model.js
- app.js -> modules/<module>/routes.js

Keep module internals self-contained.
Controllers handle HTTP only.
Services hold business logic.
Repositories hold database logic."""


MODEL_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Module Model
1. Import mongoose.
2. Define exactly one module-local Mongoose schema/model.
3. Use fields from ENTITIES only.
4. Use timestamps: true.
5. Export default model.
6. Do not import repository, service, controller, routes, middleware, or app.js.
7. Do not add pre('save') or pre('updateOne') timestamp hooks when using timestamps: true; Mongoose manages createdAt and updatedAt automatically."""


REPOSITORY_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Module Repository
1. Import ./model.js.
2. Export named database access functions.
3. Repository functions may call Mongoose model methods.
4. Do not use req, res, next, status, or json.
5. Do not import express.
6. Do not define routes, services, or controllers."""


SERVICE_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Module Service
1. Import functions from ./repository.js.
2. Export named business logic functions.
3. Do not import Mongoose models directly.
4. Do not use req, res, next, status, or json.
5. Do not import express.
6. Do not define routes or controllers."""


CONTROLLER_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Module Controller
1. Import functions from ./service.js.
2. Export named async HTTP handlers.
3. Read req.params, req.query, and req.body only here.
4. Use try/catch and next(error).
5. Return JSON responses.
6. Do not import model or repository directly.
7. Do not define route mappings or schemas.
8. Do not import next from 'next'. next is the Express callback parameter in (req, res, next), not a package import.
9. Do not define schema middleware hooks such as EntitySchema.pre('save') or EntitySchema.pre('updateOne') in controllers."""


ROUTE_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Module Route
1. Import express.
2. Create one router with express.Router().
3. Import controller functions from ./controller.js.
4. Define route mappings only.
5. Export default router.
6. Do not import model, repository, or service.
7. Do not write inline async business logic."""


APP_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: App Entry
1. Import dotenv/config first.
2. Import express and cors if package.json includes cors.
3. Import connectDB from ./config/db.js.
4. Import planned module route files from modules/<module>/routes.js.
5. Mount routes under /api/<module>.
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
    "shared": GENERIC_PROMPT,
    "auth_controller": AUTH_CONTROLLER_PROMPT,
    "auth_route": AUTH_ROUTE_PROMPT,
    "middleware": ERROR_MIDDLEWARE_PROMPT,
    "auth_middleware": AUTH_MIDDLEWARE_PROMPT,
    "error_middleware": ERROR_MIDDLEWARE_PROMPT,
    "generic": GENERIC_PROMPT,
}
