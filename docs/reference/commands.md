# Management commands

| Command | Options | Does |
|---|---|---|
| `policytree_seed FILE` | `--dry-run`, `--prune` | creates or updates roles and policies from JSON, in one transaction |
| `policytree_export [FILE]` | `--with-roles` | writes roles and policies as JSON (stdout without a file) |
| `policytree_import_roles` | `--with-includes` | creates `Role` rows for every role already in use |
| `policytree_rebuild_paths` | | recomputes every organization path from `parent` |

## Seed file format

A list of policies:

```json
[{"code": "manager-project-list", "role": "manager", "model": "core.project", "action": "list", "scope": "tree"}]
```

or roles and policies:

```json
{"roles": [{"code": "manager", "name": "Manager", "includes": [], "is_active": true}],
 "policies": [...]}
```

Policy keys: `code` and `model` (required), `name`, `role`, `action`, `effect`, `scope`, `condition`, `readable_fields`, `writable_fields`.

Role keys: `code` (required), `name`, `description`, `is_active`, `includes`.
