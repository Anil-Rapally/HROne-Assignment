# uvicorn app.main:app --port 8000
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
EMPLOYEE_CODE_RE = r"^EMP\d{4,6}$"

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index("date")
    db.attendance_logs.create_index("emp_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_out", ASCENDING)])


@app.on_event("startup")
def startup_event() -> None:
    ensure_indexes()


def round_half_up(value: float | int | Decimal, digits: int) -> float:
    quant = Decimal("1") if digits == 0 else Decimal("1").scaleb(-digits)
    q = Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)
    return float(q)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def validate_epoch_millis(value: int | None, *, allow_none: bool = False) -> int | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError("Value is required")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Epoch milliseconds must be an integer")
    if value < 100000000000 or value > 4102444800000:
        raise ValueError("Epoch milliseconds out of range")
    return value


def epoch_to_datetime(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0)


def datetime_to_epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def localize_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def get_shift_bounds_for_date(emp: dict[str, Any], attendance_date: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(attendance_date)
    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))
    shift_start = datetime.combine(day, time(start_h, start_m), tzinfo=IST)
    shift_end = datetime.combine(day, time(end_h, end_m), tzinfo=IST)
    if end_h * 60 + end_m <= start_h * 60 + start_m:
        shift_end += timedelta(days=1)
    return shift_start, shift_end


def get_attendance_date_for_punch(emp: dict[str, Any], punched_at: datetime) -> str:
    local = localize_ist(punched_at)
    day = local.date()
    shift_start_h, shift_start_m = map(int, emp["shift_start"].split(":"))
    shift_end_h, shift_end_m = map(int, emp["shift_end"].split(":"))
    overnight = shift_end_h * 60 + shift_end_m <= shift_start_h * 60 + shift_start_m
    if overnight:
        if local.time() < time(shift_end_h, shift_end_m):
            return (day - timedelta(days=1)).isoformat()
        return day.isoformat()
    return day.isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start_dt: datetime) -> int:
    delta_minutes = int((punch_in_dt - shift_start_dt).total_seconds() // 60)
    return delta_minutes if delta_minutes > 10 else 0


def compute_overtime_minutes(punch_out_dt: datetime, shift_end_dt: datetime) -> int:
    delta_minutes = int((punch_out_dt - shift_end_dt).total_seconds() // 60)
    if delta_minutes < 30:
        return 0
    return delta_minutes


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    hours = (punch_out_dt - punch_in_dt).total_seconds() / 3600
    return round_half_up(hours, 2)


def compute_half_day(work_hours: float | None) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def serialize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, value in (entry.get("changes") or {}).items():
        changes[key] = {
            "from": datetime_to_epoch(value.get("from")) if isinstance(value.get("from"), datetime) else value.get("from"),
            "to": datetime_to_epoch(value.get("to")) if isinstance(value.get("to"), datetime) else value.get("to"),
        }
    return {
        "at": datetime_to_epoch(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    data = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in data and isinstance(data["created_at"], datetime):
        data["created_at"] = datetime_to_epoch(data["created_at"])
    return data


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    data = {k: v for k, v in doc.items() if k != "_id"}
    if data.get("punch_in") is not None and isinstance(data["punch_in"], datetime):
        data["punch_in"] = datetime_to_epoch(data["punch_in"])
    if data.get("punch_out") is not None and isinstance(data["punch_out"], datetime):
        data["punch_out"] = datetime_to_epoch(data["punch_out"])
    if "history" in data:
        data["history"] = [serialize_history_entry(h) for h in data.get("history") or []]
    return data


def build_record_derived_values(emp: dict[str, Any], status: str, punch_in_dt: datetime | None, punch_out_dt: datetime | None, attendance_date: str) -> tuple[int, float | None, int, bool]:
    if status in PRESENCE_STATUSES and punch_in_dt is not None:
        shift_start_dt, shift_end_dt = get_shift_bounds_for_date(emp, attendance_date)
        late_minutes = compute_late_minutes(punch_in_dt, shift_start_dt)
        if punch_out_dt is not None:
            work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
            overtime_minutes = compute_overtime_minutes(punch_out_dt, shift_end_dt)
            half_day = compute_half_day(work_hours)
            return late_minutes, work_hours, overtime_minutes, half_day
        return late_minutes, None, 0, False
    return 0, None, 0, False


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value: str) -> str:
        import re

        if re.fullmatch(EMPLOYEE_CODE_RE, value) is None:
            raise ValueError("emp_code must match ^EMP\\d{4,6}$")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift(cls, value: str) -> str:
        import re

        if re.fullmatch(r"^([01]\d|2[0-3]):[0-5]\d$", value) is None:
            raise ValueError("Shift time must be HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:  # pragma: no cover
            raise ValueError("joined_on must be YYYY-MM-DD") from exc
        return value


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: int | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return validate_epoch_millis(value)


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: int

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: int) -> int:
        return validate_epoch_millis(value)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: int | None = None
    punch_out: int | None = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch_fields(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return validate_epoch_millis(value)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    existing = db.employees.find_one({"emp_code": body.emp_code})
    if existing:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        result = db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    created = db.employees.find_one({"_id": result.inserted_id})
    return serialize_employee(created)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size))
    for item in items:
        if "created_at" in item and isinstance(item["created_at"], datetime):
            item["created_at"] = datetime_to_epoch(item["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(status_code=422, detail="status must be PRESENT, WFH or ON_DUTY")
    punched_at = epoch_to_datetime(body.punched_at) if body.punched_at is not None else utc_now()
    punched_at = punched_at.replace(microsecond=0)
    attendance_date = get_attendance_date_for_punch(emp, punched_at)
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_date}):
        raise HTTPException(status_code=409, detail="already punched in for this date")
    shift_start_dt, _ = get_shift_bounds_for_date(emp, attendance_date)
    late_minutes = compute_late_minutes(punched_at, shift_start_dt)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_attendance(db.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_date}))


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = epoch_to_datetime(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}, "punch_out": None},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record["status"] not in PRESENCE_STATUSES:
        raise HTTPException(status_code=422, detail="record is not a presence status")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")
    punch_in_dt = record["punch_in"]
    if punched_at <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    hours_diff = (punched_at - punch_in_dt).total_seconds()
    if hours_diff > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")
    shift_start_dt, shift_end_dt = get_shift_bounds_for_date(emp, record["date"])
    late_minutes = compute_late_minutes(punch_in_dt, shift_start_dt)
    work_hours = compute_work_hours(punch_in_dt, punched_at)
    overtime_minutes = compute_overtime_minutes(punched_at, shift_end_dt)
    half_day = compute_half_day(work_hours)
    update_result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )
    if update_result.matched_count == 0:
        raise HTTPException(status_code=409, detail="record already punched out")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
        if date_from and date_to and date.fromisoformat(date_from) > date.fromisoformat(date_to):
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    if status is not None:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    items = list(db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size))
    return {
        "items": [serialize_attendance(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="attendance record not found")

    new_status = body.status if body.status is not None else record.get("status")
    new_punch_in = record.get("punch_in") if body.punch_in is None else epoch_to_datetime(body.punch_in)
    new_punch_out = record.get("punch_out") if body.punch_out is None else epoch_to_datetime(body.punch_out)

    if new_status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE records must not include punch times")
    if new_status in PRESENCE_STATUSES and new_punch_in is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")
    if new_punch_in is not None:
        candidate_date = get_attendance_date_for_punch(emp, new_punch_in)
        if candidate_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")
    if new_punch_in is not None and new_punch_out is not None:
        if new_punch_out <= new_punch_in:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    final_status = new_status
    previous_status = record.get("status")
    previous_punch_in = record.get("punch_in")
    previous_punch_out = record.get("punch_out")
    if final_status in {"ABSENT", "LEAVE"}:
        final_punch_in = None
        final_punch_out = None
    else:
        final_punch_in = new_punch_in
        final_punch_out = new_punch_out

    if final_status in PRESENCE_STATUSES and final_punch_in is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")

    derived_late, derived_work_hours, derived_overtime, derived_half = build_record_derived_values(
        emp,
        final_status,
        final_punch_in,
        final_punch_out,
        date,
    )

    new_values: dict[str, Any] = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": derived_work_hours,
        "late_minutes": derived_late,
        "overtime_minutes": derived_overtime,
        "half_day": derived_half,
    }
    changes: dict[str, Any] = {}
    for field in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        before = record.get(field)
        after = new_values.get(field)
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")
    history_entry = {
        "at": utc_now(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {
            key: {"from": value["from"], "to": value["to"]} for key, value in changes.items()
        },
    }
    update_result = db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {"$set": new_values, "$push": {"history": history_entry}},
    )
    if update_result.matched_count == 0:
        raise HTTPException(status_code=409, detail="concurrent update detected")
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


def month_to_date_range(month: str) -> tuple[str, str]:
    try:
        dt = datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid month") from exc
    start = dt.strftime("%Y-%m-01")
    end = (dt.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end.strftime("%Y-%m-%d")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="employee not found")
    month_start, month_end = month_to_date_range(month)
    first_day = date.fromisoformat(month_start)
    last_day = date.fromisoformat(month_end)
    working_days = 0
    for offset in range((last_day - first_day).days + 1):
        cur = first_day + timedelta(days=offset)
        if cur.weekday() < 5 and cur >= date.fromisoformat(employee["joined_on"]):
            working_days += 1
    docs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}))
    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    for doc in docs:
        if doc["status"] in PRESENCE_STATUSES and date.fromisoformat(doc["date"]).weekday() < 5:
            present_days += 0.5 if doc.get("half_day") else 1.0
        if doc["status"] == "LEAVE":
            leave_days += 1
        if doc.get("late_minutes", 0) > 0:
            late_count += 1
        total_late_minutes += int(doc.get("late_minutes") or 0)
        total_overtime_minutes += int(doc.get("overtime_minutes") or 0)
    attendance_pct = None
    if working_days:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(float(present_days), 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
) -> dict[str, Any]:
    month_start, month_end = month_to_date_range(month)
    employee_filter = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_filter["department"] = department
    employees = list(db.employees.find(employee_filter, {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1}))
    dept_map = {}
    for emp in employees:
        dept_map.setdefault(emp["department"], []).append(emp["emp_code"])
    if not dept_map:
        return {"month": month, "items": []}
    all_items = []
    for dept in sorted(dept_map):
        employees_in_dept = dept_map[dept]
        docs = list(db.attendance_logs.find({"emp_code": {"$in": employees_in_dept}, "date": {"$gte": month_start, "$lte": month_end}}))
        present_days = 0.0
        late_count = 0
        total_late_minutes = 0
        leave_count = 0
        on_duty_count = 0
        work_hours_values = []
        for doc in docs:
            day = date.fromisoformat(doc["date"])
            if doc["status"] in PRESENCE_STATUSES and day.weekday() < 5:
                present_days += 0.5 if doc.get("half_day") else 1.0
            if doc.get("late_minutes", 0) > 0:
                late_count += 1
            total_late_minutes += int(doc.get("late_minutes") or 0)
            if doc["status"] == "LEAVE":
                leave_count += 1
            if doc["status"] == "ON_DUTY":
                on_duty_count += 1
            if doc["status"] in PRESENCE_STATUSES and doc.get("work_hours") is not None:
                work_hours_values.append(float(doc["work_hours"]))
        headcount = len([emp for emp in employees if emp["department"] == dept and date.fromisoformat(emp["joined_on"]) <= date.fromisoformat(month_end)])
        if headcount == 0:
            continue
        avg_work_hours = None
        if work_hours_values:
            avg_work_hours = round_half_up(sum(work_hours_values) / len(work_hours_values), 2)
        all_items.append(
            {
                "department": dept,
                "headcount": headcount,
                "present_days": round_half_up(float(present_days), 2),
                "avg_work_hours": avg_work_hours,
                "late_count": late_count,
                "total_late_minutes": total_late_minutes,
                "leave_count": leave_count,
                "on_duty_count": on_duty_count,
            }
        )
    return {"month": month, "items": all_items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
) -> dict[str, Any]:
    month_start, month_end = month_to_date_range(month)
    pipeline = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
    ]
    if department is not None:
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
        pipeline[0]["$match"]["emp_code"] = {"$in": emp_codes}
    rows = list(db.attendance_logs.aggregate(pipeline))
    employee_lookup = {e["emp_code"]: e for e in db.employees.find({"emp_code": {"$in": [r["_id"] for r in rows]}}, {"_id": 0})}
    ranked = []
    for row in rows:
        emp = employee_lookup.get(row["_id"])
        if not emp:
            continue
        ranked.append({
            "emp_code": row["_id"],
            "name": emp["name"],
            "department": emp["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        })
    ranked.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
    results = []
    if ranked:
        rank = 1
        prev_total = ranked[0]["total_late_minutes"]
        for idx, row in enumerate(ranked, start=1):
            if idx > 1 and row["total_late_minutes"] != prev_total:
                rank = idx
                prev_total = row["total_late_minutes"]
            results.append({
                "rank": rank,
                **row,
            })
    filtered = [r for r in results if r["rank"] <= limit]
    return {"month": month, "items": filtered}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
) -> dict[str, Any]:
    emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    if not emp_codes:
        raise HTTPException(status_code=404, detail="department not found")
    from_date = date.fromisoformat(from_)
    to_date = date.fromisoformat(to)
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="trend range must not exceed 92 days")
    start_dt = datetime.combine(from_date, time.min, tzinfo=IST)
    end_dt = datetime.combine(to_date, time.max, tzinfo=IST)
    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_, "$lte": to},
            }
        },
        {
            "$project": {
                "_id": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}},
                "status": 1,
                "late_minutes": 1,
                "half_day": 1,
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
        {"$sort": {"_id": 1}},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id", "timezone": "Asia/Kolkata"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
    ]
    records = list(db.attendance_logs.aggregate(pipeline))
    records_by_date = {r["date"]: r for r in records}
    rows = []
    for single_day in (from_date + timedelta(days=i) for i in range((to_date - from_date).days + 1)):
        day_str = single_day.isoformat()
        rec = records_by_date.get(day_str, {"date": day_str, "present_count": 0, "late_count": 0})
        headcount = sum(1 for e in db.employees.find({"department": department, "joined_on": {"$lte": day_str}}, {"_id": 0, "emp_code": 1}))
        is_working_day = single_day.weekday() < 5
        attendance_rate = None
        if is_working_day and headcount > 0:
            attendance_rate = round_half_up(float(rec["present_count"]) / headcount, 4)
            if float(rec["present_count"]) == 0:
                attendance_rate = 0.0
        rows.append({
            "date": day_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": float(rec["present_count"]),
            "late_count": int(rec["late_count"]),
            "attendance_rate": attendance_rate,
        })
    recent_rows = []
    output = []
    for row in rows:
        recent_rows.append(row)
        if len(recent_rows) > 7:
            recent_rows.pop(0)
        values = [item["attendance_rate"] for item in recent_rows if item["attendance_rate"] is not None]
        row["moving_avg_7d"] = None if not values else round_half_up(sum(values) / len(values), 4)
        output.append(row)
    return {"department": department, "items": [{
        "date": row["date"],
        "is_working_day": row["is_working_day"],
        "headcount": row["headcount"],
        "present_count": row["present_count"],
        "late_count": row["late_count"],
        "attendance_rate": row["attendance_rate"],
        "moving_avg_7d": row["moving_avg_7d"],
    } for row in output]}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status is not None:
            query["status"] = status
        collection = "attendance_logs"
        command = {"find": collection, "filter": query, "sort": [("date", -1), ("emp_code", 1)], "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        collection = "attendance_logs"
        month_start, month_end = month_to_date_range(month)
        command = {
            "aggregate": collection,
            "pipeline": [
                {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
                {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}}, "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": "$late_minutes"}, "total_overtime_minutes": {"$sum": "$overtime_minutes"}}},
            ],
            "cursor": {},
        }
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "attendance_logs"
        month_start, month_end = month_to_date_range(month)
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
            {"$group": {"_id": "$emp_code", "present_days": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}, "total_late_minutes": {"$sum": "$late_minutes"}, "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}}, "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}}},
        ]
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        collection = "attendance_logs"
        month_start, month_end = month_to_date_range(month)
        match = {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}
        if department is not None:
            emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
            match["emp_code"] = {"$in": emp_codes}
        command = {"aggregate": collection, "pipeline": [{"$match": match}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}}], "cursor": {}}
    elif endpoint == "department_trend":
        if department is None or from_ is None or to is None:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        collection = "attendance_logs"
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
        start_dt = datetime.combine(date.fromisoformat(from_), time.min, tzinfo=IST)
        end_dt = datetime.combine(date.fromisoformat(to), time.max, tzinfo=IST)
        command = {
            "aggregate": collection,
            "pipeline": [
                {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_, "$lte": to}}},
                {"$project": {"_id": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}, "status": 1, "late_minutes": 1, "half_day": 1}},
                {"$group": {"_id": "$_id", "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": ["$half_day", 0.5, 1]}, 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
                {"$sort": {"_id": 1}},
                {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
                {"$project": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id", "timezone": "Asia/Kolkata"}}, "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
            ],
            "cursor": {},
        }
    else:
        raise HTTPException(status_code=422, detail="unsupported endpoint")
    explain = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": explain}


# Keep import compatibility for `uvicorn app.main:app --port 8000`.
