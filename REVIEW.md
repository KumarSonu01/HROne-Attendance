# REVIEW.md

## Defects found in the starter code

| # | Where (function / area) | What is wrong | How to notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` / overnight shifts | Early-morning punch-ins for overnight shifts can be compared against the wrong shift date. | Test a shift from 22:00 to 06:00 with a punch-in at 05:55 IST the following day. | Associate the punch-in with the previous attendance date and calculate lateness from that shift's start time. |
| 2 | `/admin/explain/{endpoint}` | An explain plan built from a shortened pipeline may not represent the work performed by the actual analytics endpoint. | Compare the explain pipeline with the corresponding analytics aggregation pipeline. | Reuse the same pipeline-building helpers as the analytics endpoints. |
| 3 | Attendance computation helpers | Incorrect threshold or rounding behavior can produce wrong late minutes, overtime, work hours, or half-day results. | Test grace-period boundaries, the 30-minute overtime threshold, and half-day rounding boundaries. | Apply the specified whole-minute calculations and half-up rounding rules consistently. |
| 4 | `/attendance/punch-in` | Concurrent duplicate requests can create duplicate attendance records if uniqueness is enforced only in application logic. | Send two requests for the same employee and attendance date concurrently. | Enforce a unique compound database index and translate duplicate-key errors into HTTP 409. |
| 5 | `/attendance/punch-out` | Concurrent requests can both attempt to close the same attendance record. | Send two punch-out requests for one open attendance record concurrently. | Use a conditional database update so only one request succeeds. |
| 6 | Attendance regularization | Corrections can leave computed fields stale or fail to preserve an audit trail. | Correct punch times or change a record to `LEAVE` or `ABSENT`, then inspect computed fields and history. | Recompute derived fields, append a history entry, and clear punch-related fields when required. |

## Reviewed but not considered defects

- The FastAPI startup deprecation warning does not cause the tests to fail. It is a maintenance warning, not an attendance-rule defect.
- Employees with no attendance logs must remain in department headcount; this is intentional behavior required by R9.
- Weekend attendance records may contribute to late and overtime totals even though weekends are excluded from working-day attendance calculations.
