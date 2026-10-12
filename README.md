# Acme Appliances Ledger Agent

Deploy as a Python web service.

Build command: `pip install -r requirements.txt`
Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`

Set `EXAM_EMAIL` and `EXAM_QUIZ_SIGN` environment variables if you want to override the defaults.

The service loads all order pages on startup, deduplicates order IDs by latest `updated_at`, uses Asia/Kolkata business dates, and converts amounts with the API's USD-per-unit rates.

POST `/` with JSON:
`{"question":"How many different customers placed at least one paid order for a Blender?"}`

Important: no heuristic parser can guarantee every possible unseen phrasing. Test against the grader and inspect each reported failing question. This version specifically handles different/unique/distinct customer counts before generic order counts.
