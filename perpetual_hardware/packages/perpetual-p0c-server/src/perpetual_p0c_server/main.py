from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from perpetual_p0c_server.core.storage import init_file_storage
from perpetual_p0c_server.core.sizes import init_file_sizes
from perpetual_p0c_server.routers import storage, gantry, sizes
from contextlib import asynccontextmanager
from typing import AsyncGenerator
#from perpetual_p0c_server.utils import get_base_models, load_models_and_get_json_schema
#import os
#import json
#from pathlib import Path
# file_path = Path("your_python_file.py")
# from .dependencies import get_query_token, get_token_header
# app = FastAPI(dependencies=[Depends(get_query_token)])

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    print(app.title)
    init_file_storage()
    init_file_sizes()
    yield 


app = FastAPI(lifespan=lifespan)

origins = ["http://localhost", "http://localhost:8080", "http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(storage.router, prefix="/api/storage")
app.include_router(sizes.router, prefix="/api/sizes")
app.include_router(gantry.router, prefix="/api")

#def save_schemas_to_files():
#    file_path = "./app/routers/types.py"
#    models = get_base_models(file_path)
#    for model in models:
#        print(model.model_json_schema)
#
#save_schemas_to_files()
#schemas = load_models_and_get_json_schema(Path("./app/routers/types.py"))
#with open("./schemas.json", "w") as f:
#    json.dump(schemas, f, indent=4)

