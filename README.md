# CallGuard

Screens unknown callers to your iPhone and hangs up on sales and spam. Everything else
rings through, with a short spoken summary when you pick up.

```
Unknown caller → iPhone silences it → US Mobile forwards it → Twilio number → CallGuard
  "Please say your name and why you're calling."
  Claude checks the answer against rules.md
    ├─ sales/spam → "Please remove this number from your list." → hang up
    └─ anything else → rings your iPhone as "CallGuard"
                       you answer: "Screened call: Dr. Patel's office about tomorrow."
                       you don't: CallGuard takes a voicemail
```

## What you need
- A **Twilio** account on a paid plan, with one local number with Voice. Free trials no longer
  include a number of their own and block calling your cell and recording voicemail, so
  upgrade by adding funds. About $1.15/month plus a few cents per screened call.
- An **Anthropic API key** from console.anthropic.com. Well under a cent per call.
- A **Fly.io** account for hosting. About $2–3/month for one small always-on machine.

## 1. Try the classifier locally (optional, 2 min)
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
ANTHROPIC_API_KEY=sk-ant-... .venv/bin/python try_classifier.py
```
`try_classifier.py` runs sample transcripts and prints each decision and how long it took.
Edit `rules.md` until you like the results.

## 2. Deploy to Fly.io
```bash
brew install flyctl && fly auth login
fly launch --no-deploy --copy-config      # choose an app name, then put it in fly.toml
fly volumes create callguard_data --size 1
fly secrets set ANTHROPIC_API_KEY=... TWILIO_AUTH_TOKEN=... \
  TWILIO_NUMBER=+1XXXXXXXXXX MY_CELL=+1YYYYYYYYYY \
  PUBLIC_BASE_URL=https://<app-name>.fly.dev ADMIN_TOKEN=<long random string> \
  OWNER_NAME=<your name> SCREEN_MODE=shadow
fly deploy
curl https://<app-name>.fly.dev/health
```

## 3. Point the Twilio number at it
Twilio Console → Phone Numbers → your number → **Voice configuration** →
"A call comes in": **Webhook**, `https://<app-name>.fly.dev/voice`, **HTTP POST**. Save.

## 4. Set up the iPhone
1. **Save the Twilio number as a contact** called "CallGuard". Calls that CallGuard puts
   through come from this number, so iOS has to know it or it will silence them too.
2. Settings → Apps → Phone → **Screen Unknown Callers → Silence**. Don't use "Ask Reason
   for Calling", or Apple's screener answers before CallGuard can.
3. Settings → Apps → Phone → **Live Voicemail → off**. With it on, the iPhone answers
   silenced calls itself and records the voicemail on the phone, so the carrier sees the
   call as answered and never forwards it to CallGuard.
4. In the Phone app, dial each of these codes and press call (replace `1XXXXXXXXXX` with the
   Twilio number, no `+`). Lightspeed uses standard GSM codes:

   | Code | Forwards when |
   |---|---|
   | `**67*1XXXXXXXXXX#` | busy or declined (this is how silenced calls leave your phone) |
   | `**61*1XXXXXXXXXX**30#` | not answered within 30 seconds |
   | `**62*1XXXXXXXXXX#` | phone off or no signal |

   To check a code: `*#67#`. To turn all of them off and go back to US Mobile voicemail: `##004#`.
   If a code fails, ask US Mobile support to enable conditional call forwarding on your line.

## 4b. Add a backup for when the server is down (recommended)
`fallback/fallback.js` is a Twilio Function that rings your cell unscreened (then takes a
voicemail) if CallGuard doesn't answer Twilio. Deploy it and attach it as the number's
"Primary handler fails" URL by running, wherever your Twilio secrets are set:
```bash
python fallback/deploy_fallback.py <your Account SID> fallback/fallback.js
```

## 5. Test it, then turn on blocking
Call yourself from a number that isn't in your contacts (a friend's phone, Google Voice):
- Pitch solar: in `shadow` mode it still rings you, and the summary says "Would have been blocked as sales".
- Say you're from a doctor's office: it rings you with the summary.
- Stay silent: it asks again, then hangs up.

Every call is logged at `https://<app-name>.fly.dev/calls?token=<ADMIN_TOKEN>` with what the
caller said and what CallGuard decided. After a few days without wrong calls, start blocking:
```bash
fly secrets set SCREEN_MODE=enforce
```
To change the rules, edit `rules.md` and run `fly deploy`.

## Things to know
- **Missed calls from your contacts go to CallGuard too.** Carrier forwarding can't tell
  them apart. They get screened like anyone else (a real person is let through) and then
  reach CallGuard's voicemail. CallGuard's voicemail replaces US Mobile's.
- **Calls that get through show up as "CallGuard"**, not the caller's number. The spoken
  summary tells you who it is, and the log shows the real number.
- **Wanted callers wait about 3–6 seconds** before your phone rings.
- **If anything fails** (Claude times out or errors, the server restarts mid-call), the call
  rings through to you. CallGuard never hangs up because of its own error.
- **Voicemail links** on the log page open in Twilio and ask for your Twilio login.
- **Recording consent:** callers are told their call is being screened ("<your name> asks me to screen all unknown calls").
  Audio is only recorded for voicemail, after "leave a message after the tone".
