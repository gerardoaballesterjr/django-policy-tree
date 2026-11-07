# The engine anywhere

`PolicyEngine` has no DRF dependency. Use it in services, background tasks, management commands, GraphQL resolvers or anywhere else.

```python
from policytree.engine import PolicyEngine

engine = PolicyEngine.for_user(user)       # in a request: PolicyEngine.for_request(request)
```

`for_request` keeps one engine per request, so memberships load once.

| Call | Returns |
|---|---|
| `engine.can("update", project)` | may the user do this to this object |
| `engine.require("update", project)` | raises `PermissionDenied` unless allowed |
| `engine.can_in(Project, "create", team)` | may the user do this to any `Project` record inside `team`; `True` only when certain without a record |
| `engine.has_any(Project, "list")` | is there any allow at all (a quick gate) |
| `engine.filter(Project.objects.all(), "list")` | only the projects the user may list |
| `engine.filter_objects(projects, "list")` | the same for objects already in memory |
| `engine.fields(Project, "detail", "read")` | field names, or `None` for all |
| `engine.fields(Project, "detail", "read", obj=project)` | the same, for one record |
| `engine.fields_map(Project, "list", "read", projects)` | `{pk: fields}` for many records at once |
| `engine.can_create(obj, submitted=...)` | re-check a freshly saved record and the fields that were sent |
| `engine.can_change(obj, before)` | re-check a saved update; `before` is `engine.place_of(obj)` taken before the change |
| `engine.place_of(obj)` | where a record is placed: its organization, or its parent for organizations |
| `engine.assess(Project, "create", team)` | `(allowed, provisional)`: provisional answers depend on the record (a condition or `own`) and need a follow-up check |
| `engine.bulk_update(qs, status="closed")` | `update()` on the rows the user may update and write those fields on, re-checked afterwards |
| `engine.bulk_delete(qs)` | deletes the rows the user may delete; returns the count |

`bulk_update` refuses to change the field that places a record (for example `organization`, or `project` for tasks), since moving records needs the per-record checks. It skips rows where the fields aren't writable, and if any changed row would break an `after` condition, nothing changes and `PermissionDenied` is raised.

## Background tasks

Pass the user's id, not the engine:

```python
@shared_task
def close_orders(user_id):
    user = User.objects.get(pk=user_id)
    engine = PolicyEngine.for_user(user)
    engine.bulk_update(Order.objects.filter(status="open"), status="closed")
```

## Saving outside DRF and views

Re-check your own saves inside the same transaction:

```python
with transaction.atomic():
    before = engine.place_of(order)
    order.total = 12_000
    order.save()
    if not engine.can_change(order, before):
        raise PermissionDenied   # rolls back

with transaction.atomic():
    refund = Refund.objects.create(order=order, amount=10, created_by=user)
    if not engine.can_create(refund, submitted={"amount"}):
        raise PermissionDenied
```

The DRF and view layers do this for you.
