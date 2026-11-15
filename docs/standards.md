# Standards: implemented and mapped

This is an application-level purchasing API. A mapping below is a design comparison, not a claim that the app implements another protocol's complete schema, transport, trust framework, or certification.

| Standard | Status | Concrete implementation or mapping |
| --- | --- | --- |
| OpenAPI | **Implemented** | FastAPI serves `/openapi.json` and interactive `/docs`. Operations are discoverable over HTTP. The emitted document declares its actual OpenAPI version. |
| JWS and JWK | **Implemented** | PyJWT/cryptography signs mandates, offers, and receipts with ES256. Door public keys are exposed as a JWK Set at `/.well-known/jwks.json`; simulated buyer public keys are separate at `/demo/buyer-jwks`. |
| GTIN | **Implemented** | The catalog's sales-unit GTIN is carried on each offer line when present. Missing identifiers remain missing; the door never invents them. |
| UCP | **Mapped** | A held order returns `status: "requires_escalation"` and `continue_url` for the buyer's sign-off page. It does not claim full UCP checkout discovery, capabilities, schemas, or lifecycle compliance. |
| AP2 open mandate | **Mapped** | The buyer's signed limits bind a named bot and an expiry, constraining future purchases. This resembles an open mandate's constrained delegation. |
| AP2 closed mandate | **Mapped** | An order binds a particular signed offer to a buyer and bot; a signed receipt links the outcome to the decision record. This is a conceptual transaction-binding comparison, not an AP2 closed-mandate implementation. |
| Peppol order response and cancellation | **Mapped** | Commit/hold/reject explain the response to an order; undo records withdrawal and releases the local reservation. No Peppol XML message or access-point exchange is sent. |

OpenAPI describes HTTP interfaces; the generated contract is the app's integration surface. [OpenAPI specification](https://spec.openapis.org/oas/latest.html).

JWS provides the signed-document format and JWK provides the public-key representation. Application payload fields such as `kind`, `record_hash`, and buyer limits are local schemas, not extra JOSE standards. [JWS, RFC 7515](https://www.rfc-editor.org/rfc/rfc7515), [JWK, RFC 7517](https://www.rfc-editor.org/rfc/rfc7517).

UCP uses escalation and a continuation URL to hand a checkout to a trusted buyer interface. This app adopts those names; local HTTP development URLs are not presented as UCP production conformance. [UCP checkout specification](https://ucp.dev/latest/specification/shopping/checkout/).

AP2 distinguishes a constrained open mandate from a mandate bound to a particular transaction. Its key-binding and credential-chain rules go beyond this demo's plain JWS mandate and named-bot checks; proof of possession, SD-JWT credentials, and AP2 receipt schemas are not implemented. [AP2 agent authorization](https://ap2-protocol.org/ap2/agent_authorization/).

Peppol Advanced Ordering defines order, order-change, cancellation, and response exchanges. A production bridge would need document transformation, identifiers, message validation, transport, and acknowledgement reconciliation before any Peppol interoperability claim. [Peppol BIS Advanced Ordering 3.0](https://docs.peppol.eu/poacc/upgrade-3/2025-Q4/profiles/65-advanced-ordering/).
