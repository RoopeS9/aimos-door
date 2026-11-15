# Design plan: purchasing console

## Subject, audience, job
- **Subject:** a purchasing console for professional kitchens buying from a Finnish food-service wholesaler. A buyer's agent places orders through Aimo's Door, and the door enforces the limits the buyer signed.
- **Audience:** hackathon judges on a projector and in a 1920×1080 screen recording, and people opening a public link on a laptop or phone.
- **Job:** show at a glance what the agent was allowed to do, what it did, and what needs a human.

## Palette
Blue on white with a Valio-like feel, as the owner asked. There is one warm accent, and it is used only for holds and warnings.

| Name | Hex | Role |
| --- | --- | --- |
| Snow | `#FFFFFF` | Reading surfaces: panels, the request box, the sidebar |
| Frost | `#EEF4FB` | Page background and quiet sections |
| Glacier | `#CBDAEC` | Rules and borders |
| Dairy blue | `#0B4A9E` | Primary: buttons, active navigation, earned freedom (hue 214°) |
| Ink | `#0E2747` | Text. Secondary text uses Slate `#4A5F7C` (6.3:1 on white) |
| Amber | `#A04E00` on `#FFF1DE` | Only holds, warnings, simulated data and the "needs you" state |

## Type
- **Space Grotesk:** headings and every figure that matters (prices, limits, percentages). Its squared numerals read like instrument readouts, which suits a page about limits.
- **Inter:** body text, labels and controls.
- **Scale:** 16, 18, 22, 30 and 44 px, with 16 px as the floor (acceptance case J02).
- Both faces are already bundled from `@fontsource-variable` under the OFL.
- **No all-caps labels:** sentence case everywhere. The type hierarchy replaces eyebrow labels.

## Layout
The shell stays as it is: navigation, a working column, and a narrow column with the signed limits. What changes is the hierarchy inside it.

```
+-----------+---------------------------------------+-------------------+
| Aimo's    | Page title (what this view is)        | What your agent   |
| Door      | One sentence: what you can do here    | may do            |
|           |                                       | [=====|....] cap  |
| Buying    | +- outcome: deep-blue or amber rule --+ | [=====|....] month|
| for [v]   | | Order committed / Needs your sign-off | Never traded away |
|           | +---------------------------------------+ | Real / simulated  |
| Chat      | panels: white, 1 px rule, 6 px radius |                   |
| Answer    | details and signed copies: flat rows   |                   |
| Sign-off  |                                       |                   |
| Limits    |                                       |                   |
| Record    |                                       |                   |
+-----------+---------------------------------------+-------------------+
```

- **Alignment:** everything is left aligned. Figures are right aligned in columns, with tabular numerals.
- **Sidebar:** white, not navy. The page is blue on white, and the sidebar is a quiet index. The active view is a filled Dairy-blue item, so the selected state carries the colour.
- **Cards by rank:**
  - Decisions (outcome, held reasons, proposals): white panels with a 4 px left rule in blue or amber.
  - Content (options, lines, checks): white panels with a 1 px Glacier border.
  - Supporting material (signed documents, record integrity): flat rows on Frost, with no box.
- **Numbering:** none on the views, which are not a sequence. The ten checks keep their numbers because each check has a fixed identity.

## The memorable element: the discretion bar
The agent is treated like a new hire. It starts with no discretion, earns some, and loses it when corrected. The limits view shows this as a bar for each limit:

```
Departure from usual
[███████████░░░░░░░░░░░░░░░░░░░░░░]|  hold beyond
 acts alone 10%   with a notice up to 30% (signed ceiling)
```

- **Solid Dairy blue:** current freedom, where the agent acts alone.
- **Hatched light blue:** the rest of the signed ceiling, where the agent acts and you get a notice with an undo.
- **Hard end mark:** the signed ceiling. Beyond it, the order holds for your sign-off.
- **The legend is the key:** the Commit, Notify and Hold explainer becomes the bar's legend, so the vocabulary is learned once and reused.
- **Why this one:** it is the one picture in the product that holds the whole idea, signed ceiling against earned freedom. A projector audience reads it in a second, and when undo resets the freedom to zero it visibly empties.

## Principles
1. **Say what it is, then what to do.** Every view opens with a plain title and one sentence on what the buyer can do there. No slogans.
2. **Colour means state.** Blue is permission and action; amber is "needs you" or "simulated". Nothing is coloured for decoration.
3. **Rank decides weight.** Decisions get a coloured rule, content gets a hairline border, and evidence gets no box.
4. **One name per action.** "Approve order" leads to the toast "Order approved"; "Undo this order" leads to "Order undone".
5. **Quiet motion.** Motion appears only in answer to an action (opening a detail, a toast), and none at all under reduced motion.

## Review against the brief
- **First draft:** a navy sidebar, a gradient hero number for the spend cap, and a card for every block. That is the SaaS-card kit with a dark rail, a default for any dashboard. I kept the white sidebar and gave the colour to the selected state and to the discretion bar instead.
- **Rejected:** a big-number hero on the chat view ("€2,000 cap"). The job of the chat view is the request, so the hero stays the request box with a plain question above it.
- **Kept on purpose:** Space Grotesk and Inter. They are bundled and licensed, and Space Grotesk's numerals do real work in the bar and price columns. Swapping faces would add risk under a one-hour box without changing the reading.
- **Amber kept narrow:** used only where the owner's brief allows it (holds and warnings), plus the simulated-data labels, which are warnings about the data.
