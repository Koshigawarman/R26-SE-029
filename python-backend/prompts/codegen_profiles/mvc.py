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


MVC_IMPORT_EXPORT_CONTRACT = """
## MVC IMPORT / EXPORT CONTRACT
1. Models export default Mongoose models.
2. Controllers export named async handler functions.
3. Routes import the exact named controller functions that controllers export.
4. Routes export default router.
5. app.js imports route files using exact planned paths.
6. config/db.js exports default connectDB.
7. middleware/errorHandler.js exports default errorHandler in the current profile unless the related file shows a named export.
8. Every local import path must match ALL PROJECT FILES exactly, including casing and .js extension."""


MVC_CONTROLLER_NAMING_CONTRACT = """
## MVC CONTROLLER NAMING CONTRACT
For entity CRUD, prefer these named async exports:
- getAll<EntityPlural>
- get<Entity>ById
- create<Entity>
- update<Entity>
- delete<Entity>

The matching route file must import and use those exact names.
For non-CRUD actions, export clear named handlers matching the route action, such as cancelBooking or getMemberPaymentHistory."""


MVC_MODEL_SHAPE_GUIDE = """
## MVC MODEL SHAPE GUIDE
Use this as the structure pattern only. Replace Product/product/productSchema and fields with the target entity from ENTITIES.

import mongoose from 'mongoose';

const productSchema = new mongoose.Schema({
  name: {
    type: String,
    required: true,
    trim: true,
  },
  price: {
    type: Number,
    required: true,
    min: 0,
  },
  isActive: {
    type: Boolean,
    default: true,
  },
}, { timestamps: true });

export default mongoose.model('Product', productSchema);

Do not copy Product fields unless the target entity actually has those fields."""


MVC_CONTROLLER_SHAPE_GUIDE = """
## MVC CONTROLLER SHAPE GUIDE
Use this as the structure pattern only. Replace Product/products/product fields with the target entity and planned fields.

import Product from '../models/Product.js';

export const getAllProducts = async (req, res, next) => {
  try {
    const products = await Product.find();
    res.status(200).json(products);
  } catch (error) {
    next(error);
  }
};

export const getProductById = async (req, res, next) => {
  try {
    const product = await Product.findById(req.params.id);

    if (!product) {
      return res.status(404).json({ message: 'Product not found' });
    }

    res.status(200).json(product);
  } catch (error) {
    next(error);
  }
};

export const createProduct = async (req, res, next) => {
  try {
    const { name, price } = req.body;

    if (!name || price === undefined) {
      return res.status(400).json({ message: 'Required fields are missing' });
    }

    const product = await Product.create({ name, price });
    res.status(201).json(product);
  } catch (error) {
    next(error);
  }
};

export const updateProduct = async (req, res, next) => {
  try {
    const { name, price } = req.body;
    const updateData = {};

    if (name !== undefined) updateData.name = name;
    if (price !== undefined) updateData.price = price;

    const product = await Product.findByIdAndUpdate(
      req.params.id,
      updateData,
      { new: true, runValidators: true }
    );

    if (!product) {
      return res.status(404).json({ message: 'Product not found' });
    }

    res.status(200).json(product);
  } catch (error) {
    next(error);
  }
};

export const deleteProduct = async (req, res, next) => {
  try {
    const product = await Product.findByIdAndDelete(req.params.id);

    if (!product) {
      return res.status(404).json({ message: 'Product not found' });
    }

    res.status(200).json({ message: 'Product deleted successfully' });
  } catch (error) {
    next(error);
  }
};

For custom actions such as cancelBooking, stockIn, returnBook, or getUpcomingSessions:
- Keep the handler in this controller only for MVC.
- Import another model only when its model file is listed in ALL PROJECT FILES.
- Query only fields that exist in the related model schema.
- Do not invent middleware, service, repository, validator, or utility imports."""


MVC_ROUTE_SHAPE_GUIDE = """
## MVC ROUTE SHAPE GUIDE
Use this as the structure pattern only. Replace Product/product/productRouter and controller names with the target entity.

import express from 'express';
import {
  getAllProducts,
  getProductById,
  createProduct,
  updateProduct,
  deleteProduct,
} from '../controllers/productController.js';

const productRouter = express.Router();

productRouter.get('/', getAllProducts);
productRouter.post('/', createProduct);
productRouter.get('/:id', getProductById);
productRouter.put('/:id', updateProduct);
productRouter.delete('/:id', deleteProduct);

export default productRouter;

For action routes, import the action handler only if the matching controller exports it.
Place action/static routes before /:id, for example:
productRouter.get('/reports/low-stock', getLowStockProducts);
productRouter.get('/member/:memberId/history', getMemberPaymentHistory);

If no action handlers exist for this entity, omit action routes and action imports.
Never place /:id before paths like /member/:memberId, /reports/low-stock, /upcoming, /history, /borrow, or /return."""


MVC_APP_SHAPE_GUIDE = """
## MVC APP SHAPE GUIDE
Use this as the structure pattern only. Import only planned route files and use exact planned casing.

import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import connectDB from './config/db.js';
import productRouter from './routes/productRoutes.js';
import errorHandler from './middleware/errorHandler.js';

const app = express();

app.use(cors());
app.use(express.json());

app.use('/api/products', productRouter);

app.use(errorHandler);

const startServer = async () => {
  try {
    await connectDB();

    console.log('MongoDB connected successfully');

    if (process.env.NODE_ENV !== 'test') {
      const PORT = process.env.PORT || 3000;

      app.listen(PORT, () => {
        console.log(`Server running on port ${PORT}`);
      });
    }
  } catch (error) {
    console.error('MongoDB connection failed');
    console.error(`Error: ${error.message}`);

    if (process.env.NODE_ENV !== 'test') {
      process.exit(1);
    }
  }
};

startServer();

export default app;

Do not import lowercase route filenames if ALL PROJECT FILES lists uppercase filenames. The import path must match the planned filename exactly."""


MVC_FINAL_SELF_CHECK = """
## MVC FINAL SELF-CHECK BEFORE OUTPUT
1. Is the output only the target file source code?
2. Did every local import target a file listed in ALL PROJECT FILES?
3. Did every local import include .js and match filename casing exactly?
4. Did named imports match the exports from the related file?
5. Did this file stay inside its MVC layer boundary?
6. Did controller handlers using next(error) accept (req, res, next)?
7. Did routes stay thin, with no models, database calls, inline business logic, or try/catch?
8. Did models avoid custom id/_id fields and include all planned entity fields?
9. Did app.js mount all planned routes, add errorHandler last, and export default app?
10. Did you avoid authentication, password hashing, JWT, validation, services, repositories, or middleware unless explicitly planned?"""


MODEL_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + MVC_IMPORT_EXPORT_CONTRACT + MVC_MODEL_SHAPE_GUIDE + """

## FILE TYPE: MVC Model
1. Import mongoose.
2. Define exactly one Mongoose schema for the target entity.
3. Use fields from ENTITIES only.
4. Use timestamps: true.
5. Export default mongoose model.
6. Do not define routes, controllers, middleware, services, or repositories.
7. Do not omit fields listed for this entity in ENTITIES.
8. Do not add relationship fields unless the planner entity fields include them or the target file description explicitly requires them.
9. If a field is required and common create flows may omit it, define a safe default only when the requirement supports that default.
10. Do not define custom id or _id fields. MongoDB provides _id automatically.
11. Do not add password fields unless authentication is explicitly planned for this project.
12. Do not add pre('save') or pre('updateOne') timestamp hooks when using timestamps: true; Mongoose manages createdAt and updatedAt automatically.""" + MVC_FINAL_SELF_CHECK


CONTROLLER_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + MVC_IMPORT_EXPORT_CONTRACT + MVC_CONTROLLER_NAMING_CONTRACT + MVC_CONTROLLER_SHAPE_GUIDE + """

## FILE TYPE: MVC Controller
1. Import the matching model as a default import.
2. Export named async handler functions.
3. Use try/catch and next(error), and every handler that calls next(error) MUST accept (req, res, next).
4. Return JSON responses with proper status codes.
5. Do not define mongoose.Schema or mongoose.model here.
6. Do not import express or create express.Router().
7. Do not define route mappings.
8. Do not import services/repositories unless the Planner explicitly listed and target architecture is not MVC.
9. If the controller calls Model.find/findById/create/update/delete, the Model variable must be imported from the matching planned model file.
10. Do not read fields from a model document unless that field exists in the planned entity/model schema.
11. Do not export a Mongoose model from a controller file.
12. Do not create or use a model variable name unless it is imported or defined in this controller file.
13. For create/update handlers, use only fields planned for the target entity.
14. If an operation needs data from another entity, import that entity model only if its model file exists in ALL PROJECT FILES.
15. For simple MVC, implement business actions directly in named controller handlers without inventing service/repository files.
16. Use status codes consistently: 200 success, 201 created, 400 invalid input, 404 not found.
17. Do not import bcryptjs, bcrypt, or jsonwebtoken unless this is an auth/user credential controller.
18. For non-auth entities, do not read req.body.password, hash passwords, or create JWT tokens.
19. Do not import next from 'next'. next is the Express callback parameter in (req, res, next), not a package import.
20. Do not define schema middleware hooks such as EntitySchema.pre('save') or EntitySchema.pre('updateOne') in controllers.
21. createdAt and updatedAt are handled by model schemas with timestamps: true, not by controllers.
22. Final self-check: no schemas, no routes, no Express app setup, no package.json/.env content.""" + MVC_FINAL_SELF_CHECK


ROUTE_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + MVC_IMPORT_EXPORT_CONTRACT + MVC_CONTROLLER_NAMING_CONTRACT + MVC_ROUTE_SHAPE_GUIDE + """

## FILE TYPE: MVC Route
1. Import express.
2. Create one router with express.Router().
3. Import named controller functions from the matching controller file.
4. Define RESTful routes and attach controller functions.
5. Export default router.
6. Do not import models.
7. Do not perform database queries.
8. Do not write inline async business logic or try/catch blocks.
9. Do not import middleware files unless they are listed in ALL PROJECT FILES.
10. Every middleware used in this route file must be imported in this file.
11. If authentication middleware is not planned, do not add authenticateToken, protect, requireAuth, or auth middleware imports.
12. Define static/action routes before parameter routes like /:id.
13. Every controller function used in a route must be imported by the exact same exported name from the controller file.
14. Do not invent validation middleware unless the planner listed the middleware file and package.json includes the dependency.
15. Do not import express-validator/check/validationResult unless a separate planned validation middleware file exists and package.json includes express-validator.
16. Final self-check: no model imports, no controller implementations, no inline async business logic, no database calls.""" + MVC_FINAL_SELF_CHECK


APP_PROMPT = BASE_PROFILE_RULES + ARCHITECTURE_RULES + MVC_IMPORT_EXPORT_CONTRACT + MVC_APP_SHAPE_GUIDE + """

## FILE TYPE: App Entry
1. Import dotenv/config first.
2. Import express and cors if package.json includes cors.
3. Import connectDB from ./config/db.js.
4. Import all planned route files using exact paths and casing from ALL PROJECT FILES.
5. Mount routes under /api/<resource>.
6. Add errorHandler last.
7. Start server only when process.env.NODE_ENV !== 'test'.
8. Export default app.
9. Use app.use(express.json()), not json().
10. Do not import route files that are not listed in ALL PROJECT FILES.
11. Do not mount middleware or routes before importing them.
12. Add errorHandler last.
13. Start server only outside NODE_ENV=test using process.env.PORT || 3000.
14. Final self-check: dotenv/config first, all planned routes mounted, error handler last, export default app.""" + MVC_FINAL_SELF_CHECK


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
