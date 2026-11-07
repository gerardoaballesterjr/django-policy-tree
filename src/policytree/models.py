import uuid

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models, transaction
from django.db.models import Value
from django.db.models.functions import Concat, Substr

from .conf import PolicyTreeSettings
from .registry import ConditionRegistry, ScopeRegistry


class OrganizationQuerySet(models.QuerySet):
    """
    Blocks writes that would skip path upkeep. Use save() to move one
    organization; bulk_create() rebuilds paths afterwards.
    """

    GUARDED = {"parent", "parent_id", "path"}
    MESSAGE = (
        "Changing parent or path in bulk would leave the tree's paths wrong. "
        "Save each organization instead, or run the policytree_rebuild_paths command."
    )

    def update(self, **kwargs):
        if self.GUARDED & set(kwargs):
            raise ValueError(self.MESSAGE)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, *args, **kwargs):
        if self.GUARDED & set(fields):
            raise ValueError(self.MESSAGE)
        return super().bulk_update(objs, fields, *args, **kwargs)

    def bulk_create(self, objs, *args, **kwargs):
        created = super().bulk_create(objs, *args, **kwargs)
        self.model.rebuild_paths()
        return created


class AbstractOrganization(models.Model):
    """
    Base for the project's organization tree. Subclass it and add your own
    fields, e.g. a `type` field (company/region/team, hospital/ward, tenant/workspace, ...).

    `path` is maintained automatically: "1/4/9/" means 9 is under 4 under 1.
    Any primary key type works; UUIDs are stored as 32-character hex, so each
    level takes 33 characters (15 levels fit in the 512-character path).
    """

    name = models.CharField(max_length=150)
    code = models.SlugField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    path = models.CharField(max_length=512, db_index=True, editable=False, default="")

    objects = OrganizationQuerySet.as_manager()

    class Meta:
        abstract = True

    def __str__(self):
        return self.name

    # ---- tree helpers

    @staticmethod
    def path_segment(pk):
        """How a primary key is written into `path` (UUIDs as hex)."""
        return pk.hex if isinstance(pk, uuid.UUID) else str(pk)

    def ancestor_ids(self, include_self=True):
        to_python = type(self)._meta.pk.to_python
        ids = [to_python(part) for part in self.path.split("/") if part]
        return ids if include_self else ids[:-1]

    def ancestors(self, include_self=True):
        return type(self).objects.filter(pk__in=self.ancestor_ids(include_self))

    def descendants(self, include_self=False):
        qs = type(self).objects.filter(path__startswith=self.path)
        return qs if include_self else qs.exclude(pk=self.pk)

    def is_within(self, other):
        """True if self is `other` or anywhere below it."""
        return bool(other.path) and self.path.startswith(other.path)

    # ---- validation and path upkeep

    def _would_create_cycle(self):
        if not self.pk or not self.parent_id:
            return False
        if self.parent_id == self.pk:
            return True
        parent_path = (
            type(self)._base_manager.filter(pk=self.parent_id).values_list("path", flat=True).first() or ""
        )
        return f"/{self.path_segment(self.pk)}/" in f"/{parent_path}"

    def clean(self):
        super().clean()
        if self._would_create_cycle():
            raise ValidationError({"parent": "An organization can't be placed under itself or its own children."})

    def save(self, *args, **kwargs):
        if self._would_create_cycle():
            raise ValidationError({"parent": "An organization can't be placed under itself or its own children."})

        base = type(self)._base_manager  # plain manager: path writes are allowed here
        with transaction.atomic():
            super().save(*args, **kwargs)

            parent_path = ""
            if self.parent_id:
                parent_path = base.filter(pk=self.parent_id).values_list("path", flat=True).first() or ""
            new_path = f"{parent_path}{self.path_segment(self.pk)}/"
            old_path = base.filter(pk=self.pk).values_list("path", flat=True).first() or ""

            if new_path != old_path:
                base.filter(pk=self.pk).update(path=new_path)
                if old_path:
                    # Move the whole subtree in one statement.
                    base.filter(path__startswith=old_path).exclude(pk=self.pk).update(
                        path=Concat(
                            Value(new_path),
                            Substr("path", len(old_path) + 1),
                            output_field=models.CharField(),
                        )
                    )
            self.path = new_path

    @classmethod
    def rebuild_paths(cls):
        """Recompute every path from `parent`. Returns how many were fixed."""
        base = cls._base_manager
        rows = list(base.values_list("pk", "parent_id", "path"))
        children = {}
        for pk, parent_id, _ in rows:
            children.setdefault(parent_id, []).append(pk)
        current = {pk: path for pk, _, path in rows}

        fixed = 0
        with transaction.atomic():
            queue = [(pk, "") for pk in children.get(None, [])]
            while queue:
                pk, parent_path = queue.pop()
                path = f"{parent_path}{cls.path_segment(pk)}/"
                if current.get(pk) != path:
                    base.filter(pk=pk).update(path=path)
                    fixed += 1
                queue.extend((child, path) for child in children.get(pk, []))
        return fixed


class RuleQuerySet(models.QuerySet):
    """
    Policy and Role querysets. Bulk changes skip model signals, so they clear the
    caches here. They are not written to the audit log; prefer save(), the admin
    or policytree_seed.
    """

    @staticmethod
    def _changed():
        from .engine import PolicyCache
        from .roles import Roles

        PolicyCache.clear()
        Roles.clear()

    def update(self, **kwargs):
        count = super().update(**kwargs)
        self._changed()
        return count

    def delete(self):
        result = super().delete()
        self._changed()
        return result

    def bulk_create(self, objs, *args, **kwargs):
        created = super().bulk_create(objs, *args, **kwargs)
        self._changed()
        return created

    def bulk_update(self, objs, fields, *args, **kwargs):
        count = super().bulk_update(objs, fields, *args, **kwargs)
        self._changed()
        return count


class Role(models.Model):
    """
    A role superusers manage in the admin. Used when the project's Member.role
    is a RoleField. Policies refer to roles by `code`, so the code can't change.
    """

    code = models.SlugField(
        max_length=50, unique=True, help_text="Used in policies and can't be changed later, e.g. manager."
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    includes = models.ManyToManyField(
        "self",
        symmetrical=False,
        blank=True,
        related_name="included_in",
        help_text="This role also gets every policy of these roles.",
    )
    is_active = models.BooleanField(default=True, help_text="Inactive roles grant nothing.")
    updated_at = models.DateTimeField(auto_now=True)

    objects = RuleQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def snapshot(self):
        includes = sorted(self.includes.values_list("code", flat=True)) if self.pk else []
        return {
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "is_active": self.is_active,
            "includes": includes,
        }

    def policy_count(self):
        return Policy.objects.filter(role=self.code).count()

    def member_count(self):
        from .roles import Roles

        if not Roles.uses_model():
            return 0
        return PolicyTreeSettings.get_member_model()._base_manager.filter(role_id=self.code).count()

    def in_use(self):
        return bool(self.policy_count() or self.member_count())

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.filter(pk=self.pk).values_list("code", flat=True).first()
            if old is not None and old != self.code:
                raise ValidationError({"code": "A role's code can't be changed; policies refer to it."})
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        policies = list(Policy.objects.filter(role=self.code)[:20])
        if policies:
            raise models.ProtectedError(
                f"Role '{self.code}' is used by {len(policies)}+ policies. Deactivate it instead.",
                set(policies),
            )
        return super().delete(*args, **kwargs)


class RoleField(models.ForeignKey):
    """
    Member.role pointing at an admin-managed Role (stored by its code):

        class Member(AbstractMember):
            role = RoleField()
    """

    def __init__(self, to="policytree.Role", on_delete=models.PROTECT, **kwargs):
        kwargs.setdefault("to_field", "code")
        kwargs.setdefault("related_name", "+")
        super().__init__(to, on_delete=on_delete, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        return name, "policytree.models.RoleField", args, kwargs


class AbstractMember(models.Model):
    """
    Base for memberships. Subclass it and add a `role` field with your own
    choices (admin/manager/member/viewer, ...).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
    )
    organization = models.ForeignKey(
        PolicyTreeSettings.organization_model_label(),
        on_delete=models.CASCADE,
        related_name="members",
    )

    class Meta:
        abstract = True
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization", "role"],
                name="%(app_label)s_%(class)s_unique_role",
            )
        ]

    def __str__(self):
        return f"{self.user} · {getattr(self, 'role', '?')} · {self.organization}"


class Policy(models.Model):
    """
    One rule: members with `role` are allowed (or denied) `action` on
    `content_type`, within `scope`, optionally only when `condition` holds.
    """

    class Action(models.TextChoices):
        LIST = "list", "List"
        CREATE = "create", "Create"
        DETAIL = "detail", "Detail"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"

    class Effect(models.TextChoices):
        ALLOW = "allow", "Allow"
        DENY = "deny", "Deny (overrides allow)"

    code = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=150)
    role = models.CharField(max_length=50, db_index=True)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    action = models.CharField(max_length=10, choices=Action.choices)
    effect = models.CharField(max_length=5, choices=Effect.choices, default=Effect.ALLOW)
    scope = models.CharField(max_length=30, default="org", help_text="A registered scope name.")
    condition = models.CharField(
        max_length=50, blank=True, help_text="Optional registered condition name."
    )
    readable_fields = models.JSONField(
        default=list, blank=True, help_text="Field names this role may see. Empty = all fields."
    )
    writable_fields = models.JSONField(
        default=list, blank=True, help_text="Field names this role may set. Empty = all fields."
    )
    updated_at = models.DateTimeField(auto_now=True)

    objects = RuleQuerySet.as_manager()

    class Meta:
        ordering = ["content_type", "role", "action"]
        verbose_name_plural = "policies"
        constraints = [
            models.UniqueConstraint(
                fields=["role", "content_type", "action", "effect", "condition"],
                name="policytree_unique_rule",
            )
        ]

    def __str__(self):
        verb = "may" if self.effect == self.Effect.ALLOW else "may not"
        when = f" when {self.condition}" if self.condition else ""
        return f"{self.role} {verb} {self.action} {self.content_type.model} ({self.scope}){when}"

    def snapshot(self):
        """A plain dict of this policy, used by the audit log and export."""
        return {
            "code": self.code,
            "name": self.name,
            "role": self.role,
            "model": self.content_type.app_label + "." + self.content_type.model,
            "action": self.action,
            "effect": self.effect,
            "scope": self.scope,
            "condition": self.condition,
            "readable_fields": list(self.readable_fields),
            "writable_fields": list(self.writable_fields),
        }

    def clean(self):
        super().clean()
        errors = {}

        from .roles import Roles

        known = Roles.known()
        if known is not None and self.role not in known:
            listed = ", ".join(sorted(known)) or "none yet; create roles first"
            errors["role"] = f"Unknown role. Choose one of: {listed}."

        if self.scope not in ScopeRegistry.names():
            errors["scope"] = f"Unknown scope. Choose one of: {', '.join(ScopeRegistry.names())}."

        model = self.content_type.model_class() if self.content_type_id else None

        if self.condition:
            try:
                condition = ConditionRegistry.get(self.condition)
            except ImproperlyConfigured:
                errors["condition"] = "Unknown condition. Is it registered?"
            else:
                if model is not None and not condition.applies_to(model):
                    errors["condition"] = f"This condition can't be used on {model.__name__}."

        if self.effect == self.Effect.DENY and (self.readable_fields or self.writable_fields):
            errors["readable_fields"] = "Deny policies can't list fields; leave both field lists empty."

        if model is not None:
            names = {f.name for f in model._meta.get_fields()} | {
                f.attname for f in model._meta.concrete_fields
            }
            for attr in ("readable_fields", "writable_fields"):
                value = getattr(self, attr)
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    errors[attr] = "Must be a list of field names."
                    continue
                unknown = sorted(set(value) - names)
                if unknown:
                    errors[attr] = f"Unknown fields on {model.__name__}: {', '.join(unknown)}."

        if errors:
            raise ValidationError(errors)


class PolicyChange(models.Model):
    """Audit log: one row per policy or role create, update or delete."""

    class Kind(models.TextChoices):
        CREATED = "created", "Created"
        UPDATED = "updated", "Updated"
        DELETED = "deleted", "Deleted"

    class Target(models.TextChoices):
        POLICY = "policy", "Policy"
        ROLE = "role", "Role"

    target = models.CharField(max_length=10, choices=Target.choices, default=Target.POLICY)
    policy_code = models.CharField(max_length=100, db_index=True, verbose_name="code")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    data = models.JSONField(default=dict)
    source = models.CharField(max_length=20, default="code")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-pk"]

    def __str__(self):
        return f"{self.target} {self.policy_code} {self.kind} ({self.source})"
