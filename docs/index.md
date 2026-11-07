# django-policy-tree

Permissions for Django projects whose users belong to a **tree of organizational units**, with rules that **superusers edit in the admin**.

The package doesn't know or care what your units are. A company with regions, branches and teams. A retail chain with stores. A hospital group with departments and wards. A SaaS product with tenants, workspaces and projects. A government ministry with agencies and offices. A school with campuses. In all of them the same question comes up on every request:

> Can this user do this action on this record, and which fields may they see or change?

The answer depends on **where** the user sits in the tree, **what role** they hold there, and **where the record** sits. django-policy-tree answers it from one place, the same way everywhere:

- **Your vocabulary.** Your levels, your roles, your models. Nothing is built in except "a unit has a parent".
- **Default deny.** No matching allow means no access, and a matching deny always wins.
- **Rules as data.** Superusers change who can do what in the admin, with an audit log. Roles can be managed there too.
- **Enforced everywhere.** List queries, single objects, creates, updates, fields, nested serializers, plain views and templates all use the same engine.

```python
class Task(models.Model):
    policytree_organization_field = "project__organization"   # how a task reaches its unit
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)


class TaskViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    permission_classes = [PolicyPermission]
```

With a policy like *"managers may update tasks in their unit and everything below it"*, a manager sees only tasks under their unit, can't edit or create tasks elsewhere, and can't move a task to a project outside their part of the tree.

## Same package, different trees

| Domain | Tree | A typical rule |
|---|---|---|
| Company | Head office → Region → Branch → Team | managers update projects in their region and below |
| Retail | Chain → Region → Store | store managers see their store's sales; area managers see every store in the region |
| Healthcare | Hospital → Department → Ward | nurses read records in their ward, but not billing fields |
| SaaS | Tenant → Workspace → Project | guests read their own projects only |
| Government | Ministry → Agency → Office | clerks create cases in their office; directors read everything below |
| Education | School → Campus → Department | registrars manage enrollments in their campus |

The [recipes](recipes/company.md) show several of these end to end.

## When to use it

- Your users belong to units of a hierarchy, and their role in a unit decides what they can do.
- The rules change, and you don't want a deploy for every change.
- You need list filtering, not just yes/no on single objects.

## When not to use it

- A flat app with a handful of global roles: Django's built-in permissions are enough.
- Rules that depend on arbitrary per-object sharing ("Ana shared this file with Ben"): look at per-object permission packages.
- More than one independent hierarchy in one project: not supported (one tree per project).

## Status

Version 0.1. The API is intended to be the 1.0 API. It is tested on Django 4.2, 5.2 and 6.1 with SQLite, PostgreSQL and MariaDB, including property-based tests, but it has not run in production yet. Read [Security](security.md) before relying on it.

Next: the [Quickstart](quickstart.md).
