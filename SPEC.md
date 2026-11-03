# Bandobast: product spec

Bandobast arranges family functions in another city. The family tells it what they need, in their own language; it calls local vendors in theirs, negotiates, gets the family's approval, confirms the booking, and keeps checking until the day, switching to a backup if a vendor falls through.

Decided on 2026-11-03. Synthetic data only (contest rule): vendors are fictional, played by friends in the demo.

## Scope

| | In scope | Out of scope |
|---|---|---|
| Functions | Griha pravesh, pujas, baby ceremonies | Birthdays, weddings, parties |
| Vendors | Priest, caterer, decorator, cake, photographer, tent and sound | Venue booking, travel, return gifts |
| City | Bengaluru (Kannada, Hindi, Telugu, English vendors) | Other cities |
| Family | WhatsApp (voice and text) and the web dashboard; any of Gnani's 10 languages | Family app |
| Vendors | Phone call (Twilio, Plivo) or Bandobast Partner app (WebRTC), one pluggable channel | |
| Money | Record the agreed advance and remind the family | Collecting or sending payments |
| After booking | Confirmation call, check-ins, backup switch, post-event rating | A day-of emergency line (the check-ins and backup switch cover a last-minute failure) |

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

### 1. Intake (family, WhatsApp or dashboard)

The family sends a voice note or text in their language. Bandobast asks one thing at a time:
function and variant, family name and contact, city and area, date and muhurat, guests, food and diet rules, rituals and their language, which vendors (suggesting the defaults for the function), budget per vendor.
It reads back a summary in the family's language and saves the brief once they confirm.

### 2. Shortlist

For each needed vendor type, the rules pick up to 3 vendors from the directory: right category, not ruled out by review warnings, ranked by rating and reliability score. The family can **add a vendor** by forwarding a contact on WhatsApp; it joins the shortlist.

### 3. Enquiry calls (vendor's language)

Bandobast greets them on behalf of the family, asks the category's checklist one question at a time, reads the amounts back, and negotiates where allowed (the rules choose every counter-offer; at most two rounds). Busy vendors are called back when they say.

### 4. Comparison and approval (family)

The family gets, per vendor type, the ranked quotes with total cost, flags (hidden charges, large advance, over budget, review warnings) and savings, in their language. They approve one; the next-best stays on standby. Nothing is booked without approval.

### 5. Confirmation call (vendor)

Bandobast calls the approved vendor, reads back the agreed terms (date, time, price, advance), and gets a spoken yes. If the vendor changes anything, the family is asked again. Both sides get a written summary.

### 6. Advance reminder (family)

"Advance of ₹5,000 to Shree Krishna Caterers is due by 12 Nov." Bandobast records when the family marks it paid. It never handles money.

### 7. Check-ins and the backup (vendor, then family)

Calls at D-3 and D-1 (6 pm), then on the day two hours and 45 minutes before the vendor's arrival time. The rules switch to the standby vendor if the booked one cancels, misses two check-ins in a row, gives an arrival time after the deadline, or is still unsure at the final check. The family is told at once; a warning goes out for smaller slips.

### 8. Rating (family, after the function)

"How was Shree Krishna Caterers? On time?" A 1 to 5 rating and an on-time yes or no per vendor. These feed a reliability score that ranks vendors in later shortlists.

## Family messages (WhatsApp)

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

1. My family's caterer backed out an hour before our griha pravesh in another state.
1. Mummy sends a Hindi voice note on WhatsApp.
2. A vendor's phone rings (Bandobast Partner app); the bot asks and negotiates in Kannada; the dashboard shows the Hindi translation and the saving.
3. Mummy taps Approve on WhatsApp; a confirmation call locks it in.
4. The morning of: the caterer says they can't make it; the backup is switched in and WhatsApp says so.
5. Built on Gnani Prisma and Timbre.

## Build status and order

Done: function types and all six vendor categories, intake, enquiry calls with negotiation, quote rules, comparison, booking with backup, check-ins, risk and backup switch, dashboard, browser test calls, simulated phone calls, Twilio adapter.

To build:

1. Family language: intake and family-facing text in any of the 10 languages.
2. Shortlist rules and enquiry calls driven from the dashboard.
3. Confirmation call and the advance reminder.
4. Post-event rating and the reliability score.
5. Pluggable calling layer: `CALL_CHANNEL=twilio | plivo | app`.
6. WhatsApp family channel (Meta test number), including "add a vendor".
7. Control-room dashboard view with live transcript and translation.
8. Bandobast Partner Android app (Flutter).
