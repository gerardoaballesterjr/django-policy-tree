# Performance

## Query counts

Per request, after the first request warms the cache:

| What | Queries |
|---|---|
| memberships | 1 (once per request) |
| policies, roles | 0 (cached; 1 after a change) |
| list endpoint | 1 for the rows, whatever the number of rows |
| per-record field rules | 0 when every matching rule grants the same fields, else 1 per rule |
| nested lists with `policy_prefetch` | 1 per nested relation, whatever the number of rows |
| detail | 1 for the object, with organization and owner paths joined |
| conditions on single objects | 1 per conditional rule checked |

The test suite locks these in: list, nested and mixed-field endpoints are asserted to run the same number of queries with 5 rows as with 60.

## Benchmark

`pytest benchmarks -q -s` on a laptop, SQLite, 10,000 records:

| Operation | Time | Queries |
|---|---|---|
| `engine.filter(...).count()`, tree scope with a conditional deny | 4 ms | 1 |
| `engine.can()` on 1,000 objects, no conditions | 60 ms | 0 |
| `engine.filter_objects()` on 1,000 objects, with a conditional deny | 4 ms | 1 |
| `engine.fields_map()` on 1,000 objects, mixed field rules | 6 ms | 2 |
| `GET` all 10,000 rows, uniform field rules | 320 ms | 2 |
| `GET` all 10,000 rows, mixed field rules | 480 ms | 4 |

Most of the endpoint time is DRF serialization; use pagination.

## Tips

- Use a shared cache so policies stay cached across workers.
- Prefer `filter()` and `filter_objects()` over calling `can()` in a loop when conditions are involved.
- Index the fields your conditions filter on.
- Keep field rules uniform for a role where you can; mixed rules cost one query per rule on lists.
