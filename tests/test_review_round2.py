"""
Regression tests for the second review pass. Each is the reviewer's proof of a
bypass or defect, turned around to assert it is now closed.
"""
import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.db.models import Q
from django.test import override_settings
from rest_framework import serializers, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from policytree.cache import SafeCache
from policytree.conf import PolicyTreeSettings
from policytree.drf import PolicyFieldsMixin, PolicyListSerializer, PolicyPermission, PolicyQuerysetMixin
from policytree.engine import PolicyEngine
from policytree.models import Policy
from policytree.registry import Condition, ConditionRegistry
from tests.conftest import client_for, rule
from tests.test_spaces import campus  # noqa: F401  (fixture)
from tests.testapp.api import EnrollmentSerializer
from tests.testapp.models import Enrollment, Invoice, Member, Organization, Room

pytestmark = pytest.mark.django_db


def as_view(viewset, actions, user, method, url, data=None, **kw):
    req = getattr(APIRequestFactory(), method)(url, data, format="json")
    force_authenticate(req, user)
    view_kwargs = {k: v for k, v in kw.items() if k == "detail"}
    call_kwargs = {k: v for k, v in kw.items() if k != "detail"}
    return viewset.as_view(actions, **view_kwargs)(req, **call_kwargs)


# R1 ----------------------------------------------------------------------------
class SmallAmount(Condition):
    name = "r2_small_amount"
    models = ["testapp.Invoice"]

    def q(self, user):
        return Q(amount__lt=100)


@pytest.fixture
def small_invoices(billing):
    ConditionRegistry.register(SmallAmount)
    p = Policy.objects.get(role="registrar", content_type=ContentType.objects.get_for_model(Invoice))
    p.condition = "r2_small_amount"
    p.save()
    yield billing
    ConditionRegistry.unregister("r2_small_amount")


def test_orm_write_with_explicit_recheck(small_invoices):
    reg, cy = small_invoices["u"]["registrar"], small_invoices["e"]["branch"]
    c = client_for(reg)
    assert c.post(f"/enrollments/{cy.pk}/grade_and_bill/", {"amount": "1000000"}, format="json").status_code == 403
    assert not Invoice.objects.exists()
    cy.refresh_from_db()
    assert cy.grade == ""  # the enrollment change was rolled back too
    assert c.post(f"/enrollments/{cy.pk}/grade_and_bill/", {"amount": "50", "grade": "B"},
                  format="json").status_code == 200


class ForgetfulViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    """A custom action that writes a second model and forgets to re-check it."""

    queryset = Enrollment.objects.order_by("id")
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"charge": ("create", Invoice), "peek": ("list", Invoice)}

    @action(detail=True, methods=["post"])
    def charge(self, request, pk=None):
        e = self.get_object()
        Invoice.objects.create(organization=e.organization, owner=request.user, number="F", amount=request.data["amount"])
        return Response({"ok": True})

    @action(detail=False)
    def peek(self, request):
        return Response({"numbers": list(Invoice.objects.values_list("number", flat=True))})


def test_unconfirmed_precheck_fails_closed(small_invoices):
    reg, cy = small_invoices["u"]["registrar"], small_invoices["e"]["branch"]
    r = as_view(ForgetfulViewSet, {"post": "charge"}, reg, "post", "/x/", {"amount": "5"}, pk=cy.pk, detail=True)
    assert r.status_code == 403
    assert not Invoice.objects.filter(number="F").exists()


def test_unconfirmed_read_of_a_second_model_is_refused(billing):
    Invoice.objects.create(organization=billing["other"], owner=billing["u"]["root"], number="SECRET", amount=1)
    r = as_view(ForgetfulViewSet, {"get": "peek"}, billing["u"]["accounting"], "get", "/x/", detail=False)
    assert r.status_code == 403
    assert "SECRET" not in str(r.data)


def test_unconditional_second_model_needs_no_extra_step(billing):
    r = client_for(billing["u"]["accounting"]).post(
        f"/enrollments/{billing['e']['branch'].pk}/bill/", {"number": "B1", "amount": "5"}, format="json")
    assert r.status_code == 201


# R2 ----------------------------------------------------------------------------
class NoQuerysetViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]

    def get_queryset(self):  # skips the mixin's get_queryset
        return Enrollment.objects.order_by("id")


def test_get_queryset_override_is_still_scoped(world):
    prof = world["u"]["prof"]
    r = as_view(NoQuerysetViewSet, {"get": "list"}, prof, "get", "/x/")
    assert sorted(row["student_name"] for row in r.data) == ["Ana", "Ben"]
    r = as_view(NoQuerysetViewSet, {"get": "retrieve"}, prof, "get", "/x/", pk=world["e"]["other"].pk)
    assert r.status_code == 404


# R3 ----------------------------------------------------------------------------
class BulkEditList(PolicyListSerializer):
    def update(self, instances, validated_data):
        by_id = {i.pk: i for i in instances}
        return [self.child.update(by_id[row["id"]], row) for row in validated_data]


class BulkEnrollmentSerializer(PolicyFieldsMixin, serializers.ModelSerializer):
    id = serializers.IntegerField()

    class Meta:
        model = Enrollment
        fields = ["id", "organization", "student_name", "tuition"]
        list_serializer_class = BulkEditList


class BulkViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Enrollment.objects.order_by("id")
    serializer_class = BulkEnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"bulk": "update"}

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        rows = self.get_queryset().filter(pk__in=[r["id"] for r in request.data])
        ser = self.get_serializer(rows, data=request.data, many=True, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response({"ok": True})


def test_list_serializer_updates_are_rechecked(world):
    reg, cy = world["u"]["registrar"], world["e"]["branch"]
    r = as_view(BulkViewSet, {"post": "bulk"}, reg, "post", "/x/bulk/",
                [{"id": cy.pk, "organization": str(world["other"].pk)}], detail=False)
    assert r.status_code == 403
    cy.refresh_from_db()
    assert cy.organization_id == world["branch"].pk
    r = as_view(BulkViewSet, {"post": "bulk"}, reg, "post", "/x/bulk/",
                [{"id": cy.pk, "student_name": "Cyrus"}], detail=False)
    assert r.status_code == 200
    cy.refresh_from_db()
    assert cy.student_name == "Cyrus"


# R4 ----------------------------------------------------------------------------
class MovingViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Enrollment.objects.order_by("id")
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]

    def perform_update(self, serializer):
        obj = serializer.save()
        transfer = self.request.data.get("transfer_to")
        if transfer:
            obj.organization = Organization.objects.get(pk=transfer)
            obj.save()


def test_changes_after_the_serializer_save_are_rechecked(world):
    reg, cy = world["u"]["registrar"], world["e"]["branch"]
    r = as_view(MovingViewSet, {"patch": "partial_update"}, reg, "patch", "/x/1/",
                {"grade": "B", "transfer_to": str(world["other"].pk)}, pk=cy.pk)
    assert r.status_code == 403
    cy.refresh_from_db()
    assert (cy.organization_id, cy.grade) == (world["branch"].pk, "")


# R5 ----------------------------------------------------------------------------
@pytest.mark.parametrize("field", ["path", "id", "parent", "parent_id"])
def test_bulk_update_refuses_structural_fields(campus, field):  # noqa: F811
    prof, dept = campus["u"]["prof"], campus["dept"]
    rule("professor", Organization, "update", "org")
    with pytest.raises(ValueError):
        PolicyEngine.for_user(prof).bulk_update(Organization.objects.filter(pk=dept.pk), **{field: "x/"})
    dept.refresh_from_db()
    assert [r.code for r in PolicyEngine.for_user(prof).filter(Room.objects.all(), "list")] == ["lab-1"]


def test_bulk_update_on_organizations_still_works_for_plain_fields(campus):  # noqa: F811
    prof, dept = campus["u"]["prof"], campus["dept"]
    rule("professor", Organization, "update", "org")
    assert PolicyEngine.for_user(prof).bulk_update(Organization.objects.all(), name="Maths") == 1


# R6 ----------------------------------------------------------------------------
def test_own_scope_deny_makes_record_free_answers_provisional(billing):
    acc, branch = billing["u"]["accounting"], billing["branch"]
    rule("accounting", Invoice, "delete", "org")
    rule("accounting", Invoice, "delete", "own", effect="deny")
    eng = PolicyEngine.for_user(acc)
    assert eng.can_in(Invoice, "delete", branch) is False
    assert eng.assess(Invoice, "delete", branch) == (True, True)
    mine = Invoice.objects.create(organization=branch, owner=acc, number="M", amount=1)
    theirs = Invoice.objects.create(organization=branch, owner=billing["u"]["root"], number="T", amount=1)
    assert not eng.can("delete", mine) and eng.can("delete", theirs)


# R7 ----------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def enrollment_fbv_with_view(request, pk):
    obj = Enrollment.objects.get(pk=pk)
    view = enrollment_fbv_with_view.cls()
    return Response(EnrollmentSerializer(obj, context={"request": request, "view": view}).data)


def test_mixin_serializer_in_a_function_view(world):
    req = APIRequestFactory().get("/x/")
    force_authenticate(req, world["u"]["prof"])
    r = enrollment_fbv_with_view(req, pk=world["e"]["dept_s1"].pk)
    assert r.status_code == 200 and "tuition" not in r.data


# Cache edges ---------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [(None, 300), (0, 300), (-5, 300), ("x", 300), (60, 60), (10**9, 3600)])
def test_cache_timeout_is_bounded(value, expected):
    with override_settings(POLICYTREE_CACHE_TIMEOUT=value):
        assert PolicyTreeSettings.cache_timeout() == expected


@pytest.mark.django_db(transaction=True)
def test_manual_transactions_never_reach_the_shared_cache(world):
    from django.core.cache import cache

    key = PolicyTreeSettings.cache_key()
    cache.delete(key)
    transaction.set_autocommit(False)
    try:
        rule("staff", Enrollment, "detail")
        PolicyEngine.for_user(world["u"]["admin"]).can("detail", world["e"]["school"])
        assert cache.get(key) is None
        transaction.rollback()
    finally:
        transaction.set_autocommit(True)
    assert not Policy.objects.filter(role="staff").exists()


@pytest.mark.django_db(transaction=True)
def test_commit_clears_even_after_a_failing_callback(world):
    import django
    from django.core.cache import cache

    if django.VERSION < (4, 2):
        pytest.skip("robust on_commit needs Django 4.2")
    key = PolicyTreeSettings.cache_key()
    PolicyEngine.for_user(world["u"]["admin"]).has_any(Enrollment, "list")  # fill the cache
    with transaction.atomic():
        transaction.on_commit(lambda: 1 / 0, robust=True)
        rule("staff", Enrollment, "detail")
        cache.set(key, {"stale": True})  # another worker refills it before commit
    assert cache.get(key) is None


def test_reads_use_the_write_database():
    assert SafeCache.database() == "default"
    assert connection.alias == "default"


# Views and serializers ---------------------------------------------------------------
def test_view_submitted_fields_come_from_the_post(world):
    from django import forms

    from policytree.views import PolicyViewMixin

    class F(forms.Form):
        a = forms.CharField(required=False)
        b = forms.CharField(required=False, disabled=True)
        c = forms.CharField(required=False)

    form = F(data={"a": "", "b": "x"})
    assert PolicyViewMixin.policy_submitted(form) == {"a"}


def test_hidden_defaults_are_not_counted_as_submitted(world):
    from policytree.drf import PolicySaveCheck

    class S(serializers.ModelSerializer):
        owner = serializers.HiddenField(default=serializers.CurrentUserDefault())

        class Meta:
            model = Enrollment
            fields = ["organization", "student_name", "owner"]

    assert PolicySaveCheck.submitted(S(), {"organization": 1, "student_name": "x", "owner": 2}) == {
        "organization", "student_name"}


def test_list_creates_must_return_every_row(world):
    from rest_framework.exceptions import PermissionDenied as DRFDenied
    from rest_framework.request import Request

    class Short(PolicyListSerializer):
        def create(self, validated_data):
            return [self.child.create(validated_data[0])]

    class S(PolicyFieldsMixin, serializers.ModelSerializer):
        class Meta:
            model = Enrollment
            fields = ["organization", "student_name"]
            list_serializer_class = Short

    request = Request(APIRequestFactory().post("/"))
    request.user = world["u"]["registrar"]
    rows = [{"organization": str(world["dept"].pk), "student_name": n} for n in ("p", "q")]
    ser = S(data=rows, many=True, context={"request": request})
    ser.is_valid(raise_exception=True)
    with pytest.raises(DRFDenied), transaction.atomic():
        ser.save(owner=world["u"]["registrar"])
    assert not Enrollment.objects.filter(student_name__in=["p", "q"]).exists()


def test_view_without_a_model_is_a_configuration_error():
    from django.views.generic import ListView

    from policytree.views import PolicyViewMixin

    class Nothing(PolicyViewMixin, ListView):
        pass

    with pytest.raises(ImproperlyConfigured):
        Nothing().get_policy_model()


def test_member_soft_delete_and_policy_queryset_update_together(world):
    """A bulk policy change and a membership change in one transaction both apply immediately."""
    prof = world["u"]["prof"]
    with transaction.atomic():
        Policy.objects.filter(role="professor").update(scope="below")
        Member.objects.filter(user=prof).delete()
        assert PolicyEngine.for_user(prof).filter(Enrollment.objects.all(), "list").count() == 0
