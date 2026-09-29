"""Find out how US Mobile treats a call your iPhone silences: busy (instant) or no answer (rings out).

Places a call from your Twilio number to your cell and times how long it rings before
something (US Mobile voicemail) answers. No server needed; run it from your laptop.

    export TWILIO_ACCOUNT_SID=AC... TWILIO_AUTH_TOKEN=... \
           TWILIO_NUMBER=+1XXXXXXXXXX MY_CELL=+1YYYYYYYYYY
    python scripts/test_silence.py silence       # Screen Unknown Callers = Silence, don't touch the phone
    python scripts/test_silence.py decline       # baseline: setting = Never, tap Decline when it rings
    python scripts/test_silence.py ignore        # baseline: setting = Never, let it ring, don't touch

Keep the Twilio number OUT of your contacts, and don't call or text it from your iPhone,
or iOS will treat it as known and let it ring.
"""

import os
import sys
import time

from twilio.rest import Client

# What the callee hears if something answers: 8 seconds of silence, then hang up.
# (On a trial account Twilio plays its trial notice first; that's fine for this test.)
TWIML = '<Response><Pause length="8"/><Hangup/></Response>'
FINAL = {"completed", "busy", "no-answer", "failed", "canceled"}


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "silence"
    env = os.environ
    client = Client(env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"])

    # timeout=45 lets it ring longer than a typical 20-30s no-answer forward, so we see it.
    call = client.calls.create(to=env["MY_CELL"], from_=env["TWILIO_NUMBER"],
                               twiml=TWIML, timeout=45)
    print(f"[{label}] calling {env['MY_CELL']} (call {call.sid})")

    start, last, ringing_at, answered_at = time.monotonic(), None, None, None
    while True:
        status = client.calls(call.sid).fetch().status
        t = time.monotonic() - start
        if status != last:
            print(f"  {t:5.1f}s  {status}")
            last = status
        if status == "ringing" and ringing_at is None:
            ringing_at = t
        if status == "in-progress" and answered_at is None:
            answered_at = t
        if status in FINAL:
            break
        time.sleep(0.5)

    print()
    if answered_at is not None:
        rang = answered_at - (ringing_at or 0)
        print(f"Result: answered (voicemail) after ringing ~{rang:.0f}s")
        if rang <= 8:
            print("=> BUSY path: the call was rejected and forwarded right away (**67).")
        else:
            print("=> NO-ANSWER path: it rang out before forwarding (**61). "
                  "Use a short timer, e.g. **61*<twilio>**10#.")
    elif last == "busy":
        print("Result: busy with no voicemail. The call was rejected (busy path), "
              "but nothing picked up; is US Mobile voicemail set up?")
    elif last == "no-answer":
        print("Result: rang 45s with no answer and nothing picked up. "
              "Is US Mobile voicemail set up?")
    else:
        print(f"Result: call ended as '{last}'. Check the Twilio console call log for details.")


if __name__ == "__main__":
    main()
