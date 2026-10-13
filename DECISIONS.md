# \# DECISIONS.md

# 

# 1\. \*\*Indexes.\*\* I created a unique index on `employees.emp\_code` to prevent duplicate employee identifiers, and a unique compound index on `attendance\_logs.emp\_code` and `date` to guarantee one attendance record per employee per attendance day. The employee indexes on `(department, emp\_code)` and `(joined\_on, department)` support department filtering and joined-date eligibility. Attendance indexes support date-based sorting, employee history, and status/date filtering. I avoided adding an index without a matching query need because indexes consume storage and increase write costs.

# 

# 2\. \*\*Punch-in race.\*\* Both requests may pass the employee lookup, but the unique compound index allows only one insert for the same employee and attendance date. The successful request returns `201`; the duplicate-key error is converted to `409 Conflict`.

# 

# 3\. \*\*Ties.\*\* The leaderboard uses competition ranking, so tied employees share a rank. Employees tied at the cutoff are retained rather than arbitrarily excluding one.

# 

# 4\. \*\*Headcount.\*\* The department summary starts from eligible employee documents and looks up attendance records. Employees without logs therefore remain in the result and count toward headcount.

# 

# 5\. \*\*Scaling 100×.\*\* I would benchmark realistic workloads, inspect execution plans, and add pagination or caching where justified. I would also monitor slow aggregations and database resource usage before choosing further optimizations.

