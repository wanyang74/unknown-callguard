"""Settings, read from environment variables (Fly secrets in production)."""

import os
from dataclasses import dataclass

from .classifier import default_rules_path


@dataclass
class Settings:
    twilio_auth_token: str
    twilio_number: str  # E.164, e.g. +15551234567
    my_cell: str  # E.164
    public_base_url: str  # e.g. https://callguard.fly.dev
    admin_token: str
    owner_name: str = "the person you're calling"
    mode: str = "enforce"  # "enforce" hangs up on blocked calls; "shadow" only logs
    ring_seconds: int = 20
    classify_timeout_s: float = 8.0
    # Measured 2026-09-24 over 55 runs: same verdicts as Opus 5 on the whole sample,
    # but no latency tail (max 2.9s vs 10.2s), which is what blew the classify timeout.
    model: str = "claude-sonnet-5"
    db_path: str = "calls.db"
    rules_path: str = "rules.md"
    validate_signature: bool = True
    timezone: str = "America/Los_Angeles"  # for times on the /calls page
    # The owner's own test phones: tagged "test" on the /calls page and filterable there.
    test_numbers: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        return cls(
            twilio_auth_token=e["TWILIO_AUTH_TOKEN"],
            twilio_number=e["TWILIO_NUMBER"],
            my_cell=e["MY_CELL"],
            public_base_url=e["PUBLIC_BASE_URL"].rstrip("/"),
            admin_token=e["ADMIN_TOKEN"],
            owner_name=e.get("OWNER_NAME", cls.owner_name),
            mode=e.get("SCREEN_MODE", cls.mode),
            ring_seconds=int(e.get("RING_SECONDS", cls.ring_seconds)),
            classify_timeout_s=float(e.get("CLASSIFY_TIMEOUT", cls.classify_timeout_s)),
            model=e.get("CLAUDE_MODEL", cls.model),
            db_path=e.get("DB_PATH", cls.db_path),
            rules_path=e.get("RULES_PATH") or default_rules_path(),
            validate_signature=e.get("VALIDATE_TWILIO_SIGNATURE", "true").lower() != "false",
            timezone=e.get("TIMEZONE", cls.timezone),
            test_numbers=tuple(n.strip() for n in e.get("TEST_NUMBERS", "").split(",")
                               if n.strip()),
        )
