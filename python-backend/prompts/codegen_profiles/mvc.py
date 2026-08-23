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
## ARCHITECTURE PATTERN: MVC
Dependency direction:
- routes -> controllers
- controllers -> models
- app -> routes/config/middleware

Controllers may import Mongoose models directly.
Routes must stay thin and call controller functions.
Models must not import controllers, routes, services, repositories, or app.js."""


MODEL_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: MVC Model
1. Import mongoose.
2. Define exactly one Mongoose schema for the target entity.
3. Use fields from ENTITIES only.
4. Use timestamps: true.
5. Export default mongoose model.
6. Do not define routes, controllers, middleware, services, or repositories."""


CONTROLLER_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: MVC Controller
1. Import the matching model as a default import.
2. Export named async handler functions.
3. Use try/catch and next(error).
4. Return JSON responses with proper status codes.
5. Do not define mongoose.Schema or mongoose.model here.
6. Do not import express or create express.Router().
7. Do not define route mappings.
8. Do not import services/repositories unless the Planner explicitly listed and target architecture is not MVC."""


ROUTE_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: MVC Route
1. Import express.
2. Create one router with express.Router().
3. Import named controller functions from the matching controller file.
4. Define RESTful routes and attach controller functions.
5. Export default router.
6. Do not import models.
7. Do not perform database queries.
8. Do not write inline async business logic or try/catch blocks."""


APP_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: App Entry
1. Import dotenv/config first.
2. Import express and cors if package.json includes cors.
3. Import connectDB from ./config/db.js.
4. Import all planned route files.
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
    "controller": CONTROLLER_PROMPT,
    "route": ROUTE_PROMPT,
    "auth_controller": AUTH_CONTROLLER_PROMPT,
    "auth_route": AUTH_ROUTE_PROMPT,
    "middleware": ERROR_MIDDLEWARE_PROMPT,
    "auth_middleware": AUTH_MIDDLEWARE_PROMPT,
    "error_middleware": ERROR_MIDDLEWARE_PROMPT,
    "generic": GENERIC_PROMPT,
}
