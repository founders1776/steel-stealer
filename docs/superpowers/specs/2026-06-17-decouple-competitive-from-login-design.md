# Decouple Competitive Pricing from the Cloudflare-Flaky Steel City Login

**Date:** 2026-06-17
**Status:** Approved

## Problem

The 12h sync does three pricing passes: `run_reprice_targets` (SEBO/Miele/Lindhaus),
`run_competitive_reprice` (B — whole catalog undercut), and the main loop (A —
Steel City stock + cost-driven, *also* competitor-aware via `get_best_price`).

Two issues:
1. **Competitive pricing is trapped behind the browser login.** `main()` calls
   `login()` *before* `run_sync()`; a Cloudflare Turnstile failure (~50% of CI
   runs) aborts the whole run, so B never runs. Competitive undercut for ungated
   + Desco SKUs only happens on lucky login-success runs.
2. **Double-pricing.** In a successful full sync, in-gate Steel City SKUs are
   priced by both B (off stored cost) and A (off fresh API cost) — A wins, B is
   wasted work + a stale-cost-margin risk.

Both "update to Steel City cost daily" and "undercut competitors" must keep
working; competitive must become reliable, not Cloudflare-dependent.

## Design

Reorder the sync so competitive passes don't depend on the login, and dedup
A↔B based on whether A actually ran.

**New `run_sync` flow** (owns the driver now; `main()` no longer logs in):
1. `run_reprice_targets(...)` — always (no browser).
2. Attempt Steel City browser: `create_driver()` + `login()` inside try/except.
   Failure is **non-fatal** — log a warning, `driver=None`, continue.
3. If the browser came up **and** not in a price-only mode → run the **main loop
   A** (Steel City stock + cost + competitive) over `sync_skus`. Record
   `main_loop_skus = {sku for sync_skus}`.
4. If browser failed → skip A; `main_loop_skus = set()`.
5. `run_competitive_reprice(..., main_loop_skus=main_loop_skus)` — **always**
   (unless `--reprice-only`). B skips any SKU in `main_loop_skus`
   (`deferred_to_main` stat). So:
   - login OK → A owns in-gate (fresh cost); B owns ungated + Desco. No overlap.
   - login fails → B covers everything (in-gate too, off stored cost) as the
     competitive fallback, so prices still move that run.

**Modes:**
- `--competitive-reprice` / `--reprice-only`: no browser at all (price-only);
  `main_loop_skus` empty → B handles everything. (Unchanged behavior for the
  one-time correction runs.)
- Default (full): the flow above.

**`run_competitive_reprice`:** new param `main_loop_skus=set()`; `if sku in
main_loop_skus: stats["deferred_to_main"] += 1; continue`.

**`calculate_competitive_prices.py` → report-only:** drop the writes to
`product_names.json` (retail + competitor_price) and the products file write;
keep `pricing_decisions.json` + the email report. The sync now owns
`product_names` retail; calculate only duplicated it.

## What this guarantees
- **Competitive undercut: every 12h, Cloudflare-independent** (B + reprice_targets
  always run).
- **Steel City cost/stock: every 12h when login works** (best-effort), with B as
  the competitive fallback when it doesn't.
- **One Shopify write per SKU** on successful runs (no double-pricing).

## Non-goals
No change to the walk / 20% floor / $6.99 logic, or to A's stock/NLA/OOS logic.

## Rollout
1. Implement; re-run pricing math check (unchanged → must still pass).
2. Dry-run a full sync locally: confirm B reports `deferred_to_main > 0` when
   login succeeds, and login-failure path skips A but runs B.
3. Commit + push.
4. Reconcile bundle: trigger `--competitive-reprice --up-only` (push-fix ensures
   the bundle commit persists). Up-only → live prices unchanged, cache refreshed.
5. Verify `product_names` retail in the new bundle matches live Shopify on a sample.
