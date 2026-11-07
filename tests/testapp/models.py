"""Example project models: a school using the package, on a UUID + soft-delete base model."""
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from policytree.models import AbstractMember, AbstractOrganization, OrganizationQuerySet, RoleField

from .base import BaseModel, BaseQuerySet, LiveManager


class OrganizationQuerySetWithSoftDelete(BaseQuerySet, OrganizationQuerySet):
    """Soft delete from BaseQuerySet, path guards from the package."""


class Organization(BaseModel, AbstractOrganization):
    class Type(models.TextChoices):
        SCHOOL = "school", "School"
        BRANCH = "branch", "Branch"
        DIVISION = "division", "Division"
        DEPARTMENT = "department", "Department"

    type = models.CharField(max_length=20, choices=Type.choices)
    code = models.SlugField(max_length=50)  # unique among live rows only (see Meta)

    objects = LiveManager.from_queryset(OrganizationQuerySetWithSoftDelete)()
    all_objects = models.Manager.from_queryset(OrganizationQuerySetWithSoftDelete)()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["code"], condition=Q(deleted_at__isnull=True), name="uniq_live_org_code"
            )
        ]

    def delete(self, using=None, keep_parents=False):
        """Soft-delete this organization and everything below it."""
        self.descendants(include_self=True).update(deleted_at=timezone.now())
        self.deleted_at = timezone.now()


class Member(BaseModel, AbstractMember):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        STAFF = "staff", "Staff"
        ACCOUNTING = "accounting", "Accounting"
        REGISTRAR = "registrar", "Registrar"
        PROFESSOR = "professor", "Professor"
        STUDENT = "student", "Student"

    role = models.CharField(max_length=20, choices=Role.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization", "role"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_live_membership",
            )
        ]


class Enrollment(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    student_name = models.CharField(max_length=100)
    grade = models.CharField(max_length=5, blank=True)
    tuition = models.DecimalField(max_digits=10, decimal_places=2, default=0)


class Invoice(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    enrollment = models.ForeignKey(
        Enrollment, null=True, blank=True, on_delete=models.CASCADE, related_name="invoices"
    )
    number = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.TextField(blank=True)


class Payment(models.Model):
    """Reaches its organization and owner only through the invoice."""

    policytree_organization_field = "invoice__organization"
    policytree_owner_field = "invoice__owner"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)


class Space(BaseModel):
    """Belongs to a branch; policies reach it through `branch`."""

    policytree_organization_field = "branch"

    @property
    def room_list(self):
        """A plain list, to test nested serializers fed with lists."""
        return list(self.rooms.all())

    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=50)
    branch = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="spaces")


class Room(BaseModel):
    """Belongs to a space; policies reach its branch through the space."""

    policytree_organization_field = "space__branch"

    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=50)
    capacity = models.PositiveIntegerField()
    space = models.ForeignKey(Space, on_delete=models.CASCADE, related_name="rooms")


class RoleMember(AbstractMember):
    """A membership whose role is managed in the admin (used by test_roles_admin.py)."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="role_members")
    role = RoleField()


class BadRoleMember(AbstractMember):
    """Misconfigured on purpose: role points at Role by primary key, not code."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="bad_role_members")
    role = models.ForeignKey("policytree.Role", on_delete=models.PROTECT, related_name="+")
