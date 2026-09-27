# Call screening rules

Edit this file in plain English. It is sent to the classifier on every call.

This is an example. To keep your real rules out of git, copy it to `rules.local.md` and edit
that copy: CallGuard uses `rules.local.md` when it exists, and `.gitignore` excludes it.

## Block (hang up automatically)
- Sales and marketing of any kind: solar, insurance quotes, extended car warranties,
  home services, credit card or loan offers, "limited time" deals, business funding.
- Robocalls and pre-recorded messages.
- Scams and suspicious calls: "your account is compromised", IRS / Social Security /
  police threats, gift cards, crypto, tech support, prize or sweepstakes winnings.
- Surveys, political campaigns and polling, donation requests.
- Callers who don't say why they're calling -- even people who sound friendly, like
  someone who only says "hi" or only gives a name. They are asked once more; if they
  still give no reason, hang up.
- Vague reasons like "following up on your inquiry" without naming a real company or
  person.

## Always let through
- Anyone who gives a personal name and a personal reason (friend, family, neighbor).
- Doctors, dentists, pharmacies, hospitals, schools, childcare.
- Deliveries, drivers, couriers, restaurants with an order, food delivery drivers trying
  to find my home.
- Companies about an appointment or an order I actually placed.
- Recruiters and job-related calls.
- Emergencies of any kind.

## What's going around — add a line here any time

Scam scripts change every few months. When a new one shows up, add a bullet: what the
caller claims, and why it is false for you. Newest first, with the date so old ones can
be pruned later. Plain English is fine — no special wording needed. Redeploy after
editing (`fly deploy`).

These carry more weight than the general rules above, because they are facts about your
life that a caller can contradict. "You applied for a loan" is a judgement call in the
abstract; it is a provable lie once this file says you never did.

- YYYY-MM-DD — Example: "You applied for / inquired about a personal loan." I never have.
  It is a script even when the caller gives their name and names a real company.

## When unsure
If the caller gave a real reason but you can't tell whether it's wanted, let the call
through. A missed sales call costs nothing; a missed real call does. This does not apply
to callers who gave no reason at all -- see Block.
