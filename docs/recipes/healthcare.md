# Recipe: a hospital group

Hospitals with departments and wards. Patient admissions belong to a ward; clinical notes belong to an admission; billing fields are visible only to billing staff.

```
CareGroup (group)                  administrator
└─ City Hospital (hospital)        billing
   ├─ Cardiology (department)      head_of_department
   │  ├─ Ward 3A (ward)            head_nurse, nurse, doctor
   │  └─ Ward 3B (ward)            nurse
   └─ Pediatrics (department)
```

## Models

```python
# care/models.py
from django.conf import settings
from django.db import models
from policytree.models import AbstractMember, AbstractOrganization, RoleField


class Organization(AbstractOrganization):
    type = models.CharField(max_length=20, choices=[
        ("group", "Group"), ("hospital", "Hospital"),
        ("department", "Department"), ("ward", "Ward"),
    ])


class Member(AbstractMember):
    role = RoleField()          # roles managed by superusers in the admin

    class Meta(AbstractMember.Meta):
        pass


class Admission(models.Model):
    policytree_owner_field = "attending"
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)   # the ward
    attending = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    patient_name = models.CharField(max_length=200)
    diagnosis = models.TextField(blank=True)
    status = models.CharField(max_length=20, default="admitted")   # admitted, discharged
    insurer = models.CharField(max_length=100, blank=True)
    amount_due = models.DecimalField(max_digits=12, decimal_places=2, default=0)


class Note(models.Model):
    policytree_organization_field = "admission__organization"
    policytree_owner_field = "author"
    admission = models.ForeignKey(Admission, on_delete=models.CASCADE, related_name="notes")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    text = models.TextField()
```

## Roles

Seeded once, then managed in the admin:

```json
{"roles": [
  {"code": "administrator",      "name": "Administrator"},
  {"code": "head_of_department", "name": "Head of department", "includes": ["doctor"]},
  {"code": "head_nurse",         "name": "Head nurse",         "includes": ["nurse"]},
  {"code": "doctor",             "name": "Doctor"},
  {"code": "nurse",              "name": "Nurse"},
  {"code": "billing",            "name": "Billing"}
]}
```

## Conditions

```python
# care/policies.py
@ConditionRegistry.register
class Discharged(Condition):
    name = "discharged"
    models = ["care.Admission"]

    def q(self, user):
        return Q(status="discharged")
```

## Policies

| Role | Model | Action | Effect | Scope | Condition | Fields |
|---|---|---|---|---|---|---|
| administrator | organization | all | allow | tree | | |
| head_of_department | admission | list, detail | allow | tree | | |
| doctor | admission | list, detail, create, update | allow | org | | read, write: id, organization, attending, patient_name, diagnosis, status |
| doctor | note | list, detail, create | allow | org | | |
| doctor | note | update | allow | own | | |
| nurse | admission | list, detail | allow | org | | read: id, patient_name, diagnosis, status, organization |
| nurse | note | list, detail, create | allow | org | | |
| head_nurse | admission | create | allow | org | | write: organization, patient_name, attending |
| anyone clinical | admission | update | deny | tree | discharged | |
| billing | admission | list, detail | allow | tree | | read: id, patient_name, status, insurer, amount_due |
| billing | admission | update | allow | tree | | write: insurer, amount_due |

"Anyone clinical" means one deny row each for `doctor` and `nurse` (heads inherit them). Billing has no deny, so they can still settle a discharged patient's bill, but they never see diagnoses.

## API

```python
class AdmissionViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Admission.objects.all()
    serializer_class = AdmissionSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"discharge": "update"}

    @action(detail=True, methods=["post"])
    def discharge(self, request, pk=None):
        admission = self.get_object()
        before = self.policy.place_of(admission)
        admission.status = "discharged"
        admission.save()
        self.policy_changed(admission, before)
        return Response({"status": admission.status})


class NoteViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Note.objects.all()
    serializer_class = NoteSerializer
    permission_classes = [PolicyPermission]

    def perform_create(self, serializer):
        serializer.save(author=self.request.user)
```

## Result

| Request | Outcome |
|---|---|
| Nurse in Ward 3A lists admissions | Ward 3A only, without billing fields |
| Nurse in Ward 3A opens a Ward 3B admission | 404 |
| Doctor in Ward 3A edits another doctor's note | 403 |
| Doctor in Ward 3A discharges a patient, then edits the diagnosis | 200, then refused: the admission is now read-only for clinical staff |
| Head of Cardiology lists admissions | every Cardiology ward |
| Billing lists admissions | the whole hospital, only billing fields |
| Billing changes a diagnosis | ignored (not writable) |
