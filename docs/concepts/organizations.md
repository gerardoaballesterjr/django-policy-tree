# Organizations

Your organization model subclasses `AbstractOrganization`. It is one node of your tree, whatever your nodes are called: company, region, store, hospital, ward, tenant, workspace, office. You add your own fields, such as a `type`.

| Field / method | Meaning |
|---|---|
| `name`, `code`, `description` | descriptive; `code` is unique |
| `parent` | the organization above; `None` for a top-level one |
| `path` | maintained automatically, never edit it |
| `ancestors(include_self=True)` | queryset of this organization and those above |
| `descendants(include_self=False)` | queryset of those below |
| `is_within(other)` | True if this is `other` or anywhere below it |
| `rebuild_paths()` (class method) | recompute every path from `parent` |

## The path

Every organization stores its chain of ancestors as text:

```
Acme             (id 1)   path "1/"
└─ North Region  (id 4)   path "1/4/"
   └─ Sales Team (id 9)   path "1/4/9/"
```

"Everything under North Region" is a single indexed query, `path__startswith="1/4/"`, with no walking up and down the tree. The trailing slash keeps `"1/1/"` from matching `"1/10/"`. UUID primary keys are stored as 32-character hex; the 512-character field holds 15 levels of UUIDs.

## Moving

Change `parent` and call `save()`. The whole subtree moves with it in one statement, inside a transaction. Placing an organization under itself or its own descendants raises `ValidationError`.

The model's manager refuses changes that would skip that upkeep:

- `Organization.objects.filter(...).update(parent=...)` raises `ValueError`.
- `bulk_update(..., ["parent"])` raises `ValueError`.
- `bulk_create(...)` works and rebuilds paths afterwards.

After raw SQL, fixtures loaded with `loaddata`, or imports that bypass `save()`, run:

```bash
python manage.py policytree_rebuild_paths
```

An organization whose path is empty grants nothing through tree-based scopes, so a forgotten rebuild fails closed rather than open.

## Enforcing your hierarchy

Rules like "a team must be under a branch" belong to your project, because only you know your levels:

```python
class Organization(AbstractOrganization):
    PARENT_TYPE = {"company": None, "region": "company", "branch": "region", "team": "branch"}
    type = models.CharField(max_length=20, choices=[...])

    def check_hierarchy(self):
        expected = self.PARENT_TYPE.get(self.type)
        actual = self.parent.type if self.parent_id else None
        if actual != expected:
            raise ValidationError({"parent": f"A {self.type} must be under a {expected or 'nothing'}."})

    def clean(self):
        super().clean()
        self.check_hierarchy()

    def save(self, *args, **kwargs):
        self.check_hierarchy()
        super().save(*args, **kwargs)
```

The [company recipe](../recipes/company.md) has a complete version, including the DRF serializer side.
