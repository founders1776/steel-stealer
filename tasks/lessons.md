# Lessons

Patterns from corrections — review at session start.

## 2026-06-12 — Steel City costs are NOT the store's costs for dual-source brands

For dual-source brands (SEBO etc.), the store buys DIRECT from the brand's
distributor. Steel City also carries some of those SKUs, but at a marked-up
reseller cost. Steel City's cost is therefore:

- **Valid** as a conservative price floor (true cost ≤ SC cost)
- **Invalid** as the Shopify cost-per-item — that field holds the true direct
  dealer cost and drives margin reporting. Never write SC costs onto
  dual-source brand products (`update_variant_price(..., cost=None)` in the
  reprice pass).

General shape of the mistake: a data field that is "a cost" is not
interchangeable with every other "cost" — always ask WHOSE cost it is and
which supplier the store actually buys from.
