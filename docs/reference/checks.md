# System checks

`python manage.py check` reports:

| ID | Level | Problem | Fix |
|---|---|---|---|
| `policytree.E001` | error | organization model setting missing, wrong, or not an `AbstractOrganization` | set `POLICYTREE_ORGANIZATION_MODEL` |
| `policytree.E002` | error | member model setting missing, wrong, or not an `AbstractMember` | set `POLICYTREE_MEMBER_MODEL` |
| `policytree.E003` | error | member model has no `role` field | add `role` |
| `policytree.E004` | error | `POLICYTREE_ROLE_INCLUDES` has a cycle | remove the loop |
| `policytree.E005` | error | `role` is a foreign key but not a `RoleField` | use `RoleField()` |
| `policytree.W001` | warning | the policy cache is per process (outside `DEBUG`) | use Redis or Memcached |
| `policytree.W002` | warning | `POLICYTREE_ROLE_INCLUDES` names roles not in your choices | fix the names |
| `policytree.W003` | warning | the organization manager allows unsafe bulk path writes | base it on `OrganizationQuerySet` |

Checks never touch the database, so they're safe before `migrate`.
