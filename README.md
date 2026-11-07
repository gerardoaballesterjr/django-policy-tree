# django-policy-tree

Permissions for Django projects whose users belong to a **tree of organizational units**, with rules that **superusers edit in the admin**.

Any hierarchy works. You name the levels, you name the roles, you point your models at the tree:

| Domain | Tree | Roles |
|---|---|---|
| Company | Head office → Region → Branch → Team | admin, manager, member, viewer |
| Retail | Chain → Region → Store | owner, area manager, store manager, cashier |
| Healthcare | Hospital group → Hospital → Department → Ward | administrator, head nurse, doctor, nurse |
| SaaS | Tenant → Workspace → Project | owner, admin, editor, guest |
| Government | Ministry → Agency → Office | director, officer, clerk |
| Education | School → Campus → Department | admin, registrar, professor, student |

Wherever a user's role *in a unit* decides what they can do, django-policy-tree answers *"can this user do this, to this record, and with which fields?"* from one place, for lists, objects, creates, updates and fields alike.

```python
class Project(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)   # the record's place in the tree
    name = models.CharField(max_length=100)
    budget = models.DecimalField(max_digits=12, decimal_places=2)


class ProjectViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    permission_classes = [PolicyPermission]
```

With the policy *"managers may update projects, scope tree"*, a manager of the North region sees only projects in North and the branches and teams below it, can't create or edit projects anywhere else, and can't move a project out of North. Add *"members may read projects, but not the `budget` field"* and the field disappears from their responses.

## Features

- **Default deny**, with allow and deny policies; deny wins
- **Scopes:** `org`, `tree`, `below`, `ancestors`, `siblings`, `own`, and your own
- **Conditions** written in code, picked per policy, checked before and/or after a change
- **Roles** in code or managed by superusers in the admin, with inheritance
- **Field rules** per record, on the way in and out
- **DRF** (permission class, viewset and serializer mixins), **plain Django views**, a **template tag**, and an engine for everything else
- **Audit log**, policies and roles as **JSON in git**, **system checks**
- Works with **UUID** primary keys and **soft delete**

## Install

```bash
pip install "django-policy-tree[drf]"
```

```python
INSTALLED_APPS = [..., "rest_framework", "policytree", "core"]
POLICYTREE_ORGANIZATION_MODEL = "core.Organization"
POLICYTREE_MEMBER_MODEL = "core.Member"
```

Then follow the **[Quickstart](docs/quickstart.md)**.

## Documentation

- [Quickstart](docs/quickstart.md)
- Concepts: [organizations](docs/concepts/organizations.md), [members and roles](docs/concepts/roles.md), [policies](docs/concepts/policies.md), [scopes](docs/concepts/scopes.md), [conditions](docs/concepts/conditions.md), [fields](docs/concepts/fields.md)
- Guides: [DRF](docs/guides/drf.md), [plain views](docs/guides/views.md), [engine](docs/guides/engine.md), [UUIDs and soft delete](docs/guides/base-model.md), [operations](docs/guides/operations.md)
- Recipes, one per domain: [company](docs/recipes/company.md), [retail](docs/recipes/retail.md), [healthcare](docs/recipes/healthcare.md), [SaaS](docs/recipes/saas.md), [education](docs/recipes/school.md)
- [Security](docs/security.md) · [Performance](docs/performance.md) · [Changelog](CHANGELOG.md)

Build the docs site with `pip install mkdocs-material && mkdocs serve`.

## Status

**0.1**: the API is intended to be the 1.0 API. Tested on Django 4.2, 5.2 and 6.1 with Python 3.10+, on SQLite, PostgreSQL and MariaDB, with property-based tests and an independent security review. It has not yet run in production; read [Security](docs/security.md) before relying on it.

## Development

```bash
pip install -e ".[test]"
pytest                        # SQLite
DB=postgres pytest            # see tests/settings.py
DB=mysql pytest
pytest benchmarks -q -s       # timings
mkdocs build --strict         # docs
```

`tests/testapp` holds the test fixtures. They model one sample domain so the tests read naturally; nothing in the package depends on it. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
