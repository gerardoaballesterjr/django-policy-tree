"""
Regression tests for the independent security review (0.1). Each test is the
reviewer's proof of a bypass, turned around to assert the bypass now fails.
"""
import io
import json

import pytest
from django.core.management import call_command
from django.db import transaction
from django.db.models import Q
from django.test import Client, override_settings
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from policytree.drf import PolicyFieldsMixin, PolicyPermission, PolicyQuerysetMixin
from policytree.engine import PolicyEngine, Stage
from policytree.models import Policy, Role
from policytree.registry import Condition, ConditionRegistry
from tests.conftest import client_for, names, rule
from tests.test_spaces import campus  # noqa: F401  (fixture)
from tests.testapp.api import EnrollmentSerializer
from tests.testapp.models import Enrollment, Invoice, Member, Organization, RoleMember

pytestmark = pytest.mark.django_db


class PlainEnrollmentSerializer(serializers.ModelSerializer):
    """No PolicyFieldsMixin: the viewset's own re-check must still apply."""

    class Meta:
        model = Enrollment
        fields = ["id", "organization", "student_name", "grade", "tuition"]


class ProbeViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Enrollment.objects.select_related("organization").order_by("id")
    serializer_class = EnrollmentSerializer
    permission_classes = [PolicyPermission]
    policy_actions = {"clone": "create", "edit": "update", "lookup": "create", "bulk": "create"}

    @action(detail=True, methods=["post"])
    def clone(self, request, pk=None):
        src = self.get_object()
        return Response({"student_name": src.student_name, "tuition": str(src.tuition)})

    @action(detail=True, methods=["post"])
    def edit(self, request, pk=None):
        ser = self.get_serializer(self.get_object(), data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)

    @action(detail=False, methods=["post"])
    def lookup(self, request):
        return Response({"rows": self.get_queryset().count()})

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        ser = EnrollmentSerializer(data=request.data["rows"], many=True, context=self.get_serializer_context())
        ser.is_valid(raise_exception=True)
        ser.save(owner=request.user)
        return Response({"created": len(ser.data)}, status=201)


class PlainViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
    queryset = Enrollment.objects.all()
    serializer_class = PlainEnrollmentSerializer
    permission_classes = [PolicyPermission]

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


def call(view, user, name, data, pk=None, method="post"):
    factory = APIRequestFactory()
    url = f"/x/{pk}/{name}/" if pk else f"/x/{name}/"
    req = getattr(factory, method)(url, data, format="json")
    force_authenticate(req, user)
    kwargs = {"pk": pk} if pk else {}
    return view.as_view({method: name}, detail=pk is not None)(req, **kwargs)


# F1 ----------------------------------------------------------------------------
def test_detail_action_mapped_to_create_needs_the_object_visible(world):
    r = call(ProbeViewSet, world["u"]["registrar"], "clone", {"organization": str(world["branch"].pk)},
             pk=world["e"]["other"].pk)
    assert r.status_code == 404


# T2 ----------------------------------------------------------------------------
def test_create_actions_see_no_existing_rows(world):
    # A "create" action sees no rows, and one that never creates (and checks) a
    # record can't settle its owed create check either, so it is refused.
    r = call(ProbeViewSet, world["u"]["registrar"], "lookup", {"organization": str(world["branch"].pk)})
    assert r.status_code == 403


# F2 ----------------------------------------------------------------------------
def test_member_cannot_regraft_own_organization(campus):  # noqa: F811
    prof = campus["u"]["prof"]
    rule("professor", Organization, "update", "org")
    c = client_for(prof)
    assert c.patch(f"/organizations/{campus['dept'].pk}/", {"name": "Maths"}, format="json").status_code == 200
    r = c.patch(f"/organizations/{campus['dept'].pk}/", {"parent": str(campus["south_branch"].pk)}, format="json")
    assert r.status_code == 403
    campus["dept"].refresh_from_db()
    assert campus["dept"].parent_id == campus["branch"].pk
    assert [row["code"] for row in client_for(prof).get("/rooms/").json()] == ["lab-1"]


def test_admin_can_move_inside_their_tree_but_not_out(world):
    admin = world["u"]["admin"]  # tree at North School: may create and update organizations there
    c = client_for(admin)
    other_branch = Organization.objects.create(name="Second", code="second", type="branch", parent=world["school"])
    assert c.patch(f"/organizations/{world['dept'].pk}/", {"parent": str(other_branch.pk)},
                   format="json").status_code == 200
    r = c.patch(f"/organizations/{world['dept'].pk}/", {"parent": str(world["other"].pk)}, format="json")
    assert r.status_code == 403  # South School is outside their tree
    r = c.patch(f"/organizations/{world['branch'].pk}/", {"parent": None}, format="json")
    assert r.status_code == 403  # detaching to the top needs a superuser


# F3 ----------------------------------------------------------------------------
def test_custom_update_action_is_rechecked(world):
    cy = world["e"]["branch"]
    r = call(ProbeViewSet, world["u"]["registrar"], "edit", {"organization": str(world["other"].pk)}, pk=cy.pk)
    assert r.status_code == 403
    cy.refresh_from_db()
    assert cy.organization_id == world["branch"].pk


def test_serializers_without_the_mixin_are_rechecked_too(world):
    reg = world["u"]["registrar"]
    cy = world["e"]["branch"]
    r = call(PlainViewSet, reg, "partial_update", {"organization": str(world["other"].pk)}, pk=cy.pk, method="patch")
    assert r.status_code == 403
    cy.refresh_from_db()
    assert cy.organization_id == world["branch"].pk


# F4 ----------------------------------------------------------------------------
class Always(Condition):
    name = "review_always"

    def q(self, user):
        return Q(pk__isnull=False)


def test_conditional_deny_on_second_model_applies(billing):
    ConditionRegistry.register(Always)
    try:
        rule("staff", Invoice, "create", effect="deny", scope="tree", condition="review_always")
        r = client_for(billing["u"]["cashier"]).post(
            "/enrollments/issue/", {"organization": str(billing["branch"].pk), "number": "X1", "amount": "5"},
            format="json")
        assert r.status_code == 403
        assert not Invoice.objects.filter(number="X1").exists()
    finally:
        ConditionRegistry.unregister("review_always")


# T4 ----------------------------------------------------------------------------
def test_bulk_creates_are_checked_row_by_row(world):
    reg = world["u"]["registrar"]
    rows = [{"organization": str(world["dept"].pk), "student_name": "ok"},
            {"organization": str(world["other"].pk), "student_name": "sneaky"}]
    r = call(ProbeViewSet, reg, "bulk", {"rows": rows, "organization": str(world["branch"].pk)})
    assert r.status_code == 403
    assert not Enrollment.objects.filter(student_name__in=["ok", "sneaky"]).exists()


# F5 ----------------------------------------------------------------------------
def test_create_fields_are_judged_where_the_record_lands(world):
    u = world["u"]["nobody"]
    Member.objects.create(user=u, organization=world["dept"], role="staff")
    Member.objects.create(user=u, organization=world["other"], role="accounting")
    rule("staff", Enrollment, "create", "org", write=["organization", "student_name"])
    rule("staff", Enrollment, "detail", "org")
    rule("accounting", Enrollment, "create", "org")
    c = client_for(u)
    r = c.post("/enrollments/", {"organization": str(world["dept"].pk), "student_name": "Zed",
                                 "tuition": "9999", "grade": "A+"}, format="json")
    assert r.status_code == 403
    assert not Enrollment.objects.filter(student_name="Zed").exists()
    r = c.post("/enrollments/", {"organization": str(world["dept"].pk), "student_name": "Zed"}, format="json")
    assert r.status_code == 201
    r = c.post("/enrollments/", {"organization": str(world["other"].pk), "student_name": "Yan",
                                 "tuition": "9999"}, format="json")
    assert r.status_code == 201


# F6 ----------------------------------------------------------------------------
def test_nested_list_fields_follow_list_rules(billing):
    acc = billing["u"]["accounting"]
    rule("accounting", Invoice, "detail", "own")
    enr = billing["e"]["branch"]
    Invoice.objects.create(organization=billing["branch"], owner=billing["u"]["registrar"],
                           enrollment=enr, number="N1", amount=1, notes="SECRET")
    inv = client_for(acc).get(f"/enrollments/{enr.pk}/full/").json()["invoices"][0]
    assert "notes" not in inv and set(inv) == {"id", "number", "amount"}


# F7 ----------------------------------------------------------------------------
def test_bulk_update_respects_writable_fields(world):
    prof = world["u"]["prof"]  # may write only "grade"
    assert PolicyEngine.for_user(prof).bulk_update(Enrollment.objects.all(), tuition=1) == 0
    assert PolicyEngine.for_user(prof).bulk_update(Enrollment.objects.all(), grade="B") == 2
    assert set(Enrollment.objects.filter(organization=world["dept"]).values_list("tuition", flat=True)) == {100, 200}


def test_bulk_update_respects_after_conditions(world):
    p = Policy.objects.get(role="registrar", action="update")
    p.condition = "small_tuition"
    p.save()
    from django.core.exceptions import PermissionDenied

    reg = world["u"]["registrar"]
    with pytest.raises(PermissionDenied):
        PolicyEngine.for_user(reg).bulk_update(Enrollment.objects.all(), tuition=50000)
    assert not Enrollment.objects.filter(tuition=50000).exists()
    assert PolicyEngine.for_user(reg).bulk_update(Enrollment.objects.all(), tuition=10) == 3


# F8 ----------------------------------------------------------------------------
def test_rolled_back_policy_grants_nothing(world):
    u = world["u"]["nobody"]
    Member.objects.create(user=u, organization=world["other"], role="staff")
    ed = world["e"]["other"]
    with pytest.raises(RuntimeError), transaction.atomic():
        rule("staff", Enrollment, "detail")
        assert PolicyEngine.for_user(u).can("detail", ed)  # visible inside its own transaction
        raise RuntimeError("a later step fails")
    assert not Policy.objects.filter(role="staff").exists()
    assert not PolicyEngine.for_user(u).can("detail", ed)


# F9 ----------------------------------------------------------------------------
@override_settings(POLICYTREE_MEMBER_MODEL="testapp.RoleMember")
def test_seed_dry_run_changes_nothing_at_all(world, tmp_path):
    Role.objects.create(code="librarian", name="Librarian", is_active=False)
    user = world["u"]["nobody"]
    RoleMember.objects.create(user=user, organization=world["dept"], role_id="librarian")
    rule("librarian", Enrollment, "list")
    assert client_for(user).get("/enrollments/").status_code == 403
    f = tmp_path / "p.json"
    f.write_text(json.dumps({
        "roles": [{"code": "librarian", "name": "Librarian", "is_active": True}],
        "policies": [{"code": "x", "model": "testapp.Enrollment", "role": "librarian", "action": "detail"}],
    }))
    call_command("policytree_seed", str(f), "--dry-run", stdout=io.StringIO())
    assert client_for(user).get("/enrollments/").status_code == 403


# F10 / T1 -------------------------------------------------------------------------
def test_siblings_at_the_top_reach_only_itself(world):
    p = Policy.objects.get(role="admin", action="list", content_type__model="enrollment")
    p.scope = "siblings"
    p.save()
    assert names(client_for(world["u"]["admin"]).get("/enrollments/")) == ["Di"]


# F11 ---------------------------------------------------------------------------
def test_update_form_hides_values_it_cannot_show(world):
    u = world["u"]["nobody"]
    Member.objects.create(user=u, organization=world["dept"], role="staff")
    rule("staff", Enrollment, "detail", "org", read=["student_name"])
    rule("staff", Enrollment, "update", "org", write=["tuition"])
    c = Client()
    c.force_login(u)
    form = c.get(f"/web/{world['e']['dept_s1'].pk}/edit/").context["form"]
    assert "tuition" in form.fields
    assert form["tuition"].value() in (None, "")


# F12 ---------------------------------------------------------------------------
@override_settings(POLICYTREE_MEMBER_MODEL="testapp.RoleMember")
def test_bulk_role_and_policy_changes_clear_the_cache(world):
    Role.objects.create(code="librarian", name="Librarian")
    user = world["u"]["nobody"]
    RoleMember.objects.create(user=user, organization=world["dept"], role_id="librarian")
    rule("librarian", Enrollment, "list")
    assert names(client_for(user).get("/enrollments/")) == ["Ana", "Ben"]
    Role.objects.filter(code="librarian").update(is_active=False)
    assert client_for(user).get("/enrollments/").status_code == 403
    Role.objects.filter(code="librarian").update(is_active=True)
    Policy.objects.filter(role="librarian").update(scope="below")
    assert client_for(user).get("/enrollments/").json() == []


# F13 ---------------------------------------------------------------------------
def test_list_body_on_create_is_a_400(world):
    r = client_for(world["u"]["registrar"]).post(
        "/enrollments/", [{"organization": str(world["branch"].pk), "student_name": "x"}], format="json")
    assert r.status_code == 400


# T3 ----------------------------------------------------------------------------
def test_own_scope_needs_a_record_for_second_models(billing):
    rule("staff", Invoice, "update", "own")
    engine = PolicyEngine.for_user(billing["u"]["cashier"])
    assert not engine.can_in(Invoice, "update", billing["school"])
    assert engine.can_in(Invoice, "create", billing["school"])  # tree create still applies


# T6 ----------------------------------------------------------------------------
def test_viewset_without_a_model_is_a_configuration_error():
    from django.core.exceptions import ImproperlyConfigured

    from policytree.drf import DRFActionMap

    class NoModel:
        action = "list"
        detail = False

    with pytest.raises(ImproperlyConfigured, match="needs a `queryset`"):
        DRFActionMap.requirements(NoModel())


# T8 ----------------------------------------------------------------------------
def test_unknown_scopes_and_conditions_fail_closed(world):
    reg = world["u"]["registrar"]
    ed, cy = world["e"]["other"], world["e"]["branch"]
    Policy.objects.filter(role="registrar", action="list").update(scope="vanished")
    engine = PolicyEngine.for_user(reg)
    assert list(engine.filter(Enrollment.objects.all(), "list")) == []
    assert not engine.can("list", cy)

    rule("registrar", Enrollment, "detail", "tree", effect="deny", condition="vanished")
    engine = PolicyEngine.for_user(reg)
    assert not engine.can("detail", cy)                                  # unknown deny blocks
    assert list(engine.filter(Enrollment.objects.all(), "detail")) == []
    assert not engine.can("detail", ed)
