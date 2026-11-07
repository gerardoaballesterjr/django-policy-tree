"""
A project-side base model: UUID primary key, created/deleted/archived timestamps.

    Model.objects          live rows only (not soft-deleted)
    Model.all_objects      everything, including soft-deleted rows
    obj.delete()           soft delete;  obj.hard_delete() removes the row
    obj.archive()          read-only by policy (see the `archived` condition)
"""
import uuid

from django.db import models
from django.utils import timezone


class BaseQuerySet(models.QuerySet):
    def delete(self):
        """Soft delete. Returns the number of rows marked."""
        return self.update(deleted_at=timezone.now())

    def hard_delete(self):
        return super().delete()

    def archived(self):
        return self.filter(archived_at__isnull=False)

    def active(self):
        return self.filter(archived_at__isnull=True)


class LiveManager(models.Manager):
    """Hides soft-deleted rows."""

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

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    @property
    def is_archived(self):
        return self.archived_at is not None

    def delete(self, using=None, keep_parents=False):
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def hard_delete(self, using=None, keep_parents=False):
        return super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=["deleted_at"])

    def archive(self):
        self.archived_at = timezone.now()
        self.save(update_fields=["archived_at"])

    def unarchive(self):
        self.archived_at = None
        self.save(update_fields=["archived_at"])
