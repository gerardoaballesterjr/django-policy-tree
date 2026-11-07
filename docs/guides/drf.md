# Django REST Framework

```bash
pip install "django-policy-tree[drf]"
```

Three classes do the work:

```python
from policytree.drf import PolicyFieldsMixin, PolicyPermission, PolicyQuerysetMixin


class ProjectSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["id", "name", "budget", "organization"]


class ProjectViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    permission_classes = [PolicyPermission]
```

No other settings are needed: field paths come from the model. A `get_queryset()` you override is scoped too, whether or not it calls `super()`, and so is one inherited from another mixin. When an action needs several actions on its own rows (for example `[("list", None), ("delete", None)]`), rows must allow all of them. `values()` querysets are scoped through their primary keys; return querysets unsliced.

## What happens

| Request | Checks |
|---|---|
| `GET /projects/` | a `list` allow exists; the queryset is filtered by scope, conditions and denies |
| `GET /projects/5/` | out-of-scope projects return 404 |
| `POST /projects/` | the target organization (from the request) must be in scope; the saved project is checked again |
| `PATCH /projects/5/` | the project must be updatable now, and the saved result must still be allowed |
| `DELETE /projects/5/` | the project must be deletable |

Every save through a serializer that uses `PolicyFieldsMixin` is re-checked inside a transaction, so a failed check rolls the change back. This covers standard create and update, **custom actions**, **second models** saved in custom actions, and **bulk creates** (`many=True`). Viewsets also re-check saves from serializers without the mixin. The re-check makes sure:

- the record is in a place the user may create in (for organizations: under a parent they may create in);
- every field submitted on create was writable **where the record landed**;
- an updated record is still allowed afterwards, including `after` conditions;
- a record **moved** to another place (another organization, or an organization under another parent) is allowed to be **created** there.

Saves that skip serializers, such as `Model.objects.create()` in an action, aren't seen; check them with `self.policy.can_create(obj)` or `self.policy_require(...)`.

The queryset automatically gets `select_related()` along the organization and owner paths, so checks don't query per row.

## Where creates go

The target organization is read from the request through the model's organization path:

| Model path | Request field | Target |
|---|---|---|
| `organization` (default) | `organization` | that organization |
| `team` | `team` | that team |
| `project__organization` | `project` | that project's organization |
| the organization model | `parent` | the new organization's parent |

When the place is decided on the server, override `get_policy_target`:

```python
class ExpenseViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    def get_policy_target(self, model):
        return self.request.user.profile.team

    def perform_create(self, serializer):
        serializer.save(organization=self.request.user.profile.team, owner=self.request.user)
```

Only superusers can create top-level organizations.

## Custom actions

Custom `@action`s are refused (superusers excepted) until you map them in `policy_actions`:

```python
class OrderViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    policy_actions = {
        "approve": "update",                                  # the viewset's own model
        "refund": ("create", Refund),                         # another model
        "payments": ("list", "billing.Payment"),              # by label
        "close": [("update", None), ("create", Refund)],      # all must pass
    }
```

- `detail=True` actions always need the main object to be visible, even when mapped to `create`.
- Actions mapped to own-model `create` see no existing rows through `get_queryset()`.
- For another model on a detail action, the user's policy for that model must reach the **main object's organization**.
- For another model on a list-level `create`, the target comes from the request, through that model's path.
- Unknown actions or models raise `ImproperlyConfigured`.

### Checking the records an action touches

Before the action runs, `PolicyPermission` can only judge the organization involved. Anything it can't settle without the real records is recorded as **owed**, and the action must settle it with one of the helpers below:

- every second-model requirement (the action decides which records it reads, changes or creates);
- every `create`, own model included: the pre-check covers only the place named in the request, not where the action actually writes;
- `update` on the viewset's own model in a custom action (the built-in `update` re-checks by itself).

In addition, **every record of the viewset's models that a request saves is tracked**, and must be checked after its last save. Deletes of those models, including cascades from deleting something else, are checked as they happen. So an action can't write one record properly and slip a second one past, or change a record again after it was checked.

If the action finishes without settling an owed check, the response is replaced with a **403** and every change in the request is rolled back, including changes made in GET actions. Forgetting a check fails closed.

| Helper | Settles |
|---|---|
| `self.policy_queryset(qs, action=None)` | reads (`list`, `detail`) through the returned, scoped queryset; it never settles changes |
| `self.policy_prefetch(lookup, qs)` | nested rows through a scoped prefetch |
| `self.policy_require(action, obj)` | reading (`list`, `detail`) that object; 403 if not allowed |
| `self.policy_created(obj, submitted=())` | a record the action created without a serializer |
| `self.policy_changed(obj, before)` | a record the action changed without a serializer (`before = self.policy.place_of(obj)` taken first) |
| `self.policy_update(qs, **values)` | a bulk update, limited to rows the user may update and fields they may write |
| `self.policy_delete(qs)` | a bulk delete, limited to rows the user may delete |
| `self.policy_skip(model, action)` | waives an owed check on a branch that touched nothing (e.g. "nothing to do"); records written are still checked |

Saves through a serializer using `PolicyFieldsMixin` settle creates and updates automatically. Checking one object never settles changes to others: use `policy_update` and `policy_delete` for bulk changes, never `QuerySet.update()` on your own. An action mapped to `create` or `update` that writes nothing must say so with `policy_skip`; read-only actions belong on `list` or `detail`.

```python
@action(detail=True, methods=["post"])
def approve(self, request, pk=None):
    order = self.get_object()               # needs "update" (policy_actions = {"approve": "update"})
    self.policy_skip(Order, "update")       # a permission gate: nothing is written
    notify_approval(order)
    return Response({"approved": True})
```

```python
@action(detail=True, methods=["post"])
def void(self, request, pk=None):
    order = self.get_object()
    payment = get_object_or_404(self.policy_queryset(Payment.objects.filter(order=order), "update"),
                                pk=request.data["payment"])
    before = self.policy.place_of(payment)
    payment.status = "void"
    payment.save()
    self.policy_changed(payment, before)
    return Response({"ok": True})
```

Use the queryset `policy_queryset()` returns for everything the action touches: settling a check with one queryset and then using another is a bug the package can't see.

### Transactions

Every request to a viewset with `PolicyQuerysetMixin` runs in a transaction on the default database and on the databases its models write to. A request that ends in an error, or in an exception (even one your exception handler answers with 2xx), keeps nothing. This works like Django's `ATOMIC_REQUESTS`, and nests inside it.

Things to know:

- Code that uses `transaction.atomic(durable=True)` can't run inside these views.
- `on_commit` callbacks run when the request ends, and long requests hold their transaction open.
- Rollback undoes database writes only: emails, HTTP calls and cache writes stay. Send them from `on_commit`.
- Writes to databases that don't support transactions, or that no model of the viewset routes to, aren't rolled back.
- A `StreamingHttpResponse` body runs after the transaction has ended.
- Overriding `dispatch`, `initial` or `finalize_response` without calling `super()` raises `ImproperlyConfigured`, or refuses owed checks, rather than silently dropping enforcement. So does replacing `get_queryset` on a class after it's created. Async viewsets aren't supported yet.
- Records saved as a side effect (for example by a serializer's nested create) must be checked too, or the request is refused. Writes to models the viewset doesn't name aren't tracked.
- Schema generators that call `get_queryset()` without a request get an empty queryset.

`PolicyPermission` without the mixin can't enforce owed checks, so it refuses anything that would be owed.

## Nested serializers

A nested serializer that uses `PolicyFieldsMixin` with `many=True`:

- shows only related rows in scope, whether the data is a queryset, prefetched, or a plain list;
- applies that model's **list** field rules per row (nested rows are a list, so both rows and fields follow `list`).

To avoid a query per parent row, prefetch with `policy_prefetch`:

```python
def get_queryset(self):
    return super().get_queryset().prefetch_related(
        self.policy_prefetch("tasks", Task.objects.all()))
```

Prefetched rows are scoped in the database and re-checked in memory, so even an ordinary `prefetch_related("tasks")` can't leak rows.

## Not covered

Ordering, search and filter backends see every field of the queryset. A field hidden from some rows can still be sorted or filtered by there; limit `ordering_fields`, `search_fields` and `filterset_fields` to fields every role may read.

## Serializers used outside viewsets

A serializer you create yourself with a queryset (`ProjectSerializer(qs, many=True, context=...)`) shows what you give it. Scope the queryset first with `self.policy_queryset(qs)` or `engine.filter(qs, "list")`.
