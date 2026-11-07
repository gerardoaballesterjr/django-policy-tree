"""
Regression tests for the fourth review pass, plus the lifecycle guards. Each
bypass is turned around, alongside the legitimate way to write the action.
"""
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from policytree.drf import PolicyPermission, PolicyQuerysetMixin
from policytree.engine import PolicyEngine
from policytree.registry import Condition, ConditionRegistry
from tests.conftest import rule
from tests.testapp.api import EnrollmentSerializer, InvoiceSerializer
from tests.testapp.models import Enrollment, Invoice

pytestmark = pytest.mark.django_db


def call(viewset, actions, user, method, data=None, detail=None, **kwargs):
    req = getattr(APIRequestFactory(), method)("/x/", data, format="json")
    force_authenticate(req, user)
    resp = viewset.as_view(actions, **({"detail": detail} if detail is not None else {}))(req, **kwargs)
    resp.render()
    return resp


class Base(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Enrollment.objects.order_by("id")
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]


# P1 ----------------------------------------------------------------------------
class PurgeViewSet(Base):
    policy_actions = {"purge": [("list", None), ("delete", None)]}

    @action(detail=False, methods=["post"])
    def purge(self, request):
        return Response({"deleted": self.get_queryset().delete()[0]})


def test_rows_must_allow_every_own_action(world):
    reg = world["u"]["registrar"]
    rule("registrar", Enrollment, "delete", "org")  # North Branch only
    r = call(PurgeViewSet, {"post": "purge"}, reg, "post", {}, detail=False)
    assert r.status_code == 200 and r.data == {"deleted": 1}
    assert not Enrollment.objects.filter(pk=world["e"]["branch"].pk).exists()
    assert Enrollment.objects.filter(pk=world["e"]["dept_s1"].pk).exists()


# P2 ----------------------------------------------------------------------------
class BillAnywhereViewSet(Base):
    policy_actions = {"bill_anywhere": ("create", Invoice), "bill_here": ("create", Invoice)}

    @action(detail=True, methods=["post"])
    def bill_anywhere(self, request, pk=None):
        self.get_object()
        Invoice.objects.create(organization_id=request.data["org"], owner=request.user, number="X", amount=1)
        return Response({"ok": True})

    @action(detail=True, methods=["post"])
    def bill_here(self, request, pk=None):
        e = self.get_object()
        ser = InvoiceSerializer(data=request.data, context=self.get_serializer_context())
        ser.is_valid(raise_exception=True)
        ser.save(organization=e.organization, enrollment=e, owner=request.user)
        return Response(ser.data, status=201)


def test_every_create_is_checked_on_the_saved_record(billing):
    reg, cy = billing["u"]["registrar"], billing["e"]["branch"]
    r = call(BillAnywhereViewSet, {"post": "bill_anywhere"}, reg, "post", {"org": str(billing["other"].pk)},
             detail=True, pk=cy.pk)
    assert r.status_code == 403
    assert not Invoice.objects.filter(number="X").exists()
    r = call(BillAnywhereViewSet, {"post": "bill_here"}, reg, "post", {"number": "H", "amount": "1"},
             detail=True, pk=cy.pk)
    assert r.status_code == 201


# P3 ----------------------------------------------------------------------------
class Big(Condition):
    name = "r4_big"
    models = ["testapp.Invoice"]

    def q(self, user):
        return Q(amount__gte=100)


class ClearViewSet(Base):
    policy_actions = {"clear_invoices": ("delete", Invoice), "clear_properly": ("delete", Invoice)}

    @action(detail=True, methods=["post"])
    def clear_invoices(self, request, pk=None):
        invoices = Invoice.objects.filter(enrollment=self.get_object()).order_by("pk")
        self.policy_require("delete", invoices.first())
        return Response({"deleted": invoices.delete()[0]})

    @action(detail=True, methods=["post"])
    def clear_properly(self, request, pk=None):
        return Response({"deleted": self.policy_delete(Invoice.objects.filter(enrollment=self.get_object()))})


@pytest.fixture
def two_invoices(billing):
    ConditionRegistry.register(Big)
    reg, cy = billing["u"]["registrar"], billing["e"]["branch"]
    rule("registrar", Invoice, "delete", "tree")
    rule("registrar", Invoice, "delete", "tree", effect="deny", condition="r4_big")
    Invoice.objects.create(organization=billing["branch"], owner=reg, enrollment=cy, number="s", amount=1)
    Invoice.objects.create(organization=billing["branch"], owner=reg, enrollment=cy, number="b", amount=5000)
    yield billing
    ConditionRegistry.unregister("r4_big")


def test_checking_one_record_does_not_confirm_a_bulk_delete(two_invoices):
    reg, cy = two_invoices["u"]["registrar"], two_invoices["e"]["branch"]
    r = call(ClearViewSet, {"post": "clear_invoices"}, reg, "post", {}, detail=True, pk=cy.pk)
    assert r.status_code == 403
    assert Invoice.objects.count() == 2
    r = call(ClearViewSet, {"post": "clear_properly"}, reg, "post", {}, detail=True, pk=cy.pk)
    assert r.status_code == 200 and r.data == {"deleted": 1}
    assert list(Invoice.objects.values_list("number", flat=True)) == ["b"]


class TouchUp(Base):
    policy_actions = {"grade_all": ("update", Enrollment)}

    @action(detail=False, methods=["post"])
    def grade_all(self, request):
        return Response({"changed": self.policy_update(Enrollment.objects.all(), grade=request.data["grade"])})


def test_policy_update_helper(world):
    r = call(TouchUp, {"post": "grade_all"}, world["u"]["prof"], "post", {"grade": "C"}, detail=False)
    assert r.status_code == 200 and r.data == {"changed": 2}


# P4 ----------------------------------------------------------------------------
class TenantMixin:
    def get_queryset(self):
        return Enrollment.objects.order_by("id")


class MixinViewSet(TenantMixin, PolicyQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"names": "list"}

    @action(detail=False)
    def names(self, request):
        return Response(sorted(e.student_name for e in self.get_queryset()))


def test_get_queryset_from_another_mixin_is_scoped(world):
    assert call(MixinViewSet, {"get": "names"}, world["u"]["prof"], "get", detail=False).data == ["Ana", "Ben"]


# Lifecycle guards -------------------------------------------------------------------
class SkipsDispatch(Base):
    def dispatch(self, request, *args, **kwargs):
        return viewsets.ModelViewSet.dispatch(self, request, *args, **kwargs)


class SkipsFinalize(Base):
    def finalize_response(self, request, response, *args, **kwargs):
        return viewsets.ModelViewSet.finalize_response(self, request, response, *args, **kwargs)


class SkipsInitial(Base):
    def initial(self, request, *args, **kwargs):
        return viewsets.ModelViewSet.initial(self, request, *args, **kwargs)

    policy_actions = {"peek": ("list", Invoice)}

    @action(detail=False)
    def peek(self, request):
        return Response(list(Invoice.objects.values_list("number", flat=True)))


@pytest.mark.parametrize("viewset", [SkipsDispatch, SkipsFinalize])
def test_overrides_that_skip_super_are_configuration_errors(world, viewset):
    with pytest.raises(ImproperlyConfigured, match="must call super"):
        call(viewset, {"get": "list"}, world["u"]["prof"], "get", detail=False)


def test_initial_override_that_skips_super_refuses_owed_checks(billing):
    rule("registrar", Invoice, "list", "tree")
    r = call(SkipsInitial, {"get": "peek"}, billing["u"]["registrar"], "get", detail=False)
    assert r.status_code == 403


def test_permission_alone_still_works_for_plain_reads(world):
    class Plain(viewsets.ReadOnlyModelViewSet):
        queryset = Enrollment.objects.order_by("id")
        serializer_class = EnrollmentSerializer
        permission_classes = [PolicyPermission]

    assert call(Plain, {"get": "list"}, world["u"]["prof"], "get", detail=False).status_code == 200
    assert not PolicyEngine.for_user(world["u"]["nobody"]).has_any(Enrollment, "list")
