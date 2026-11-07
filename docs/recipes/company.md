# Recipe: a company

A company with regions, branches and teams. Projects belong to any unit; tasks belong to a project; expense claims belong to the person who filed them.

```
Acme (company)                     admin, finance
├─ North (region)                  manager
│  ├─ Lisbon (branch)              manager
│  │  ├─ Sales (team)              team_lead, member
│  │  └─ Support (team)            member
│  └─ Porto (branch)
└─ South (region)                  manager, viewer
```

## Models

```python
# core/models.py
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from policytree.models import AbstractMember, AbstractOrganization


class Organization(AbstractOrganization):
    class Type(models.TextChoices):
        COMPANY = "company"
        REGION = "region"
        BRANCH = "branch"
        TEAM = "team"

    PARENT_TYPE = {Type.COMPANY: None, Type.REGION: Type.COMPANY,
                   Type.BRANCH: Type.REGION, Type.TEAM: Type.BRANCH}

    type = models.CharField(max_length=20, choices=Type.choices)

    def check_hierarchy(self):
        expected = self.PARENT_TYPE.get(self.type)
        actual = self.parent.type if self.parent_id else None
        if actual != expected:
            where = f"under a {expected}" if expected else "at the top"
            raise ValidationError({"parent": f"A {self.type} must be {where}."})

    def clean(self):
        super().clean()
        self.check_hierarchy()

    def save(self, *args, **kwargs):
        self.check_hierarchy()
        super().save(*args, **kwargs)


class Member(AbstractMember):
    role = models.CharField(max_length=20, choices=[
        ("admin", "Admin"), ("finance", "Finance"), ("manager", "Manager"),
        ("team_lead", "Team lead"), ("member", "Member"), ("viewer", "Viewer"),
    ])

    class Meta(AbstractMember.Meta):
        pass


class Project(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    name = models.CharField(max_length=100)
    budget = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, default="draft")   # draft, approved, archived


class Task(models.Model):
    policytree_organization_field = "project__organization"
    policytree_owner_field = "assignee"
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    title = models.CharField(max_length=200)
    done = models.BooleanField(default=False)


class Expense(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default="submitted")   # submitted, approved, paid
```

```python
POLICYTREE_ROLE_INCLUDES = {"admin": ["manager"], "manager": ["team_lead"], "team_lead": ["member"]}
```

## Conditions

```python
# core/policies.py
from django.db.models import Q
from policytree.registry import Condition, ConditionRegistry


@ConditionRegistry.register
class Archived(Condition):
    name = "archived"
    models = ["core.Project"]

    def q(self, user):
        return Q(status="archived")


@ConditionRegistry.register
class Submitted(Condition):
    name = "submitted"
    label = "Only expenses not yet approved"
    models = ["core.Expense"]

    def q(self, user):
        return Q(status="submitted")
```

## Policies

| Role | Model | Action | Effect | Scope | Condition | Fields |
|---|---|---|---|---|---|---|
| admin | organization | all | allow | tree | | |
| manager | project | all | allow | tree | | |
| manager | project | update, delete | deny | tree | archived | |
| manager | expense | list, detail, update | allow | tree | | |
| team_lead | task | all | allow | tree | | |
| member | project | list, detail | allow | ancestors | | read: id, name, status, organization |
| member | task | list, detail, update | allow | own | | write: done |
| member | expense | create | allow | org | | write: organization, amount |
| member | expense | list, detail | allow | own | | |
| member | expense | update, delete | allow | own | submitted | write: amount |
| finance | expense | list, detail, update | allow | tree | | |
| viewer | project, task | list, detail | allow | tree | | read: id, name, title, status, done |

`member` uses `ancestors` for projects, so a team sees the projects of its branch, region and company too, but `own` for tasks and expenses.

## API

```python
class ProjectViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"approve": "update"}

    def get_queryset(self):
        return super().get_queryset().prefetch_related(
            self.policy_prefetch("tasks", Task.objects.all()))

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        project = self.get_object()
        before = self.policy.place_of(project)
        project.status = "approved"
        project.save()
        self.policy_changed(project, before)
        return Response({"status": project.status})


class TaskViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    permission_classes = [PolicyPermission]


class OrganizationSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "code", "type", "parent", "path"]
        read_only_fields = ["path"]

    def validate(self, attrs):
        # DRF doesn't call model.clean(); run the hierarchy rule on a copy for a 400 instead of a 500.
        candidate = Organization(**{f.attname: getattr(self.instance, f.attname)
                                    for f in Organization._meta.concrete_fields}) if self.instance else Organization()
        for key, value in attrs.items():
            setattr(candidate, key, value)
        try:
            candidate.check_hierarchy()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        return attrs
```

`TaskViewSet` needs no settings: a new task names its `project`, and the user must be allowed to create tasks in that project's unit.

## Result

| Request | Outcome |
|---|---|
| Manager of North lists projects | every project in North, Lisbon, Porto and their teams |
| Manager of North creates a project in South | 403 |
| Manager of North moves a project to South | 403, rolled back |
| Manager of North edits an archived project | refused (still visible, read-only) |
| Member of Sales lists projects | Sales, Lisbon, North and Acme projects, without `budget` |
| Member of Sales marks their own task done | 200 |
| Member of Sales renames their own task | 200, but `title` is ignored (read-only for them) |
| Member of Sales edits an approved expense | refused |
| Viewer in South opens a North project | 404 |
| Admin creates a team directly under a region | 400: must be under a branch |
