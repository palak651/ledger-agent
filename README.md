# Acme Appliances Ledger Agent (v3)

FastAPI service for the ledger-agent assignment.

## Deploy
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Public POST endpoint: `/`
- JSON body: `{"question":"How many different customers placed at least one paid order for a Blender?"}`
- Response: `{"answer": 205}` (the actual answer depends on the live ledger data; the service computes it from the API at startup).

## Notes
- Fetches all pages of `/orders` and `/rates`; uses retries and checks response shape.
- Keeps the latest `updated_at` row per order ID.
- Uses Asia/Kolkata for date filtering.
- Uses the ledger's USD-per-unit rates and enforces paid-only for revenue questions.
- Ordinary order counts include all statuses unless a status is specified.
- Natural-language parsing is rule-based; it should be tested against the exact question set before submission. It cannot guarantee correct interpretation of every possible unseen wording.

Set `EXAM_EMAIL` and `EXAM_QUIZ_SIGN` as environment variables if required. Defaults are the assignment values currently known.
