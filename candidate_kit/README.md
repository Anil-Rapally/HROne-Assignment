# Employee Attendance & Analytics API

This project implements the required FastAPI + MongoDB employee attendance service described in the assignment and OpenAPI contract. It covers:

- employee creation and listing
- punch-in and punch-out flows with business-rule validation
- manual attendance regularization with an audit history
- MongoDB-backed analytics for monthly summaries, department summaries, late leaderboard, and daily trend reports
- an admin endpoint for raw MongoDB explain plans

## Setup

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

Set environment variables as needed:

- `MONGO_URI` (default: `mongodb://localhost:27017`)
- `MONGO_DB` (default: `attendance_db`)

## Main API

- `GET /health`
- `POST /employees`
- `GET /employees`
- `POST /attendance/punch-in`
- `POST /attendance/punch-out`
- `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

## Notes

- Employee IDs are business codes like `EMP0001`; MongoDB `_id` remains internal.
- All instants are handled as epoch milliseconds at the API boundary.
- Derived attendance values are computed and stored by the API and then used for analytics.
