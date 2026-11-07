# UUIDs, soft delete, archiving

The abstract models combine with your own base model:

```python
class Organization(BaseModel, AbstractOrganization): ...
class Member(BaseModel, AbstractMember): ...
```

## A complete base model

```python
import uuid
from django.db import models
from django.utils import timezone


class BaseQuerySet(models.QuerySet):
    def delete(self):                     # soft delete
        return self.update(deleted_at=timezone.now())

    def hard_delete(self):
        return super().delete()


class LiveManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class BaseModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = LiveManager.from_queryset(BaseQuerySet)()
    all_objects = models.Manager.from_queryset(BaseQuerySet)()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def archive(self):
        self.archived_at = timezone.now()
        self.save(update_fields=["archived_at"])
```

## The organization model

Keep the package's path guards by combining querysets, and make codes unique only among live rows:

```python
from django.db.models import Q
from policytree.models import AbstractOrganization, OrganizationQuerySet


class OrgQuerySet(BaseQuerySet, OrganizationQuerySet):
    pass


class Organization(BaseModel, AbstractOrganization):
    code = models.SlugField(max_length=50)
    objects = LiveManager.from_queryset(OrgQuerySet)()
    all_objects = models.Manager.from_queryset(OrgQuerySet)()

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["code"], condition=Q(deleted_at__isnull=True), name="uniq_live_org_code")]

    def delete(self, using=None, keep_parents=False):
        self.descendants(include_self=True).update(deleted_at=timezone.now())
```

`manage.py check` warns (`policytree.W003`) when the default manager loses the guards.

## What the engine does with it

- **UUIDs** work everywhere; paths store them as hex.
- **Soft-deleted memberships** grant nothing, nor do memberships in soft-deleted organizations, because the engine uses your default managers.
- **API deletes** call your model's `delete()`, so they're soft. `engine.bulk_delete()` works with soft-delete querysets too.
- **Malformed ids** in requests return 400, not a server error.

## Archived means read-only

Register a condition and add deny policies:

```python
@ConditionRegistry.register
class Archived(Condition):
    name = "archived"
    label = "Archived records"

    def q(self, user):
        return Q(archived_at__isnull=False)
```

| Role | Model | Action | Effect | Scope | Condition |
|---|---|---|---|---|---|
| manager | project | update | deny | tree | archived |
| manager | project | delete | deny | tree | archived |

Archived projects stay visible and can't be changed.
