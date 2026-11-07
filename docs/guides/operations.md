# Operations

## Policies and roles as code

Keep rules in a JSON file in git, so changes are reviewed like code, and apply it on deploy:

```bash
python manage.py policytree_export policies.json         # current state
python manage.py policytree_seed policies.json --dry-run # what would change
python manage.py policytree_seed policies.json           # apply
python manage.py policytree_seed policies.json --prune   # also remove what's not in the file
```

The file is a list of policies, or, with admin-managed roles:

```json
{
  "roles": [
    {"code": "member", "name": "Member"},
    {"code": "team_lead", "name": "Team lead", "includes": ["member"]}
  ],
  "policies": [
    {"code": "member-project-list", "role": "member", "model": "core.project",
     "action": "list", "scope": "ancestors"}
  ]
}
```

Everything is validated and applied in one transaction: one bad entry changes nothing. `--prune` keeps roles that are still in use and says so.

## Audit log

Every create, update and delete of a policy or role is recorded at **Admin → Policy tree → Policy changes**, with who, when, how (`admin`, `seed`, `import` or `code`) and a full snapshot. The log is read-only, even for superusers.

To record who made a change in your own code:

```python
from policytree.audit import PolicyAudit

PolicyAudit.set_actor(policy, request.user, source="api")
policy.save()
```

## Cache

Policies and roles are cached and the cache is cleared on every change, again when the change commits, and expires after `POLICYTREE_CACHE_TIMEOUT` seconds. Data read inside a transaction that changed policies or roles is never put in the shared cache, so a rolled-back change (including `policytree_seed --dry-run`) can't leave access behind. Bulk `QuerySet.update()` on policies or roles clears the cache too, but isn't written to the audit log. With several processes or servers, the cache must be shared (Redis, Memcached), or a change may not reach every worker. `manage.py check` warns (`policytree.W001`) about per-process caches outside `DEBUG`.

```python
POLICYTREE_CACHE_ALIAS = "default"
```

## Paths

```bash
python manage.py policytree_rebuild_paths
```

Run it after raw SQL, `loaddata`, or imports that bypass `save()`.

## Switching to admin-managed roles

1. Upgrade the package and run `migrate`.
2. `python manage.py policytree_import_roles --with-includes` creates a role for every value in your choices, members, policies and `POLICYTREE_ROLE_INCLUDES`, and copies that setting into each role's `includes`.
3. Change `role` to `RoleField()`, then `makemigrations` and `migrate`. Existing memberships keep their roles.
4. Optionally remove `POLICYTREE_ROLE_INCLUDES`.

Tested on SQLite and PostgreSQL with existing data.

## Before going live

- [ ] Shared cache configured
- [ ] Policies seeded from a reviewed file
- [ ] `manage.py check` clean
- [ ] Tests for your own conditions and custom scopes
- [ ] A test per important role: what they can and can't see and change
