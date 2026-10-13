# DECISIONS.md

## 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employee identifiers, and a unique compound index on `attendance_logs.emp_code` and `date` to guarantee one attendance record per employee per attendance day. The employee indexes on `(department, emp_code)` and `(joined_on, department)` support department filtering and joined-date eligibility. Attendance indexes support date-based sorting, employee history, and status/date filtering. I avoided adding an index without a matching query need because indexes consume storage and increase write costs.

## 2. Punch-in race

Both requests may pass the employee lookup, but the unique compound index allows only one insert for the same employee and attendance date. The successful request returns `201`; the duplicate-key error is converted to `409 Conflict`.

## 3. Leaderboard ties

The leaderboard sorts employees by `total_late_minutes` descending, then `late_count` descending, and finally `emp_code` ascending. This provides deterministic ordering when employees have the same totals. The requested limit is applied after sorting, so the response contains at most the specified number of employees.

## 4. Headcount

The department summary starts from eligible employee documents and looks up attendance records. Employees without logs therefore remain in the result and count toward headcount.

## 5. Scaling 100×

I would benchmark realistic workloads, inspect execution plans, and add pagination or caching where justified. I would also monitor slow aggregations and database resource usage before choosing further optimizations.