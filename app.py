"""CallGuard: screens unknown callers forwarded from your iPhone and hangs up on sales/spam.

Call flow (all webhooks come from Twilio):
  /voice      caller arrives -> ask for name + reason (speech Gather)
  /partial    partial transcript while they talk -> start classifying early
  /screen     got the answer -> start classifying, say "one moment"
  /decide     block -> goodbye + hang up;  allow -> Dial your cell
  /whisper    played to you when you pick up: who it is and why
  /dial-done  you didn't answer -> take a voicemail
  /voicemail  voicemail saved
"""

import asyncio
import html
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Dial, Gather, VoiceResponse

from classifier import NO_REASON, Classifier, Verdict, default_rules_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("callguard")

VOICE = "Polly.Salli-Neural"  # US English

# Words the recognizer should expect from real callers; improves transcript accuracy.
SPEECH_HINTS = ("pharmacy, prescription, appointment, doctor, dentist, clinic, delivery, "
                "package, school, loan, refinance, warranty, insurance, solar, survey")
# A partial transcript shorter than this isn't enough to start judging a caller on.
MIN_PARTIAL_WORDS = 12
# ...and hanging up on one needs more words still. Measured 2026-09-24: "calling about the
# loan application you submitted with us last week" reads as spam at 13 words (6/6 block)
# but as a legitimate lender at 36 (6/6 allow). Ringing on a short partial is free; hanging
# up on one cuts off a real caller.
MIN_TRUSTED_PARTIAL_WORDS = 30


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


class CallLog:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS calls (
                sid TEXT PRIMARY KEY, ts REAL, from_number TEXT, transcript TEXT,
                confidence REAL, decision TEXT, category TEXT, caller TEXT, summary TEXT,
                mode TEXT, error TEXT, outcome TEXT, recording_url TEXT)"""
        )
        # Added after the first deploys, so patch older databases in place.
        if "wrong" not in {r[1] for r in self.db.execute("PRAGMA table_info(calls)")}:
            self.db.execute("ALTER TABLE calls ADD COLUMN wrong INTEGER DEFAULT 0")
        self.db.commit()

    def upsert(self, sid: str, **fields):
        cols = ["sid", *fields]
        self.db.execute(
            f"INSERT INTO calls ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(sid) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in fields)}",
            [sid, *fields.values()],
        )
        self.db.commit()

    def get(self, sid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM calls WHERE sid = ?", [sid]).fetchone()

    @staticmethod
    def _source_filter(source: str, test_numbers: tuple[str, ...]) -> tuple[str, list]:
        """SQL condition for source "test" (from a test number), "real" (not), or "" (all)."""
        if source not in ("test", "real"):
            return "1", []
        if not test_numbers:  # "x NOT IN (NULL)" would match nothing
            return ("0" if source == "test" else "1"), []
        marks = ",".join("?" * len(test_numbers)) or "NULL"
        op = "IN" if source == "test" else "NOT IN"
        return f"COALESCE(from_number, '') {op} ({marks})", list(test_numbers)

    def recent(self, limit: int = 200, decision: str = "", source: str = "",
               test_numbers: tuple[str, ...] = ()) -> list[sqlite3.Row]:
        where, args = self._source_filter(source, test_numbers)
        if decision:
            where, args = f"{where} AND decision = ?", args + [decision]
        return self.db.execute(f"SELECT * FROM calls WHERE {where} ORDER BY ts DESC LIMIT ?",
                               args + [limit]).fetchall()

    def counts(self, source: str = "", test_numbers: tuple[str, ...] = ()) -> dict[str, int]:
        where, args = self._source_filter(source, test_numbers)
        rows = self.db.execute(
            "SELECT decision, COUNT(*) n, SUM(COALESCE(wrong, 0)) w FROM calls "
            f"WHERE {where} GROUP BY decision", args)
        out = {}
        for r in rows:
            out[r["decision"] or "pending"] = r["n"]
            out[f"{r['decision'] or 'pending'}_wrong"] = r["w"] or 0
        return out


def twiml(r: VoiceResponse) -> Response:
    return Response(content=str(r), media_type="application/xml")


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


def create_app(settings: Settings, classifier: Classifier | None = None) -> FastAPI:
    if classifier is None:
        rules = Path(settings.rules_path).read_text()
        classifier = Classifier(rules, settings.model, settings.classify_timeout_s)
    st = State(settings, classifier, CallLog(settings.db_path))
    validator = RequestValidator(settings.twilio_auth_token)
    app = FastAPI(title="CallGuard")
    app.state.callguard = st

    async def twilio_form(request: Request) -> dict[str, str]:
        form = {k: str(v) for k, v in (await request.form()).items()}
        if settings.validate_signature:
            # Rebuild the public URL Twilio signed (we sit behind the host's proxy).
            url = settings.public_base_url + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            if not validator.validate(url, form, request.headers.get("X-Twilio-Signature", "")):
                raise HTTPException(status_code=403, detail="bad Twilio signature")
        return form

    tz = ZoneInfo(settings.timezone)

    def url(path: str) -> str:
        return settings.public_base_url + path

    def connect_to_owner(r: VoiceResponse, sid: str) -> VoiceResponse:
        # Caller ID is the Twilio number (saved in your contacts) so iOS rings instead of
        # silencing it. The Dial timeout must be shorter than the carrier's no-answer
        # forwarding timer, or your voicemail forwarding would bounce the call back here.
        dial = Dial(caller_id=settings.twilio_number, timeout=settings.ring_seconds,
                    action=url("/dial-done"), method="POST")
        dial.number(settings.my_cell, url=url(f"/whisper?sid={quote(sid)}"), method="POST")
        r.append(dial)
        return r

    @app.get("/health")
    async def health():
        return {"ok": True, "mode": settings.mode}

    def ask(prompt: str) -> Gather:
        gather = Gather(input="speech", action=url("/screen"), method="POST",
                        speech_timeout="auto", timeout=6, language="en-US",
                        hints=SPEECH_HINTS,
                        partial_result_callback=url("/partial"),
                        partial_result_callback_method="POST")
        gather.say(prompt, voice=VOICE)
        return gather

    @app.post("/voice")
    async def voice(request: Request, attempt: int = 1):
        form = await twilio_form(request)
        sid, caller = form.get("CallSid", ""), form.get("From", "")
        r = VoiceResponse()

        # Loop guard: our own Dial to your cell got forwarded back (you declined or it
        # timed out on the carrier side). Reject without answering so the Dial sees
        # "busy" and falls through to voicemail.
        if caller in (settings.twilio_number, settings.my_cell):
            r.reject(reason="busy")
            return twiml(r)

        if attempt == 1:
            st.calls.upsert(sid, ts=time.time(), from_number=caller, mode=settings.mode)
            prompt = (f"Hi, this is {settings.owner_name}'s personal assistant Emma. "
                      f"{settings.owner_name} asks me to screen all unknown calls. "
                      "Please say your name and briefly why you're calling and I'll help you "
                      "connect.")
        else:
            prompt = "Sorry, I didn't catch that. Please say your name and the reason for your call."

        r.append(ask(prompt))
        # Only reached if the caller said nothing (robocalls often stay silent).
        if attempt == 1:
            r.redirect(url("/voice?attempt=2"), method="POST")
        else:
            st.calls.upsert(sid, decision="block", category="silent", outcome="no_response")
            r.say("Goodbye.", voice=VOICE)
            r.hangup()
        return twiml(r)

    @app.post("/screen")
    async def screen(request: Request):
        form = await twilio_form(request)
        sid = form.get("CallSid", "")
        transcript = form.get("SpeechResult", "")
        if sid in st.asked_again:
            # Second answer: judge it together with the first, e.g. "Hi." + "It's your aunt."
            transcript = f"{st.asked_again[sid]} {transcript}".strip()
        st.calls.upsert(sid, transcript=transcript,
                        confidence=float(form.get("Confidence") or 0))
        # The speculative run from /partial stands in for the real one only if it saw the
        # same words the caller ended up saying; otherwise classify the final transcript.
        early = st.early.get(sid)
        if early is not None and early[1] == transcript.strip():
            st.pending[sid] = st.early.pop(sid)[0]
        else:
            # Classify in the background so the caller hears something instead of silence.
            st.pending[sid] = asyncio.create_task(
                st.classifier.classify(transcript, form.get("From", "")))
        r = VoiceResponse()
        r.say("Thank you. One moment please.", voice=VOICE)
        r.redirect(url("/decide"), method="POST")
        return twiml(r)

    @app.post("/partial")
    async def partial(request: Request):
        """Twilio posts partial transcripts here while the caller is still speaking. The
        first substantial one starts a speculative classification, so the verdict is usually
        ready before /decide runs. Twilio ignores any TwiML returned from this callback."""
        form = await twilio_form(request)
        sid = form.get("CallSid", "")
        text = (form.get("UnstableSpeechResult") or "").strip()
        if sid and sid not in st.early and len(text.split()) >= MIN_PARTIAL_WORDS:
            st.early[sid] = (asyncio.create_task(
                st.classifier.classify(text, form.get("From", ""))), text)
            log.info("call %s: early classify on %d words", sid, len(text.split()))
        return Response(status_code=204)

    @app.post("/decide")
    async def decide(request: Request):
        form = await twilio_form(request)
        sid = form.get("CallSid", "")
        task = st.pending.pop(sid, None)
        early_task, early_text = st.early.pop(sid, (None, ""))
        row = st.calls.get(sid)
        transcript = (row["transcript"] if row else "") or ""

        def fallback(reason: str) -> Verdict:
            """The final classification is unusable. Use the verdict from the partial
            transcript if we got one -- it judged most of what the caller said, which beats
            ringing the owner blindly. Falls open when there is no usable partial verdict."""
            if (early_task is not None and early_task.done() and not early_task.cancelled()
                    and early_task.exception() is None):
                v = early_task.result()
                words = len(early_text.split())
                if v.decision == "allow" or words >= MIN_TRUSTED_PARTIAL_WORDS:
                    log.info("call %s: %s, using partial verdict %s from %d words",
                             sid, reason, v.decision, words)
                    return replace(v, error=f"{reason}_partial_verdict")
                log.info("call %s: %s, partial says block on only %d words -- ringing instead",
                         sid, reason, words)
            return Verdict.fail_open(transcript, reason)

        if task is None:  # e.g. the server restarted mid-call
            verdict = fallback("no_pending_task")
        else:
            # asyncio.wait never raises, so every failure below falls through to fallback().
            await asyncio.wait({task}, timeout=settings.classify_timeout_s + 2)
            if not task.done():
                task.cancel()
                verdict = fallback("timeout")
            elif task.cancelled():
                verdict = fallback("cancelled")
            elif task.exception() is not None:
                log.error("classification failed", exc_info=task.exception())
                verdict = fallback(f"exception_{type(task.exception()).__name__}")
            else:
                verdict = task.result()
        if early_task is not None and not early_task.done():
            early_task.cancel()  # final verdict won; don't leave the speculative call running

        r = VoiceResponse()
        if verdict.category == NO_REASON and sid not in st.asked_again:
            # One more chance before hanging up: a real person who only said "hi" will
            # usually answer a direct question. Silence here counts as no reason given.
            st.asked_again[sid] = transcript
            log.info("call %s: no reason given, asking again", sid)
            r.append(ask("Sorry, could you tell me who's calling and what it's about?"))
            # Only reached if they say nothing to the second question.
            st.calls.upsert(sid, decision="block", category=NO_REASON,
                            summary="Gave no reason for calling, even when asked again.")
            if settings.mode == "enforce":
                st.calls.upsert(sid, outcome="blocked")
                r.say("Goodbye.", voice=VOICE)
                r.hangup()
                return twiml(r)
            r.say("Thanks. Connecting you now.", voice=VOICE)
            return twiml(connect_to_owner(r, sid))
        st.asked_again.pop(sid, None)

        st.calls.upsert(sid, decision=verdict.decision, category=verdict.category,
                        caller=verdict.caller, summary=verdict.summary, error=verdict.error)
        log.info("call %s: %s (%s) %s", sid, verdict.decision, verdict.category, verdict.summary)

        if verdict.decision == "block" and settings.mode == "enforce":
            st.calls.upsert(sid, outcome="blocked")
            r.say(f"Thanks. {settings.owner_name} isn't available for this call. "
                  "Please remove this number from your list. Goodbye.", voice=VOICE)
            r.hangup()
            return twiml(r)
        r.say("Thanks. Connecting you now.", voice=VOICE)
        return twiml(connect_to_owner(r, sid))

    @app.post("/whisper")
    async def whisper(request: Request, sid: str = ""):
        await twilio_form(request)
        row = st.calls.get(sid)
        r = VoiceResponse()
        if row and row["decision"] == "block":  # only happens in shadow mode
            r.say(f"Screened call. Would have been blocked as {row['category']}.", voice=VOICE)
        if row and row["summary"]:
            r.say(f"Screened call: {row['summary']}", voice=VOICE)
        else:
            r.say("Screened call.", voice=VOICE)
        return twiml(r)

    @app.post("/dial-done")
    async def dial_done(request: Request):
        form = await twilio_form(request)
        sid, status = form.get("CallSid", ""), form.get("DialCallStatus", "")
        r = VoiceResponse()
        if status in ("completed", "answered"):
            st.calls.upsert(sid, outcome="answered")
            r.hangup()
            return twiml(r)
        st.calls.upsert(sid, outcome=f"missed_{status}")
        r.say(f"Sorry, {settings.owner_name} can't pick up right now. "
              "Please leave a message after the tone.", voice=VOICE)
        r.record(max_length=120, play_beep=True, action=url("/voicemail"), method="POST")
        r.hangup()
        return twiml(r)

    @app.post("/voicemail")
    async def voicemail(request: Request):
        form = await twilio_form(request)
        st.calls.upsert(form.get("CallSid", ""), outcome="voicemail",
                        recording_url=form.get("RecordingUrl", ""))
        r = VoiceResponse()
        r.say("Thanks, your message was saved. Goodbye.", voice=VOICE)
        r.hangup()
        return twiml(r)

    @app.post("/flag")
    async def flag(request: Request):
        """Mark a verdict right or wrong from the review page. Browser form post, so this
        is guarded by the admin token rather than a Twilio signature."""
        form = await request.form()
        if str(form.get("token", "")) != settings.admin_token:
            raise HTTPException(status_code=404)
        st.calls.upsert(str(form.get("sid", "")),
                        wrong=1 if str(form.get("wrong")) == "1" else 0)
        back = f"/calls?token={quote(settings.admin_token)}"
        if form.get("back"):  # the page's filters, e.g. "decision=block&source=real"
            back += "&" + str(form.get("back"))
        return RedirectResponse(back, status_code=303)

    @app.get("/calls", response_class=HTMLResponse)
    async def calls_page(token: str = "", decision: str = "", source: str = ""):
        if token != settings.admin_token:
            raise HTTPException(status_code=404)
        esc = lambda v: html.escape("" if v is None else str(v))  # noqa: E731
        tok = quote(settings.admin_token)
        tests = settings.test_numbers
        n = st.calls.counts(source, tests)
        filters = urlencode({k: v for k, v in (("decision", decision), ("source", source)) if v})

        rows = []
        for c in st.calls.recent(decision=decision, source=source, test_numbers=tests):
            rec = (f'<a href="{esc(c["recording_url"])}.mp3">play</a>'
                   if c["recording_url"] else "")
            # Twilio's own confidence in the transcript: a block on a garbled one is the
            # case worth a second look, and it is free to spot.
            conf = c["confidence"] or 0
            conf_cell = (f'<span class="lo">{conf:.2f}</span>' if 0 < conf < 0.6
                         else (f"{conf:.2f}" if conf else ""))
            wrong = (c["wrong"] or 0) if "wrong" in c.keys() else 0
            btn = (f'<form method=post action="/flag"><input type=hidden name=token value="{esc(settings.admin_token)}">'
                   f'<input type=hidden name=sid value="{esc(c["sid"])}">'
                   f'<input type=hidden name=back value="{esc(filters)}">'
                   f'<input type=hidden name=wrong value="{0 if wrong else 1}">'
                   f'<button class="{"bad" if wrong else ""}">{"wrong &#10007;" if wrong else "mark wrong"}</button></form>')
            rows.append(
                f'<tr class="{"flagged" if wrong else ""}">' + "".join(f"<td>{x}</td>" for x in [
                    esc(datetime.fromtimestamp(c["ts"] or 0, tz).strftime("%b %d %H:%M %Z")),
                    esc(c["from_number"]) + (' <span class=tag>test</span>'
                                              if c["from_number"] in tests else ""),
                    f'<b>{esc(c["decision"])}</b>', esc(c["category"]),
                    esc(c["caller"]), esc(c["summary"]), esc(c["transcript"]), conf_cell,
                    esc(c["outcome"]), rec, esc(c["error"]), btn,
                ]) + "</tr>")

        def tab(label, key, val):
            cur = {"decision": decision, "source": source}
            on = " class=on" if cur[key] == val else ""
            q = urlencode({k: v for k, v in {**cur, key: val}.items() if v})
            return f'<a{on} href="/calls?token={tok}{"&" + q if q else ""}">{esc(label)}</a>'

        blocked, allowed = n.get("block", 0), n.get("allow", 0)
        return (
            "<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width'>"
            "<title>CallGuard log</title><style>body{font:14px system-ui;margin:16px}"
            "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;"
            "padding:6px;text-align:left;vertical-align:top}"
            "nav{margin:8px 0 16px}nav a{margin-right:12px;text-decoration:none;color:#2a5d8f}"
            "nav a.on{font-weight:700;text-decoration:underline}"
            ".lo{color:#b8412c;font-weight:600}tr.flagged{background:#fff4f0}"
            "button{font:12px system-ui;cursor:pointer}button.bad{color:#b8412c;font-weight:600}"
            "form{margin:0}.tag{font-size:11px;background:#e8eef5;color:#2a5d8f;"
            "border-radius:4px;padding:1px 5px}</style>"
            f"<h1>Screened calls</h1><p>Mode: <b>{esc(settings.mode)}</b> &middot; "
            f"{blocked} blocked ({n.get('block_wrong', 0)} marked wrong) &middot; "
            f"{allowed} allowed ({n.get('allow_wrong', 0)} marked wrong)</p>"
            f'<nav>{tab("All", "decision", "")}{tab("Blocked", "decision", "block")}'
            f'{tab("Allowed", "decision", "allow")}</nav>'
            f'<nav>{tab("All callers", "source", "")}{tab("Real calls", "source", "real")}'
            f'{tab("Test calls", "source", "test")}</nav>'
            "<table><tr>"
            "<th>When</th><th>From</th><th>Decision</th><th>Category</th><th>Caller</th>"
            "<th>Why</th><th>They said</th><th>Conf</th><th>Outcome</th><th>Voicemail</th>"
            "<th>Error</th><th>Review</th></tr>"
            + "".join(rows) + "</table>"
        )

    return app


def _app_from_env() -> FastAPI:
    return create_app(Settings.from_env())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(_app_from_env(), host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
