"""CallGuard: screens unknown callers forwarded from your iPhone and hangs up on sales/spam.

Wires the pieces together: the call flow Twilio drives (call_flow.py), the owner's call log
page (review.py), and /health.
"""

import logging
from pathlib import Path

from fastapi import FastAPI

from . import call_flow, review
from .calllog import CallLog
from .classifier import Classifier
from .config import Settings
from .state import State

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(settings: Settings, classifier: Classifier | None = None) -> FastAPI:
    if classifier is None:
        rules = Path(settings.rules_path).read_text()
        classifier = Classifier(rules, settings.model, settings.classify_timeout_s)
    st = State(settings, classifier, CallLog(settings.db_path))
    app = FastAPI(title="CallGuard")
    app.state.callguard = st

    @app.get("/health")
    async def health():
        return {"ok": True, "mode": settings.mode}

    app.include_router(call_flow.build_router(st))
    app.include_router(review.build_router(st))
    return app


def app_from_env() -> FastAPI:
    return create_app(Settings.from_env())
