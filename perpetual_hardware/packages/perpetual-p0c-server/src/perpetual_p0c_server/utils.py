import ast
import importlib.util
import inspect
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from pathlib import Path

def get_base_models(file_path: str) -> List[str]:
    with open(file_path, "r") as file:
        tree = ast.parse(file.read())
    base_models = []
    for node in ast.walk(tree):
        # Look for class definitions
        if isinstance(node, ast.ClassDef):
            # Check if the class inherits from BaseModel
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id == "BaseModel":
                    base_models.append(node.name)
                elif isinstance(base, ast.Attribute) and base.attr == "BaseModel":
                    base_models.append(node.name)
    return base_models

def load_models_and_get_json_schema(file_path: Path) -> Optional[Dict[str, Any]]:
    module_name = file_path.stem
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec:
        module = importlib.util.module_from_spec(spec)
        if spec.loader:
            spec.loader.exec_module(module)
            schemas = {}
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseModel) and obj is not BaseModel:
                    schemas[name] = obj.model_json_schema()

            return schemas
    return None

#from pathlib import Path
#
#file_path = Path("your_python_file.py")
#schemas = load_models_and_get_json_schema(file_path)
#print("JSON Schemas for BaseModel subclasses:")
#for model_name, schema in schemas.items():
#    print(f"{model_name}: {schema}")
