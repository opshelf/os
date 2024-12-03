from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routers import storage, gantry
from .utils import get_base_models, load_models_and_get_json_schema
import os
import json
from pathlib import Path
#file_path = Path("your_python_file.py")
# from .dependencies import get_query_token, get_token_header
# from .internal import admin
# from .routers import items, users
# app = FastAPI(dependencies=[Depends(get_query_token)])

app = FastAPI()

origins = ["http://localhost", "http://localhost:8080", "http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(storage.router, prefix="/api")
app.include_router(gantry.router, prefix="/api")



@app.get("/")
async def root():
    return {"message": "Hello Bigger Applications!"}


#def save_schemas_to_files():
#    file_path = "./app/routers/types.py"
#    models = get_base_models(file_path)
#    for model in models:
#        print(model.model_json_schema)
#
#save_schemas_to_files()

schemas = load_models_and_get_json_schema(Path("./app/routers/types.py"))
with open("./schemas.json", "w") as f:
    json.dump(schemas, f, indent=4)


