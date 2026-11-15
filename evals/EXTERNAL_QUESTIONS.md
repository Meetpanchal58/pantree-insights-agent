# Questions for the Pantree analyst: a one-page brief

Thank you for helping test an AI data analyst. **Pantree** is a simulated Indian online fashion shop
(apparel, footwear, accessories) with 22 months of history: orders, returns, deliveries, customers,
website and app visits, marketing spend, stock, reviews and customer support. Money is in rupees.

Please write **5-10 business questions** you would genuinely ask a data analyst about a shop like this.
Write them the way you would in a message to a colleague. You do not need to know how the data is stored.

## What makes a good question

- It informs a decision ("Should we push COD less in tier 3 cities?" -> "What is the RTO rate for COD
  orders by city tier?").
- It names what to measure and how to split it, if you know ("return rate by category, last 6 months").
- Vague questions are welcome too (the analyst should ask you to clarify); mark them as such.
- Please do not ask for personal details of individual customers.

## Words the analyst understands

| Term | Meaning |
|---|---|
| GMV | Gross merchandise value: everything ordered at selling price, before coupons and returns. |
| AOV | Average order value. |
| RTO | Return to origin - a shipped parcel that came back undelivered, mostly COD refusals. |
| COD | Cash on delivery; everything else is prepaid. |
| prepaid | Any payment method other than COD. |
| ROAS | Return on ad spend, as measured by the shop (ad platforms report their own, higher figures). |
| CAC | Customer acquisition cost. |
| CTR | Click-through rate on products shown in lists. |
| CSAT | Customer satisfaction score 1-5, only given when a ticket is resolved. |
| MRP | Maximum retail price (list price); discounts are measured against it. |
| sell-through | Units sold compared with stock available.|
| churn | A customer who has stopped ordering; no fixed definition in the data - say which window you mean (e.g. no order in 90 days). |
| repeat customer | A customer with more than one order. |
| new customer | An order that was the customer's first. |
| festive season | The Navratri, Diwali and Grand Festive Sale events. |
| sale event | A festival or sale (e.g. Diwali, Republic Day Sale), live between its start and end dates. |
| revenue | Unless the user says otherwise, realized revenue (delivered minus refunds). |

## Measures it knows

| Measure | Definition |
|---|---|
| Orders | Orders placed (any status, including later cancelled or returned). |
| Units sold | Units ordered and not cancelled (includes units later returned or not delivered). |
| Gross sales (GMV) | Value of everything ordered at selling price, before coupons, cancellations and returns. |
| Net sales | What customers agreed to pay (after coupons, incl. shipping) for orders not cancelled. Returns not yet deducted. |
| Realized revenue | Money the shop keeps: delivered lines minus refunds. Excludes cancelled, returned-to-origin, lost and undelivered lines. THE default meaning of "revenue" or "sales" unless the user asks otherwise. |
| Average order value | Net sales per order, over orders that were not fully cancelled. |
| Gross margin % | (Revenue excl. GST and shipping - product cost) / revenue excl. GST and shipping, on delivered lines that were not refunded. |
| Average discount | How far below list price (MRP) items sold, before coupons. 0.25 = 25% off. |
| Orders from new customers | Share of orders that were the customer's first ever order. |
| Cancellation rate | Share of order lines cancelled before shipping. |
| RTO rate | Share of shipped lines that came back undelivered (return to origin), mostly COD refusals. |
| Return rate | Share of delivered lines the customer asked to return (refund, store credit or exchange). |
| Late deliveries | Share of delivered lines that arrived after the promised date. |
| Average delivery time | Days from order to delivery, for delivered lines. |
| Sessions | Visits to the website or app. |
| Conversion rate | Share of visits that ended in a purchase. |
| Click-through rate | Share of products shown in a list that were clicked. |
| Marketing spend | Money spent on paid media, affiliates and CRM messages. |
| Platform-reported ROAS | Revenue the ad platforms and affiliate networks claim per Rs spent (CRM has no platform reporting, so it is excluded). Platforms over-report. |
| Cost per click | Paid media spend per ad click (paid search, paid social, display). Affiliates are paid mostly per sale and CRM clicks are message clicks, so both are excluded. |
| Support tickets | Support tickets opened, dated by their first contact. |
| Tickets resolved | Share of tickets resolved so far (the rest are pending or escalated). |
| Customer satisfaction (CSAT) | Average satisfaction score (1-5) given when a ticket is resolved. |
| First response time | Average minutes until an agent first replied. |
| Average product rating | Average stars (1-5) of published reviews. |
| Negative reviews | Share of published reviews with negative sentiment. |
| Lost sales (units) | Units customers wanted (added to cart) that could not be sold because the size was out of stock. |
| ROAS (last-click, shop-measured) | Booked order revenue the shop credits to each channel under last-click attribution, per Rs spent. Use this, not the platform-reported figure, to judge channels. Revenue is counted when the order is placed (later cancellations and returns are not deducted). Orders with no campaign touch (Direct/Organic) are not credited to any channel. |
| Repeat purchase rate | Share of customers with at least one order who ordered again. Customers who first bought recently have had less time to come back; for a fair comparison use a fixed window (examples: h08). |
| Cart abandonment rate | Share of visits with an add-to-cart that did not end in a purchase. |
| SKUs out of stock now | Share of active products' sizes (SKUs) with nothing in stock today. |
| Customer acquisition cost (CAC, blended) | Paid media and affiliate spend per new customer (first order from any channel), by month. CRM spend is excluded: it targets existing customers. |

## Form (one row per question)

| # | Question | What decision it informs | Expected shape of the answer (e.g. one row per month) | Clear or vague? |
|---|---|---|---|---|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |
| 4 | | | | |
| 5 | | | | |
