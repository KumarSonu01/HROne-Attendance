"""Employee Attendance & Analytics API.

Run from the repository root with:
    uvicorn app.main:app --port 8000

Configuration is read from MONGO_URI and MONGO_DB. The .env file is optional.
"""
import calendar
import json
import os
import re
from bson import json_util
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # Environment variables already present in the process take priority.

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Date/time and serialization helpers
# --------------------------------------------------------------------------- #
def truncate_to_seconds(value: datetime) -> datetime:
    """Truncate an instant to whole seconds and normalize it to UTC."""
    if value.tzinfo is None:
        # MongoDB values should be UTC; treat legacy naive datetimes as UTC.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    value = truncate_to_seconds(value)
    return int(value.timestamp() * 1000)


def from_epoch_ms(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    # Pydantic validates the integer range/type at the request boundary.
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def parse_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD")
    return parsed


def parse_shift_time(value: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("time must use 24-hour HH:MM format")
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def shift_start_instant(attendance_day: date, shift_start: str, shift_end: str, punch_in: datetime) -> datetime:
    """Return the shift start instant associated with this attendance record."""
    local_punch = as_ist(punch_in)
    start_clock = parse_shift_time(shift_start)
    end_clock = parse_shift_time(shift_end)
    shift_day = attendance_day
    # If the caller did not supply the stored attendance date, infer it from
    # the punch instant. Otherwise the stored date is already the shift-start day.
    if end_clock <= start_clock and local_punch.time().replace(tzinfo=None) < end_clock:
        if attendance_day == local_punch.date():
            shift_day -= timedelta(days=1)
    return datetime.combine(shift_day, start_clock, tzinfo=IST)



def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str = "18:30",
    attendance_day: Optional[str] = None,
) -> int:
    punch_in = truncate_to_seconds(punch_in)
    local_punch = as_ist(punch_in)
    day = parse_date(attendance_day) if attendance_day else local_punch.date()

    start = shift_start_instant(day, shift_start, shift_end, punch_in)

    difference_seconds = int(
        (punch_in - start.astimezone(UTC)).total_seconds()
    )

    # Grace is 10 minutes; lateness must be strictly greater than 10 minutes.
    return difference_seconds // 60 if difference_seconds > 600 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    punch_in = truncate_to_seconds(punch_in)
    punch_out = truncate_to_seconds(punch_out)
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = (seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_end: str, attendance_day: str, shift_start: str = "09:30") -> int:
    punch_out = truncate_to_seconds(punch_out)
    day = parse_date(attendance_day)
    start_clock = parse_shift_time(shift_start)
    end_clock = parse_shift_time(shift_end)
    end_day = day + timedelta(days=1) if end_clock <= start_clock else day
    end_ist = datetime.combine(end_day, end_clock, tzinfo=IST)
    minutes = int((punch_out - end_ist.astimezone(UTC)).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def serialize_employee(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    if "created_at" in result:
        result["created_at"] = epoch_ms(result["created_at"])
    return result


def serialize_attendance(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    for key in ("punch_in", "punch_out"):
        result[key] = epoch_ms(result.get(key))
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    history = []
    for entry in result.get("history", []) or []:
        item = dict(entry)
        item["at"] = epoch_ms(item.get("at"))
        changes = {}
        for field, change in (item.get("changes") or {}).items():
            pair = dict(change)
            if field in {"punch_in", "punch_out"}:
                pair["from"] = epoch_ms(pair.get("from"))
                pair["to"] = epoch_ms(pair.get("to"))
            changes[field] = pair
        item["changes"] = changes
        history.append(item)
    result["history"] = history
    return result


# --------------------------------------------------------------------------- #
# MongoDB indexes and lifecycle
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Create required indexes safely on every application startup."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], name="ix_attendance_employee_date_desc")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)], name="ix_attendance_status_date")


@app.on_event("startup")
def startup() -> None:
    ensure_indexes()


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_clock(cls, value: str) -> str:
        parse_shift_time(value)
        return value


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000, strict=True)


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000, strict=True)
    punch_out: Optional[int] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000, strict=True)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Existing endpoints (foundation pass)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {"department": department} if department else {}
    skip = (page - 1) * page_size
    try:
        total = db.employees.count_documents(query)
        docs = list(
            db.employees.find(query, {"_id": 0})
            .sort("emp_code", ASCENDING)
            .skip(skip)
            .limit(page_size)
        )
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    return {
        "items": [serialize_employee(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# Remaining endpoints are implemented in the next stages.
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = truncate_to_seconds(from_epoch_ms(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    local_punch = as_ist(punched_at)
    start_clock = parse_shift_time(emp["shift_start"])
    end_clock = parse_shift_time(emp["shift_end"])
    attendance_day = local_punch.date()
    if end_clock <= start_clock and local_punch.time().replace(tzinfo=None) < end_clock:
        attendance_day -= timedelta(days=1)
    day_string = attendance_day.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": day_string,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], emp["shift_end"], day_string),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this attendance date")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    skip = (page - 1) * page_size
    try:
        total = db.attendance_logs.count_documents(query)
        docs = list(
            db.attendance_logs.find(query, {"_id": 0})
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")
    return {
        "items": [serialize_attendance(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    punched_at = truncate_to_seconds(
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC)
    )

    try:
        # Find the latest punch-in at or before the requested punch-out.
        record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": punched_at},
            },
            sort=[("punch_in", DESCENDING)],
        )

        if record is None:
            employee_exists = db.employees.find_one(
                {"emp_code": body.emp_code},
                {"_id": 1},
            )
            if employee_exists is None:
                raise HTTPException(status_code=404, detail="employee not found")
            raise HTTPException(status_code=404, detail="no matching punch-in found")

        if record.get("punch_out") is not None:
            raise HTTPException(status_code=409, detail="attendance already punched out")

        elapsed = (punched_at - record["punch_in"]).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(
                status_code=422,
                detail="punch-out must be after punch-in and within 24 hours",
            )

        employee = db.employees.find_one({"emp_code": body.emp_code})
        if employee is None:
            raise HTTPException(status_code=404, detail="employee not found")

        work_hours = compute_work_hours(record["punch_in"], punched_at)
        half_day = work_hours < 4.50
        overtime_minutes = compute_overtime(
            punched_at,
            employee["shift_end"],
            record["date"],
            employee["shift_start"],
        )

        # Conditional update prevents two concurrent requests from closing
        # the same attendance record successfully.
        result = db.attendance_logs.update_one(
            {
                "_id": record["_id"],
                "punch_out": None,
            },
            {
                "$set": {
                    "punch_out": punched_at,
                    "work_hours": work_hours,
                    "half_day": half_day,
                    "overtime_minutes": overtime_minutes,
                }
            },
        )

        if result.modified_count != 1:
            raise HTTPException(
                status_code=409,
                detail="attendance already punched out",
            )

        updated = db.attendance_logs.find_one({"_id": record["_id"]})
        return serialize_attendance(updated)

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")



@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    attendance_date = parse_date(date, "date")
    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    try:
        record = db.attendance_logs.find_one(
            {"emp_code": emp_code, "date": attendance_date.isoformat()}
        )
        if record is None:
            raise HTTPException(status_code=404, detail="attendance record not found")

        updates = {}
        if body.status is not None:
            updates["status"] = body.status
        if body.punch_in is not None:
            updates["punch_in"] = truncate_to_seconds(from_epoch_ms(body.punch_in))
        if body.punch_out is not None:
            updates["punch_out"] = truncate_to_seconds(from_epoch_ms(body.punch_out))

        current = {
            "status": record.get("status"),
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "work_hours": record.get("work_hours"),
            "late_minutes": record.get("late_minutes", 0),
            "overtime_minutes": record.get("overtime_minutes", 0),
            "half_day": record.get("half_day", False),
        }

        target = {**current, **updates}
        status = target["status"]
        punch_in = target["punch_in"]
        punch_out = target["punch_out"]

        if status in {"ABSENT", "LEAVE"}:
            punch_in = None
            punch_out = None
        elif punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="presence status requires punch_in",
            )

        if punch_out is not None:
            if punch_in is None or punch_out <= punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in",
                )
            if (punch_out - punch_in).total_seconds() > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be within 24 hours of punch_in",
                )

        target["punch_in"] = punch_in
        target["punch_out"] = punch_out

        if status in {"ABSENT", "LEAVE"}:
            target.update({
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            })
        else:
            target["late_minutes"] = compute_late_minutes(
                punch_in,
                employee["shift_start"],
                employee["shift_end"],
                attendance_date.isoformat(),
            )
            if punch_out is None:
                target.update({
                    "work_hours": None,
                    "overtime_minutes": 0,
                    "half_day": False,
                })
            else:
                target["work_hours"] = compute_work_hours(punch_in, punch_out)
                target["overtime_minutes"] = compute_overtime(
                    punch_out,
                    employee["shift_end"],
                    attendance_date.isoformat(),
                    employee["shift_start"],
                )
                target["half_day"] = target["work_hours"] < 4.50

        fields = (
            "status",
            "punch_in",
            "punch_out",
            "work_hours",
            "late_minutes",
            "overtime_minutes",
            "half_day",
        )
        changes = {}
        for field in fields:
            old = current[field]
            new = target[field]
            if old != new:
                changes[field] = {"from": old, "to": new}

        if not changes:
            raise HTTPException(status_code=422, detail="regularization makes no changes")

        history_entry = {
            "at": truncate_to_seconds(datetime.now(UTC)),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes,
        }

        # Use a conditional update so a concurrent correction cannot silently
        # overwrite another correction's audit history.
        result = db.attendance_logs.update_one(
            {
                "_id": record["_id"],
                "history": record.get("history", []),
                "status": record.get("status"),
            },
            {
                "$set": {
                    field: target[field]
                    for field in fields
                },
                "$push": {"history": history_entry},
            },
        )

        if result.modified_count != 1:
            raise HTTPException(
                status_code=409,
                detail="attendance record changed concurrently; retry",
            )

        updated = db.attendance_logs.find_one({"_id": record["_id"]})
        return serialize_attendance(updated)

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database operation failed")




def build_employee_monthly_pipeline(
    emp_code: str,
    month_start: str,
    month_end: str,
    month: str,
):
    pipeline = [
        {"$match": {"emp_code": emp_code}},

        # Calculate the employee's eligible date range.
        {
            "$set": {
                "_month_start": {
                    "$dateFromString": {
                        "dateString": month_start
                    }
                },
                "_month_end": {
                    "$dateFromString": {
                        "dateString": month_end
                    }
                },
                "_effective_start": {
                    "$cond": [
                        {"$gt": ["$joined_on", month_start]},
                        "$joined_on",
                        month_start
                    ]
                }
            }
        },
        {
            "$set": {
                "_effective_start_date": {
                    "$dateFromString": {
                        "dateString": "$_effective_start"
                    }
                }
            }
        },
        {
            "$set": {
                "_calendar_day_count": {
                    "$cond": [
                        {
                            "$lte": [
                                "$_effective_start_date",
                                "$_month_end"
                            ]
                        },
                        {
                            "$add": [
                                {
                                    "$dateDiff": {
                                        "startDate": "$_effective_start_date",
                                        "endDate": "$_month_end",
                                        "unit": "day"
                                    }
                                },
                                1
                            ]
                        },
                        0
                    ]
                }
            }
        },
        {
            "$set": {
                "_calendar_dates": {
                    "$map": {
                        "input": {
                            "$range": [0, "$_calendar_day_count"]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$_effective_start_date",
                                "unit": "day",
                                "amount": "$$offset"
                            }
                        }
                    }
                }
            }
        },
        {
            "$set": {
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_calendar_dates",
                            "as": "day",
                            "cond": {
                                "$not": [
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": "$$day"
                                            },
                                            [1, 7]
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        },

        # Fetch the employee's attendance records for this month.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code"
                                        ]
                                    },
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    }
                ],
                "as": "_attendance"
            }
        },

        # Present days count only on weekdays.
        # Half-day records contribute 0.5.
        {
            "$set": {
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$_attendance",
                                    "as": "record",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$record.status",
                                                    [
                                                        "PRESENT",
                                                        "WFH",
                                                        "ON_DUTY"
                                                    ]
                                                ]
                                            },
                                            {
                                                "$not": [
                                                    {
                                                        "$in": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$$record.date"
                                                                    }
                                                                }
                                                            },
                                                            [1, 7]
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                }
                            },
                            "as": "record",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$record.half_day", True]},
                                    0.5,
                                    1.0
                                ]
                            }
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_attendance",
                            "as": "record",
                            "cond": {
                                "$eq": ["$$record.status", "LEAVE"]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_attendance",
                            "as": "record",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$record.late_minutes", 0]},
                                    0
                                ]
                            }
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1
            }
        }
    ]
    return pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate month in YYYY-MM format.
    try:
        if (
            len(month) != 7
            or month[4] != "-"
            or not month[:4].isdigit()
            or not month[5:].isdigit()
        ):
            raise ValueError

        month_start_date = datetime.strptime(month, "%Y-%m").date()
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format"
        )

    month_start = month_start_date.isoformat()
    month_end = (
        month_start_date.replace(
            day=calendar.monthrange(
                month_start_date.year,
                month_start_date.month
            )[1]
        )
    ).isoformat()

    try:
        pipeline = build_employee_monthly_pipeline(
            emp_code=emp_code,
            month_start=month_start,
            month_end=month_end,
            month=month,
        )

        result = list(db.employees.aggregate(pipeline))

        if not result:
            raise HTTPException(
                status_code=404,
                detail="Employee not found"
            )

        summary = result[0]
        working_days = summary["working_days"]
        present_days = summary["present_days"]

        # R8: percentage rounded half-up to two decimal places.
        if working_days == 0:
            attendance_pct = None
        else:
            attendance_pct = float(
                (
                    Decimal(str(present_days))
                    * Decimal("100")
                    / Decimal(str(working_days))
                ).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP
                )
            )

        summary["attendance_pct"] = attendance_pct
        return summary

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed"
        )




def build_department_summary_pipeline(
    month_start: str,
    month_end: str,
    department: Optional[str] = None,
):
    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        # Start from employees so that employees with no logs count.
        {"$match": employee_match},

        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "joined_on": "$joined_on"
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code"
                                        ]
                                    },
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                    {
                                        "$gte": [
                                            "$date",
                                            "$$joined_on"
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                ],
                "as": "attendance"
            }
        },

        # Compute employee-level metrics before grouping by department.
        {
            "$set": {
                "_effective_start": {
                    "$cond": [
                        {"$gt": ["$joined_on", month_start]},
                        "$joined_on",
                        month_start
                    ]
                }
            }
        },
        {
            "$set": {
                "_start_date": {
                    "$dateFromString": {
                        "dateString": "$_effective_start"
                    }
                },
                "_end_date": {
                    "$dateFromString": {
                        "dateString": month_end
                    }
                }
            }
        },
        {
            "$set": {
                "_day_count": {
                    "$add": [
                        {
                            "$dateDiff": {
                                "startDate": "$_start_date",
                                "endDate": "$_end_date",
                                "unit": "day"
                            }
                        },
                        1
                    ]
                }
            }
        },
        {
            "$set": {
                "_days": {
                    "$map": {
                        "input": {"$range": [0, "$_day_count"]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": "$_start_date",
                                "unit": "day",
                                "amount": "$$offset"
                            }
                        }
                    }
                }
            }
        },
        {
            "$set": {
                "working_days": {
                    "$size": {
                        "$filter": {
                            "input": "$_days",
                            "as": "day",
                            "cond": {
                                "$not": [
                                    {
                                        "$in": [
                                            {"$dayOfWeek": "$$day"},
                                            [1, 7]
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                },
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$attendance",
                                    "as": "record",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$record.status",
                                                    [
                                                        "PRESENT",
                                                        "WFH",
                                                        "ON_DUTY"
                                                    ]
                                                ]
                                            },
                                            {
                                                "$not": [
                                                    {
                                                        "$in": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$$record.date"
                                                                    }
                                                                }
                                                            },
                                                            [1, 7]
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                }
                            },
                            "as": "record",
                            "in": {
                                "$cond": [
                                    {
                                        "$eq": [
                                            "$$record.half_day",
                                            True
                                        ]
                                    },
                                    0.5,
                                    1.0
                                ]
                            }
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$attendance",
                            "as": "record",
                            "cond": {
                                "$eq": ["$$record.status", "LEAVE"]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$attendance",
                            "as": "record",
                            "cond": {
                                "$gt": [
                                    {
                                        "$ifNull": [
                                            "$$record.late_minutes",
                                            0
                                        ]
                                    },
                                    0
                                ]
                            }
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$attendance",
                            "as": "record",
                            "in": {
                                "$ifNull": [
                                    "$$record.late_minutes",
                                    0
                                ]
                            }
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$attendance",
                            "as": "record",
                            "cond": {
                                "$eq": [
                                    "$$record.status",
                                    "ON_DUTY"
                                ]
                            }
                        }
                    }
                },
                "_work_hours_records": {
                    "$filter": {
                        "input": "$attendance",
                        "as": "record",
                        "cond": {
                            "$and": [
                                {
                                    "$in": [
                                        "$$record.status",
                                        [
                                            "PRESENT",
                                            "WFH",
                                            "ON_DUTY"
                                        ]
                                    ]
                                },
                                {
                                    "$ne": [
                                        "$$record.work_hours",
                                        None
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        },
        {
            "$set": {
                "_work_hours_sum": {
                    "$sum": {
                        "$map": {
                            "input": "$_work_hours_records",
                            "as": "record",
                            "in": "$$record.work_hours"
                        }
                    }
                },
                "_work_hours_count": {
                    "$size": "$_work_hours_records"
                }
            }
        },

        # Aggregate employees into departments.
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "leave_count": {"$sum": "$leave_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {
                    "$sum": "$total_late_minutes"
                },
                "on_duty_count": {"$sum": "$on_duty_count"},
                "_work_hours_sum": {"$sum": "$_work_hours_sum"},
                "_work_hours_count": {
                    "$sum": "$_work_hours_count"
                }
            }
        },
        {
            "$set": {
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$_work_hours_count", 0]},
                        {
                            "$divide": [
                                "$_work_hours_sum",
                                "$_work_hours_count"
                            ]
                        },
                        None
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]
    return pipeline


def build_late_leaderboard_pipeline(
    month_start: str,
    month_end: str,
    department: Optional[str],
    limit: int,
):
    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "joined_on": "$joined_on",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                    {"$gte": ["$date", "$$joined_on"]},
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "late_count": {"$sum": 1},
                            "total_late_minutes": {
                                "$sum": {
                                    "$ifNull": ["$late_minutes", 0]
                                }
                            },
                        }
                    },
                ],
                "as": "late_stats",
            }
        },
        {
            "$set": {
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$late_stats.late_count", 0]},
                        0,
                    ]
                },
                "total_late_minutes": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$late_stats.total_late_minutes",
                                0,
                            ]
                        },
                        0,
                    ]
                },
            }
        },
        {"$match": {"late_count": {"$gt": 0}}},
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "late_count": 1,
                "total_late_minutes": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "late_count": -1,
                "emp_code": 1,
            }
        },
        {"$limit": limit},
    ]
    return pipeline


def build_department_trend_pipeline(
    department: str,
    start_str: str,
    end_str: str,
    day_count: int,
):
    pipeline = [
        {
            "$match": {
                "department": department,
                "joined_on": {"$lte": end_str},
            }
        },
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$set": {
                "employee_codes": {
                    "$map": {
                        "input": "$employees",
                        "as": "employee",
                        "in": "$$employee.emp_code",
                    }
                },
                "offsets": {"$range": [0, day_count]},
            }
        },
        {"$unwind": "$offsets"},
        {
            "$set": {
                "date_value": {
                    "$dateAdd": {
                        "startDate": {
                            "$dateFromString": {
                                "dateString": start_str,
                            }
                        },
                        "unit": "day",
                        "amount": "$offsets",
                    }
                }
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_value",
                    }
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "employee",
                            "cond": {
                                "$lte": [
                                    "$$employee.joined_on",
                                    {
                                        "$dateToString": {
                                            "format": "%Y-%m-%d",
                                            "date": "$date_value",
                                        }
                                    },
                                ]
                            },
                        }
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_value"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_value"}, 6]},
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$date",
                    "codes": "$employee_codes",
                    "employees": "$employees",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {
                                        "$anyElementTrue": {
                                            "$map": {
                                                "input": "$$employees",
                                                "as": "employee",
                                                "in": {
                                                    "$and": [
                                                        {
                                                            "$eq": [
                                                                "$$employee.emp_code",
                                                                "$emp_code",
                                                            ]
                                                        },
                                                        {
                                                            "$lte": [
                                                                "$$employee.joined_on",
                                                                "$$day",
                                                            ]
                                                        },
                                                    ]
                                                },
                                            }
                                        }
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$in": [
                                            "$$log.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$$log.half_day", True]},
                                            0.5,
                                            1.0,
                                        ]
                                    },
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_value": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    # Validate month in YYYY-MM format.
    try:
        if (
            len(month) != 7
            or month[4] != "-"
            or not month[:4].isdigit()
            or not month[5:].isdigit()
        ):
            raise ValueError

        month_start_date = datetime.strptime(month, "%Y-%m").date()
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format"
        )

    month_start = month_start_date.isoformat()
    month_end = month_start_date.replace(
        day=calendar.monthrange(
            month_start_date.year,
            month_start_date.month
        )[1]
    ).isoformat()

    try:
        pipeline = build_department_summary_pipeline(
            month_start=month_start,
            month_end=month_end,
            department=department,
        )
        results = list(db.employees.aggregate(pipeline))

        # R8: round averages to two decimal places, half-up.
        for item in results:
            average = item["avg_work_hours"]
            if average is not None:
                item["avg_work_hours"] = float(
                    Decimal(str(average)).quantize(
                        Decimal("0.01"),
                        rounding=ROUND_HALF_UP
                    )
                )

        return results

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed"
        )



@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    # Validate YYYY-MM.
    try:
        if (
            len(month) != 7
            or month[4] != "-"
            or not month[:4].isdigit()
            or not month[5:].isdigit()
        ):
            raise ValueError

        month_start_date = datetime.strptime(month, "%Y-%m").date()

    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

    month_start = month_start_date.isoformat()
    month_end = month_start_date.replace(
        day=calendar.monthrange(
            month_start_date.year,
            month_start_date.month,
        )[1]
    ).isoformat()

    try:
        pipeline = build_late_leaderboard_pipeline(
            month_start=month_start,
            month_end=month_end,
            department=department,
            limit=limit,
        )
        return list(db.employees.aggregate(pipeline))

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database operation failed",
        )



@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")

    if end < start:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    day_count = (end - start).days + 1
    if day_count > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    start_str = start.isoformat()
    end_str = end.isoformat()

    try:
        if db.employees.find_one(
            {"department": department},
            {"_id": 1},
        ) is None:
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        pipeline = build_department_trend_pipeline(
            department=department,
            start_str=start_str,
            end_str=end_str,
            day_count=day_count,
        )
        rows = list(db.employees.aggregate(pipeline))

        # Apply the API's required half-up rounding to four decimal places.
        for row in rows:
            for field in ("attendance_rate", "moving_avg_7d"):
                value = row.get(field)
                if value is not None:
                    row[field] = float(
                        Decimal(str(value)).quantize(
                            Decimal("0.0001"),
                            rounding=ROUND_HALF_UP,
                        )
                    )

        return rows

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable",
        )





@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
):
    import re
    from copy import deepcopy

    supported = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in supported:
        raise HTTPException(status_code=422, detail="Unsupported endpoint")

    def validate_month(value: Optional[str]) -> str:
        if not value or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
            raise HTTPException(
                status_code=422,
                detail="month must be in YYYY-MM format",
            )
        return value

    def month_bounds(value: str):
        parsed = datetime.strptime(value, "%Y-%m").date()
        start = parsed.isoformat()
        end = parsed.replace(
            day=calendar.monthrange(parsed.year, parsed.month)[1]
        ).isoformat()
        return start, end

    try:
        if endpoint == "attendance_list":
            if date_from:
                parse_date(date_from, "date_from")
            if date_to:
                parse_date(date_to, "date_to")
            if date_from and date_to and date_from > date_to:
                raise HTTPException(
                    status_code=422,
                    detail="date_from must be on or before date_to",
                )

            query = {}
            if emp_code:
                query["emp_code"] = emp_code
            if status:
                query["status"] = status
            if date_from or date_to:
                query["date"] = {}
                if date_from:
                    query["date"]["$gte"] = date_from
                if date_to:
                    query["date"]["$lte"] = date_to

            command = {
                "find": "attendance_logs",
                "filter": query,
                "projection": {"_id": 0},
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            }
            collection = "attendance_logs"

        elif endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(
                    status_code=422,
                    detail="emp_code is required",
                )
            month_value = validate_month(month)
            start, end = month_bounds(month_value)

            pipeline = build_employee_monthly_pipeline(
                emp_code=emp_code,
                month_start=start,
                month_end=end,
                month=month_value,
            )
            command = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection = "employees"

        elif endpoint == "department_summary":
            month_value = validate_month(month)
            start, end = month_bounds(month_value)
            pipeline = build_department_summary_pipeline(
                month_start=start,
                month_end=end,
                department=department,
            )
            command = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection = "employees"

        elif endpoint == "late_leaderboard":
            month_value = validate_month(month)
            start, end = month_bounds(month_value)
            pipeline = build_late_leaderboard_pipeline(
                month_start=start,
                month_end=end,
                department=department,
                limit=limit,
            )
            command = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection = "employees"

        else:  # department_trend
            if not department:
                raise HTTPException(
                    status_code=422,
                    detail="department is required",
                )
            if not from_date or not to_date:
                raise HTTPException(
                    status_code=422,
                    detail="'from' and 'to' are required",
                )

            start_date = parse_date(from_date, "from")
            end_date = parse_date(to_date, "to")
            if end_date < start_date:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid date range; maximum range is 92 days",
                )
            day_count = (end_date - start_date).days + 1
            if day_count > 92:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid date range; maximum range is 92 days",
                )

            start, end = start_date.isoformat(), end_date.isoformat()
            if db.employees.find_one(
                {"department": department},
                {"_id": 1},
            ) is None:
                raise HTTPException(
                    status_code=404,
                    detail="Department not found",
                )

            pipeline = build_department_trend_pipeline(
                department=department,
                start_str=start,
                end_str=end,
                day_count=day_count,
            )
            command = {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            }
            collection = "employees"

        explain = db.command({
            "explain": command,
            "verbosity": "executionStats",
        })
        explain_json = json.loads(json_util.dumps(explain))
        return {
            "endpoint": endpoint,
            "collection": collection,
            "explain": explain_json
        }

    except HTTPException:
        raise
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database explain operation failed",
        )
