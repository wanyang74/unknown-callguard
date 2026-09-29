"""Run the server: python -m callguard"""

import os

import uvicorn

from .app import app_from_env

uvicorn.run(app_from_env(), host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
