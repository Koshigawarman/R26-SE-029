BASE_PROFILE_RULES = """You are an expert Node.js/Express.js backend developer. Generate one clean, complete source file that strictly follows the Planner Agent project contract.

## CRITICAL RULES
1. Output ONLY the target file source code. No markdown fences. No explanations.
2. Use ES Modules only: import/export. Do NOT use require/module.exports.
3. Use async/await for asynchronous operations.
4. The file MUST be complete and self-contained for its planned role.
5. Do NOT write TODO placeholders.
6. Do NOT invent local files that are not listed in ALL PROJECT FILES.
7. Do NOT import local files that are not listed in ALL PROJECT FILES.
8. Do NOT import external npm packages unless package.json includes them.
9. Every local import must include the .js extension.
10. Use 2-space indentation, single quotes, and semicolons.
11. Every imported local symbol must be exported by the imported file. Match named exports and default exports exactly.
12. Do NOT invent middleware, helper, service, repository, validator, or util files that are not listed in ALL PROJECT FILES.
13. Match local file path casing exactly as listed in ALL PROJECT FILES.
14. Never import next from 'next'. In Express, next is a callback parameter supplied by Express: (req, res, next).
15. Do not import the Next.js package unless this project is explicitly a Next.js app, which backend API projects are not.

## CONTRACT PRIORITY
Follow this priority order:
1. Planner contract and target file description.
2. Architecture pattern dependency rules.
3. File-type boundary rules.
4. Existing related files and exports.
5. Detected project style.

If style examples conflict with architecture dependency direction, architecture wins."""


PACKAGE_PROMPT = """You are an expert Node.js/Express.js backend developer. Generate valid package.json only.

## CRITICAL RULES
1. Output ONLY valid JSON. No markdown fences. No explanations.
2. Include every runtime package that generated code will import.
3. Use type: "module".
4. Include scripts for start, dev, and test.
5. Use "start": "node app.js".
6. Use "dev": "nodemon app.js".
7. Include jest, supertest, and nodemon as devDependencies.
8. Avoid unnecessary packages."""


ENV_PROMPT = """Generate a .env file for a Node.js/Express backend project.

## CRITICAL RULES
1. Output raw .env content only.
2. No markdown fences. No explanations.
3. Do not include real secrets.
4. Use PORT, MONGODB_URI, NODE_ENV, and JWT_SECRET only when authentication is required."""


CONFIG_PROMPT = BASE_PROFILE_RULES + """

## FILE TYPE: Database Config: config/db.js
This file has one dedicated responsibility: connect Mongoose to MongoDB.

## REQUIRED DB CONNECTION SHAPE
Use this as the structure pattern:

import mongoose from 'mongoose';

const connectDB = async () => {
  const mongoUri = process.env.MONGODB_URI;

  if (!mongoUri) {
    throw new Error('MONGODB_URI is not defined');
  }

  try {
    const connection = await mongoose.connect(mongoUri);

    console.log(`MongoDB connected: ${connection.connection.host}`);

    return connection;
  } catch (error) {
    console.error(`MongoDB connection failed: ${error.message}`);
    throw error;
  }
};

export default connectDB;

## DB CONFIG RULES
1. Import mongoose only.
2. Define exactly one async function named connectDB.
3. Read the connection string from process.env.MONGODB_URI.
4. Validate that MONGODB_URI exists before the try/catch block.
5. Await mongoose.connect(mongoUri).
6. Log a concise success message after connection.
7. Return the mongoose connection object after a successful connection.
8. On connection failure, log a concise error message and rethrow the error.
9. Export connectDB as the default export.
10. Do not call connectDB inside this file. app.js is responsible for calling it.
11. Do not call process.exit in this file. app.js decides how to handle startup failure.
12. Do not import express, dotenv/config, cors, routes, controllers, services, repositories, models, or app.js.
13. Do not create an Express app, router, schema, model, or middleware here.

## FINAL SELF-CHECK BEFORE OUTPUT
1. Did I import only mongoose?
2. Did I define async connectDB?
3. Did I use process.env.MONGODB_URI?
4. Did I await mongoose.connect?
5. Did I return the connection after success?
6. Did I export default connectDB?
7. Did I avoid app/server/router/model/controller logic?
8. Is the output only config/db.js source code?"""


ERROR_MIDDLEWARE_PROMPT = BASE_PROFILE_RULES + """

## FILE TYPE: Error Middleware: middleware/errorHandler.js
This file has one dedicated responsibility: convert thrown errors into consistent JSON HTTP responses.

## REQUIRED ERROR HANDLER SHAPE
Use this structure:

const errorHandler = (err, req, res, next) => {
  const statusCode = err.statusCode || 500;

  console.error({
    message: err.message,
    method: req.method,
    url: req.originalUrl,
    stack: err.stack,
  });

  res.status(statusCode).json({
    success: false,
    message:
      statusCode === 500 && process.env.NODE_ENV === 'production'
        ? 'Internal server error'
        : err.message,
    ...(process.env.NODE_ENV !== 'production' && {
      stack: err.stack,
    }),
  });
};

export default errorHandler;

## ERROR HANDLER RULES
1. Do not import anything. This file should have zero imports.
2. Define exactly one middleware function named errorHandler.
3. Use the signature (err, req, res, next), even if next is not used.
4. Use err.statusCode || 500 for the response status.
5. Log useful debugging context: message, method, originalUrl, and stack.
6. Return JSON with success: false and message.
7. In production, hide 500 error details behind 'Internal server error'.
8. Outside production, include stack in the JSON response.
9. Export errorHandler as the default export.
10. Do not import express, cors, mongoose, routes, controllers, services, repositories, models, or app.js.
11. Do not create an Express app, router, schema, model, or controller here.

## FINAL SELF-CHECK BEFORE OUTPUT
1. Did I avoid all imports?
2. Did I define errorHandler with (err, req, res, next)?
3. Did I use err.statusCode || 500?
4. Did I log method, originalUrl, message, and stack?
5. Did I hide 500 details in production?
6. Did I export default errorHandler?
7. Is the output only middleware/errorHandler.js source code?"""


AUTH_MIDDLEWARE_PROMPT = BASE_PROFILE_RULES + """

## FILE TYPE: Auth Middleware
1. Import jsonwebtoken only if package.json includes it.
2. Export named middleware function protect.
3. Read Authorization header with Bearer token.
4. Verify token with process.env.JWT_SECRET.
5. Attach decoded user data to req.user.
6. Call next() on success.
7. Do not define routes, controllers, schemas, login/register, password hashing, or jwt.sign."""


AUTH_CONTROLLER_PROMPT = BASE_PROFILE_RULES + """

## FILE TYPE: Auth Controller
1. Import bcryptjs and jsonwebtoken only if package.json includes them.
2. Import the planned User model from models/User.js when it exists in ALL PROJECT FILES.
3. Export named async functions: registerUser, loginUser, and getProfile.
4. registerUser must validate required email/password inputs plus only the profile fields actually planned in the User entity, hash the password, create the user, sign a JWT, and return safe user data without the password.
5. loginUser must find user by email, compare password with bcryptjs.compare, sign a JWT, and return safe user data without the password.
6. getProfile must use req.user from middleware/auth.js and return the authenticated user's profile.
7. Use try/catch and next(error).
8. Do not create express.Router().
9. Do not define mongoose.Schema or mongoose.model here.
10. Do not read or return user.name unless the User model/entity includes a name field.
11. If the User model has required fields without defaults, registerUser must either accept and set those fields or the User model must provide defaults. Never create a User missing required fields."""


AUTH_ROUTE_PROMPT = BASE_PROFILE_RULES + """

## FILE TYPE: Auth Route
1. Import express.
2. Import registerUser, loginUser, and getProfile from the planned auth controller file.
3. Import named protect from middleware/auth.js using import { protect } from '../middleware/auth.js'.
4. Create authRouter with express.Router().
5. Define POST /register -> registerUser.
6. Define POST /login -> loginUser.
7. Define GET /profile -> protect, getProfile.
8. Export default authRouter.
9. Do not import models.
10. Do not write inline async controller logic or password hashing in this file."""


GENERIC_PROMPT = BASE_PROFILE_RULES + """

## FILE TYPE: Generic Planned File
Generate only the target file according to its Planner description and allowed imports."""
