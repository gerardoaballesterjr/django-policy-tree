"""
Regression tests for the third review pass: the owed-check safety net, request
transactions, and scoping of overridden querysets. Each is the reviewer's
proof turned around, plus the legitimate way to write the same action.
"""
import pytest
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from policytree.drf import PolicyFieldsMixin, PolicyListSerializer, PolicyPermission, PolicyQuerysetMixin
from policytree.engine import PolicyEngine
from policytree.models import Policy
from policytree.registry import Condition, ConditionRegistry
from tests.conftest import rule
from tests.testapp.api import EnrollmentSerializer, EnrollmentWithInvoicesSerializer
from tests.testapp.models import Enrollment, Invoice

pytestmark = pytest.mark.django_db


def call(viewset, actions, user, method, data=None, **kwargs):
    req = getattr(APIRequestFactory(), method)("/x/", data, format="json")
    force_authenticate(req, user)
    detail = kwargs.pop("detail", None)
    view = viewset.as_view(actions, **({"detail": detail} if detail is not None else {}))
    resp = view(req, **kwargs)
    resp.render()
    return resp


class SmallAmount(Condition):
    name = "r3_small"
    models = ["testapp.Invoice"]

    def q(self, user):
        return Q(amount__lt=100)


@pytest.fixture
def small_amount(billing):
    ConditionRegistry.register(SmallAmount)
    p = Policy.objects.get(role="registrar", content_type__model="invoice", action="create")
    p.condition = "r3_small"
    p.save()
    yield billing
    ConditionRegistry.unregister("r3_small")


# Q1 ----------------------------------------------------------------------------
class OverviewViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Enrollment.objects.order_by("id")
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"overview": [("list", None), ("list", Invoice)], "careful": [("list", None), ("list", Invoice)]}

    def _rows(self):
        return EnrollmentWithInvoicesSerializer(self.get_queryset(), many=True, context=self.get_serializer_context()).data

    @action(detail=False)
    def overview(self, request):
        total = Invoice.objects.aggregate(t=Sum("amount"))["t"]  # forgot policy_queryset()
        return Response({"rows": self._rows(), "total": str(total)})

    @action(detail=False)
    def careful(self, request):
        total = self.policy_queryset(Invoice.objects.all()).aggregate(t=Sum("amount"))["t"]
        return Response({"rows": self._rows(), "total": str(total)})


def test_nested_serializers_do_not_confirm_owed_checks(billing):
    reg = billing["u"]["registrar"]
    rule("registrar", Invoice, "list", "tree")
    Invoice.objects.create(organization=billing["other"], owner=billing["u"]["root"], number="S", amount=777)
    r = call(OverviewViewSet, {"get": "overview"}, reg, "get", detail=False)
    assert r.status_code == 403 and b"777" not in r.content
    r = call(OverviewViewSet, {"get": "careful"}, reg, "get", detail=False)
    assert r.status_code == 200 and r.data["total"] == "None"


def test_engine_checks_on_other_records_do_not_confirm(billing):
    engine = PolicyEngine.for_user(billing["u"]["registrar"])
    engine.owe(Invoice, "list")
    engine.filter(Invoice.objects.all(), "list")
    engine.can("list", Invoice(organization=billing["branch"], number="x", amount=1))
    assert engine.owed == {(Invoice, "list")}
    engine.confirm(Invoice, "list")
    assert engine.owed == set()


# Q2 ----------------------------------------------------------------------------
class GetBillViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Enrollment.objects.order_by("id")
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"quick_bill": ("create", Invoice)}

    @action(detail=True)  # GET
    def quick_bill(self, request, pk=None):
        e = self.get_object()
        Invoice.objects.create(organization=e.organization, owner=request.user, number="Q", amount=10 ** 6)
        return Response({"ok": True})


def test_writes_in_get_actions_are_rolled_back(small_amount):
    cy = small_amount["e"]["branch"]
    r = call(GetBillViewSet, {"get": "quick_bill"}, small_amount["u"]["registrar"], "get", pk=cy.pk, detail=True)
    assert r.status_code == 403
    assert not Invoice.objects.filter(number="Q").exists()


# Q3 ----------------------------------------------------------------------------
class NoMixinViewSet(viewsets.ModelViewSet):
    queryset = Enrollment.objects.order_by("id")
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"quick_bill": ("create", Invoice), "peek": ("list", Invoice)}

    @action(detail=True, methods=["post"])
    def quick_bill(self, request, pk=None):
        e = self.get_object()
        Invoice.objects.create(organization=e.organization, owner=request.user, number="N", amount=10 ** 6)
        return Response({"ok": True})

    @action(detail=False)
    def peek(self, request):
        return Response(list(Invoice.objects.values_list("number", flat=True)))


def test_permission_without_mixin_refuses_what_it_cannot_enforce(small_amount):
    reg, cy = small_amount["u"]["registrar"], small_amount["e"]["branch"]
    r = call(NoMixinViewSet, {"post": "quick_bill"}, reg, "post", {}, pk=cy.pk, detail=True)
    assert r.status_code == 403
    assert not Invoice.objects.filter(number="N").exists()
    rule("registrar", Invoice, "list", "tree")
    assert call(NoMixinViewSet, {"get": "peek"}, reg, "get", detail=False).status_code == 403


# Q4 ----------------------------------------------------------------------------
class TouchViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Enrollment.objects.order_by("id")
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"void": ("update", Invoice), "void_checked": ("update", Invoice)}

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        self.get_object()
        Invoice.objects.filter(pk=request.data["invoice"]).update(notes="VOID")
        return Response({"ok": True})

    @action(detail=True, methods=["post"])
    def void_checked(self, request, pk=None):
        self.get_object()
        invoice = get_object_or_404(self.policy_queryset(Invoice.objects.all(), "update"), pk=request.data["invoice"])
        before = self.policy.place_of(invoice)
        invoice.notes = "VOID"
        invoice.save()
        self.policy_changed(invoice, before)
        return Response({"ok": True})


def test_second_model_records_on_detail_actions_must_be_checked(billing):
    rule("registrar", Invoice, "update", "tree")
    foreign = Invoice.objects.create(organization=billing["other"], owner=billing["u"]["root"], number="F", amount=1)
    local = Invoice.objects.create(organization=billing["dept"], owner=billing["u"]["root"], number="L", amount=1)
    reg, cy = billing["u"]["registrar"], billing["e"]["branch"]
    r = call(TouchViewSet, {"post": "void"}, reg, "post", {"invoice": foreign.pk}, pk=cy.pk, detail=True)
    assert r.status_code == 403
    foreign.refresh_from_db()
    assert foreign.notes == ""
    r = call(TouchViewSet, {"post": "void_checked"}, reg, "post", {"invoice": foreign.pk}, pk=cy.pk, detail=True)
    assert r.status_code == 404
    r = call(TouchViewSet, {"post": "void_checked"}, reg, "post", {"invoice": local.pk}, pk=cy.pk, detail=True)
    assert r.status_code == 200
    local.refresh_from_db()
    assert local.notes == "VOID"


# Q5 ----------------------------------------------------------------------------
def test_own_scope_creates_are_provisional(billing):
    acc, branch = billing["u"]["accounting"], billing["branch"]
    Policy.objects.filter(role="accounting", content_type__model="invoice", action="create").update(scope="own")
    eng = PolicyEngine.for_user(acc)
    assert eng.assess(Invoice, "create", branch) == (True, True)
    assert eng.assess(Invoice, "create", billing["school"]) == (False, False)
    assert not eng.can_in(Invoice, "create", branch)
    mine = Invoice.objects.create(organization=branch, owner=acc, number="M", amount=1)
    theirs = Invoice.objects.create(organization=branch, owner=billing["u"]["registrar"], number="O", amount=1)
    assert eng.can_create(mine) and not eng.can_create(theirs)


# Q6 ----------------------------------------------------------------------------
class OverrideViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"names": "list"}

    def get_queryset(self):
        return Enrollment.objects.order_by("id")

    @action(detail=False)
    def names(self, request):
        return Response(sorted(e.student_name for e in self.get_queryset()))


class OverrideSubclass(OverrideViewSet):
    def get_queryset(self):
        return super().get_queryset().filter(grade="")


def test_overridden_get_queryset_is_always_scoped(world):
    prof = world["u"]["prof"]
    assert call(OverrideViewSet, {"get": "names"}, prof, "get", detail=False).data == ["Ana", "Ben"]
    assert call(OverrideSubclass, {"get": "names"}, prof, "get", detail=False).data == ["Ana", "Ben"]
    r = call(OverrideSubclass, {"get": "retrieve"}, prof, "get", pk=world["e"]["other"].pk)
    assert r.status_code == 404


# Q7 ----------------------------------------------------------------------------
class ValuesViewSet(PolicyQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    queryset = Enrollment.objects.all()

    def get_queryset(self):
        return Enrollment.objects.values("id", "student_name")

    def list(self, request):
        return Response(sorted(row["student_name"] for row in self.filter_queryset(self.get_queryset())))


class SlicedViewSet(ValuesViewSet):
    def get_queryset(self):
        return Enrollment.objects.all()[:2]


def test_values_querysets_are_scoped_and_sliced_ones_refused(world):
    assert call(ValuesViewSet, {"get": "list"}, world["u"]["prof"], "get", detail=False).data == ["Ana", "Ben"]
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(ImproperlyConfigured, match="unsliced"):
        call(SlicedViewSet, {"get": "list"}, world["u"]["prof"], "get", detail=False)


# Q8 ----------------------------------------------------------------------------
class ReturnNothing(PolicyListSerializer):
    def update(self, instances, validated_data):
        by_id = {i.pk: i for i in instances}
        for row in validated_data:
            self.child.update(by_id[row["id"]], row)
        return []


class BulkSer(PolicyFieldsMixin, serializers.ModelSerializer):
    id = serializers.IntegerField()

    class Meta:
        model = Enrollment
        fields = ["id", "organization", "student_name"]
        list_serializer_class = ReturnNothing


class BulkViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Enrollment.objects.order_by("id")
    serializer_class = BulkSer
    permission_classes = [PolicyPermission]
    policy_actions = {"bulk": "update"}

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        rows = self.get_queryset().filter(pk__in=[r["id"] for r in request.data])
        ser = self.get_serializer(rows, data=request.data, many=True, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response({"ok": True})


def test_list_updates_check_every_row_given(world):
    reg, cy = world["u"]["registrar"], world["e"]["branch"]
    r = call(BulkViewSet, {"post": "bulk"}, reg, "post", [{"id": cy.pk, "organization": str(world["other"].pk)}],
             detail=False)
    assert r.status_code == 403
    cy.refresh_from_db()
    assert cy.organization_id == world["branch"].pk


# Theoretical items ------------------------------------------------------------------
class SwallowingViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    """An exception handler that answers 200 must not keep the half-done write."""

    queryset = Enrollment.objects.order_by("id")
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"half": "update"}

    def get_exception_handler(self):
        return lambda exc, context: Response({"ok": "swallowed"}, status=200)

    @action(detail=True, methods=["post"])
    def half(self, request, pk=None):
        e = self.get_object()
        e.student_name = "half-done"
        e.save()
        raise ValueError("later step fails")


def test_exceptions_roll_back_even_if_answered_with_200(world):
    cy = world["e"]["branch"]
    r = call(SwallowingViewSet, {"post": "half"}, world["u"]["registrar"], "post", {}, pk=cy.pk, detail=True)
    assert r.status_code == 403  # the half-done write was never checked, so the handler's 200 is replaced
    cy.refresh_from_db()
    assert cy.student_name == "Cy"


def test_each_view_call_gets_a_fresh_engine(world):
    """Owed checks from one view call never carry into another sharing the HttpRequest."""
    from django.test import RequestFactory

    http = RequestFactory().get("/x/")
    http.user = world["u"]["registrar"]
    stale = PolicyEngine.for_request(http)
    stale.owe(Invoice, "list")
    force_authenticate(http, world["u"]["registrar"])
    r = OverrideViewSet.as_view({"get": "names"}, detail=False)(http)
    assert r.status_code == 200
