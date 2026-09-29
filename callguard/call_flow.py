"""Twilio webhooks for one screened call.

  /voice      caller arrives -> ask for name + reason (speech Gather)
  /partial    partial transcript while they talk -> start classifying early
  /screen     got the answer -> start classifying, say "one moment"
  /decide     block -> goodbye + hang up;  allow -> Dial your cell
  /whisper    played to you when you pick up: who it is and why
  /dial-done  you didn't answer -> take a voicemail
  /voicemail  voicemail saved
"""

import asyncio
import logging
import time
from dataclasses import replace
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Dial, Gather, VoiceResponse

from .classifier import NO_REASON, Verdict
from .state import State

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


def twiml(r: VoiceResponse) -> Response:
    return Response(content=str(r), media_type="application/xml")


def build_router(st: State) -> APIRouter:
    settings = st.settings
    validator = RequestValidator(settings.twilio_auth_token)
    router = APIRouter()

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

    def ask(prompt: str) -> Gather:
        gather = Gather(input="speech", action=url("/screen"), method="POST",
                        speech_timeout="auto", timeout=6, language="en-US",
                        hints=SPEECH_HINTS,
                        partial_result_callback=url("/partial"),
                        partial_result_callback_method="POST")
        gather.say(prompt, voice=VOICE)
        return gather

    @router.post("/voice")
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

    @router.post("/screen")
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

    @router.post("/partial")
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

    @router.post("/decide")
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

    @router.post("/whisper")
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

    @router.post("/dial-done")
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

    @router.post("/voicemail")
    async def voicemail(request: Request):
        form = await twilio_form(request)
        st.calls.upsert(form.get("CallSid", ""), outcome="voicemail",
                        recording_url=form.get("RecordingUrl", ""))
        r = VoiceResponse()
        r.say("Thanks, your message was saved. Goodbye.", voice=VOICE)
        r.hangup()
        return twiml(r)

    return router
