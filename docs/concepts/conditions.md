# Conditions

A condition is an extra rule on the record itself: "only unpaid invoices", "only draft documents", "total under 10,000". Conditions are written in code and picked per policy from a dropdown, so no logic is stored in the database.

```python
# billing/policies.py  (auto-discovered)
from django.db.models import Q
from policytree.registry import Condition, ConditionRegistry


@ConditionRegistry.register
class Unpaid(Condition):
    name = "unpaid"
    label = "Only unpaid invoices"
    models = ["billing.Invoice"]      # optional; empty means any model

    def q(self, user):
        return Q(status="unpaid")
```

`q()` is used for lists and, through one small query, for single records.

## When a condition is tested

An update has two states: the record before the change and the saved result. `check_on` decides which one a condition looks at:

| `check_on` | Tests | Example |
|---|---|---|
| `before` (default) | the record as it is: lists, detail, which records may be changed | "only draft documents may be edited" |
| `after` | the saved result of a change | "a total must stay under 10,000" |
| `both` | both | "only small invoices, and they must stay small" |

New records are always tested once saved. The check runs inside the same transaction as the save, so a failing result is rolled back.

```python
class UnderLimit(Condition):
    name = "under_limit"
    label = "Total under 10,000"
    check_on = Condition.BOTH

    def q(self, user):
        return Q(total__lt=10_000)
```

## With deny

A deny with a condition blocks only records where the condition holds:

| Role | Action | Effect | Condition |
|---|---|---|---|
| manager | update | allow | |
| manager | update | deny | `archived` |

Archived records stay visible but become read-only. A deny whose condition is `after`-only never hides existing records; it only refuses results that would match.

## Conditions that use the user

`q(user)` receives the user, so rules like "records assigned to me" are possible:

```python
class AssignedToMe(Condition):
    name = "assigned_to_me"

    def q(self, user):
        return Q(assignee=user)
```
