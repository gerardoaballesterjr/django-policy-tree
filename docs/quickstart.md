# Quickstart

A working setup in five steps. The example is a company with regions, branches and teams, but every name in it (the app, the levels, the roles, the models) is yours to choose. The [recipes](recipes/company.md) show the same steps for other domains.

## 1. Install

```bash
pip install "django-policy-tree[drf]"     # leave out [drf] for plain Django
```

```python
# settings.py
INSTALLED_APPS = [
    ...,
    "rest_framework",
    "policytree",
    "core",
]

POLICYTREE_ORGANIZATION_MODEL = "core.Organization"
POLICYTREE_MEMBER_MODEL = "core.Member"
```

Set both before the first `makemigrations`, like `AUTH_USER_MODEL`.

## 2. Define the tree and memberships

```python
# core/models.py
from django.db import models
from policytree.models import AbstractMember, AbstractOrganization


class Organization(AbstractOrganization):
    type = models.CharField(max_length=20, choices=[        # optional, and entirely yours
        ("company", "Company"), ("region", "Region"),
        ("branch", "Branch"), ("team", "Team"),
    ])


class Member(AbstractMember):
    role = models.CharField(max_length=20, choices=[
        ("admin", "Admin"), ("manager", "Manager"),
        ("member", "Member"), ("viewer", "Viewer"),
    ])

    class Meta(AbstractMember.Meta):
        pass
```

`AbstractOrganization` gives you `name`, `code`, `description`, `parent` and an automatic `path`. `AbstractMember` gives you `user` and `organization`. The tree can be as deep as you like, and the `type` field is only for your own use: policies never look at it.

To let superusers create roles without code, use `role = RoleField()` instead. See [Members and roles](concepts/roles.md).

## 3. Point your models at the tree

```python
class Project(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    budget = models.DecimalField(max_digits=12, decimal_places=2)


class Task(models.Model):
    policytree_organization_field = "project__organization"
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    done = models.BooleanField(default=False)
```

The default is a field called `organization`, so `Project` needs no attribute at all. `Task` reaches its unit through its project.

```bash
python manage.py makemigrations && python manage.py migrate
```

## 4. Protect the API

```python
from policytree.drf import PolicyFieldsMixin, PolicyPermission, PolicyQuerysetMixin


class ProjectSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["id", "name", "budget", "organization"]


class ProjectViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    permission_classes = [PolicyPermission]

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
```

## 5. Add policies

As a superuser, open **Admin → Policy tree → Policies**, or keep them in a JSON file:

```json
[
  {"code": "manager-project-list",   "role": "manager", "model": "core.project", "action": "list",   "scope": "tree"},
  {"code": "manager-project-detail", "role": "manager", "model": "core.project", "action": "detail", "scope": "tree"},
  {"code": "manager-project-create", "role": "manager", "model": "core.project", "action": "create", "scope": "tree"},
  {"code": "manager-project-update", "role": "manager", "model": "core.project", "action": "update", "scope": "tree"},
  {"code": "member-project-list",    "role": "member",  "model": "core.project", "action": "list",   "scope": "ancestors",
   "readable_fields": ["id", "name", "organization"]}
]
```

```bash
python manage.py policytree_seed policies.json
```

That's it:

| Who | `GET /projects/` | `POST /projects/` | `PATCH /projects/5/` |
|---|---|---|---|
| Manager of the North region | projects in North and every branch and team below | only into North or below | only those projects, and can't move them out |
| Member of a team in North | projects of their team, branch, region and company, without `budget` | 403 | 403 |
| Anyone else | 403 | 403 | 403 |

Rename `region` to `store`, `manager` to `head nurse` and `Project` to `PatientRecord`, and nothing else changes.

Next, learn how [scopes](concepts/scopes.md) decide who reaches what, or browse the [recipes](recipes/company.md).
