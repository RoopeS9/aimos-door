# Assumptions and production boundary

The current application is a Junction demonstration. [STATUS.md](../STATUS.md) records tested milestones and known gaps. No real order is placed with Aimo by this app.

## Data and quantities

- The event catalog and the price/availability API are the available upstream sources. There is no upstream search, order, reserve, cancel, or history endpoint.
- The manufacturer's classifications establish must-hold claims. Product-type inference cannot establish such claims. Missing evidence requires an explicit buyer confirmation for that product/property. Real allergen IDs `ML` (milk/lactose) and `AW` (cereals containing gluten) with `CONTAINS` or `MAY_CONTAIN` override any conflicting free-from claim and cannot be overridden by a buyer confirmation.
- Demo mandates are deliberately broad enough to explore food and drink, using category prefixes rather than a milk-only allowlist, while non-food remains out of scope. Their standing lactose-free requirement applies only to dairy families, not to plant drinks merely sharing a higher-level category with milk. Buyer-stated request properties remain order-specific requirements on any category. Older mandates without `must_hold_categories` retain their former global must-hold behavior; production scopes and property requirements must be reviewed and signed by each buyer.
- Comparison prices are rounded. Pack conversion therefore uses catalog price divided by comparison price, reconciled with name volume and the sales-unit hierarchy when they corroborate the ratio. The conversion source is returned for review.
- Prices and signed spending limits use EUR excluding VAT. Production must settle tax, deposits, delivery charges, and rounding rules explicitly.
- A terminal-stock amount is supplier-dependent, not a promise of physical stock. Lines retain the supplier-confirmation notice. Empty schedules and expired cutoffs cannot become silent on-time commitments.
- When an upstream cutoff is absent, the current demo uses a four-hour local reservation deadline. Production needs an authoritative cutoff, time zone, holiday calendar, and reservation-expiry policy. A passed cutoff yields the earliest open-cutoff delivery date when one is confirmed and a fresh offer for buyer confirmation; it is not reported as short stock.

## What Aimo's back end must provide

| Capability | Needed production contract |
| --- | --- |
| Customer identity and prices | Authenticated account binding and customer-specific price entitlement; the mock ignores its customer field. |
| Product evidence | Versioned manufacturer claims, pack units, GTINs, availability of substitutions, and correction provenance. |
| Offers | Authoritative quote identifier, price validity, tax/charge basis, and delivery promise. |
| Reservations | Atomic quantity reservation/release, expiry, stock reconciliation, and supplier confirmation. |
| Orders | Idempotent submission, query by client key, durable acknowledgement, and recovery after unknown network outcomes. |
| Changes and cancellation | Cutoffs, allowable amendments, cancellation acknowledgement, and downstream fulfilment reconciliation. |
| History | Buyer-authorized order history plus explicit human confirmation of a usual-order anchor. |
| Records | Shared identifiers, event delivery, retention/export rules, and a mutually verifiable audit trail. |

## Trust and operation

Buyer keys in this demo are separately generated but held by the same local application. `POST /demo/buyer-session` issues an expiring, explicitly simulated buyer-login token; human mutations require it in `X-Buyer-Session`, while bot reads use mandate plus bot ID. Production requires buyer-controlled signing and real authenticated buyer login, registered bot identity, key rotation/revocation, tenant isolation, and authorization on private records. Neither the demo access code nor the simulated buyer session is a production identity system.

SQLite and the named data volume are intended for one application instance. Preserve that volume to keep ledger history and signing identity. The catalog carries an index schema/rules version, so a rules change rebuilds legacy indexes. If a restaurant history quote fails at startup, the app remains available, reports that history as unavailable in `/healthz`, and retries on a later restaurant read; production should make this degradation visible to operators. A production multi-instance service needs coordinated reservations, idempotency, model admission, rate limits, and reset/admin controls.

The supplied Compose deployment trusts forwarded headers only from local/private proxy peers. The origin must be reached through that trusted proxy path. `PUBLIC_BASE_URL` defines the externally visible sign-off origin; `DOOR_API_BASE_URL` remains loopback for the reference bot.

The latest provider decision selects Verda DeepSeek Flash for the reference bot through `AGENT_PROVIDER=openai_compatible`, with base `https://inference.verda.com/v1`, private `AGENT_API_KEY`, and model `deepseek-4-1-flash-1017`. Cold-model handling makes one cheap warm-up at startup and at most once per five idle minutes, stops after 30 idle minutes, and resumes on real bot activity. After a failed or timed-out visitor call it permits exactly one primary retry and then one Kimi (`kimi-k3-1-pro`) fallback through the same provider. There is no retry loop, and response metadata names the answering model. Anthropic and the dedicated OpenRouter provider remain supported. An OpenRouter-plus-Valio deployment needs no Verda or Anthropic key, but its model override must be an OpenRouter slug rather than a Verda model ID.

Each bot inference request has `max_tokens=800`. The default persisted admission cap is 300 visitor model calls per UTC day; warm-ups are excluded, while visitor retries and fallbacks count. `/bot/chat` also has an independently configurable per-address limit (20 per minute by default). The prompt supplies the next 14 dates with English and Finnish weekdays. Past deadlines and empty responses without tool calls require clarification before a purchasing call. Live verification for the selected provider is recorded separately in `STATUS.md`.

Product-type labels still use the optional OpenRouter decision model and are cached; normal decision-call failures use deterministic rules. Paid usage and credit limits are surfaced, never silently replaced with a claimed live-model result.
