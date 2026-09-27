import asyncio
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import Settings, create_app  # noqa: E402
from classifier import Verdict  # noqa: E402

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


@pytest.fixture
def make(tmp_path):
    clients = []

    def _make(verdict, mode="enforce", delay=0):
        s = Settings(twilio_auth_token=TOKEN, twilio_number=TWILIO_NUM, my_cell=CELL,
                     public_base_url=BASE, admin_token="admin", owner_name="Sam",
                     mode=mode, db_path=str(tmp_path / "calls.db"), classify_timeout_s=0.5)
        fake = FakeClassifier(verdict, delay)
        # Entering the client keeps one event loop across requests, like uvicorn does,
        # so the classification task started in /screen survives until /decide.
        client = TestClient(create_app(s, fake)).__enter__()
        clients.append(client)
        return client, fake

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


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


def test_rejects_unsigned_requests(make):
    client, _ = make(SALES)
    r = client.post("/voice", data={"CallSid": "CA1", "From": SPAMMER})
    assert r.status_code == 403


def test_greeting_asks_for_reason(make):
    client, _ = make(SALES)
    r = post(client, "/voice", CallSid="CA1", From=SPAMMER)
    assert '<Gather' in r.text and 'input="speech"' in r.text
    assert "why you're calling" in r.text


def test_blocked_call_hangs_up(make):
    client, fake = make(SALES)
    r = run_call(client, "Hi this is Mike from SunPower about solar")
    assert "<Hangup" in r.text and "<Dial" not in r.text
    assert fake.seen == ["Hi this is Mike from SunPower about solar"]
    row = client.app.state.callguard.calls.get("CA1")
    assert (row["decision"], row["outcome"]) == ("block", "blocked")


def test_allowed_call_rings_cell_with_whisper(make):
    client, _ = make(DOCTOR)
    r = run_call(client, "This is Dr. Lee's office")
    assert f"<Number" in r.text and CELL in r.text
    assert f'callerId="{TWILIO_NUM}"' in r.text and 'timeout="20"' in r.text
    w = post(client, "/whisper?sid=CA1", CallSid="CA2")
    assert "Dr. Lee's office about your appointment" in w.text


def test_shadow_mode_never_hangs_up(make):
    client, _ = make(SALES, mode="shadow")
    r = run_call(client, "solar deal")
    assert "<Dial" in r.text
    w = post(client, "/whisper?sid=CA1", CallSid="CA2")
    assert "Would have been blocked as sales" in w.text


def test_classifier_timeout_fails_open(make):
    client, _ = make(SALES, delay=5)  # slower than timeout + 2s grace
    client.app.state.callguard.settings.classify_timeout_s = 0.1
    r = run_call(client, "hello")
    assert "<Dial" in r.text


def test_forwarded_back_loop_is_rejected(make):
    client, _ = make(SALES)
    r = post(client, "/voice", CallSid="CA9", From=TWILIO_NUM)
    assert '<Reject reason="busy"' in r.text


def test_silent_caller_retries_then_hangs_up(make):
    client, _ = make(SALES)
    first = post(client, "/voice", CallSid="CA1", From=SPAMMER)
    assert "attempt=2" in first.text
    second = post(client, "/voice?attempt=2", CallSid="CA1", From=SPAMMER)
    assert "<Hangup" in second.text


NO_REASON_V = Verdict("block", "no_reason", "unknown", "Caller only said hi.")
AUNT = Verdict("allow", "personal", "Aunt Mei", "Your aunt Mei.")


def answer(client, said):
    r = post(client, "/screen", CallSid="CA1", From=SPAMMER, SpeechResult=said, Confidence="0.9")
    assert "/decide" in r.text
    return post(client, "/decide", CallSid="CA1", From=SPAMMER)


def test_no_reason_asks_again_then_connects(make):
    client, fake = make([NO_REASON_V, AUNT])
    r = run_call(client, "Hi.")
    assert "<Gather" in r.text and "who's calling" in r.text and "<Dial" not in r.text
    r = answer(client, "It's your aunt Mei.")
    assert "<Dial" in r.text
    assert fake.seen == ["Hi.", "Hi. It's your aunt Mei."]  # second answer judged with the first
    assert client.app.state.callguard.calls.get("CA1")["decision"] == "allow"


def test_no_reason_twice_hangs_up(make):
    client, _ = make([NO_REASON_V, NO_REASON_V])
    run_call(client, "Hi.")
    r = answer(client, "Hello?")
    assert "<Hangup" in r.text and "<Dial" not in r.text and "<Gather" not in r.text
    row = client.app.state.callguard.calls.get("CA1")
    assert (row["decision"], row["category"], row["outcome"]) == ("block", "no_reason", "blocked")


def test_silence_after_asking_again_hangs_up(make):
    client, _ = make(NO_REASON_V)
    r = run_call(client, "Hi.")
    # The TwiML after the Gather only runs if the caller says nothing to the second question.
    assert r.text.index("<Gather") < r.text.index("<Hangup")
    assert client.app.state.callguard.calls.get("CA1")["category"] == "no_reason"


def test_silence_after_asking_again_rings_in_shadow_mode(make):
    client, _ = make(NO_REASON_V, mode="shadow")
    r = run_call(client, "Hi.")
    assert "<Gather" in r.text and "<Dial" in r.text and "<Hangup" not in r.text


def test_missed_call_takes_voicemail(make):
    client, _ = make(DOCTOR)
    run_call(client, "Dr. Lee's office")
    r = post(client, "/dial-done", CallSid="CA1", DialCallStatus="no-answer")
    assert "<Record" in r.text
    post(client, "/voicemail", CallSid="CA1", RecordingUrl="https://api.twilio.com/rec/RE1")
    page = client.get("/calls?token=admin")
    assert "voicemail" in page.text and "RE1" in page.text
    assert client.get("/calls?token=wrong").status_code == 404


# --- partial-transcript streaming -------------------------------------------------

# Long enough to be trusted for a block (>= MIN_TRUSTED_PARTIAL_WORDS).
LONG_PARTIAL = ("this is Kayla Davis calling about the personal loan you previously considered "
                "and there may be another lending option worth reviewing with you the numbers "
                "could look a little different this time around especially the monthly payment")


class ScriptedClassifier:
    """Answers the Nth call with verdicts[N], delaying by delays[N]."""

    def __init__(self, verdicts, delays):
        self.verdicts, self.delays, self.seen = verdicts, delays, []

    async def classify(self, transcript, from_number):
        i = len(self.seen)
        self.seen.append(transcript)
        await asyncio.sleep(self.delays[i])
        return self.verdicts[i]


def test_partial_starts_early_classification(make):
    client, fake = make(SALES)
    post(client, "/voice", CallSid="CA1", From=SPAMMER)
    r = post(client, "/partial", CallSid="CA1", From=SPAMMER, UnstableSpeechResult=LONG_PARTIAL)
    assert r.status_code == 204
    # The speculative task is registered against the partial text (it runs on the next
    # loop tick, so fake.seen is still empty here).
    early = client.app.state.callguard.early
    assert list(early) == ["CA1"] and early["CA1"][1] == LONG_PARTIAL


def test_short_partial_is_ignored(make):
    client, fake = make(SALES)
    post(client, "/voice", CallSid="CA1", From=SPAMMER)
    post(client, "/partial", CallSid="CA1", From=SPAMMER, UnstableSpeechResult="hi it's me")
    assert fake.seen == []  # too few words to judge anyone on


def test_identical_final_transcript_reuses_early_run(make):
    client, fake = make(SALES)
    post(client, "/voice", CallSid="CA1", From=SPAMMER)
    post(client, "/partial", CallSid="CA1", From=SPAMMER, UnstableSpeechResult=LONG_PARTIAL)
    post(client, "/screen", CallSid="CA1", From=SPAMMER, SpeechResult=LONG_PARTIAL,
         Confidence="0.9")
    r = post(client, "/decide", CallSid="CA1", From=SPAMMER)
    assert "Hangup" in r.text
    assert fake.seen == [LONG_PARTIAL]  # not classified twice


def test_partial_verdict_blocks_when_final_times_out(make, tmp_path):
    # Early run (from the partial) is fast and says block; the final run hangs past the
    # timeout. The partial verdict should win instead of failing open.
    s = Settings(twilio_auth_token=TOKEN, twilio_number=TWILIO_NUM, my_cell=CELL,
                 public_base_url=BASE, admin_token="admin", owner_name="Sam",
                 mode="enforce", db_path=str(tmp_path / "p.db"), classify_timeout_s=0.1)
    scripted = ScriptedClassifier([SALES, DOCTOR], [0.0, 10.0])
    with TestClient(create_app(s, scripted)) as client:
        post(client, "/voice", CallSid="CA1", From=SPAMMER)
        post(client, "/partial", CallSid="CA1", From=SPAMMER, UnstableSpeechResult=LONG_PARTIAL)
        post(client, "/screen", CallSid="CA1", From=SPAMMER,
             SpeechResult=LONG_PARTIAL + " and more words here", Confidence="0.9")
        r = post(client, "/decide", CallSid="CA1", From=SPAMMER)
        assert "Hangup" in r.text and "Dial" not in r.text
        row = client.app.state.callguard.calls.get("CA1")
        assert row["decision"] == "block"
        assert row["error"] == "timeout_partial_verdict"


def test_fails_open_when_no_partial_verdict_available(make):
    # No /partial ever fired, so a final timeout must still ring the owner.
    client, _ = make(SALES, delay=10)
    client.app.state.callguard.settings.classify_timeout_s = 0.1
    r = run_call(client, "something")
    assert "Dial" in r.text
    row = client.app.state.callguard.calls.get("CA1")
    assert row["decision"] == "allow" and row["error"] == "timeout"


def test_short_partial_block_still_rings(make, tmp_path):
    # A block verdict from a too-short partial must not hang up: real callers get cut off
    # mid-sentence, and a 13-word partial is not enough evidence to end a call.
    s = Settings(twilio_auth_token=TOKEN, twilio_number=TWILIO_NUM, my_cell=CELL,
                 public_base_url=BASE, admin_token="admin", owner_name="Sam",
                 mode="enforce", db_path=str(tmp_path / "q.db"), classify_timeout_s=0.1)
    short = " ".join(["word"] * 15)  # over MIN_PARTIAL_WORDS, under MIN_TRUSTED
    scripted = ScriptedClassifier([SALES, SALES], [0.0, 10.0])
    with TestClient(create_app(s, scripted)) as client:
        post(client, "/voice", CallSid="CA1", From=SPAMMER)
        post(client, "/partial", CallSid="CA1", From=SPAMMER, UnstableSpeechResult=short)
        post(client, "/screen", CallSid="CA1", From=SPAMMER, SpeechResult=short + " extra",
             Confidence="0.9")
        r = post(client, "/decide", CallSid="CA1", From=SPAMMER)
        assert "Dial" in r.text  # rang the owner rather than trusting a thin partial
        assert client.app.state.callguard.calls.get("CA1")["error"] == "timeout"


def test_long_partial_allow_is_used_on_timeout(make, tmp_path):
    # An allow verdict from a partial is safe at any length and gives a better whisper
    # summary than the raw-transcript fail-open does.
    s = Settings(twilio_auth_token=TOKEN, twilio_number=TWILIO_NUM, my_cell=CELL,
                 public_base_url=BASE, admin_token="admin", owner_name="Sam",
                 mode="enforce", db_path=str(tmp_path / "r.db"), classify_timeout_s=0.1)
    scripted = ScriptedClassifier([DOCTOR, DOCTOR], [0.0, 10.0])
    with TestClient(create_app(s, scripted)) as client:
        post(client, "/voice", CallSid="CA1", From=SPAMMER)
        post(client, "/partial", CallSid="CA1", From=SPAMMER, UnstableSpeechResult=LONG_PARTIAL)
        post(client, "/screen", CallSid="CA1", From=SPAMMER,
             SpeechResult=LONG_PARTIAL + " plus more", Confidence="0.9")
        r = post(client, "/decide", CallSid="CA1", From=SPAMMER)
        assert "Dial" in r.text
        row = client.app.state.callguard.calls.get("CA1")
        assert row["decision"] == "allow" and row["error"] == "timeout_partial_verdict"
        assert "appointment" in row["summary"]


# --- review page -------------------------------------------------------------------

def page(client, **qs):
    q = "&".join(f"{k}={v}" for k, v in qs.items())
    return client.get(f"/calls?token=admin&{q}")


def test_calls_page_shows_reason_and_confidence(make):
    client, _ = make(SALES)
    run_call(client, "solar panels for your home today")
    r = page(client)
    assert "Solar panel offer." in r.text      # the summary column
    assert "0.90" in r.text                     # Twilio confidence
    assert "mark wrong" in r.text


def test_calls_page_shows_pacific_time(make):
    client, _ = make(SALES)
    run_call(client, "solar panels")
    client.app.state.callguard.calls.upsert("CA1", ts=1790000000)  # 2026-09-21 14:13 UTC
    assert "Sep 21 07:13 PDT" in page(client).text


def test_calls_page_filters_by_decision(make):
    client, _ = make(SALES)
    run_call(client, "solar panels")
    assert "solar panels" in page(client, decision="block").text
    assert "solar panels" not in page(client, decision="allow").text


def test_calls_page_filters_test_numbers(make):
    client, _ = make(SALES)
    client.app.state.callguard.settings.test_numbers = ("+14085550100",)
    post(client, "/voice", CallSid="CA1", From=SPAMMER)
    post(client, "/voice", CallSid="CA2", From="+14085550100")
    assert SPAMMER not in page(client, source="test").text
    assert "+14085550100" in page(client, source="test").text
    real = page(client, source="real").text
    assert SPAMMER in real and "+14085550100" not in real
    assert "<span class=tag>test</span>" in page(client).text


def test_real_filter_without_test_numbers_shows_everything(make):
    client, _ = make(SALES)
    post(client, "/voice", CallSid="CA1", From=SPAMMER)
    assert SPAMMER in page(client, source="real").text
    assert SPAMMER not in page(client, source="test").text


def test_flag_keeps_page_filters(make):
    client, _ = make(SALES)
    run_call(client, "solar panels")
    r = client.post("/flag", data={"token": "admin", "sid": "CA1", "wrong": "1",
                                   "back": "decision=block&source=real"}, follow_redirects=False)
    assert r.headers["location"].endswith("&decision=block&source=real")


def test_flag_requires_admin_token(make):
    client, _ = make(SALES)
    run_call(client, "solar panels")
    assert client.post("/flag", data={"token": "nope", "sid": "CA1", "wrong": "1"}).status_code == 404


def test_flag_marks_and_unmarks_a_verdict(make):
    client, _ = make(SALES)
    run_call(client, "solar panels")
    r = client.post("/flag", data={"token": "admin", "sid": "CA1", "wrong": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.app.state.callguard.calls.get("CA1")["wrong"] == 1
    assert "1 marked wrong" in page(client).text
    client.post("/flag", data={"token": "admin", "sid": "CA1", "wrong": "0"},
                follow_redirects=False)
    assert client.app.state.callguard.calls.get("CA1")["wrong"] == 0


def test_low_confidence_is_highlighted(make):
    client, _ = make(SALES)
    post(client, "/voice", CallSid="CA1", From=SPAMMER)
    post(client, "/screen", CallSid="CA1", From=SPAMMER, SpeechResult="mumble", Confidence="0.31")
    post(client, "/decide", CallSid="CA1", From=SPAMMER)
    assert 'class="lo">0.31' in page(client).text


def test_wrong_column_added_to_existing_db(tmp_path):
    # A database created before the review flag existed must still open.
    import sqlite3
    from app import CallLog
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE calls (sid TEXT PRIMARY KEY, ts REAL, from_number TEXT, "
                "transcript TEXT, confidence REAL, decision TEXT, category TEXT, caller TEXT, "
                "summary TEXT, mode TEXT, error TEXT, outcome TEXT, recording_url TEXT)")
    old.execute("INSERT INTO calls (sid, decision) VALUES ('OLD1', 'block')")
    old.commit(); old.close()
    log = CallLog(str(db))
    assert log.get("OLD1")["wrong"] == 0
    assert log.counts()["block"] == 1
