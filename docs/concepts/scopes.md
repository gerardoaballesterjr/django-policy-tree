# Scopes

A scope says which organizations, relative to the member's own, a policy reaches.

```
Acme
└─ North Region
   ├─ Lisbon Branch
   │  ├─ Sales Team        ← member here
   │  └─ Support Team
   └─ Porto Branch
```

| Scope | A member of Sales Team reaches |
|---|---|
| `org` | Sales Team |
| `tree` | Sales Team and everything below it |
| `below` | everything below Sales Team, not itself |
| `ancestors` | Sales Team, Lisbon Branch, North Region, Acme |
| `siblings` | Sales Team and Support Team (same parent); a top-level organization reaches only itself |
| `own` | records in Sales Team whose owner is the user |

The names are only an example: the scopes work the same on any tree, at any depth.

## Choosing a scope

- **Managers of a unit:** `tree`.
- **Shared resources higher up**, like templates or price lists kept at the region and used by every team: `ancestors`, usually for `list` and `detail` only.
- **Personal records**, like a user's own expense claims: `own`.
- **Exceptions:** a `deny` with `below`, for example "a region admin may not touch team-level records".

`own` uses the model's `policytree_owner_field` (default `owner`), which can be a path such as `invoice__owner`. It needs a record to judge, so it never grants `can_in()` checks except for `create` (a new record will be owned by the user). Custom scopes like that set `needs_object = True`.

## Custom scopes

Write a class with `covers()` (for one object, in memory) and `q()` (the same rule as a queryset filter), and register it in a `policies.py` module in any app:

```python
# core/policies.py
from django.db.models import Q
from policytree.registry import Scope, ScopeRegistry


@ScopeRegistry.register
class ParentOnly(Scope):
    name = "parent_only"
    label = "Only the parent organization"

    def covers(self, user, membership, org, obj, paths):
        return org.pk == membership.organization.parent_id

    def q(self, user, membership, paths):
        return Q(**{paths.id: membership.organization.parent_id})
```

`paths` gives the lookups for the model being checked: `paths.id` (organization primary key), `paths.path`, `paths.parent`, `paths.owner`.

!!! warning
    `covers()` and `q()` must agree: an object is covered exactly when the filter returns it. The package's own scopes are checked against each other by property-based tests; do the same for yours.
