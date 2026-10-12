# Acme Appliances Ledger Agent

## Render deployment
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Public endpoint: `https://YOUR-SERVICE.onrender.com/`
- Send POST JSON: `{"question":"What was the total revenue in USD from the North region in March 2026?"}`

The service fetches all order pages at startup, keeps the latest `updated_at` row for each order ID, applies the API's USD exchange rates, and counts only `paid` orders as revenue. Refund questions sum orders with status `refunded`.

Optional Render environment variables:
- `EXAM_EMAIL`
- `EXAM_QUIZ_SIGN`

Keep these values private. The quiz signature is included as a default only to make setup easier; setting it as an environment variable is preferable.
