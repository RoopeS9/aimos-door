# Public HTTP API

Base for local development: `http://127.0.0.1:8000`. External deployment example: `https://door.example.com`. All request and response bodies use JSON. `/openapi.json` lists operations and `/docs` provides interactive documentation. Check [STATUS.md](../STATUS.md) for implementation and acceptance status.

## Identity and documents

Purchasing bodies contain `mandate` (the buyer-signed JWS string) and `bot_id`. The seeded demo bot is `reference-bot`; customer IDs are `hotel` and `restaurant`. A valid signature, unexpired mandate, registered mandate version, and matching bot identity are required. The demo exposes buyer-signed mandates through `/demo/customers`; production registration needs independent buyer identity and key custody.

Seeded mandates allow the catalogue's food-and-drink category families and exclude non-food such as tableware, cleaning, hygiene and office supplies. `allowed_categories` contains category **prefixes**: a product matches when its complete category code starts with any entry, so an existing full code still matches itself. The demo's standing lactose-free must-hold is restricted by `must_hold_categories` to dairy category prefixes. It does not apply automatically to coffee, bread, plant drinks or other non-dairy lines. A property explicitly present in the request's `need.properties` still applies to that order regardless of category. For backward compatibility, a mandate with `must_hold` but no `must_hold_categories` applies that must-hold to every line. `allowed_categories_label` and `must_hold_label` describe the seeded scopes in words; new buyer-signed versions preserve the category fields.

Mandates are signed with the buyer key. Offers and receipts are signed with the door key. The door never signs a mandate with its own key. Fetch the door's ES256 public JWK Set from `/.well-known/jwks.json`; simulated buyer public keys are at `/demo/buyer-jwks`.

| Operation | Purpose |
| --- | --- |
| `GET /healthz` | Status, product count, fixture flag, restaurant-history availability, warm-up state, and measured process memory. |
| `GET /openapi.json` | HTTP operation description. |
| `GET /docs` | Interactive API documentation. |
| `GET /.well-known/jwks.json` | Public door verification key set. |
| `GET /demo/buyer-jwks` | Public simulated buyer verification keys. |
| `POST /mandates` | Human-only registration of `{ "mandate": "<buyer-signed JWS>" }` with `X-Buyer-Session` for its customer. Unsigned payloads and door-signed mandates are refused. |
| `GET /mandates/{mandate_id}` | `{ "payload": <decoded mandate payload> }` only; never the signed bearer token. |
| `POST /demo/buyer-session` | Body `{ "customer_id": "hotel" }`; returns an expiring token, public session ID, and explicit simulated-login metadata. |
| `POST /demo/identify` | Body `{ "customer_id": "hotel", "method": "bank" }` plus that customer's `X-Buyer-Session`; returns an opaque, five-minute, single-use simulated identification proof bound to the exact session and customer. |

The buyer session is a **simulated login**. Sign-off `approve`, `reject`, `edit` and `acknowledge`, exact property confirmation, undo, proposal/freedom/preference mutations, mandate registration, signed-limit change, and demo reset require `X-Buyer-Session: <token>` for the resource's customer. Missing/invalid sessions return 401; a different customer's session returns 403. Answer preparation alone takes no standing effect. For reads of usual orders, limits, and order details, either that header or `Authorization: Bearer <mandate>` with `X-Bot-Id: <bot-id>` is accepted. The bot never mints or persists buyer sessions. At the interview's final `stage: "confirm"`, `/bot/chat` accepts the human header and forwards it only for that confirmed sign-off step; decision records use only the public session ID.

The identification step is also **entirely simulated**: it contacts no bank or identity provider and imitates none. Its response contains `proof`, `expires_at`, `simulated: true`, `method: "Simulated bank identification"`, a clearly fictional `person.name`, and an `authority` sentence stating the person's simulated right to sign for the company. Only the proof's one-way digest indexes its server-side record. A mandate/bot credential cannot substitute for the buyer session. Identification failures on the limit-change call return 403 with a plain `detail`; 401 is reserved for a missing or invalid buyer session.

## Catalog and matching

`GET /catalog/search` is public and returns no prices or stock by default. Query parameters: `text`, `category`, repeated `properties`, `storage_class`, and `limit` (1–100). Response: `{ "products": [...] }`, including `id`, `gtin`, multilingual `names`, manufacturer properties, and inferred product type.

For conversational browsing, add `customer_id=hotel|restaurant` and `delivery_date=YYYY-MM-DD`. This read-only variant uses the same cached Valio price/availability client as matching and returns at most six products, most relevant first. Product types such as milk use type candidates first; kinds not distinguished by type inference (including cheese and oat drink) fall back to product-name text search. Asked properties are retained only where manufacturer data states them. The response adds `delivery_date`, and each compact product contains `product_id`, `name`, `pack`, numeric ex-VAT `price_eur` (or null), display `unit_price`, `availability` (`in_stock`, `limited`, `unavailable`, or `unknown`), `available_text`, and a list of manufacturer-stated `properties`. Browsing creates no match option, offer, reservation, or order. Omitting `delivery_date` preserves the original catalogue-only response.

`POST /needs/match` requires a valid mandate:

```json
{
  "mandate": "<buyer-signed JWS>",
  "bot_id": "reference-bot",
  "need": {
    "product_type": "drinking milk",
    "properties": ["lactose-free"],
    "quantity": 1000,
    "unit": "L",
    "deadline": "2026-11-16",
    "preferences": {},
    "request": "I need 1,000 litres of lactose-free milk by Monday."
  }
}
```

Use an actual ISO delivery deadline; the date above illustrates the event's Monday. Units include `L`, `KG`, and `PACK`. Optional `product_ids` narrows an exact-product request. `product_id` selects one buyer-chosen product: matching uses that product's actual catalogue type, puts its option first when its must-hold properties hold, and returns a `product_selection` explanation with any missing properties instead of silently substituting the default best match. Preferences can contain `required` and `soft` maps (such as `brand` or `pack_size`); request fields cannot override buyer-signed preferences.

A restaurant reorder first reads `GET /usual-order?customer_id=restaurant`. The frozen synthetic history contains 13 distinct real-catalog staples across 13 categories, including exactly one drinking-milk line; Reset restores this same basket and anchor. Submit a need with `product_type: "usual basket"`, that response's `anchor_id`, the new `deadline`, and `lines` containing each exact `product_id`, `product_type`, `quantity`, `unit`, and required `properties`. Unchanged staples use their existing whole-pack quantity with `unit: "PACK"`; an increased milk line may use litres and is rounded up by the door. Basket-level properties apply to every line, so use `properties: []` when requirements differ by line. The restaurant uses the same food-and-drink scope and dairy-only standing must-hold as the hotel, plus its explicit drinking-milk requirement. Matching obtains current prices and availability; historical quote values never authorize a purchase.

The response has `need`, `filters_applied`, and up to three `options` labelled `best`, `better`, `good`. Ranking uses on-time delivery, preference compliance, least overage, then lowest price per requested unit. Each option carries its server-issued `option_id`, lines, total, delivery date, overage, and trade/partial flags. Each line reports whole `packs`, `pack_size`, delivered `quantity`, `unit`, `unit_price`, `total`, available packs, cutoff, GTIN, manufacturer evidence, and conversion source. Type labels are marked `product_type_source: "inferred"`. When a requested cutoff has passed, `earliest_orderable_date` identifies the earliest confirmed open-cutoff delivery and the hold reason names the cutoff instead of claiming short stock.

## Offer and order

| Operation | Request | Response or effect |
| --- | --- | --- |
| `POST /offers` | Identity plus `option` returned by matching, or `option_id`. | `{ "offer": "<JWS>", "payload": {...} }`; no stock reserved. |
| `POST /orders` | Identity plus `offer` JWS. Required header `Idempotency-Key`. | Order, ten checks, receipt if committed, record identifier, and any escalation URL. |
| `GET /orders/{order_id}` | Order identifier plus buyer-session or mandate/bot read headers. | Current order state and signed-document references. |
| `POST /orders/{order_id}/signoff` | Actions `approve`, `reject`, `edit`, `prepare_answer`, `acknowledge`; the human header is required except for answer preparation alone. | Approval issues a receipt; rejection releases reservation; edit reruns all checks; acknowledgment records an explicit review of a committed order. |
| `POST /orders/{order_id}/undo` | Empty JSON object is sufficient. | Withdraws a committed order before its cutoff and releases reservation. |
| `GET /records/{record_id}` | Record identifier. | Buyer's copy of the readable decision record and hash-chain fields. |

Offers use server-stored options, not caller-edited price fields. They state EUR, the VAT basis, delivery, availability, and expiry; the default validity is five minutes. Valid order requests reserve available stock even if held. Invalid mandates reserve nothing. A repeated buyer/idempotency-key pair returns the existing order and receipt rather than creating another order. When an option replenishes short stock after the deadline, the delivery hold reason says that stock is short before naming the actual later delivery date; an ordinary late but fully stocked single-product offer is not labelled as a stock shortage.

Order statuses are `committed`, `requires_escalation`, `rejected`, and `withdrawn`. Tiers are `Commit`, `Notify`, and `Hold`. A held order has `continue_url` pointing to the sign-off page. Check results contain `id` 1–10, `name`, `passed`, and a plain-language `reason`. Every check runs even when an earlier one fails. Product scope uses the signed category prefixes above; Must-hold properties evaluates the standing property only on lines matching `must_hold_categories`, while retaining request-stated properties on every line. Check reasons are reused in the buyer-readable record sentences. A terminal cutoff hold includes `approvable: false`; the field may be absent when approval is allowed.

Example held response shape:

```json
{
  "id": "<order-id>",
  "status": "requires_escalation",
  "tier": "Hold",
  "continue_url": "https://door.example.com/?view=signoff&order=<order-id>",
  "checks": [{"id": 6, "name": "Per-order cap", "passed": false, "reason": "The order exceeds the signed cap."}],
  "receipt": null,
  "record_id": "<record-id>"
}
```

The example abbreviates `checks`; actual records contain all ten. A signature or buyer-binding failure cannot be overridden by sign-off. A physically unreserved quantity also cannot be committed by clicking approve; choose an available alternative or edit the quantity.

Quantity editing uses `{ "action": "edit", "quantity": 10 }` and preserves the order/idempotency identity while repricing, rechecking and atomically replacing its reservation. An expired held offer cannot be approved: `GET /orders/{id}` refreshes it and returns `requires_price_confirmation: true`. If the held order's cutoff has passed, reading it or attempting approval produces a fresh signed offer for the earliest later date that is fully available with an open cutoff, when one exists. Show the new total and delivery date, obtain confirmation, then send `{ "action": "approve", "offer": "<exact fresh JWS>" }`. A boolean alone does not acknowledge a particular price or date.

If the requested date's cutoff has passed and no later fully available date has an open cutoff, `GET /orders/{id}` instead returns the still-held order with `approvable: false`. Its `question_spec.needed` says that the order cannot be delivered on the requested date, no later delivery date is known, and it can only be rejected or replaced by a new request. `question_spec.answers` contains only `reject`; the scope list remains unchanged for contract compatibility. An approval attempt returns 409 with that same plain `detail.message` and `detail.earliest_orderable_date: null`. It does not create another offer. Rejection still works and releases all of the order's reservations. To purchase after this terminal hold, submit a new request.

For a usual basket, edit `quantity` is a basket count: `1` keeps one requested basket, `0.5` halves each line, and `2` doubles it, with each line rounded up independently to whole sales packs. The door never adds quantities expressed in different units. A refresh without a quantity edit preserves every pack count.

A tradeable hold includes `question_spec` with `needed`, accepted `answers` and price/date costs, `scope_options`, and its `hash`. Prepare a human answer with `{ "action": "prepare_answer", "answer": "approve", "scope": "always" }`. Display the returned `pending_answer.confirmation_summary`; after confirmation submit the answer, scope, `confirmed: true`, exact `confirmation_summary`, and `question_hash`. The scopes are `just_this_once`, `always`, and `always_ask`. Standing answers create a new buyer-signed mandate containing exact product-type/preference/value entries; signed ceilings are preserved. Superseded mandate tokens cannot authorize new requests. Remove an entry via `POST /limits/{entry_id}/reset` with its `customer_id`.

Missing manufacturer data requires explicit `confirmations: [{ "product_id": "<displayed product>", "property": "lactose-free", "confirmed": true }]` with `confirmed: true` on sign-off. This applies only to that buyer, product and property. Manufacturer statements to the contrary cannot be overridden by this confirmation. Past orders never substitute for explicit evidence.

## Reference bot

The selected bot configuration uses Verda through `AGENT_PROVIDER=openai_compatible`, `AGENT_BASE_URL=https://inference.verda.com/v1`, private `AGENT_API_KEY`, and `AGENT_MODEL=deepseek-4-1-flash-1017`. Startup/idle warm-up uses a single inexpensive request at most every five minutes, stops after 30 idle minutes, and resumes on real bot activity. Warm-ups do not consume the visitor model admission cap. A failed or timed-out visitor inference gets one primary retry, then one request to `AGENT_FALLBACK_MODEL` (`kimi-k3-1-pro` by default on the generic provider), with no retry loop. The `model` response object includes `answered_by`, the attempt count, and fallback use. The existing Anthropic and OpenRouter adapters remain supported; the purchasing HTTP sequence below is independent of the model provider. Visitor inference is capped at 800 output tokens per request and 300 admitted model calls per UTC day by default; each real attempt, retry, and fallback consumes one admission. `/bot/chat` separately admits 20 calls per address per minute by default (`BOT_CHAT_RATE_LIMIT`, `BOT_CHAT_RATE_WINDOW_SECONDS`). English and Finnish requests use an explicit 14-day weekday/date table. Past deadlines or empty replies without a tool call trigger clarification before matching or ordering.

`POST /bot/chat` accepts:

```json
{
  "customer_id": "hotel",
  "message": "I need 1,000 litres of lactose-free milk by Monday.",
  "stage": "all",
  "simulate_limited_stock": false,
  "idempotency_key": "<stable-key-for-this-request>"
}
```

`stage: "preview"` calls the configured model and returns a validated `need` and plain-language `restatement`. The console displays that statement before sending `stage: "execute"` with the returned `need`, original `message`, and `restatement`. Execution does not call the model again. `stage: "all"` (default) performs both and returns ordered events.

### Conversation stage

`stage: "converse"` supports multi-turn buying without changing the legacy stages. It makes one model inference call per turn and accepts exactly one model tool choice: `reply`, `search_catalog`, `ask`, `draft_order`, or `place_order`. Send:

```json
{
  "stage": "converse",
  "customer_id": "hotel",
  "message": "What lactose-free milks do you have?",
  "history": [
    {"role": "user", "text": "Hi"},
    {"role": "assistant", "text": "How can I help?", "action": "reply", "products": [], "draft": null}
  ],
  "simulate_limited_stock": false,
  "idempotency_key": "<stable-key-when-an-order-is-possible>"
}
```

`history` is oldest first and limited to the last 12 turns. Assistant turns can retain their `action`, up to six `products` (only `product_id` and `name` are needed for later references), and `draft`. Products history resolves “the second one”; draft history resolves “yes, place it”. The current request and response language can be English or Finnish, and the explicit 14-day bilingual date table applies.

Every successful response has `stage: "conversation"`, one `action` (`reply`, `products`, `clarification`, `draft`, or `order`), plain-language `reply`, and `model` metadata:

- `reply` answers small talk or capabilities without calling the purchasing API.
- `products` includes `delivery_date` and at most six compact products in the enriched catalogue shape above.
- `clarification` asks for missing information and can include the same `delivery_date` and `products` fields. A matcher miss never turns into a substitute purchase; the bot shows nearby catalogue products and asks which to use.
- `draft` includes `{ "need", "restatement", "product_name", "pack", "packs", "total_eur", "delivery_date" }`. The bot obtains a current match to calculate whole packs and total, but creates no offer, reservation, or order. Send the draft's `need` and `restatement` through the existing `stage: "execute"` request to place it, or confirm it conversationally with the draft in history. “No” or a change never confirms it.
- `order` includes the complete legacy result fields: `need`, `restatement`, `match`, `offer`, `order`, `messages`, and `idempotency_key`.

Ordering is also enforced in code rather than entrusted to the prompt. Only the current message and history's **user** turns can establish buying intent, product, quantity, required properties, and delivery date. A model-authored quantity absent from those buyer turns becomes a clarification; an otherwise complete order with no buyer-stated date becomes a draft with a proposed date. A model-authored property absent from those buyer turns is silently removed before matching and before the restatement is built. Property comparison uses catalogue-style canonical IDs and spelling variants in English and Finnish, including `lactose-free`/`lactose free`/`laktoositon`, `gluten-free`/`gluteeniton`, and `organic`/`luomu`. The same filter protects legacy preview/execute execution. Confirming an existing draft is the exception for its proposed date and earlier buyer-stated quantity. Confirmed usual-basket reorders keep their existing server-side expansion and validation, including each basket line's own confirmed properties.

The completed response contains `restatement`, `need`, `match`, `offer`, `order`, `messages`, and the idempotency key. Each event has a role, kind, text, and timestamp. With no matching options, `order` is null and the bot states that no order was placed. On an uncertain network outcome, retry with the same idempotency key before creating a new request.

The portable bot calls only public HTTP operations. A simpler bot can provide the structured need directly and relay the resulting checks and continuation URL, without using this model adapter.

For an interview, send `stage: "interview"`, `customer_id`, and `order_id`. The bot fetches the server's current question using its mandate/bot read headers and asks in its own words. Send `stage: "answer"` with the human's `message` and chosen `scope`; the order remains held and a confirmation summary is returned. Finally send `stage: "confirm"`, `confirmed: true`, the exact returned `confirmation_summary` and `question_hash`, and the human's `X-Buyer-Session` header. The route forwards that header only for this one confirmed sign-off; it is not stored on the bot. Altered or stale summaries are refused. The bot cannot replace the door's accepted answers or broaden their scopes. For an order with `approvable: false`, the interview presents only rejection and never prepares or confirms an approval.

## Limits and demo operations

| Operation | Purpose |
| --- | --- |
| `GET /demo/customers` | Demo IDs, names, bot IDs, signed mandates, real/simulated labels, and the worded `product_scope_label` / `must_hold_label` fields. |
| `POST /demo/reset` | Restore initial demo state, including the frozen restaurant seed and one pending proposal; retain catalog, label cache, and signing keys. |
| `GET /usual-order?customer_id=restaurant` | Confirmed usual basket, anchor identifier, and 12 explicitly synthetic weekly orders. |
| `GET /limits?customer_id=hotel` | Signed ceiling/current freedom rows, reasons, learned preferences, and proposals. The monthly-budget row includes numeric current-month `used` and `remaining` amounts in EUR. Top-level `product_scope_label` and `must_hold_label` summarize the mandate scope without enumerating category codes. |
| `POST /limits/proposals/{proposal_id}/accept` | Explicit human acceptance with `{ "customer_id": "restaurant", "confirmed": true }`; a pending proposal is required. |
| `POST /limits/{limit_id}/change` | Change `per_order_cap` or `monthly_budget` after simulated strong identification. Requires `X-Buyer-Session` and `X-Identification-Proof`, with body `{ "customer_id": "hotel", "value": 3000 }`. |
| `POST /limits/{limit_id}/reset` | Tighten current freedom or remove a learned preference, with the buyer's `customer_id`. |

A successful limit change creates and registers a strictly newer buyer-signed mandate version, supersedes its predecessor for subsequent calls, and returns `limits` in exactly the same shape as the updated `GET /limits` response, the integer `mandate_version`, and `record_id`. All other signed limits, including `must_hold_categories` and the worded scope labels, carry forward unchanged. Its `change` object reports `limit_id`, `name`, `old_value`, `new_value`, `signed_by`, `identified_by`, and the ISO timestamp `at`. The linked buyer-readable decision record has the same record/hash-chain shape as other decisions and states the limit, both values, signer, identification method and time. `/demo/customers` exposes the new active mandate immediately; Reset restores the original values and clears unused proofs.

The proof is consumed only on success and cannot be replayed. Missing, unknown, expired, used, wrong-session and wrong-customer proofs all return 403 without any mutation. A non-positive or non-finite value, a non-numeric value, or a value equal to the current ceiling returns 422 without mutation. `departure` is not directly changeable here: its earned freedom changes by the proposal mechanism, so this route refuses it with a plain reason. Unknown limit IDs return 404. Raising a spend ceiling does not increase earned departure freedom; displayed current freedom remains clamped to its signed ceiling.

The restaurant starts at 10% current freedom under a 30% buyer-signed departure ceiling. Its signed caps are EUR 400 per order and EUR 6,000 per month, below the hotel's EUR 2,000 and EUR 20,000. Monthly usage counts committed current-month orders through this door and uses the same total as budget enforcement. Departure is measured for each product against the fixed confirmed pack count, including added or missing lines. Within freedom the order commits silently; above freedom but within the ceiling it commits with a notice that explicitly says "approval by silence" and offers undo until cutoff. Exceeding the ceiling holds the whole order. The hotel has no usual-order history, so this comparison does not apply.

Undo or editing tightens that kind of order automatically and makes the agent ask next time; neither operation changes the signed ceiling. Four clean human approvals can support a proposal but never increase freedom automatically. The initial proposal is explicitly backed by four synthetic approvals. Accepting a proposal requires a human action, stays below the current signed ceiling, and removes the pending proposal. Clean orders never move the usual anchor. Proposal acceptance, freedom reset, learned-preference removal, and new mandate registration each append a `buyer_copy` decision to the same per-customer hash chain and return its `record_id` and `record_hash`.

To explicitly review a committed order, send `action: "acknowledge"`, its `customer_id`, and `confirmed: true` to sign-off. This records clean human approval once per order without replacing its original receipt or reservation. At the signed freedom ceiling, four clean reviews of the same changed basket may instead propose a new usual anchor. The proposal exposes the exact proposed lines; only confirmed acceptance moves that anchor. Reset restores the original synthetic seed.

## Errors and verification

Missing purchasing authority returns 401; invalid/expired/mismatched or superseded authority returns 403. The same 401/403 rule applies respectively to missing and wrong-customer buyer sessions. Invalid arguments return 400 or 422. Invalid sign-off state or stale confirmation returns 409; a terminal no-later-date approval also returns 409 with the order's plain reject-only reason, while a passed cutoff with confirmed later supply instead gives a fresh offer to confirm. External service failures return a safe 502/503 error rather than synthetic live data. Usage or credit limits return 429 with a blocked reason, except that product-type labelling falls back to rules and still matches. The optional demo gate accepts `X-Demo-Access-Code`; the root login form posts to `/demo/unlock` and receives an opaque HTTP-only session cookie. The default per-IP limit is 120 requests per minute (`DEMO_RATE_LIMIT`); health and static assets are exempt. Authenticated internal bot-to-door calls do not consume the visitor general bucket, while `/bot/chat` has its own separate bucket. Forwarded client addresses are trusted only from local/private proxy peers, and Compose binds the origin to host loopback for the reverse proxy.

The receipt contains `record_id` and `record_hash`. A record includes `previous_hash`, connecting it to the previous record for the same buyer. Signature verification and record-chain verification are separate checks: a valid signature proves the signed bytes and key, not upstream stock truth.

```sh
uv run python scripts/verify_receipt.py receipt.json --jwks http://127.0.0.1:8000/.well-known/jwks.json
```
