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
## ARCHITECTURE PATTERN: Clean Architecture
Dependency direction:
- interfaces/routes -> interfaces/controllers
- interfaces/controllers -> application/use-cases
- application/use-cases -> infrastructure/repositories
- infrastructure/repositories -> infrastructure/database models
- domain/entities must not depend on outer layers

Domain files must not import Express or Mongoose.
Use cases must not use req/res.
Controllers handle HTTP only and call use cases."""


DOMAIN_ENTITY_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Domain Entity
1. Define the domain entity representation and simple invariants.
2. Do not import mongoose.
3. Do not import express.
4. Do not use req, res, next.
5. Export named or default domain helpers/classes according to the file description."""


MODEL_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Infrastructure Database Model
1. Import mongoose.
2. Define exactly one Mongoose schema/model for the target entity.
3. Use fields from ENTITIES only.
4. Use timestamps: true.
5. Export default model.
6. Do not import domain, use cases, controllers, or routes.
7. Do not add pre('save') or pre('updateOne') timestamp hooks when using timestamps: true; Mongoose manages createdAt and updatedAt automatically."""


REPOSITORY_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Infrastructure Repository
1. Import the matching infrastructure/database model.
2. Export persistence functions.
3. Repository functions may call Mongoose model methods.
4. Do not use req, res, next, status, or json.
5. Do not import express.
6. Do not define route or controller logic."""


USE_CASE_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Application Use Cases
1. Import functions from the matching infrastructure repository.
2. Export named use-case functions.
3. Implement application/business operations.
4. Do not import express.
5. Do not use req, res, next, status, or json.
6. Do not import Mongoose models directly."""


CONTROLLER_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Interface Controller
1. Import named use-case functions from application/use-cases.
2. Export named async HTTP handlers.
3. Read req.params, req.query, and req.body only here.
4. Use try/catch and next(error).
5. Return JSON responses.
6. Do not import Mongoose models or repositories directly.
7. Do not define routes or schemas.
8. Do not import next from 'next'. next is the Express callback parameter in (req, res, next), not a package import.
9. Do not define schema middleware hooks such as EntitySchema.pre('save') or EntitySchema.pre('updateOne') in controllers."""


ROUTE_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: Interface Route
1. Import express.
2. Create one router with express.Router().
3. Import controller functions from interfaces/controllers.
4. Define route mappings only.
5. Export default router.
6. Do not import use cases, repositories, models, or domain entities.
7. Do not write inline async business logic."""


APP_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + """

## FILE TYPE: App Entry
1. Import dotenv/config first.
2. Import express and cors if package.json includes cors.
3. Import connectDB from ./config/db.js.
4. Import planned interface route files.
5. Mount routes under /api/<resource>.
6. Add errorHandler last.
7. Start server only when process.env.NODE_ENV !== 'test'.
8. Export default app."""


PROMPTS = {
    "package": PACKAGE_PROMPT,
    "env": ENV_PROMPT,
    "app": APP_PROMPT,
    "config": CONFIG_PROMPT,
    "domain_entity": DOMAIN_ENTITY_PROMPT,
    "use_case": USE_CASE_PROMPT,
    "model": MODEL_PROMPT,
    "repository": REPOSITORY_PROMPT,
    "controller": CONTROLLER_PROMPT,
    "route": ROUTE_PROMPT,
    "auth_controller": AUTH_CONTROLLER_PROMPT,
    "auth_route": AUTH_ROUTE_PROMPT,
    "middleware": ERROR_MIDDLEWARE_PROMPT,
    "auth_middleware": AUTH_MIDDLEWARE_PROMPT,
    "error_middleware": ERROR_MIDDLEWARE_PROMPT,
    "generic": GENERIC_PROMPT,
}
