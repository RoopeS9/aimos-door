# Status

Last verified on Sunday 15 November 2026, commit `995f5c5`.

## What is built

- **The door:** an open HTTP interface for purchasing agents: catalogue search with price and stock, need matching, signed fixed-price offers, orders with ten rule-based checks, a signed receipt and a hash-chained decision record. See [docs/API.md](docs/API.md).
- **Limits the buyer signs:** a mandate with a per-order cap, a monthly budget, a product scope (food and drink), a must-hold property for dairy and a ceiling on departure from the usual order. The door enforces them and has no path to widen one.
- **Three outcomes:** commit, commit with a notice and an undo until the cutoff, or hold for the buyer's sign-off with ranked options.
- **Earned autonomy:** corrections tighten the agent's freedom at once; the door proposes more freedom after clean approvals, and only the buyer can accept.
- **Changing a limit:** the buyer changes a signed limit after a simulated strong identification; the change is a new signed mandate version on the record.
- **A conversational reference bot:** it answers questions, lists products with price and stock, reads the buyer's limits and usual order from the door, drafts when a date is missing, and orders by itself only on a complete instruction. It uses the same public operations as any other agent.
- **A minimal example agent:** [examples/minimal_agent.py](examples/minimal_agent.py), plain HTTP, about forty lines.
- **An MCP server:** [examples/mcp_server.py](examples/mcp_server.py) exposes the agent-side operations as MCP tools over stdio. It has no tool for any buyer-only action.
- **A console** with five views: Chat, Door's answer, Sign-off, Limits view and Record.

## How it was verified

- `make acceptance`: 116 of 116 cases in [ACCEPTANCE.md](ACCEPTANCE.md) pass (sections A to O), run on a clean copy against a live local server with real catalogue data, the Valio mock API and the configured model.
- `make test`: 104 unit tests pass.
- The three demo requests were each sent eight times to the configured model and gave the same outcome every time: a committed order, a held order under limited stock, and a reorder committed with a notice.
- The chat was walked through with about sixty varied messages in English and Finnish; the problems found became cases N05 to N13.

## What is simulated

The order book, stock reservations, the restaurant's order history, the buyer's signing key, buyer sessions and the bank identification are simulated and labelled as such in the console. Prices and availability come from Valio Aimo's mock API. No purchase is sent to Valio.

## Known limits

- The bot places one product per order; a message naming several products gets a question about which to start with.
- Fixed sentences from the bot, such as product-list introductions, are in English even when the buyer writes Finnish.
- Catalogue browsing ranks by product names and categories, without a search engine; two-word searches such as "red wine" list close matches first and looser ones after.
- Demo state is shared by all visitors of one deployment; an idle reset restores it.
