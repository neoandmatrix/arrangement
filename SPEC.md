# Bandobast: product spec

Bandobast arranges family functions in another city. The family tells it what they need, in their own language; it calls local vendors in theirs, negotiates, gets the family's approval, confirms the booking, and keeps checking until the day, switching to a backup if a vendor falls through.

Decided on 2026-11-03. Synthetic data only (contest rule): vendors are fictional, played by friends in the demo.

## Scope

| | In scope | Out of scope |
|---|---|---|
| Functions | Griha pravesh, pujas, baby ceremonies | Birthdays, weddings, parties |
| Vendors | Priest, caterer, decorator, cake, photographer, tent and sound | Venue booking, travel, return gifts |
| City | Bengaluru (Kannada, Hindi, Telugu, English vendors) | Other cities |
| Family | The Bandobast app (Flutter, Android); family-facing text in any of Gnani's 10 languages | WhatsApp (later) |
| Vendors | The same app in vendor mode: calls ring in the app over the internet (PCM WebSocket audio, Firebase push). Vendors without the app get a phone call (Twilio; code in the repo, needs a real number) | |
| Money | Record the agreed advance and remind the family | Collecting or sending payments |
| After booking | Confirmation call, check-ins, confirmed backup activation | Post-event rating (planned), a day-of emergency line |


## The app (decided 2026-11-03)

One Flutter app, two roles chosen on first launch.

**Family:** pin the venue on a map (OpenStreetMap), pick the occasion and variant, tick the services (the function's usual ones are pre-ticked), date and muhurat, guests, a budget and wishes per service. Submit, then watch it work: vendors found, called one by one, quotes in. Per quote: **approve**, **reject**, or **renegotiate** (Bandobast calls again and asks for a better price, the rules choosing the number). Approving reserves the vendor; spoken confirmation completes the booking. A backup is only secured after its separate standby call.

**Vendor:** pick which (demo) vendor you are. Calls from Bandobast ring full-screen, through Firebase push even when the app is closed, or the live connection while it's open. Answer and talk; the call runs through the same pipeline as every other channel. The vendor sees their enquiries and bookings.

**Voice filling:** the planning form also offers “Fill with voice”, in the selected family language.
Bandobast asks one question at a time and fills a draft. The map pin and area are never changed by
the model. The family reviews the answers, selects the venue and submits explicitly.

**Vendor callbacks:** “Talk to Bandobast” records a revised base rate, cancellation, delayed arrival
or another note. Revised rates, including decreases, need family approval. Other charges remain
unchanged. Operational risks use the existing backup rules. One update is collected per call.

**Direct calls:** the family and its approved primary vendor can call each other inside the app, including while confirmation is pending.
Each family phone has a remembered temporary demo ID; vendors keep directory IDs such as C01.
Direct audio is relayed through the backend, without an LLM, transcript or recording. This is
demo identity under the shared app key, not production account authentication.

**App audio:** mono PCM16 over WebSockets (16 kHz up, 24 kHz down for AI; 16 kHz both ways for
direct calls). Android uses the voice-communication microphone and hardware echo cancellation
where available. Earpiece or headphones are preferred on phones without hardware AEC.
Browser testing continues to use WebRTC. The single-current-event limitation remains.

**Price safeguards:** every base price, transport charge, extra charge and advance must be
grounded in the vendor's answer for that specific charge. Explicit zero is required for absent
charges. Digit amounts with thousand/lakh/crore units are normalized; dates, times and guest
counts are not money. Ambiguous wording prompts a separate question, not a guess.
Unusually high charges, large advances and extreme rate changes need a second vendor statement
of the amount. An advance cannot exceed the total. Caterers must give an actual GST-inclusive
price; tax is never guessed or silently treated as zero. Unresolved amounts block booking,
backup activation and revision approval, even after conversation retries are exhausted.
Old quotes without field-level evidence need “Reconfirm prices”. Rechecking an already booked
quote only refreshes verification if charges match; corrected charges become a pending quote
for family approval. This is price verification, not the booking-confirmation call.
Savings use a verified opening quote with its original pricing unit and charges; a total price
is never reinterpreted as a per-plate opening price, and waived transport is counted accurately.

**Vendor booking confirmation:** family approval reserves the primary vendor and queues a
confirmation call. Code reads the approved date, venue, arrival/setup deadline, guest count,
service details, price unit, charges, total and advance, one question at a time. A clear spoken
yes is required after each read-back has reached the audio output. Only then is the booking
marked confirmed and its check-ins created. The family and vendor see the same written summary.
No answer, decline, callback or an interrupted call never counts as confirmation. Retries use a
fresh attempt ID; old or duplicated replies cannot confirm a newer approval. A server restart
retains approval and exposes a retry rather than claiming success or silently redialling.
Changed prices or service coverage need family approval and a new complete confirmation call.
Date/time/venue changes are shown for family review, not applied to one vendor independently
of the function. The family can reject the change and retry original terms, or cancel the
unconfirmed selection and choose another vendor.

**Backup confirmation:** a selected runner-up is only a candidate until it agrees to standby
availability on its quoted terms. After the primary confirms, Bandobast calls the backup. This
is explicitly not a booking, and no advance is due. Standby failures or quote changes do not
cancel the primary booking; they remove the claim that a backup is secured and alert the family.
If the primary fails, it is marked as needing replacement and its pending check-ins are removed.
Bandobast makes a separate activation call to the backup. Only complete spoken confirmation
promotes the backup to the active primary and creates its check-ins. Standby agreement alone
cannot do this. Higher replacement cost or advance, or a cost above budget, requires family
approval before the activation call. Changed quotes still pass the price safeguards.
Standby and activation attempts have separate immutable IDs and are bound to the primary
approval. Old standby replies cannot complete a newer activation or a changed backup selection.
The family can retry or choose another quoted backup. No-answer, decline, restart or failed
activation leaves an explicit unconfirmed-replacement warning, not a pretend successful switch.
**Automatic check-ins:** a lightweight server worker polls the current function's scheduled
follow-ups and dispatches one due call at a time. Naive venue times mean IST, not the server's
local timezone. Each check-in is bound to the current vendor booking confirmation and each
call has a durable attempt ID. Automatic calls and “Call now” share the same atomic claim;
duplicate clicks, workers or stale call results cannot complete another attempt.
Calls are revalidated after waiting for the Gnani call lock. D-3/D-1 calls have a two-hour useful
window; morning calls thirty minutes and final ETA calls ten minutes, never beyond required
arrival. Expired calls are skipped with an alert, not dialled in a catch-up burst or counted as
vendor no-answers. Service failures/restarts retry up to three attempts within the window.
A genuine no-answer completes one scheduled check-in once; system failures, busy channels and
callbacks do not manufacture a second no-answer strike. Vendor-requested callback times are
honoured if still useful. The family sees queued/running/retry/skipped states and venue times.
AUTO_CHECKINS=0 disables automatic dispatch for manual-only testing; the default is enabled.
The server must stay running. Only the latest/current function is dispatched in this demo.

**Vendors without the app** get a normal phone call through Twilio (`CALL_CHANNEL` code path). It is in the repo and tested with the simulator; it needs a real Indian number to go live.

**Finding vendors:** the demo directory (synthetic vendors with locations), plus discovery near the pin: OpenStreetMap places by type (caterers, bakeries, photo studios, temples) and web search when a search key is set. Discovered businesses are shown as "found nearby" and never contacted in the demo (synthetic data only). The rules shortlist up to three reachable vendors per service by distance, reviews and rating.

**Server:** the existing FastAPI backend. The app talks to it over HTTPS (a tunnel or a small VM) with an access key, so strangers can't spend API credits.

## Functions

Each function has default vendor needs; the family can add or remove any.

| Function | Variants | Default needs | Optional |
|---|---|---|---|
| `griha_pravesh` | Griha pravesh, vastu shanti | priest, caterer, decorator | tent and sound (over 60 guests), photographer |
| `puja` | Satyanarayan, Lakshmi, Ganesh, Rudrabhishek | priest, caterer (prasad or meal), decorator (flowers) | photographer |
| `baby_ceremony` | Naamkaran, annaprashan, mundan | priest, caterer, decorator | cake, photographer |

The function and its variant shape the vendor scripts: what the priest is asked to perform, what "decoration" means, and whether a cake fits.

## Vendor categories

| Category | Asked on the call | Price | Negotiated | Must arrive before muhurat |
|---|---|---|---|---|
| Priest | Free at the muhurat; can do the rituals in the family's way and language; dakshina; samagri included or its cost; travel; advance | Total | No (respect) | 30 min |
| Caterer | Free and can serve before the muhurat ends; can make the food (diet rules); price per plate; transport and staff; GST; advance | Per plate | Yes | 60 min |
| Decorator | Free, set up 3 h early; what the package includes (toran, flowers, rangoli); price; transport; advance | Total | Yes | 3 h |
| Cake | Free to deliver; flavour, eggless, weight; price; delivery charge; advance | Total | Yes (gently) | 2 h |
| Photographer | Free; hours covered; photos and video; when they're delivered; price; travel; advance | Total | Yes | 30 min |
| Tent and sound | Free; tent size, chairs, sound system; setup and removal; price; transport; advance | Total | Yes | 4 h |

Every quote must cover all of its category's items before it can be saved, and every amount must be something the vendor actually said.

## The flow

### 1. Intake (family app; dashboard for testing)

The family fills the form manually or through a guided voice call. Bandobast asks one thing at a time:
function and variant, family name and contact, city and area, date and muhurat, guests, food and diet rules, rituals and their language, which vendors (suggesting the defaults for the function), budget per vendor.
Voice fills a draft; the family chooses the venue pin, reviews it and submits explicitly. Dashboard voice intake remains a separate test entry.

### 2. Shortlist

For each needed vendor type, the rules pick up to 3 synthetic vendors from the directory by category, distance, review warnings and rating. Discovered businesses are displayed only, never called. Forwarded WhatsApp contacts and post-event reliability scoring are future features.

### 3. Enquiry calls (vendor's language)

Bandobast greets them on behalf of the family, asks the category's checklist one question at a time, reads the amounts back, and negotiates where allowed (the rules choose every counter-offer; at most two rounds). Busy vendors are called back when they say.

### 4. Comparison and approval (family)

The family gets, per vendor type, the ranked quotes with total cost, flags (hidden charges, large advance, over budget, review warnings) and savings, in their language. They approve one; the next-best stays on standby. Nothing is booked without approval.

### 5. Confirmation call (vendor)

Bandobast calls the approved vendor, reads back the agreed terms (date, time, price, advance), and gets a spoken yes. If the vendor changes anything, the family is asked again. Both sides get a written summary.

### 6. Advance reminder (planned)

"Advance of ₹5,000 to Shree Krishna Caterers is due by 12 Nov." Bandobast records when the family marks it paid. It never handles money.

### 7. Check-ins and the backup (vendor, then family)

Calls at D-3 and D-1 (6 pm), then on the day two hours and 45 minutes before the vendor's arrival time. The rules switch to the standby vendor if the booked one cancels, misses two check-ins in a row, gives an arrival time after the deadline, or is still unsure at the final check. The family is told at once; a warning goes out for smaller slips.

### 8. Rating (planned, after the function)

"How was Shree Krishna Caterers? On time?" A 1 to 5 rating and an on-time yes or no per vendor. These feed a reliability score that ranks vendors in later shortlists.

## Family messages (planned WhatsApp integration; not in the app demo)

All in the family's language, as text plus a voice note.

| When | Message | Interactive |
|---|---|---|
| Brief saved | Summary of the function | Edit |
| Quotes ready, per vendor type | Ranked quotes with totals, flags and savings | Approve one, see more |
| Booked | Vendor, terms, backup on standby | |
| Advance due | Amount, vendor, date | Mark paid |
| Check-in result | Only when something changed | |
| Backup switched | What happened, who's coming instead | |
| After the function | Rating request | 1 to 5, on time yes or no |

Messages outside WhatsApp's 24-hour window use approved utility templates.

## States

Function: `intake → shortlisting → enquiring → awaiting_approval → confirming → booked → done → rated`, tracked per vendor type.

Vendor within a vendor type: `shortlisted → called → quoted | declined | no_answer → approved | standby | not_chosen → confirmed → checked_in → arrived | replaced`.

## Rules (code, not the LLM)

Landed cost; quote flags; ranking (bookable, fewest warnings, cost, rating, reliability); shortlist; negotiation counter-offers and when to stop; check-in schedule by category; risk and the backup switch; reliability score from ratings; verifying that every amount was said by the vendor.

## Demo story (60 seconds)

1. Tell the real motivation: the family's caterer backed out an hour before a housewarming in another state.
2. Show a fictional family planning a griha pravesh in the app, using guided voice or the form and a manual venue pin.
3. Show the synthetic caterer C03 answering and negotiating in Kannada; show the actual quote and verified savings.
4. The family approves C03. Show booking confirmation and C02's separate standby confirmation.
5. C03 reports cancellation through “Talk to Bandobast”. Show C02's activation call and the confirmed replacement in the timeline.
6. Credit Gnani Prisma STT and Timbre TTS. Edit out waiting, not confirmations or decisions. This is a condensed video of real role-play calls, not a claim the entire run takes sixty seconds.

The in-app Demo walkthrough gives setup and role-play values. Its sample form fills only answers;
it never selects a venue, creates quotes or injects successful call results. The offline test
`tests/test_demo_walkthrough.py` rehearses the state machine with synthetic transcripts and a
fake clock; it is not a live Gnani voice test or submission video.

## Build status and order

Done: family and vendor Android app, guided voice drafting, discovery and shortlists, multilingual
enquiries and negotiation, field-bound price safeguards, family approval, primary confirmation,
confirmed standby, separately confirmed replacement activation, automatic IST follow-ups with
durable attempts, vendor updates, direct calls, focused family tabs and timeline, dashboard,
browser testing, simulated phone adapter and CI APK builds.

Still to finish or verify:

1. Run the latest APK on three phones and rehearse the full live-call story, including failed and changed-term cases.
2. Record the demo video. Review README and submission/social posts separately; those are owner-controlled deliverables.
3. Deploy HTTPS and validate real tunnel/device behavior; local tests do not prove deployment or microphone/audio quality.
4. Later: advance reminders/payment marking, family UI translations, occasion history, real account authentication, ratings/reliability scoring, WhatsApp and control-room translation.
