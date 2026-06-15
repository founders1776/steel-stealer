# Desco Login Spike — Findings (Task 1, 2026-06-15)

Live recon against descovac.com with the dealer login. **Plain `requests` works — no browser, no Cloudflare.** This drives `desco_ingest.py`.

## Auth
- **GET** `https://www.descovac.com/signin.asp` first (sets initial cookies).
- **POST** `https://www.descovac.com/security_logonscript_sitefront.asp?action=logon&parent_c_id=&returnpage=signin%2Easp%3F&pageredir=%2Fsignin%2Easp`
  - form fields: `username` (= DESCO_EMAIL), `password` (= DESCO_PASSWORD), `logontype=customer` (hidden).
- Success = response URL is NOT signin and `my_account.asp` returns 200 without redirect. Session cookies: `customer_logon`, `cookie_session`, `ASPSESSIONID*`, `anon_sc_id`.
- No Cloudflare / Turnstile on any page → no undetected-chromedriver needed (lighter than Steel City).

## Catalog data — embedded JSON (no AJAX reverse-engineering)
- Category page **`GET pc_combined_results.asp?pc_id=<32-hex GUID>`** returns HTML containing a server-side JSON array `"products":[ {...}, ... ]`.
- The page has multiple `"products":[` islands (featured/trending = ~2 items). **Take the LARGEST block** = the category's result set. (Bracket-balance parse; `json.loads`.)
- Per-product fields used:
  - `key` (32-hex product id — dedupe key), `sku`, `name`, `brand` (often empty — derive from name/category).
  - `uomPrice[0].price` and `.suggestedPrice` = **dealer/login price** (this is dealer COST for this account). `mapPriceType: require_login_for_price_and_atc`.
  - `inventory`: `{ isInventoryItem (bool), stock (int|null), stockMessage (str) }`. Many parts are `isInventoryItem:false, stock:null` (non-tracked — treat untracked/continue-selling like Steel City special orders).
  - image relative paths: `largePic` `images/products/<sku>_l.jpg`, `pic` `_n.jpg`, `thumb` `_t.jpg`.

## Images
- Base host: **`https://www.descovac.com/`** + relative path. (Beats CloudFront `dqmy05zjbnp6b.cloudfront.net` — e.g. 6332-X is 200 on descovac, 404 on CDN. Most are 200 on both.)
- Prefer `_l.jpg` (large), fall back to `_n.jpg`.
- Some SKUs 404 (genuinely imageless — special-order "(O.S)" items, like Steel City `picture='0'`). **Verify HTTP 200 before download**; skip imageless rather than substitute (James's rule).

## Enumeration
- Home page (`/`) carries ~1,020 unique category links (`pc_combined_results.asp?pc_id=<GUID>`). Some nested/duplicate.
- Sample: 40 categories → 184 unique products (175 with a pic field). Full catalog ≈ low thousands.
- Pagination: categories sampled fit one page; large categories use a page/results param (`page=`, `start_at`, `p=50` seen in nav) — confirm + handle in the discover step for big categories.
- Dedupe by `key` (product GUID) across categories.

## Open verify items (handle in build, not blockers)
1. **Account/pricing:** logged-in `accountHistory.accountName = "MANCHESTER VACUUMS & MORE"`. Confirm these creds return evacuumsandmore's correct dealer cost (it IS the cost tied to this login regardless). Flagged to James.
2. Exact large-category pagination param (test a category with >1 page).
3. Stock semantics for `isInventoryItem:true` items (capture `stock`/`stockMessage` live to map in_stock bool).

## Conclusion
Build `desco_ingest.py` as: login (requests) → enumerate categories from home (+ nested) → GET each category, parse largest `"products"` JSON → normalize `{sku,clean_name,brand,dealer_cost,in_stock,image_urls,source:"desco"}` → dedupe by key → `desco_products.json`. Resumable, rate-limited (~0.4s). No Selenium.
