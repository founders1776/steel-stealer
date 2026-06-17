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

## 2026-06-17 — Competitive reprice overhaul (the $54.99 money-loser)

Full paper trail in `OPERATIONS.md` §0 + the two specs under
`docs/superpowers/specs/2026-06-1{6,7}-*`. Transferable lessons:

- **A "failure" can be cosmetic; verify the real outcome, not the run conclusion.**
  CI run 27701409076 reported `failure` but had already pushed 1,920 price
  updates to Shopify — only the *post-step* bundle `git push` was rejected.
  Check the actual system (Shopify API, git log), not just the green/red.

- **Diagnose flakiness before prescribing infra.** I told James the stock sync
  "needs a residential proxy" after a 3-failure streak. The run history was
  actually ~50/50 since day one (intermittent Cloudflare Turnstile on shared CI
  IPs). It was never cleanly broken. Pull the *history* before concluding
  "broken → needs X."

- **For Shopify competitors, pull `/products.json` (full catalog) and match
  locally — never per-SKU `/search/suggest.json`.** Per-SKU was ~160k requests
  → rate-limited to 17h for 1.5 of 12 stores. Catalog pull is a few hundred
  requests, ~2 min for all 12, no throttling. (James's idea; it was right.)
  Pagination: `?page=N` until a short page — `since_id` is silently ignored by
  many storefronts (returns the same page → infinite/empty loop).

- **"Redundant" can mean "resilient" — check before deduping.** Two passes
  pricing the same SKU looked wasteful, but removing one would have made
  competitive pricing depend on the flaky Steel City login. Fix was to
  *decouple* (login non-fatal, competitive pass always runs) rather than just
  dedup. Ask what a duplicate path protects against before deleting it.

- **The bundle on `origin` is the source of truth, not a local decrypt.** A local
  `competitor_prices.json` can be a stale months-old decrypt while CI's bundle is
  current (or vice-versa). Always decrypt `origin/main:data.tar.gz.gpg` to check
  real state. And `competitor_prices.json` has an overwrite guard (refuses if new
  < 50% of existing) — that's why a failed scrape kept the old data instead of
  going empty.

- **CI bundle commits must rebase before push.** A concurrent commit (a code
  push, the other workflow) otherwise rejects the bundle push and the data is
  LOST (happened run 27701409076). Both workflows now `git pull --rebase
  --autostash` + retry.

- **Two separate floors: 20% gross margin (the undercut gate) vs $6.99 store
  display floor (a final clamp).** Don't conflate them — `max(cost/0.80, 6.99)`
  as a single gate is wrong; the margin gate is `cost/0.80` only, $6.99 is
  applied after. (James corrected this during design.)
