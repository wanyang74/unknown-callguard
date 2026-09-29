"""Shared test helpers: Twilio-signed requests, a fake classifier, sample verdicts."""

import asyncio
from urllib.parse import urlencode

from twilio.request_validator import RequestValidator

from callguard.classifier import Verdict

BASE = "https://callguard.example.com"
TOKEN = "test-auth-token"
TWILIO_NUM, CELL, SPAMMER = "+15550000001", "+15550000002", "+15559999999"


class FakeClassifier:
    def __init__(self, verdict: Verdict, delay: float = 0):
        self.verdict, self.delay, self.seen = verdict, delay, []

    async def classify(self, transcript, from_number):
        self.seen.append(transcript)
        await asyncio.sleep(self.delay)
        # A list of verdicts is returned one per call, in order.
        return self.verdict.pop(0) if isinstance(self.verdict, list) else self.verdict


def post(client, path, **params):
    sig = RequestValidator(TOKEN).compute_signature(BASE + path, params)
    return client.post(path, content=urlencode(params),
                       headers={"X-Twilio-Signature": sig,
                                "Content-Type": "application/x-www-form-urlencoded"})


def run_call(client, said):
    assert post(client, "/voice", CallSid="CA1", From=SPAMMER).status_code == 200
    r = post(client, "/screen", CallSid="CA1", From=SPAMMER, SpeechResult=said, Confidence="0.9")
    assert "/decide" in r.text
    return post(client, "/decide", CallSid="CA1", From=SPAMMER)


SALES = Verdict("block", "sales", "Mike from SunPower", "Solar panel offer.")
DOCTOR = Verdict("allow", "medical", "Dr. Lee's office", "Dr. Lee's office about your appointment.")
