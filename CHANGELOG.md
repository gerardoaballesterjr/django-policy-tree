# Changelog

## 0.1.0

First release.

**Organizations and roles**
- `AbstractOrganization` with an automatic materialized `path`; any primary key type, including UUIDs.
- Moving an organization moves its whole subtree; cycles are refused; `bulk_create` keeps paths; `policytree_rebuild_paths` command.
- `AbstractMember` with roles in code (`choices`) or managed by superusers in the admin (`RoleField`, `policytree.Role` with `includes` and `is_active`).
- Role inheritance through `includes` or `POLICYTREE_ROLE_INCLUDES`.
- Memberships load through the models' default managers, so soft-deleted rows grant nothing.

**Policies**
- Policies stored in the database and edited by superusers in the admin: role, model, action, effect, scope, condition, readable and writable fields.
- Default deny; allow and deny policies; deny wins.
- Scopes: `org`, `tree`, `below`, `ancestors`, `siblings`, `own`, plus custom scopes.
- Conditions written in code, picked per policy, checked `before`, `after` or `both`.
- Per-record field rules, on the way in and out.
- Models reach their organization and owner through any path (`policytree_organization_field`, `policytree_owner_field`).

**Engine**
- `PolicyEngine`: `can`, `require`, `can_in`, `assess`, `has_any`, `filter`, `filter_objects`, `fields`, `fields_map`, `can_create`, `can_change`, `place_of`, `bulk_update`, `bulk_delete`.

**Django REST Framework**
- `PolicyPermission`, `PolicyQuerysetMixin`, `PolicyFieldsMixin`.
- Custom actions mapped through `policy_actions`, including second models.
- Every save re-checked inside a transaction: creates, updates, moves, custom actions, bulk creates.
- Helpers for custom actions: `policy_queryset`, `policy_prefetch`, `policy_require`, `policy_created`, `policy_changed`, `policy_update`, `policy_delete`, `policy_skip`.
- Per-record write tracking: a request that writes without checking is refused and rolled back.
- Scoped nested serializers; `get_policy_target()` hook for server-decided placement.

**Plain Django**
- `PolicyViewMixin` for generic class-based views, with form field rules.
- `{% policy_can %}` and `{% policy_fields %}` template tags.

**Operations**
- Audit log of policy and role changes.
- `policytree_seed`, `policytree_export`, `policytree_import_roles` commands; policies and roles as JSON in git.
- System checks for role cycles, unknown roles, local caches and unsafe managers.
- Transaction-safe caching with `POLICYTREE_CACHE_TIMEOUT`.

**Quality**
- Tested on Django 4.2, 5.2 and 6.1, on SQLite, PostgreSQL and MariaDB/MySQL.
- Property-based tests (Hypothesis) that `can`, `filter`, `filter_objects`, `fields` and `fields_map` agree.
- Independent security review with regression tests; query-count tests; benchmarks.
- MkDocs documentation site; `SECURITY.md`, `CONTRIBUTING.md`.
