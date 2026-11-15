# Acceptance cases

These cases define when the product is ready. They were written before the code by the strategy advisor (the reviewing role in the build); the engineering agents implemented against them and could not edit, skip or weaken a case. Each case has one executable test under `tests/acceptance/`, named after its ID. `make acceptance` prints a table of ID and PASS or FAIL.

- **LIVE** cases run against a running local server and the real Valio mock API (and a real model where stated).
- Other cases may use fixtures and stubbed model responses.
- The milestone column says when a case must pass.

A milestone is done only when every case for it passes and the advisor has re-run the suite.

## A. Foundation

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| A01 | 1 | Start | One documented command starts the app. `GET /healthz` returns ok and the console loads at `/`. |
| A02 | 1 | OpenAPI | `GET /openapi.json` lists every door call, and the interactive docs page renders. |
| A03 | 1 | Public key | `GET /.well-known/jwks.json` returns a JWK Set containing the door's ES256 public key. |
| A04 | 1 | No leaked secrets | No key value appears in the git history, the built client bundle or the server logs. `.env` is gitignored. |
| A05 | 1 | Data index | The index holds 17,546 products. The running app stays under 400 MB of memory after warm-up. |

## B. Finding products

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| B01 | 1 | Open catalog | Catalog search without a mandate returns products with id, GTIN and names, and contains no price or stock fields. |
| B02 | 1 | Prices need identity | Match, offer and any price or stock call without a valid mandate is refused with 401 or 403. |
| B03 | 1 | LIVE. The brief's need | Matching {drinking milk, lactose-free, 1,000 litres, next Monday} returns between one and three options labelled best, better, good. Each lists the filters applied, including lactose-free and product type. No sour milk, coffee milk, cream or milk powder appears. |
| B04 | 1 | Stated property | Every returned product carries a manufacturer-stated lactose-free claim, and the response marks that property's source as the manufacturer. |
| B05 | 1 | Litres to packs | With fixtures: 1,000 L in 1.75 L packs gives 572 packs and 1 L over; in 10 L boxes gives 100 and 0 over; in cases of 12 x 1 L gives 84 cases and 8 L over. Packs are always whole and never fall short. |
| B06 | 1 | Ranking rule | With fixtures, options are ordered: on time first, then inside stated preferences, then least overage, then lowest price per litre. |
| B07 | 1 | Model is bounded | The product-type label is marked as inferred. With the decision model unreachable, matching still returns correct results by rules. |
| B08 | 1 | Cutoff | A product whose order cutoff has passed for the requested date is not presented as on time. |

## C. Offer, order and checks

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| C01 | 1 | Signed offer | An offer is a JWS that verifies against the JWKS and states price, quantity, delivery date and expiry. Creating offers changes no reservation. |
| C02 | 1 | Commit | An order with a valid mandate and offer, inside every limit, is committed. Its receipt verifies against the JWKS and carries the record's hash. |
| C03 | 1 | All ten checks | The record lists all ten checks with pass or fail, including when an early check fails. Two simultaneous failures are both reported. |
| C04 | 1 | Over the cap | An order above the per-order cap returns `requires_escalation` with a `continue_url`. Nothing is committed. |
| C05 | 2 | Monthly budget | When orders would cross the monthly budget, the crossing order is held. |
| C06 | 1 | Out of scope | A line outside the product scope holds the order. |
| C07 | 1 | Idempotency | Sending the same order twice with one idempotency key yields one order and the same receipt, with stock reserved once. |
| C08 | 1 | Bad mandate | An expired mandate, a mandate naming another bot, or one with a broken signature is rejected and reserves nothing. |
| C09 | 1 | Tampered offer | An offer whose price was altered after signing fails check 2 and is never committed at the altered price. |
| C10 | 2 | Reservation | After an order for N units of a finite-stock product, the available amount shown is lower by N. Rejecting or undoing the order releases it. |
| C11 | 2 | Sign-off | Approve commits and issues a receipt. Reject releases the stock. Editing the quantity re-runs the checks. |
| C12 | 2 | Expired offer in a hold | When the offer expires while an order is held, the sign-off page shows a fresh offer and its price before approval is possible. |
| C13 | 2 | The door never widens | No call lets the door raise a signed ceiling. A mandate changes only through a new version signed with the buyer's key, and the door's key never signs a mandate. |

## D. Exceptions

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| D01 | 2 | Short stock | Asking for more than a product's stock returns up to three ranked options: fill the rest with the nearest product inside preferences; part on time and the rest later; everything later. |
| D02 | 2 | Soft trade | Committing an option that trades a soft preference or is a partial fill lands in the Notify tier, with a notice. |
| D03 | 2 | Outside preferences | A substitute outside the stated preferences is held, with the reason in a plain sentence. |
| D04 | 2 | Missing data | A must-hold property the manufacturer does not state holds that line. After the human confirms it, the same product passes next time and a different product is still held. |
| D05 | 2 | Late delivery | An order that cannot meet the deadline is never committed silently. |

## E. Asking and learning

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| E01 | 2 | Question spec | A hold caused by a tradeable preference returns a question spec: what is needed, the accepted answers with their cost, and the three scope options. |
| E02 | 2 | Always | Answering "always", then confirming the one-line summary, creates a new buyer-signed mandate version. Replaying the same order passes without the question. |
| E03 | 2 | Just this once | "Just this once" changes no mandate and is written to the record. |
| E04 | 2 | Scope is exact | A preference granted for one product type does not apply to another. |
| E05 | 2 | Visible and removable | The limits view lists each learned preference with the question and answer that created it, and removing it works. |

## F. Earned autonomy

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| F01 | 3 | Synthetic history | The restaurant has 12 weekly orders, deterministic across resets, and both the API and the console label them as synthetic. |
| F02 | 3 | LIVE. The reorder | "Same as last week, plus 5 litres more milk" produces last week's basket with the milk line raised by at least 5 litres in whole packs. |
| F03 | 3 | Three zones | A departure inside the current freedom commits silently; between the freedom and the ceiling it is Notify with an undo; beyond the ceiling it is held. |
| F04 | 3 | Undo tightens | Undoing an order before its cutoff withdraws it, releases the stock, and lowers that limit's current freedom. The limits view shows the change and its reason. |
| F05 | 3 | Proposal | After a reset, act 3 opens with exactly one loosening proposal with its evidence. Accepting it raises the current freedom, never above the ceiling. |
| F06 | 3 | Nothing loosens alone | Clean approvals alone never change a current freedom. Only an accepted proposal does. |
| F07 | 3 | Creep is caught | With the usual anchored, an order growing 11% a week trips a plus or minus 30% ceiling in its third week. |

## G. Bot and console

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| G01 | 1 | LIVE. Act 1 | As the hotel chain, typing "I need 1,000 litres of lactose-free milk by Monday." ends in a committed order with a receipt, with no manual correction, in under 90 seconds. |
| G02 | 1 | Restated need | Before ordering, the bot restates the need in plain words, with quantity, property and date. |
| G03 | 2 | LIVE. Act 2 | The same request on a limited-stock product ends in a hold, then the sign-off page, then a receipt after approval. |
| G04 | 1 | Console | The console has the five views (Chat, Door's answer, Sign-off, Limits view, Record), a customer switcher, visible real and simulated labels, and a reset that restores the initial state. |
| G05 | 1 | Bot uses the door | The bot reaches the door only through its public HTTP API. It imports no door internals and touches no database. |
| G06 | 2 | Interview | Given a question spec, the bot asks in its own words and obtains a confirmed answer. |
| G07 | 3 | LIVE. Act 3 | As the restaurant, the reorder ends in a notice with an undo; the limits view shows the proposal; an undo tightens a row. |

## H. Record and standards

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| H01 | 1 | Plain-words record | The record is written in sentences and contains the request, the filters, the options, each check, the outcome and who approved. The hash chain validates, and the buyer can fetch their copy. |
| H02 | 1 | Verify in one command | `scripts/verify_receipt.py` verifies a receipt against the JWKS and fails on a tampered one. |
| H03 | 2 | Standards | `docs/standards.md` says which standards are implemented and which are mapped. A held order uses `requires_escalation` and `continue_url`. |
| H04 | 2 | Line identity | Every offer line carries a GTIN where the catalog has one, plus origin and labels marked as stated by the manufacturer. |

## I. Deploy and docs

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| I01 | 2 | Container | `docker build` succeeds. The container runs with only environment variables set, and fetches data and builds its index on first start. |
| I02 | 2 | README | The README and `docs/SETUP.md` together have the one-sentence and one-paragraph description, how to run locally, how to deploy, the environment variables, what is real and what is simulated, and the licence. (Reworded at publication, when the README was shortened and the operating detail moved to `docs/SETUP.md`.) |
| I03 | 2 | Demo protection | With `DEMO_ACCESS_CODE` set, the console and bot require it. A per-IP rate limit and a daily cap on model calls are enforced. |
| I04 | 1 | Suites | `make test` passes and `make acceptance` prints the table. |
| I05 | 2 | OpenRouter only | With `AGENT_PROVIDER=openrouter` and no Anthropic key, the bot completes act 1. |

## J. Refinements (milestone 4)

Added by the advisor at 22:40 EET on 14 Nov after reviewing the console in a browser and trialling the Verda models. The acceptance runner must be extended to pick up IDs starting with J.

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| J01 | 4 | Narrow window | Rendered in a real headless browser at 380 px wide and at 626 px wide, the Chat view shows its heading and the request box, the page has no horizontal overflow, and the header title is not covered by another element. |
| J02 | 4 | Legible text | In a real headless browser at 1440 px wide, no body text, label or table cell in the five views is rendered below 16 px. |
| J03 | 4 | Active view | Opening each of the five views marks that view, and only that view, as the active item in the navigation. |
| J04 | 4 | Theme | The console's primary colour is a blue (hue between 185 and 235 degrees; the buyer-chosen value is #005670) on white or near-white surfaces. The product wordmark uses a bundled open-licence script face. The built bundle contains no Valio or Aimo logo, wordmark or trademark asset. |
| J05 | 4 | Restaurant basket | The restaurant's usual order has between 12 and 15 lines of products that exist in the real catalog, spread over at least five product categories, deterministic across resets and labelled synthetic. |
| J06 | 4 | Two different mandates | The restaurant's per-order cap and monthly budget are both lower than the hotel chain's. |
| J07 | 4 | Budget used | The limits view's monthly budget row shows the amount used this month and the amount remaining, and both change after a committed order. |
| J08 | 4 | Short stock wording | In act 2, the hold reason states that stock is short before it states the later delivery date. |
| J09 | 4 | LIVE. Finnish request | On the configured production model, "Tarvitsen 1000 litraa laktoositonta maitoa maanantaiksi." ends in the same committed order as the English request. |
| J10 | 4 | Past date | A plan whose deadline lies in the past is never sent to the door. The bot answers with a clarifying question. |
| J11 | 4 | Silent model | When the model returns no tool call and no text, the bot asks a fixed clarifying question and places no order. |
| J12 | 4 | LIVE. Interview on the production model | On the configured production model, given a real question spec from a held order, the bot asks in its own words, and a confirmed "just this once" answer commits the order while a confirmed "always" answer creates a new buyer-signed mandate version. |
| J13 | 4 | Caddy layout | `docker-compose.caddy.yml` passes `docker compose config`, publishes no host port, and attaches the `door` service to the external network named by `PROXY_NETWORK`. `docs/SETUP.md` documents the matching Caddy site block. |
| J14 | 4 | Model cap | With `MODEL_DAILY_CAP` unset the cap is 300 model calls a day, and the call after the cap is refused with a plain message and makes no provider request. |
| J15 | 4 | Cold model | Found in the advisor's re-run: the first bot call after the model provider had been idle failed with "model service unavailable" after about 10 seconds, and later calls took half a second. Pass when: the server makes one cheap warm-up call to the model at start and at most every 5 minutes while idle; a failed or timed-out bot call is retried exactly once; and if `AGENT_FALLBACK_MODEL` is set (default `kimi-k3-1-pro` on the same provider) a second failure falls back to it. The reply states which model answered. No retry loops. |

## K. Fixes from the independent quality review (milestone 5)

Added by the advisor at 23:45 EET on 14 Nov. The acceptance runner must pick up IDs starting with K. Engineering owns K01 to K09 in `tests/acceptance/test_review_engineering.py`; the UI owns K10 and K11 in `tests/acceptance/test_review_ui.py`.

| ID | M | Case | Pass when |
| --- | --- | --- | --- |
| K01 | 5 | Property names | A need whose must-hold property is written "lactose free", "Lactose-Free", "lactose_free" or "FREE_FROM_LACTOSE" gets the same offer as "lactose-free" and the order commits; check 4 does not fail on spelling. |
| K02 | 5 | Label fallback | When the decision model answers with a credit or rate limit, or the daily model cap is reached, `/needs/match` still returns correct options by rules and never returns 429 or 5xx for that reason. |
| K03 | 5 | Cap is not a kill switch | Warm-up calls do not count against the daily model cap, and warming stops after 30 idle minutes. `/bot/chat` has its own per-address limit, so one address cannot exhaust the daily cap in a few minutes. |
| K04 | 5 | Start without history | If the restaurant's history cannot be seeded at start (the Valio API unreachable, or a staple not deliverable), the server still starts, serves acts 1 and 2, reports the missing history in `/healthz`, and seeds it later when it can. No crash loop. |
| K05 | 5 | Human actions need a human credential | Sign-off, undo, accepting or resetting a limit proposal, removing a learned preference and confirming a property require a buyer-session credential that the bot never holds. With only the mandate and bot id, each of these is refused with 401 or 403, and an order above the cap cannot be committed. The record names the session that approved. |
| K06 | 5 | No open reads | `/usual-order`, `/limits` and order details require the mandate or the buyer session. `GET /mandates/{id}` returns the payload only, never the bearer token. |
| K07 | 5 | Contrary manufacturer data | A product whose manufacturer data says it contains or may contain lactose (allergen codes as they appear in the real catalog, such as `ML`) is never treated as lactose-free, whatever its claims say, and a buyer confirmation cannot override it. Same for gluten (`AW`). |
| K08 | 5 | Cutoff has passed | When the order cutoff for the requested date has passed, the hold reason says so in plain words (not "stock is short"), the response offers the earliest date that can still be ordered, and approving a held order whose cutoff has passed leads to a fresh offer for that later date instead of a dead end. |
| K09 | 5 | Limit decisions are recorded | Accepting a proposal, resetting a freedom, removing a learned preference and registering a new mandate version each append an entry to the hash-linked record that the buyer can fetch. |
| K10 | 5 | After approval | Once a held order is approved, the Sign-off view shows it as committed and no longer shows "Why this needs you" or a "not committed" label. |
| K11 | 5 | Probabilities one click behind | The Record view can reveal the model's probabilities for the options considered, read from the record the API actually sends. |
| K12 | 5 | Idle auto-reset | With `DEMO_IDLE_RESET_MINUTES` set (default 20), when no request has reached the door for that long, the next request finds the demo in its initial state, exactly as after `POST /demo/reset`: orders, reservations, budgets used, freedoms, learned preferences and proposals. The response to that first request tells the console that an idle reset happened, so it can say so. A value of 0 disables it. A reset never happens while requests keep arriving. Engineering owns this case. |
| K13 | 5 | Access-code lockout | With `DEMO_ACCESS_CODE` set, after `DEMO_ACCESS_MAX_FAILURES` wrong codes (default 10) from one address within `DEMO_ACCESS_LOCKOUT_SECONDS` (default 600), further codes from that address get 429 with `Retry-After`, on both the unlock form and the `X-Demo-Access-Code` header, even when the code is then correct. Another address is unaffected, and a session unlocked earlier keeps working. A correct code clears the count. With `CLIENT_IP_HEADER` set, the address is read from that header only when the peer is a private or loopback proxy. |

## L. Conversational agent (milestone 6)

The reference bot must hold a conversation, not turn every message into an order. Decision by the buyer (15 Nov): the bot orders without asking only when the instruction is clear and complete. The console sends `stage: "converse"` with the message and the earlier turns in `history`; the response carries `action`, one of `reply`, `products`, `clarification`, `draft` or `order`, plus a plain-language `reply`. The earlier stages keep working unchanged.

| ID | M | Case | Pass when |
|---|---|---|---|
| L01 | 6 | Greeting or capability question | "Hi, what can you help me with?" returns `action: reply` with a short answer that says the bot can show what Aimo sells, check price and stock, and place orders inside the signed limits. No match, offer or order is created; the customer's order count is unchanged. |
| L02 | 6 | Catalogue question | "What lactose-free milks do you have?" returns `action: products` with at least two products. Each has a name, pack size, price and availability for a stated delivery date, and each satisfies the asked property by manufacturer data. No offer or order is created. A question about a kind of product that exists in the catalogue ("Do you sell oat drink?", "what cheese do you have?") also returns products, found by catalogue text search when type matching finds nothing. |
| L03 | 6 | Vague buying request | "some cheese please" and "Can I get butter?" return `action: clarification`: the bot asks for the missing quantity (and may list matching products). No order is created. |
| L04 | 6 | Clear, complete instruction | A message with buying intent, product, quantity and delivery date, such as "I need 1,000 litres of lactose-free milk by Monday.", and a reorder such as "Same as last week, plus 5 litres more milk.", return `action: order` and are placed at once, exactly as before. All earlier cases still pass. |
| L05 | 6 | Draft when the date is missing | "Order 200 litres of lactose-free milk" (no date) returns `action: draft`: the restated need with a proposed delivery date, the matched product, whole packs and total price, and a `draft` object the console can send back. Nothing is reserved and no order exists. Sending the draft back through `stage: "execute"` places exactly that order. Typing "yes, place it" with the draft in `history` does the same. Typing "no" or a change does not place it. |
| L06 | 6 | Quantities are never invented | In code, not only in the prompt: an order or draft whose quantity does not appear in the buyer's own messages (current message or `history`) is refused and becomes a clarification. |
| L07 | 6 | Memory of earlier turns | After a products answer, "I'll take 20 litres of the second one by Monday" with that answer in `history` produces an order or draft for that specific product, not the door's default best match, provided it meets the must-hold properties. |
| L08 | 6 | No match on an order | A clear order for a product type the matcher cannot find returns `action: products` or `clarification` with the nearest catalogue products and a question. No order is created. |
| L09 | 6 | Console conversation | The Chat view keeps the whole transcript across turns and sends the earlier turns. A reply shows as text; products show as a compact list with name, pack, price and availability; a draft shows as a card with the need, product, total and buttons "Place this order" and "Change something". Turns that create no order show no outcome card and do not change the Sign-off badge. The welcome text says you can ask what Aimo sells, and one suggestion is a catalogue question. Text is at least 16 px and the view works at 380 px. |

## M. Changing the mandate with strong identification (milestone 7, stretch)

Only the buyer can change a signed limit, and only after identifying themself. The identification is simulated and labelled as such; it stands for strong identification plus the right to sign for the company. No real bank or identity provider name, logo or page is imitated.

| ID | M | Case | Pass when |
|---|---|---|---|
| M01 | 7 | Identification step | With a buyer session, the buyer can start an identification for their customer and receives a short-lived (five minutes), single-use proof bound to that session. It names a fictional person, states that the person may sign for the company, and carries `simulated: true`. A bot credential cannot obtain one. |
| M02 | 7 | Change a limit | With a buyer session and a fresh identification proof, the buyer sets a new value for the per-order spend cap or the monthly budget. The door registers a new buyer-signed mandate version; the old version is superseded; `/limits` shows the new ceiling. The decision record gains an entry with the limit, the old and new values, who signed, how they were identified and when. |
| M03 | 7 | Refusals | Without a proof, with an expired or already used proof, with a proof for another customer, or with a bot credential, the change is refused (401 or 403 with a plain reason) and nothing changes. There is still no path by which the door itself raises a ceiling. |
| M04 | 7 | Effect on orders and freedom | After the hotel's per-order cap is raised, an order above the old cap and below the new one passes the spend check; after it is lowered again, the same order is held. Raising a ceiling does not raise the earned freedom for departure from usual; lowering a ceiling below the current freedom clamps the freedom to it. |
| M05 | 7 | Console flow | In the Limits view, the spend cap and monthly budget rows have "Change this limit". The flow has three steps: enter the new value; identify yourself on a screen plainly marked as a simulation; review "old value → new value" and sign. After signing, the row shows the new ceiling and links to the record entry. Cancelling at any step changes nothing. Text is at least 16 px and the flow works at 380 px. |
| M06 | 7 | Mock bank window | The identification is shown as a mock bank window: a dialog drawn like a separate small window, plainly marked as a simulation, imitating no real bank. It opens before the first sign-off approval and the first proposal acceptance in a buyer session, and before every limit change (where it replaces the inline identification step). Confirming it identifies the buyer and the action goes ahead; cancelling it leaves the action undone. After the first identification the console shows who is identified for this session and does not ask again for approvals or proposals; Reset demo and switching customer clear it. The dialog is keyboard-usable, works at 380 px and has no text under 16 px. |

## N. Fixes from the buyer's own testing (milestone 8)

| ID | M | Case | Pass when |
|---|---|---|---|
| N01 | 8 | Browsing finds the right shelf | On a catalogue that also holds look-alikes, "oat milk" and "kauramaito" return oat drinks (not dairy milk, not milk chocolate), "milk" returns dairy milk, "wine" returns wines (not wine vinegar or wine sauce), "cream" returns cream (not ice cream) and "butter" returns butter (not butter croissants or butter dishes). |
| N02 | 8 | Properties are never invented | A must-hold property that the buyer's own messages do not state is removed from the need before matching, in code. An order for a product with no stated property has no must-hold failure for a property nobody asked for. A stated property ("lactose-free", "laktoositon", "gluten-free", "organic") is kept. |
| N03 | 8 | A hold that cannot be approved says so | When the order cutoff for the requested date has passed and no later delivery date is available, the held order's question offers only rejection, and its text says plainly that the order cannot be delivered on that date and no later date is known. An approval attempt is refused with 409 and that reason; rejection works and releases any reservation. Holds that can be approved are unchanged. |
| N04 | 8 | The console shows a refusal | When the door refuses a sign-off or an answer confirmation (409 or another 4xx), the reason appears in place in plain words and the controls stay usable. For a hold that cannot be approved, the approve choice is absent and the page says the order cannot be delivered and can only be rejected or changed. |
| N05 | 8 | Limits question | "What are my limits?" and "How much can I spend per order?" are answered from the door's own `/limits` for that customer: the per-order cap, the monthly budget with what is used and left, and what the agent may do alone. The bot never says it cannot see the limits. No order is created. |
| N06 | 8 | Usual-order question | "What is in my usual order?" lists the usual basket and creates no order. "Same as last week" still places the reorder. A question is never treated as a complete instruction. |
| N07 | 8 | Ways of asking to buy | "Give me", "get me", "I'll take", "I want", "we need", "send me", a bare "2 litres of cream by Monday" and "10 packs of the third one by Friday" all count as buying intent. They lead to an order, a draft or a question about a missing detail, never to "are you asking about the catalogue". |
| N08 | 8 | Bot failures become questions | A reorder change that names no single line of the usual basket ("same as last week but without the chicken" when chicken is not one line) returns a clarification that says so. No 5xx reaches the console for a request the buyer can rephrase. |
| N09 | 8 | Undo and cancel requests | After an order turn, "undo that" or "cancel it" gets a reply that the agent cannot undo an order, and that the buyer can use "Undo this order" until the cutoff, or reject a held order in Sign-off. The bot never claims that nothing was ordered when an order turn is in the history. |
| N10 | 8 | Questions about how the door works | Questions about order cutoffs, running out of stock, substitutions, who can change limits and what is simulated are answered from the door's rules in the prompt (cutoffs are per product and checked on ordering; the door never substitutes silently, it ranks options and holds the order; only the buyer changes limits). The bot does not send the buyer to "contact Aimo". |
| N11 | 8 | Cheapest and in-stock follow-ups | After a products answer, "which is the cheapest?" names the cheapest product that is in stock by unit price for that kind of product, and lists the products with the cheapest first. |
| N12 | 8 | Product scope fit for exploring | The demo mandates allow food and drink and exclude non-food (tableware, cleaning, hygiene, office and similar). The lactose-free must-hold applies to dairy products only. So an order for coffee, bread or oat drink inside the spend limits passes the scope and must-hold checks; a dairy product without a manufacturer's lactose-free statement still needs the buyer's confirmation; a non-food order is held for scope. Acts one to three behave as before. |
| N13 | 8 | Compound names | "Ice cream" returns ice cream, while "cream" still returns cream. |
