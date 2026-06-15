# Desco Distributor Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Desco Vacs (descovac.com) as a second product distributor — scrape its catalog with the dealer login, dedupe against the live store, write SEO descriptions, process images, create drafts on all channels (except POS), tag every product with a `custom.source` metafield, and fold Desco into the existing competitor-undercut pricing + 12h sync.

**Architecture:** Approach C — one new ingestion adapter (`desco_ingest.py`) emitting the existing `product_names.json` record shape into `desco_products.json`; everything downstream (dedup, research, images, create, channels, pricing, sync) reuses the Steel City / sheet_import machinery, which is source-neutral. A `custom.source` metafield + `source_map.json` add the source dimension. The live Steel City path is untouched except one additive sync-gate extension.

**Tech Stack:** Python 3 (`.venv`), `requests` (primary; undetected-chromedriver v145 fallback), BeautifulSoup/lxml for ASP HTML parsing, Shopify Admin REST + GraphQL 2024-10, PIL + rembg for images, Claude research agents for descriptions.

**Spec:** `docs/superpowers/specs/2026-06-15-desco-distributor-integration-design.md`
**Phase 0 findings:** `docs/desco_access_findings.md`

**Project conventions:** no test suite — verify via `--dry-run`, smoke checks, report files, live Shopify spot-checks (same as Steel City / sheet_import). Mutating steps gated on James's approval. Drafts-first for the first batch. Publishing to non-Online-Store channels uses the publications-scoped token from `EVAC SEO Optimization Run/shopify_token.txt` (the main `.env` token lacks `read/write_publications`).

---

## Reused code (read before starting)
- `import_missing_products.py` — `shopify_get/post`, `shopify_api_url/headers`, `slugify`, `parse_price`, `generate_tags`, `calculate_markup_price`, image helpers `load_logo/create_background_with_logo/process_single_image`, env constants.
- `sheet_import.py` — `step_match` (exact + O↔0 fuzzy + live GraphQL double-check), `graphql_find_sku`, `normalize_o0`, image download+process step, `build_image_payloads`, content-file contract + `step_research_validate`, `shopify_put`, `step_create`/`resolve_price` patterns.
- `generate_pricing.py` — `calculate_markup_price`, `charm_price`, `get_markup`.
- `sync_stock_prices.py:764-781` — the "our products" gate (union of `bulk_import_progress.json` + `missing_import_progress.json`).
- `build_reprice_targets.py` — `MACHINE_TITLE`, `reprice_brands.json` mechanism.

## File structure
- Create `desco_ingest.py` — Desco login + catalog scrape → `desco_products.json` (only Desco-specific logic).
- Create `source_metafield.py` — create the `custom.source` metafield definition + set/backfill it + maintain `source_map.json`.
- Create `desco_imports/<date>/` run dir (manifest/content/images/progress), reusing sheet_import step machinery.
- Modify `sync_stock_prices.py` — one additive gate extension to include Desco's progress file.
- Modify both `.github/workflows/*.yml` — add `desco_products.json`, `source_map.json`, Desco progress file to the (identical) bundle tar lists.
- Reuse unchanged: `sheet_import.py`, `import_missing_products.py`, `build_reprice_targets.py`.

---

### Task 1: Authenticated login spike (SPIKE — gates the scraper)

**Files:** Create `desco_imports/spike_findings.md` (notes only, no production code yet).

This task is investigation against the live site with the dealer login. It produces the facts the scraper needs. Creds: add `DESCO_EMAIL`/`DESCO_PASSWORD` to `.env` (James places them; mirrors `SC_*`).

- [ ] **Step 1: Confirm creds present**

Run: `cd "/Users/jamesfeeney98/Desktop/Projects/Steel Stealer" && grep -c "^DESCO_EMAIL=" .env && grep -c "^DESCO_PASSWORD=" .env`
Expected: `1` and `1`. If `0`, stop and ask James to add them (do not transcribe the password yourself).

- [ ] **Step 2: Capture the login form shape (unauthenticated GET)**

```python
import requests, re
r = requests.get("https://www.descovac.com/signin.asp", headers={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"}, timeout=30)
# print the <form ... action> and every <input name=...> on the signin page
print([m for m in re.findall(r'<form[^>]*action=["\']([^"\']+)', r.text)])
print(re.findall(r'<input[^>]*name=["\']([^"\']+)["\'][^>]*>', r.text))
print("cloudflare" , 'cf-' in r.text.lower() or 'turnstile' in r.text.lower())
```
Record in `spike_findings.md`: form action URL, the username/password field names, any hidden fields (CSRF/viewstate), and whether Cloudflare/Turnstile is present.

- [ ] **Step 3: Attempt a programmatic login with a `requests.Session`**

Using the field names from Step 2, POST credentials to the form action; follow redirects; confirm an authenticated page (e.g. `my_account.asp` or a product page no longer 302s to signin). Use `os.environ['DESCO_EMAIL']/['DESCO_PASSWORD']`. Do NOT print the password. If `requests` login fails or Cloudflare blocks, note it — the scraper will use undetected-chromedriver v145 (copy `create_driver`/`login` pattern from `sync_stock_prices.py`).

- [ ] **Step 4: Capture one product page's structure**

With the authenticated session, fetch one `pc_product_detail.asp?...` and one `pc_combined_results.asp?pc_id=...` page. Record in `spike_findings.md`: where SKU, product name, brand, **dealer cost** (is it dealer-specific or list?), **stock** (boolean / qty / per-warehouse text), and image URLs (CloudFront) appear in the HTML (CSS selectors or regex anchors). Capture how categories/products are enumerated (pagination params, total count if shown).

- [ ] **Step 5: Write findings + commit**

Fill `desco_imports/spike_findings.md` with: login method (requests vs browser), exact field names, product-page selectors for sku/name/brand/cost/stock/images, catalog enumeration method, approx catalog size. This document drives Tasks 2.
```bash
git add desco_imports/spike_findings.md
git commit -m "spike: Desco login + product-page structure findings"
```

---

### Task 2: `desco_ingest.py` — scaffold (config, session, login)

**Files:** Create `desco_ingest.py`.

- [ ] **Step 1: Write the scaffold using spike findings**

```python
#!/usr/bin/env python3
"""desco_ingest.py — scrape descovac.com (CIMcloud/ASP) into desco_products.json.
Auth: DESCO_EMAIL/DESCO_PASSWORD from .env (mirrors SC_*). Resumable per step.
Usage: python3 desco_ingest.py --step [login|discover|enrich|export|all] [--dry-run] [--limit N]
"""
import argparse, json, os, re, time, logging
from pathlib import Path
import requests

BASE_DIR = Path(__file__).parent
DESCO_PRODUCTS_FILE = BASE_DIR / "desco_products.json"
PROGRESS_FILE = BASE_DIR / "desco_ingest_progress.json"
BASE_URL = "https://www.descovac.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

def make_session():
    s = requests.Session(); s.headers["User-Agent"] = UA
    return s

def login(session):
    """Form login. Field names + action from spike_findings.md."""
    email = os.environ["DESCO_EMAIL"]; pw = os.environ["DESCO_PASSWORD"]
    # POST to the action captured in the spike; field names from the spike.
    # (Filled in from Task 1 findings — see spike_findings.md.)
    resp = session.post(f"{BASE_URL}/<LOGIN_ACTION_FROM_SPIKE>",
                        data={"<USER_FIELD>": email, "<PW_FIELD>": pw}, timeout=30,
                        allow_redirects=True)
    ok = "signin" not in resp.url.lower()
    if not ok:
        raise RuntimeError("Desco login failed — check creds / use browser fallback")
    log.info("Desco login OK")
    return session

def load_progress():
    if PROGRESS_FILE.exists(): return json.loads(PROGRESS_FILE.read_text())
    return {"category_ids": [], "product_urls": [], "enriched": {}, "steps_done": []}

def save_progress(p): PROGRESS_FILE.write_text(json.dumps(p, indent=2))
```

Replace `<LOGIN_ACTION_FROM_SPIKE>`/`<USER_FIELD>`/`<PW_FIELD>` with the exact values from `spike_findings.md`. If the spike found Cloudflare, swap `make_session`/`login` for the undetected-chromedriver pattern copied from `sync_stock_prices.py` (`create_driver`, `login`, `version_main=145`).

- [ ] **Step 2: Smoke-test login**

Run: `cd "/Users/jamesfeeney98/Desktop/Projects/Steel Stealer" && source .venv/bin/activate && export $(cat .env | xargs) && python3 -c "import desco_ingest as d; s=d.login(d.make_session()); print('login ok')"`
Expected: `Desco login OK` / `login ok`. (Network-dependent; if it fails on Cloudflare, switch to browser fallback per Step 1.)

- [ ] **Step 3: Commit**
```bash
git add desco_ingest.py
git commit -m "feat: desco_ingest scaffold — session + login"
```

---

### Task 3: `desco_ingest.py` — discover + enrich + export

**Files:** Modify `desco_ingest.py`.

- [ ] **Step 1: Implement discover (category + product enumeration)**

Using the enumeration method from `spike_findings.md`, add `step_discover(session, progress)` that walks `pc_combined_results.asp` category pages (and/or search), collects unique `pc_product_detail.asp` product URLs into `progress["product_urls"]`, paginated + rate-limited (`time.sleep(0.5)`), resumable (skip already-collected). Log running count.

- [ ] **Step 2: Implement enrich (parse product pages)**

Add `step_enrich(session, progress)`: for each product URL not in `progress["enriched"]`, fetch the page and parse — using the selectors from `spike_findings.md` — into a record: `{"sku","clean_name","brand","dealer_cost"(float),"in_stock"(bool),"image_urls"[list of CloudFront URLs],"source":"desco"}`. Skip products with no SKU or no cost (log to a skipped list). Save progress every 25 products (resumable). Apply the same NLA/special-order skip philosophy as `catalog_scraper.py` if those flags appear.

- [ ] **Step 3: Implement export**

Add `step_export(progress)`: write `progress["enriched"]` (the records) to `desco_products.json` as a dict keyed by SKU. Print counts: total, with-cost, with-image, by-brand top 10.

- [ ] **Step 4: Wire `main()` + run discover/enrich/export against the live site (`--limit 50` first)**

Run: `python3 desco_ingest.py --step all --limit 50` → expect a 50-product `desco_products.json` with costs + images. Spot-check 5 records against the live site by eye. Then full run (no limit).

- [ ] **Step 5: Commit**
```bash
git add desco_ingest.py desco_products.json
git commit -m "feat: desco_ingest discover/enrich/export — desco_products.json"
```

---

### Task 4: `source_metafield.py` — metafield definition + setter + source_map

**Files:** Create `source_metafield.py`.

- [ ] **Step 1: Create the metafield definition (idempotent)**

```python
#!/usr/bin/env python3
"""source_metafield.py — manage custom.source ('steel_city'|'desco') on products + source_map.json."""
import os, json, time, requests
from pathlib import Path
BASE_DIR = Path(__file__).parent
SOURCE_MAP_FILE = BASE_DIR / "source_map.json"
S = os.environ["SHOPIFY_STORE"]; T = os.environ["SHOPIFY_ACCESS_TOKEN"]
URL = f"https://{S}/admin/api/2024-10/graphql.json"
H = {"X-Shopify-Access-Token": T, "Content-Type": "application/json"}

def gql(q, v=None):
    for a in range(4):
        r = requests.post(URL, headers=H, json={"query": q, "variables": v or {}}, timeout=60)
        if r.status_code == 429: time.sleep(float(r.headers.get("Retry-After", 2))); continue
        return r.json()
    return None

def ensure_definition():
    q = '''mutation { metafieldDefinitionCreate(definition: {
      name:"Source", namespace:"custom", key:"source", type:"single_line_text_field",
      ownerType: PRODUCT }) { createdDefinition{id} userErrors{field message code} } }'''
    d = gql(q)
    errs = d["data"]["metafieldDefinitionCreate"]["userErrors"]
    # TAKEN code = already exists — that's fine
    print("definition:", "exists" if any(e["code"]=="TAKEN" for e in errs) else "created", errs)

def set_source(product_gid, source):
    q = '''mutation($id:ID!,$v:String!){ metafieldsSet(metafields:[{
      ownerId:$id, namespace:"custom", key:"source", type:"single_line_text_field", value:$v}]){
      userErrors{field message} } }'''
    return gql(q, {"id": product_gid, "v": source})
```

- [ ] **Step 2: Smoke-test definition + one set**

Run: `source .venv/bin/activate && export $(cat .env | xargs) && python3 -c "import source_metafield as m; m.ensure_definition()"`
Expected: `definition: created` (first run) or `exists`.
Then set on one known product and verify via a `product{metafield(namespace:\"custom\",key:\"source\"){value}}` query returns the value.

- [ ] **Step 3: Commit**
```bash
git add source_metafield.py
git commit -m "feat: source_metafield — custom.source definition + setter"
```

---

### Task 5: Backfill `custom.source` on existing products + build `source_map.json`

**Files:** Modify `source_metafield.py` (add `backfill()`); Create `source_map.json`.

- [ ] **Step 1: Implement backfill**

Add `backfill()`: build SKU→source and product_id→source from local truth — `bulk_import_progress.json` + `missing_import_progress.json` (product_ids) → `steel_city`; `sheet_imports/*/progress.json` `created` keys → `steel_city` (Miele/Lindhaus are still Steel-City-adjacent dealer-sheet imports, but per spec tag them by their own run: use source `"steel_city"` for the schematic imports and `"dealer_sheet"` for Miele/Lindhaus — store the precise source). Write `source_map.json` (SKU→source). Then `metafieldsSet` `custom.source` on each product_id. Rate-limit 0.3s. Resumable via a `backfilled` list in source_map.

- [ ] **Step 2: Dry-run then live backfill**

Run `python3 source_metafield.py --backfill --dry-run` (prints counts by source, writes nothing to Shopify) → review. Then `--backfill` live. Verify `custom.source` on 3 sampled products via GraphQL.

- [ ] **Step 3: Commit**
```bash
git add source_metafield.py
git commit -m "feat: backfill custom.source on existing products + source_map.json"
```

---

### Task 6: Build Desco run manifest + dedupe against live store

**Files:** Create `desco_imports/<date>/manifest.json` (Claude/script-built from `desco_products.json`); reuse `sheet_import.step_match`.

- [ ] **Step 1: Convert `desco_products.json` → sheet_import manifest shape**

Write a small builder (inline `python3 -c` or a `--manifest` flag on `desco_ingest.py`) producing `desco_imports/<date>/manifest.json` with `{brand:"DESCO", vendor:<per-product brand>, source_sheet:"descovac.com", parsed_at:<date>, rows:[{sku,name,dealer_cost,map_price:null,msrp:null,sheet_context}]}`. Note: vendor must be the product's actual brand (Bissell/Dyson/etc.), not "DESCO" — adjust `sheet_import` create to read vendor per-row, or set vendor per row in the manifest. (If `sheet_import` hardcodes one vendor, extend it to accept per-row `vendor`.)

- [ ] **Step 2: Run match (live dedupe)**

`python3 sheet_import.py --run-dir desco_imports/<date> --step parse` then `--step match`. Expect buckets: `new` (proceed), `existing` (skip — Steel City/whoever already there wins), `ambiguous` (report). Log counts. Per spec, only `new` proceeds.

- [ ] **Step 3: Commit**
```bash
git add desco_imports/<date>/manifest.json desco_imports/<date>/progress.json
git commit -m "feat: Desco manifest + live dedupe (new vs existing buckets)"
```

---

### Task 7: Research descriptions (Claude agents, agentic+Google optimized)

**Files:** `desco_imports/<date>/content/<sku>.json` (agent-written); validate via `sheet_import.step_research_validate`.

- [ ] **Step 1: Dispatch research agents** (~10 new SKUs/agent), prompt tuned for agentic-storefront + Google: entity-dense, explicit compatible-model lists, structured spec facts, SKU verbatim in body, meta_title ≤60 / meta_description ≤160, original copy. Same content schema as sheet_import (`{sku,title,body_html,meta_title,meta_description,image_urls,compatible_models,sources}`). Manager dispatches agents per the cavecrew/Agent pattern used for Miele/Lindhaus.

- [ ] **Step 2: Validate** — `python3 sheet_import.py --run-dir desco_imports/<date> --step research` → "N ok, 0 missing, 0 invalid".

- [ ] **Step 3: Commit**
```bash
git add desco_imports/<date>/content
git commit -m "feat: Desco SEO descriptions (agentic + Google optimized)"
```

---

### Task 8: Images (Desco CloudFront → rembg + 2048 + VaM watermark)

**Files:** `desco_imports/<date>/images/<sku>/`; reuse `sheet_import.step_images`.

- [ ] **Step 1: Run images step** — `python3 sheet_import.py --run-dir desco_imports/<date> --step images`. Uses Desco image URLs from content files, applies rembg + 2048 white canvas + VaM logo (the `process_single_image` path already wired in `sheet_import`). Referer header already added.
- [ ] **Step 2: Verify** sample processed images are 2048×2048 RGB; review no-image count.
- [ ] **Step 3: Commit**
```bash
git add -A desco_imports/<date>/images
git commit -m "feat: Desco images processed (Steel City treatment)"
```

---

### Task 9: Create drafts (with `custom.source=desco`) + channels (GATED)

**Files:** reuse `sheet_import.step_create`; call `source_metafield.set_source`; publish via publications token.

- [ ] **Step 1: Dry-run create** — `python3 sheet_import.py --run-dir desco_imports/<date> --step create --dry-run`. Review draft count, prices (markup chain — Desco has no MAP so tiered markup applies), images.
- [ ] **Step 2: GATED live create** — get James's OK on the dry-run, then `--step create`. Drafts only.
- [ ] **Step 3: Set `custom.source=desco`** on each created product (loop `set_source` over `progress["created"]`).
- [ ] **Step 4: Publish to channels** — using the publications-scoped token (`EVAC SEO Optimization Run/shopify_token.txt`), `publishablePublish` each created product to Online Store (116401340666), Shop (116401438970), Google & YouTube (132416471290) — exclude POS. (Same script used for Miele/Lindhaus.)
- [ ] **Step 5: Verify** 3 samples: status, `custom.source`, channel count. Commit progress.
```bash
git add desco_imports/<date>/progress.json
git commit -m "feat: Desco drafts created, source-tagged, on 3 channels"
```

---

### Task 10: Pricing + reprice registration

**Files:** reuse `generate_pricing`/`build_reprice_targets`; possibly `reprice_brands.json`.

- [ ] **Step 1: Confirm markup pricing applied** at create (Desco = no MAP → `calculate_markup_price` on dealer cost, charm-rounded). Spot-check 5 prices = `calculate_markup_price(cost)`.
- [ ] **Step 2: Reprice eligibility** — Desco parts join competitor-undercut automatically once they have competitor data (they're active, vendor=brand). If any Desco brand is MAP-protected, add it to `reprice_brands.json` and re-run `build_reprice_targets.py --dry-run` to confirm machines lock / parts target. For pure-parts Desco brands, no change needed.
- [ ] **Step 3: Commit** any config change.
```bash
git add reprice_brands.json 2>/dev/null; git commit -m "chore: Desco reprice eligibility" --allow-empty
```

---

### Task 11: Extend the 12h sync gate to include Desco

**Files:** Modify `sync_stock_prices.py` (the gate at ~764-781) + add Desco progress file to the union.

- [ ] **Step 1: Add Desco to the "our products" union**

After the `missing_import_progress` block (~line 781), add:
```python
    # Desco-sourced products (second distributor) — same handling as Steel City
    DESCO_PROGRESS_FILE = BASE_DIR / "desco_ingest_progress.json"
    if DESCO_PROGRESS_FILE.exists():
        with open(DESCO_PROGRESS_FILE) as f:
            desco_prog = json.load(f)
        before = len(our_product_ids)
        for v in (desco_prog.get("created") or {}).values():
            pid = v if isinstance(v, str) else v.get("id")
            if pid: our_product_ids.add(str(pid))
        log.info(f"  + {len(our_product_ids)-before} from Desco → {len(our_product_ids)} total")
```
(Match the exact shape `desco_ingest`/create writes for created products — adjust the value extraction to that shape.)

- [ ] **Step 2: Verify** `python3 sync_stock_prices.py --dry-run` includes the Desco count in "our products" and does not error. Desco products should be eligible for stock-draft/reactivate + cost-rise reprice like Steel City.
- [ ] **Step 3: Commit**
```bash
git add sync_stock_prices.py
git commit -m "feat: sync gate includes Desco-sourced products"
```

---

### Task 12: CI data bundle + Schematics.md + activation

**Files:** Modify both `.github/workflows/*.yml` (identical tar lists); `Schematics.md`; activation.

- [ ] **Step 1: Add Desco files to the bundle tar list in BOTH workflows** — append `desco_products.json desco_ingest_progress.json source_map.json` to the `for f in ...` list in `sync-stock-prices.yml` and `scrape-competitor-prices.yml` (keep the two lists identical). Add `DESCO_EMAIL`/`DESCO_PASSWORD` GitHub secrets if the sync will re-scrape Desco stock in CI.
- [ ] **Step 2: Update `Schematics.md`** — add a "Desco Distributor (desco_ingest.py)" section: login→discover→enrich→export→desco_products.json→manifest→(reuse sheet_import match/research/images/create)→source metafield→channels→pricing/sync. Note source metafield + source_map.json.
- [ ] **Step 3: James reviews Desco drafts → activate** — `python3 sheet_import.py --run-dir desco_imports/<date> --activate`, then publish-to-channels for the now-active products (re-run Task 9 Step 4 since draft channel records don't surface until active — learned on Lindhaus).
- [ ] **Step 4: Commit + ops** — re-encrypt the CI bundle (or trigger the scrape workflow to do it), `gh secret set` any new secrets. Push only with James's OK.
```bash
git add Schematics.md .github/workflows/sync-stock-prices.yml .github/workflows/scrape-competitor-prices.yml
git commit -m "docs+ci: Desco pipeline in Schematics + bundle/secret wiring"
```

---

## Self-review
- **Spec coverage:** Phase 0 (done, T1 verifies live) ✓; ingestion scrape (T2-3) ✓; source metafield + backfill + source_map (T4-5) ✓; dedupe keep-existing (T6) ✓; SEO agentic+Google descriptions (T7) ✓; image treatment (T8) ✓; create + source tag + channels-minus-POS (T9) ✓; same pricing engine (T10) ✓; 12h sync inclusion (T11) ✓; CI bundle + Schematics + activation (T12) ✓.
- **Placeholders:** T1/T2 intentionally carry spike-derived blanks (`<LOGIN_ACTION_FROM_SPIKE>` etc.) — these are *outputs of the spike*, not lazy placeholders; the spike (T1) produces them before T2 fills them. This is the correct pattern for a scrape against an un-spec'd login.
- **Type consistency:** record shape `{sku,clean_name,brand,dealer_cost,in_stock,image_urls,source}` consistent T3→T6; content schema matches sheet_import; `set_source(product_gid, source)` consistent T4→T5→T9.
- **Open risk:** if `sheet_import.step_create` hardcodes a single `manifest["vendor"]`, T6 Step 1 flags the needed per-row vendor extension — implement that in T6 before T9.
