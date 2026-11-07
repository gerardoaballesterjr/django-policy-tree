# Members and roles

A **membership** says: this *user* has this *role* in this *organization*. One person can hold different roles in different places, such as manager of one team and viewer of a whole region.

Your member model subclasses `AbstractMember`, which provides `user` and `organization` and a unique constraint on `(user, organization, role)`. You add `role` in one of two ways.

## Roles in code

```python
class Member(AbstractMember):
    role = models.CharField(max_length=20, choices=[
        ("admin", "Admin"), ("manager", "Manager"), ("viewer", "Viewer"),
    ])

    class Meta(AbstractMember.Meta):
        pass
```

Adding a role means editing the choices. Inheritance comes from a setting:

```python
POLICYTREE_ROLE_INCLUDES = {"admin": ["manager"], "manager": ["viewer"]}
```

An admin then gets every manager and viewer policy too. Cycles are reported by `manage.py check`.

## Roles in the admin

```python
from policytree.models import AbstractMember, RoleField


class Member(AbstractMember):
    role = RoleField()   # a foreign key to policytree.Role, stored by its code

    class Meta(AbstractMember.Meta):
        pass
```

Superusers manage roles at **Admin → Policy tree → Roles**:

| Field | Meaning |
|---|---|
| code | used by policies, e.g. `auditor`; fixed after creation |
| name, description | for people |
| includes | this role also gets every policy of these roles; cycles are refused |
| is active | an inactive role grants nothing, including what it includes |

- New roles and changes apply on the next request.
- A role used by a policy or a member can't be deleted; deactivate it instead.
- Role changes appear in the audit log.
- `POLICYTREE_ROLE_INCLUDES` still works and combines with `includes`.

To switch an existing project, see [Operations](../guides/operations.md#switching-to-admin-managed-roles).

## What counts as a membership

The engine loads memberships with your member model's **default manager**, and only in organizations your organization model's default manager returns. If those managers hide soft-deleted rows, deleted memberships and memberships in deleted organizations grant nothing. See [UUIDs, soft delete, archiving](../guides/base-model.md).

Inactive users (`is_active=False`) get nothing, superusers included.
