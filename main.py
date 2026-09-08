"""Stable ASGI entry point for Render: ``uvicorn main:app``."""

import importlib.util
from pathlib import Path


module_path = Path(__file__).with_name("FloatIQ Analytics.py")
spec = importlib.util.spec_from_file_location("floatiq_analytics", module_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load application module: {module_path}")

module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
app = module.app
