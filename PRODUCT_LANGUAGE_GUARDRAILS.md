# FloatIQ product-language guardrails

These rules keep the product factual and user-directed while legal counsel reviews the final
business model. They are product requirements, not a substitute for legal advice.

## Core behavior

- Users create their own risk, confirmation, position-size, exposure, and cooldown rules.
- FloatIQ compares recorded data with those saved rules.
- Discipline results are kept separate from profit and loss. A planned trade can lose, and an
  unplanned trade can profit.
- Historical results always include sample size and must not be described as a future probability.
- Live broker submission remains disabled until broker integration and specialist legal review.
- A `locked` rule can stop only a submission traveling through FloatIQ. It cannot control orders
  placed directly in another application.
- Terms acceptance records consent but does not waive securities laws.

## Preferred wording

- "Matches the criteria you saved."
- "Historical win rate was 74% across 312 recorded examples."
- "Quantity calculated from the account, risk, entry, and stop values you supplied."
- "This plan conflicts with your saved relative-volume rule."
- "Order preview prepared from your selections."
- "The recorded entry occurred before the confirmation in your saved plan."

## Wording to avoid

- "You should buy or sell this."
- "This is the best trade for you."
- "This trade has a 74% chance of winning."
- "You should invest $4,000."
- "FloatIQ recommends entering now."
- "Guaranteed," "safe trade," or language implying a repeated historical outcome.

## Discipline modes

- `monitor`: record rule conflicts without interrupting the user's workflow.
- `coach`: show neutral warnings while allowing the user to continue.
- `strict`: require an explicit acknowledgement before FloatIQ's workflow can continue.
- `locked`: do not allow FloatIQ's workflow to continue when a user-authored rule is violated.

The API response must always retain `broker_order_submitted: false` until an approved broker
integration actually confirms an order.

