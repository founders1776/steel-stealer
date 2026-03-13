# Pricing Strategy

## Fields
- **`price`** — Dealer cost from Steel City Vacuum
- **`retail_price`** — Our selling price (what the customer pays)

## Tiered Markup

| Dealer Cost Range | Multiplier |
|---|---|
| $0 – $1 | 8.0× |
| $1 – $3 | 4.5× |
| $3 – $7 | 3.2× |
| $7 – $15 | 2.5× |
| $15 – $30 | 2.2× |
| $30 – $60 | 1.9× |
| $60 – $120 | 1.7× |
| $120 – $300 | 1.5× |
| $300+ | 1.4× |

## Rules
- All prices end in **.99** (charm pricing)
- Minimum floor: **$6.99** — no product listed below this
- Tiers were adjusted down after competitor analysis showed initial pricing was 30-60% too high
- Validated against 20 random competitors — pricing is competitive with market rates

## Stats
- Average retail: $45.64
- Median retail: $19.99
- Average margin: 64.5%

## In Shopify CSV
- `Variant Price` = `retail_price` (what customer pays)
- `Variant Cost per item` = `price` (dealer cost, for profit tracking)
