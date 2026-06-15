# Desco Vacs — Access Findings (Phase 0, 2026-06-15)

**Distributor:** Desco Vacuum Cleaner Supply Co. Inc., Hauppauge NY — `descovac.com` (canonical www.descovac.com). Vacuum-parts wholesale, trade-only. Brands incl. Bissell, Dyson, Hoover, Eureka, Kirby. (NOT the ESD/electronics "Desco Industries".)

**Platform:** CIMcloud (Website Pipeline) on classic ASP. Storefront fully login-walled — `GET /` and product search both 302→`/signin.asp`. No unauthenticated catalog data; no public sitemap.xml. Images on public CloudFront CDN `dqmy05zjbnp6b.cloudfront.net/images/...` (no auth).

**URL patterns:** `signin.asp`/`security_logon.asp` (auth), `pc_combined_results.asp?pc_id=<32-hex>` (category/search), `pc_product_detail.asp` (product), `showcart.asp`, `my_account.asp`, `sitemap.asp`.

**Two access paths:**
1. **CIMcloud REST API ("Machine to Machine")** — JSON product feed (SKU/name/desc/image refs) + price & stock reports (customer-specific pricing, per-warehouse inventory). Bearer-token auth, tokens minted in CIMcloud Worker Portal. GATED: paid bundle Desco must own + a worker login with API rights. Business ask to Desco; not available with the dealer login on hand.
2. **Authenticated scrape (CHOSEN)** — classic-ASP form login (POST likely `security_logon.asp`) → ASPSESSIONID + CIMcloud session cookies → `requests` for `pc_combined_results`/`pc_product_detail` HTML; images direct from CloudFront. No Cloudflare seen on public pages → plain `requests` may suffice (verify under auth; fall back to undetected-chromedriver like Steel City if blocked).

**Decision:** Build authenticated scrape now using dealer creds (`DESCO_EMAIL`/`DESCO_PASSWORD` in .env). Pursue API later as an upgrade if Desco provisions a token.

**To verify under auth (first build step):** exact login form field names + POST action; whether dealer-specific cost is in post-login HTML; stock representation (bool vs qty vs per-warehouse); presence of any bot protection when authenticated.
