# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code`, a unique compound index on `attendance_logs.emp_code + date`, and supporting indexes on `employees.department`, `attendance_logs.date`, `attendance_logs.emp_code`, and `attendance_logs.emp_code + punch_out`. The unique keys prevent duplicate employee codes and duplicate punch-ins for the same day, while the remaining indexes support lookup, list, and analytics filters. I considered a broader compound index for every analytics query, but rejected it because the static query mix is small and the unique indexes provide the critical guarantees without over-indexing.

2. **Punch-in race.** Two identical requests arrive simultaneously. Each request validates the employee and then tries to insert the same `emp_code + date` record. The database unique index is the guardrail: the first insert succeeds, the second hits `DuplicateKeyError`, and the API converts that into a 409. That gives a race-safe outcome without a fragile check-then-insert pattern.

3. **Ties.** The leaderboard ranks by total late minutes descending and then by `emp_code` ascending; when two employees tie, they receive the same competition rank, and the API returns every row whose `rank` is less than or equal to the limit. That means if the cutoff is 10 and two employees share rank 10, both are included even if that pushes the row count over 10.

4. **Headcount.** The department summary uses the employees collection to build the department roster for the month, then applies the `joined_on <= month_end` rule before the aggregation. That keeps employees with zero attendance logs in the `headcount` even when there are no matching log documents, which matches the requirement.

5. **One thing I would change for 100x data.** I would pre-aggregate monthly and daily rollups into materialized collections or a warehouse-friendly job, then serve the analytics from those summaries instead of recalculating large grouped pipelines on every request. That would reduce query time dramatically while keeping the operational API lighter and more predictable.
