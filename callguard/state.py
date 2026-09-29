"""State shared by the routes of one running app."""

import asyncio
from dataclasses import dataclass, field

from .calllog import CallLog
from .classifier import Classifier
from .config import Settings


@dataclass
class State:
    settings: Settings
    classifier: Classifier
    calls: CallLog
    pending: dict[str, asyncio.Task] = field(default_factory=dict)
    # sid -> (speculative classification started from a partial transcript, that transcript)
    early: dict[str, tuple[asyncio.Task, str]] = field(default_factory=dict)
    # sid -> what the caller said before we asked them again for a reason
    asked_again: dict[str, str] = field(default_factory=dict)
