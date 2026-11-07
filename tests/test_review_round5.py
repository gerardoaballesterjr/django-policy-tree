"""
Regression tests for the fifth review pass: per-record write tracking, read-only
policy_queryset, owed own-model updates in custom actions, cascades, guards.
Each bypass is turned around, next to the legitimate way to write the action.
"""
import pytest
from django.core.exceptions import ImproperlyConfigured
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from policytree.drf import PolicyPermission, PolicyQuerysetMixin
from tests.conftest import client_for, rule
from tests.test_spaces import campus  # noqa: F401  (fixture)
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


# S1 ----------------------------------------------------------------------------
class ReassignViewSet(Base):
    policy_actions = {"reassign": ("update", Invoice), "annotate": ("update", Invoice)}

    @action(detail=True, methods=["post"])
    def reassign(self, request, pk=None):
        e = self.get_object()
        n = self.policy_queryset(Invoice.objects.filter(enrollment=e)).update(organization_id=request.data["org"])
        return Response({"n": n})

    @action(detail=True, methods=["post"])
    def annotate(self, request, pk=None):
        e = self.get_object()
        n = self.policy_update(Invoice.objects.filter(enrollment=e), notes=request.data["notes"])
        return Response({"n": n})


def test_policy_queryset_does_not_confirm_changes(billing):
    reg, cy = billing["u"]["registrar"], billing["e"]["branch"]
    rule("registrar", Invoice, "update", "tree", write=["notes"])
    inv = Invoice.objects.create(organization=billing["branch"], owner=reg, enrollment=cy, number="I", amount=1)
    r = call(ReassignViewSet, {"post": "reassign"}, reg, "post", {"org": str(billing["other"].pk)}, detail=True, pk=cy.pk)
    assert r.status_code == 403
    inv.refresh_from_db()
    assert inv.organization_id == billing["branch"].pk
    r = call(ReassignViewSet, {"post": "annotate"}, reg, "post", {"notes": "ok"}, detail=True, pk=cy.pk)
    assert r.status_code == 200 and r.data == {"n": 1}


# S2 ----------------------------------------------------------------------------
class BulkMoveViewSet(Base):
    policy_actions = {"bulk_move": "update", "bulk_grade": "update"}

    @action(detail=False, methods=["post"])
    def bulk_move(self, request):
        return Response({"n": self.get_queryset().update(organization_id=request.data["org"])})

    @action(detail=False, methods=["post"])
    def bulk_grade(self, request):
        return Response({"n": self.policy_update(self.get_queryset(), grade=request.data["grade"])})


def test_list_level_own_updates_must_be_checked(world):
    reg = world["u"]["registrar"]
    r = call(BulkMoveViewSet, {"post": "bulk_move"}, reg, "post", {"org": str(world["other"].pk)}, detail=False)
    assert r.status_code == 403
    assert Enrollment.objects.filter(organization=world["other"]).count() == 1
    r = call(BulkMoveViewSet, {"post": "bulk_grade"}, reg, "post", {"grade": "A"}, detail=False)
    assert r.status_code == 200 and r.data == {"n": 3}


# S3 ----------------------------------------------------------------------------
class MoveOneViewSet(Base):
    policy_actions = {"move": "update", "move_checked": "update"}

    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        e = self.get_object()
        e.organization_id = request.data["org"]
        e.save()
        return Response({"ok": True})

    @action(detail=True, methods=["post"])
    def move_checked(self, request, pk=None):
        e = self.get_object()
        before = self.policy.place_of(e)
        e.organization_id = request.data["org"]
        e.save()
        self.policy_changed(e, before)
        return Response({"ok": True})


def test_orm_changes_in_detail_actions_must_be_checked(world):
    reg, cy = world["u"]["registrar"], world["e"]["branch"]
    r = call(MoveOneViewSet, {"post": "move"}, reg, "post", {"org": str(world["other"].pk)}, detail=True, pk=cy.pk)
    assert r.status_code == 403
    r = call(MoveOneViewSet, {"post": "move_checked"}, reg, "post", {"org": str(world["other"].pk)},
             detail=True, pk=cy.pk)
    assert r.status_code == 403
    r = call(MoveOneViewSet, {"post": "move_checked"}, reg, "post", {"org": str(world["dept"].pk)},
             detail=True, pk=cy.pk)
    assert r.status_code == 200
    cy.refresh_from_db()
    assert cy.organization_id == world["dept"].pk


# S4 ----------------------------------------------------------------------------
class TwoInvoicesViewSet(Base):
    policy_actions = {"bill_twice": ("create", Invoice)}

    @action(detail=True, methods=["post"])
    def bill_twice(self, request, pk=None):
        e = self.get_object()
        ser = InvoiceSerializer(data={"number": "ok", "amount": "1"}, context=self.get_serializer_context())
        ser.is_valid(raise_exception=True)
        ser.save(enrollment=e, organization=e.organization, owner=request.user)
        Invoice.objects.create(organization_id=request.data["org"], owner=request.user, number="sneak", amount=1)
        return Response({"ok": True})


def test_each_written_record_must_be_checked(billing):
    reg, cy = billing["u"]["registrar"], billing["e"]["branch"]
    r = call(TwoInvoicesViewSet, {"post": "bill_twice"}, reg, "post", {"org": str(billing["other"].pk)},
             detail=True, pk=cy.pk)
    assert r.status_code == 403
    assert not Invoice.objects.exists()


def test_records_changed_after_their_check_are_rechecked(billing):
    class SaveAgain(Base):
        policy_actions = {"bill": ("create", Invoice)}

        @action(detail=True, methods=["post"])
        def bill(self, request, pk=None):
            e = self.get_object()
            ser = InvoiceSerializer(data={"number": "n", "amount": "1"}, context=self.get_serializer_context())
            ser.is_valid(raise_exception=True)
            invoice = ser.save(enrollment=e, organization=e.organization, owner=request.user)
            invoice.organization_id = request.data["org"]
            invoice.save()  # after its check
            return Response({"ok": True})

    r = call(SaveAgain, {"post": "bill"}, billing["u"]["registrar"], "post", {"org": str(billing["other"].pk)},
             detail=True, pk=billing["e"]["branch"].pk)
    assert r.status_code == 403
    assert not Invoice.objects.exists()


# S5 ----------------------------------------------------------------------------
def test_cascades_into_the_viewsets_models_are_checked(billing):
    admin = billing["u"]["admin"]  # may delete enrollments, has no invoice rules
    di = billing["e"]["school"]
    inv = Invoice.objects.create(organization=billing["other"], owner=billing["u"]["root"], enrollment=di,
                                 number="foreign", amount=1)
    assert client_for(admin).delete(f"/enrollments/{di.pk}/").status_code == 403
    assert Enrollment.objects.filter(pk=di.pk).exists() and Invoice.objects.filter(pk=inv.pk).exists()
    rule("admin", Invoice, "delete", "tree")
    inv.organization = billing["school"]
    inv.save()
    assert client_for(admin).delete(f"/enrollments/{di.pk}/").status_code == 204


def test_soft_delete_through_destroy_still_works(campus):  # noqa: F811
    from tests.testapp.models import Room

    room = campus["rooms"]["lab1"]
    assert client_for(campus["staff"]).delete(f"/rooms/{room.pk}/").status_code == 204
    assert Room.all_objects.get(pk=room.pk).deleted_at is not None



# S6 ----------------------------------------------------------------------------
class MaybeBillViewSet(Base):
    policy_actions = {"maybe_bill": ("create", Invoice)}

    @action(detail=True, methods=["post"])
    def maybe_bill(self, request, pk=None):
        self.get_object()
        if request.data.get("bill"):
            Invoice.objects.create(organization_id=request.data["org"], owner=request.user, number="m", amount=1)
            return Response({"created": True})
        self.policy_skip(Invoice, "create")  # nothing to bill today
        return Response({"created": False})


def test_policy_skip_waives_only_the_check_not_the_writes(billing):
    reg, cy = billing["u"]["registrar"], billing["e"]["branch"]
    assert call(MaybeBillViewSet, {"post": "maybe_bill"}, reg, "post", {}, detail=True, pk=cy.pk).status_code == 200


# S7 and schema generation ---------------------------------------------------------
class LateViewSet(Base):
    policy_actions = {"names": "list"}

    @action(detail=False)
    def names(self, request):
        return Response(sorted(e.student_name for e in self.get_queryset()))


LateViewSet.get_queryset = lambda self: Enrollment.objects.order_by("id")


def test_get_queryset_replaced_later_is_refused(world):
    with pytest.raises(ImproperlyConfigured, match="replaced after the class was created"):
        call(LateViewSet, {"get": "names"}, world["u"]["prof"], "get", detail=False)


def test_schema_generation_without_a_request_sees_no_rows(world):
    view = Base()
    view.action = "list"
    assert list(view.get_queryset()) == []
