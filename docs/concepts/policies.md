# Policies

A policy is one rule:

> Members with **role** are **allowed** (or **denied**) **action** on **model**, within **scope**, optionally only when **condition** holds, seeing these **fields** and writing those.

| Field | Values |
|---|---|
| `code` | unique name, e.g. `manager-project-update` |
| `role` | a role code |
| `content_type` | the model (`"model": "core.project"` in JSON) |
| `action` | `list`, `detail`, `create`, `update`, `delete` |
| `effect` | `allow` (default) or `deny` |
| `scope` | `org`, `tree`, `below`, `ancestors`, `siblings`, `own`, or [your own](scopes.md#custom-scopes) |
| `condition` | empty, or a [registered condition](conditions.md) |
| `readable_fields`, `writable_fields` | lists of field names; empty means all; allow policies only |

Only superusers can see or edit policies in the admin. If a scope or condition named by a policy is later removed from the code, that policy grants nothing (an allow) or blocks everything it covers (a deny), and an error is logged. The role dropdown, scopes, conditions and field names are validated, so a typo can't silently open or close access.

## How a decision is made

1. **Superusers** can do everything. Inactive and anonymous users can do nothing.
2. Collect the user's memberships, and for each one its role plus every role it [includes](roles.md).
3. Keep the policies for those roles, this model and this action whose **scope reaches** the record's organization and whose **condition holds**.
4. If any of them is a **deny**: no.
5. Otherwise, if any is an **allow**: yes. Field rights are the union of the matching allows.
6. Otherwise: no.

The same rules decide single objects (`can`), lists (`filter`), and fields, so the three always agree. That is checked by property-based tests over randomly generated trees and policies.

## Actions

| Action | Used for |
|---|---|
| `list` | list endpoints and pages; nested lists |
| `detail` | retrieving one record |
| `create` | creating; checked against the target organization, then against the saved record |
| `update` | the record before the change, then the saved result |
| `delete` | deleting |

Custom DRF actions map onto these. See [`policy_actions`](../guides/drf.md#custom-actions).

## Deny

Use deny to carve exceptions out of broad allows:

| Role | Action | Effect | Scope | Condition |
|---|---|---|---|---|
| manager | delete | allow | tree | |
| manager | delete | deny | tree | `approved` |

A deny can't list fields. A deny with a condition that doesn't apply at a stage (see [conditions](conditions.md)) doesn't block that stage.

## Keeping policies in git

```bash
python manage.py policytree_export policies.json
python manage.py policytree_seed policies.json --dry-run
python manage.py policytree_seed policies.json
```

See [Operations](../guides/operations.md).
