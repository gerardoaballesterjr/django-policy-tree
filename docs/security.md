# Security

django-policy-tree decides who may read and change data, so its mistakes are security bugs. This page states what it promises, what it doesn't, and how it was checked.

## Guarantees

When you use the provided integration points (`PolicyQuerysetMixin` with `PolicyPermission` in DRF, `PolicyViewMixin` in views, or the engine directly):

1. **Default deny.** Without a matching allow, the user gets nothing: lists are empty, objects return 404 or 403, creates and custom actions are refused.
2. **Deny wins.** A matching deny overrides any allow, across all of a user's roles.
3. **Lists and objects agree.** A record is in `filter()` exactly when `can()` allows it. Property-based tests check this over randomly generated trees, memberships, records and policies.
4. **Writes stay in bounds.** Creates are checked against the target organization before saving, and every create and update is re-checked on the saved result inside a transaction. A user can't create or move a record into an organization they don't reach, or produce a result an `after` condition forbids.
5. **Fields are enforced on the way in and out.** Unwritable fields are read-only or disabled; unreadable fields are removed, per record.
6. **Revocation is immediate** with a shared cache: removing a policy, membership or role, deactivating a role, or deactivating a user takes effect on the next request.
7. **Fails closed.** A record with no organization, an organization with an empty path, a malformed id, an unknown role: all mean no access, never more.
8. **Only superusers manage rules.** Policies, roles and the read-only audit log are visible only to active superusers.

## Not covered

- **Code that bypasses the integration points:** a viewset without the mixin, a raw `Model.objects.all()` passed to a serializer, `QuerySet.update()` you write yourself, raw SQL. Use `engine.filter()` there.
- **Your own conditions and custom scopes:** a wrong `q()` or `covers()` is a wrong rule. Test them, ideally by comparing `can()` with `filter()`.
- **Policy mistakes:** a policy that grants too much grants too much. Keep policies in a reviewed file and watch the audit log.
- **Per-process caches** in multi-process deployments delay revocation. `manage.py check` warns.
- **Information in counts and errors** from your own endpoints (e.g. "this code is taken" on a unique field you can't see).
- **Superuser accounts** can do everything; protect them accordingly.

## How it was checked

- Unit and integration tests for each scope, condition stage, deny, role inheritance, field rule, create target, nested serializer and admin path, on Django 4.2, 5.2 and 6.1, and on SQLite, PostgreSQL 16 and MariaDB 10.11.
- Property-based tests (Hypothesis) generating random forests, memberships, records and policies, and asserting that `can`, `filter`, `filter_objects`, `fields` and `fields_map` agree, including after moving parts of the tree. A deliberately planted bug in a scope is caught.
- An independent review by a separate reviewer, looking for bypasses (below).

This is not a substitute for a professional audit, and the package has not yet run in production.

## Review summary (0.1)

A separate reviewer with no part in writing the code looked for bypasses in five rounds. Every finding was proven with a test before it was fixed, and each fix is kept as a regression test (`tests/test_review_*.py`).

| Round | Confirmed | Most serious |
|---|---|---|
| 1 | 13 | detail actions mapped to `create` loaded any record; a member could move their own organization under another tree |
| 2 | 7 | ORM writes in custom actions skipped conditions; overridden `get_queryset` leaked rows |
| 3 | 8 | checks on unrelated records settled owed checks; writes in GET actions survived a refusal |
| 4 | 4 | list-level actions needing two actions on their rows were scoped by the first only |
| 5 | 6 | `policy_queryset` could settle an update; checks were settled per model, not per record |

The findings moved from the core rules (round 1) to increasingly unusual ways of writing custom actions (rounds 4 and 5), which led to per-record write tracking. The reviewer's assessment after round 5: low residual risk for projects using standard viewsets and serializers; the remaining risk is concentrated in custom actions that write with the ORM, now guarded by write tracking. Expect more findings: the trend is downward, not zero.

## Reporting a vulnerability

Please don't open a public issue. See `SECURITY.md` in the repository for how to report privately.
