# Python API

## `policytree.models`

| Name | Kind | Notes |
|---|---|---|
| `AbstractOrganization` | abstract model | `name`, `code`, `description`, `parent`, `path`; `ancestors()`, `descendants()`, `is_within()`, `ancestor_ids()`, `rebuild_paths()`, `path_segment(pk)` |
| `OrganizationQuerySet` | queryset | refuses bulk path/parent writes; `bulk_create` rebuilds paths |
| `AbstractMember` | abstract model | `user`, `organization`; add `role` |
| `RoleField` | field | `ForeignKey` to `Role` by code, `PROTECT` |
| `Role` | model | `code`, `name`, `description`, `includes`, `is_active`; `in_use()`, `snapshot()` |
| `Policy` | model | see [Policies](../concepts/policies.md); `Policy.Action`, `Policy.Effect` |
| `PolicyChange` | model | audit log: `target`, `policy_code`, `kind`, `data`, `source`, `changed_by`, `changed_at` |

## `policytree.engine`

| Name | Notes |
|---|---|
| `PolicyEngine.for_user(user)`, `.for_request(request)` | see [The engine anywhere](../guides/engine.md) |
| `can`, `require`, `can_in`, `has_any` | single decisions |
| `filter`, `filter_objects` | many records |
| `fields`, `fields_map` | field rights |
| `bulk_update`, `bulk_delete` | changes limited to allowed rows |
| `org_of(obj)`, `memberships`, `rules(model, action, effect=None)` | building blocks |
| `Stage.BEFORE`, `Stage.AFTER`, `Stage.NEW` | for `can(..., stage=)` |
| `PolicyCache.clear()` | clears cached policies (done automatically on changes) |

## `policytree.drf`

| Name | Notes |
|---|---|
| `PolicyPermission` | DRF permission class |
| `PolicyQuerysetMixin` | viewset mixin: `policy_actions`, `policy`, `get_policy_target(model)`, `policy_queryset()`, `policy_prefetch()`, `policy_require()` |
| `PolicyFieldsMixin` | serializer mixin |
| `PolicyListSerializer` | used automatically for `many=True` |
| `DRFActionMap` | how DRF actions map to policy actions |

## `policytree.views`

| Name | Notes |
|---|---|
| `PolicyViewMixin` | generic CBV mixin: `policy_action`, `policy`, `get_policy_target(form)` |

## `policytree.registry`

| Name | Notes |
|---|---|
| `Scope`, `ScopeRegistry` | custom scopes |
| `Condition`, `ConditionRegistry` | custom conditions; `check_on` = `BEFORE`, `AFTER`, `BOTH` |
| `FieldPaths(model)` | `organization`, `owner`, `id`, `path`, `parent`, `create_field`, `org_of()`, `owner_id_of()` |

## Other

| Name | Notes |
|---|---|
| `policytree.targets.CreateTarget` | `field(model)`, `resolve(model, value)` |
| `policytree.roles.Roles`, `RoleTree` | role lookups and inheritance |
| `policytree.audit.PolicyAudit` | `set_actor(obj, user, source)` |
| `{% load policytree %}{% policy_can action obj as var %}` | template tag |
