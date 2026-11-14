# Mobile demo script — Brindle (the reviewed app)

Brindle is a fictional bank made for this demo. It has five screens behind a bottom tab bar:
**Home**, **Cards**, **Send** (the round middle button), **Activity**, **Profile**.

Open it on the phone at `http://<laptop>:8787/` (served by the hub) or `http://<laptop>:4848/` (Vite dev).

## Planted bugs

Each one is visible on a phone without typing more than a few characters. Rotate twice, say the line while tapping or circling the thing you mean, rotate once.

To circle something: rest a finger next to it for a moment (about a quarter of a second), then draw a loop around it. A green stroke follows the finger and the picked elements light up on release. A drag that starts moving straight away just scrolls.

| # | Screen | What to show | What to say |
|---|---|---|---|
| 1 | Home | The big balance reads **$17,385.75**, but the three accounts right under it are $4,280.55 + $12,940.00 + $615.20 = **$17,835.75**. | "The total balance doesn't add up. The three accounts sum to seventeen thousand eight hundred thirty-five, but the total shows seventeen thousand three hundred eighty-five." |
| 2 | Home → Savings goal (just below Accounts) | The **Add money** button sits on top of the goal text; you can't read "of $1,500.00" or the end of the sentence. | "The Add money button overlaps the savings goal text. I can't read the goal amount." |
| 3 | Cards → Security | Tap **Block card**. The switch turns on and the card goes grey with a "Blocked" tag, but the line under it now says "Your card is active and ready to use". With the switch off it says "Card blocked — payments will be declined". | "The Block card label is the wrong way round. I blocked the card and it says the card is active." |
| 4 | Send → How much | Type `12abc` (or just `abc`) in **Amount**. The field accepts letters. With `abc` the summary shows **$NaN**. | "The amount field accepts letters. I typed A B C and the summary shows not-a-number." |
| 5 | Send → Summary (scroll down) | Tap the **$100** chip. Summary: They receive $100.00, fee $1.50, **Total leaving your account $98.50** — the fee is subtracted instead of added. | "The transfer total is wrong. One hundred plus a one fifty fee shows ninety-eight fifty; it should be one hundred one fifty." |
| 6 | Activity → Yesterday | The Volta Energy bill is dated **31 Nov** — November has 30 days, and it is listed under Yesterday (13 November). | "This electricity payment shows the thirty-first of November. That date doesn't exist, it should be the thirteenth." |
| 7 | Profile → Notifications | Turn **Offers and news** off. About a second later it switches itself back on. | "I can't turn off Offers and news. The switch turns itself back on." |

Suggested order for a two-minute demo: 1 (no navigation needed) → 3 (most visual) → 4.

## "Where is…" / "How do I…" questions that land well

These targets are off-screen from Home, so the app visibly navigates, scrolls and pulses the marker.

| Say | The app goes to |
|---|---|
| "Where is the submit button?" | Send → bottom of the long form → **Submit transfer** (`transfer-submit`) |
| "How do I block my card?" | Cards → **Block card** (`card-block-toggle`) |
| "Where can I see my PIN?" | Cards → **Show PIN** (`card-show-pin`) |
| "How do I download a statement?" | Activity → very bottom of the list → **Export statement** (`activity-export-statement`) |
| "Where's my jacket refund?" | Activity → deep in the list, 27 October (`activity-tx-refund`) |
| "How do I call support?" | Profile → opens the collapsed **Help and support** section → **Call support** (`support-call`) |
| "How do I make a transfer repeat every month?" | Send → turns on Schedule for later → **Repeat** (`transfer-repeat`) |
| "Where do I turn on dark mode?" | Profile → App → **Dark mode** (`profile-dark-mode`) |
| "How do I report a lost card?" | Cards → bottom → **Report lost or stolen** (`card-report-lost`) |
| "Where do I apply for a loan?" | Nothing matches → bubble only (`assist.none`) |

## Without the band (fallback)

- Triple-tap the top of the screen (the header area), or open the app with `?debug=1`.
- The panel has **Open ticket / Submit / Cancel**, a **Say** box (sends a final transcript line) and an
  **Assist** picker with every element id. Buttons call the hub's `/api/debug/*` routes; if the hub is
  unreachable the panel simulates the hub locally, so the indicator, captions and marker can still be shown.
- Triple-tap again (or ✕) to close it.

## Good live-change requests

Circle or tap the element, then say it. These are one-line edits for the coding agent and appear by hot reload.

| Circle / tap | Say |
|---|---|
| Submit transfer button (Send, bottom) | "Make this button blue." / "Change this button to say Send money now." |
| Total balance card (Home) | "Make this card dark green." |
| Block card row (Cards) | "This label is the wrong way round, fix it." |
| Add money button (Home, Savings goal) | "This button covers the text, move it below." |

## What the audience sees on the phone

- Ticket open (selector mode): thin red frame around the screen, a "● Listening… describe it 0:07" pill above the tab bar, a hint ("Tap to select · hold and circle · tabs still switch screens") and then live captions. Taps do not operate the app: they select the element (green outline with its name; tap again to deselect). Only the tab bar and the bell still work (grey italic while you speak, white once final).
- Circle: a glowing green stroke under the finger; on release the elements inside light up for a moment.
- Submit: green "Ticket sent" pill with a drawn check mark.
- Fix: "Working on it…" with a spinner, then green "Done: <summary>" and a flash on the changed element; the change itself arrives by hot reload.
- Band listening (no ticket open): three small green bars at the top centre.
- Assist: the app switches tab if needed, scrolls, then a green halo pulses around the element with a marker dot at its corner and a speech bubble; it fades after about five seconds.
