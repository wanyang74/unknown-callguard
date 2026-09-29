# CallGuard

Screens unknown callers to an iPhone and hangs up on sales/spam. Everything else rings
through with a spoken summary. Setup steps for humans are in README.md.

Personal details (real phone numbers, deployment status, carrier notes) live in
`CLAUDE.local.md`, which is gitignored. Never copy them into tracked files.

## How it works
Unknown call → iPhone (Silence Unknown Callers) declines it → the carrier busy-forwards it to
a Twilio number → Twilio webhooks to this FastAPI app → caller is asked for name + reason
(Twilio `<Gather input="speech">`) → Claude (`classifier.py`) returns block/allow → block:
goodbye + hang up; allow: `<Dial>` the owner's cell with caller ID = Twilio number and a
whisper summary; no answer: voicemail.

- `app.py`: webhooks `/voice` → `/screen` → `/decide` → `/whisper`, `/dial-done`, `/voicemail`;
  call log page `/calls?token=ADMIN_TOKEN` (filters: decision, real vs test calls — test
  phones are the `TEST_NUMBERS` secret, comma-separated E.164); SQLite log.
- `classifier.py`: Claude call with structured JSON output, effort low. Fails open (rings the
  owner) on any error or timeout.
- Rules: `rules.md` is a public example; real rules go in `rules.local.md`, which is used
  when present. It is gitignored but still shipped to the server (see `.dockerignore`).
- `test_silence.py`: places a call from the Twilio number to time carrier forwarding.
- `try_classifier.py`: runs sample transcripts through the real classifier.
- Tests: `.venv/bin/pytest -q` (they sign fake Twilio requests).
- `fallback/`: Twilio Function set as the number's "Primary handler fails" URL. If the server
  is down it rings the owner unscreened (same loop guard), then voicemail in Twilio.
  `deploy_fallback.py` (re)deploys it; run it where the Twilio secrets are (the Fly machine).
- `fly.toml` has a placeholder app name; deploy with `fly deploy -a <app>`.

## Design rules (keep these)
- Never hang up because of our own error: every failure path rings the owner
  (`Verdict.fail_open`); if the server itself is down, the Twilio fallback rings the owner.
- Loop guard: a call coming *from* the Twilio number or the owner's cell is
  `<Reject reason="busy">` so a declined `<Dial>` falls through to voicemail instead of looping.
- `RING_SECONDS` (20) must stay shorter than the carrier no-answer timer (30s).
- `SCREEN_MODE=shadow` logs what would be blocked but still rings; `enforce` hangs up.
- The caller transcript is untrusted input; the classifier prompt says so.
- A caller who gives no reason ("Hi.", only a name) gets category `no_reason` and is asked
  once more; the second answer is judged together with the first. Still no reason (or
  silence) → blocked.

## Carrier facts (GSM networks)
- Silenced unknown calls are treated as **busy** by the carrier and forwarded after ~3s.
  Busy forwarding is `**67*<twilio>#`; `**61*<twilio>**30#` (no answer) and
  `**62*<twilio>#` (unreachable) are backups; `##004#` removes all. Some carrier help pages
  mislabel these — the phone's own confirmation message is authoritative.
- iPhone **Live Voicemail must be off**, or the phone answers silenced calls itself and
  nothing is forwarded.
- iOS gives apps no access to Apple's screening transcript and no way to end calls, which is
  why this runs server-side.
- Twilio free trials no longer include a number; a paid account is needed.

## Ideas discussed, not built
- Cheaper speech-to-text: Twilio `<Record>` + a separate transcription service.
- Keyword pre-filter (allow words first, then spam phrases, then Claude for the rest).
- Allowlist of numbers that skip screening and go straight to voicemail.
- Multiple owners, one Twilio number each (map Twilio number → cell + name).
