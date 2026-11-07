# Recipe: education

One more tree, to show the same package in a different domain: a school with branches, divisions and departments; facilities at the branch; academic terms and sections at the division; programs at the department.

```
North School                       admin
└─ North Branch                    staff, accounting, registrar   ← spaces, rooms
   └─ Engineering Division                                        ← terms, periods, sections
      └─ CS Department             professor, student             ← programs
```

## Models

```python
# school/models.py
from django.core.exceptions import ValidationError
from django.db import models
from policytree.models import AbstractMember, AbstractOrganization, RoleField


class Organization(AbstractOrganization):
    class Type(models.TextChoices):
        SCHOOL = "school"
        BRANCH = "branch"
        DIVISION = "division"
        DEPARTMENT = "department"

    PARENT_TYPE = {Type.SCHOOL: None, Type.BRANCH: Type.SCHOOL,
                   Type.DIVISION: Type.BRANCH, Type.DEPARTMENT: Type.DIVISION}

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
    role = RoleField()          # roles managed by superusers in the admin

    class Meta(AbstractMember.Meta):
        pass


def must_be(org, kind, field):
    if org is not None and org.type != kind:
        raise ValidationError({field: f"Must be a {kind}."})


class Space(models.Model):
    policytree_organization_field = "branch"
    name = models.CharField(max_length=100)         # Computer Lab, Lecture Space
    code = models.SlugField()
    branch = models.ForeignKey(Organization, on_delete=models.CASCADE)

    def clean(self):
        must_be(self.branch, "branch", "branch")


class Room(models.Model):
    policytree_organization_field = "space__branch"
    name = models.CharField(max_length=100)
    code = models.SlugField()
    capacity = models.PositiveIntegerField()
    space = models.ForeignKey(Space, on_delete=models.CASCADE)


class Term(models.Model):
    policytree_organization_field = "division"
    name = models.CharField(max_length=100)         # 1st Sem, SY2027
    code = models.SlugField()
    start = models.DateField()
    end = models.DateField()
    division = models.ForeignKey(Organization, on_delete=models.CASCADE)

    def clean(self):
        must_be(self.division, "division", "division")


class Period(models.Model):
    policytree_organization_field = "term__division"
    name = models.CharField(max_length=100)         # Prelim, 1st Quarter
    code = models.SlugField()
    term = models.ForeignKey(Term, on_delete=models.CASCADE)


class Program(models.Model):
    policytree_organization_field = "department"
    name = models.CharField(max_length=100)
    code = models.SlugField()
    department = models.ForeignKey(Organization, on_delete=models.CASCADE)

    def clean(self):
        must_be(self.department, "department", "department")


class Section(models.Model):
    policytree_organization_field = "term__division"   # term is required; program isn't
    name = models.CharField(max_length=100)
    code = models.SlugField()
    term = models.ForeignKey(Term, on_delete=models.CASCADE)
    program = models.ForeignKey(Program, null=True, blank=True, on_delete=models.SET_NULL)
```

## Roles

Created in the admin, or seeded:

```json
{"roles": [
  {"code": "admin",      "name": "Admin", "includes": ["staff", "registrar"]},
  {"code": "staff",      "name": "Staff"},
  {"code": "accounting", "name": "Accounting"},
  {"code": "registrar",  "name": "Registrar"},
  {"code": "professor",  "name": "Professor"},
  {"code": "student",    "name": "Student"}
]}
```

## Policies

| Role | Member of | Models | Actions | Scope | Notes |
|---|---|---|---|---|---|
| admin | school | organization | all | tree | manages the structure |
| staff | branch | space, room | all | tree | manages facilities |
| registrar | branch | term, period, section | all | tree | academic calendar |
| registrar | branch | room | list, detail | tree | for scheduling |
| professor | department | room, term, period, section | list, detail | ancestors | what they teach in |
| professor | department | program | list, detail | org | own department's programs |
| student | department | term, period, section | list, detail | ancestors | read only |
| accounting | branch | term | list, detail | tree | read only |

Add a deny with an `archived` condition on `update` and `delete` if you archive terms.

## Views

No per-view settings are needed: the create target comes from each model's path (`branch`, `space`, `division`, `term`, `department`, and `parent` for organizations).

```python
class RoomViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Room.objects.all()
    serializer_class = RoomSerializer
    permission_classes = [PolicyPermission]


class OrganizationSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "code", "type", "parent", "path"]
        read_only_fields = ["path"]

    def validate(self, attrs):
        # DRF doesn't call model.clean(); run the hierarchy rule for a 400 instead of a 500.
        candidate = self.instance or Organization()
        for key, value in attrs.items():
            setattr(candidate, key, value)
        try:
            candidate.check_hierarchy()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        return attrs
```

## Result

| Request | Outcome |
|---|---|
| Staff of North Branch creates a room in a North Branch space | 201 |
| Staff of North Branch creates a room in a South Branch space | 403 |
| Staff of North Branch moves a room to a South Branch space | 403, rolled back |
| Professor in CS lists rooms | North Branch rooms only |
| Professor in CS edits a room | 403 |
| Student in CS lists sections | sections of Engineering Division's terms |
| Admin creates a department directly under a branch | 400: must be under a division |

## Variation: departments see only their programs' sections

Sections belong to the division through their term. To limit a department to sections of its own programs, add a condition:

```python
@ConditionRegistry.register
class OwnProgramSections(Condition):
    name = "own_program_sections"
    models = ["school.Section"]

    def q(self, user):
        return Q(program__department__members__user=user)
```

and use it on the professor's section policies.
