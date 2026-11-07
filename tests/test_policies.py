import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core import checks
from django.core.exceptions import ValidationError
from django.test import override_settings
from rest_framework.test import APIClient

from policytree.engine import PolicyEngine
from policytree.models import Policy
from tests.testapp.models import Enrollment, Member, Organization

pytestmark = pytest.mark.django_db


def seg(org):
    return org.path_segment(org.pk)
User = get_user_model()

from tests.conftest import client_for, names, rule  # noqa: E402


# ------------------------------------------------------------------ tree

def test_paths_are_built_automatically(world):
    s, b, d = world["school"], world["branch"], world["dept"]
    assert s.path == f"{seg(s)}/"
    assert d.path == f"{seg(s)}/{seg(b)}/{seg(d)}/"
    assert list(d.ancestors().order_by("path")) == [s, b, d]
    assert set(s.descendants()) == {b, d}


def test_moving_a_node_moves_its_subtree(world):
    b, d, other = world["branch"], world["dept"], world["other"]
    b.parent = other
    b.save()
    d.refresh_from_db()
    assert d.path == f"{seg(other)}/{seg(b)}/{seg(d)}/"


def test_cycles_are_rejected(world):
    s, d = world["school"], world["dept"]
    s.parent = d
    with pytest.raises(ValidationError):
        s.save()


# ------------------------------------------------------------------ scopes

def test_tree_scope_sees_everything_below(world):
    r = client_for(world["u"]["admin"]).get("/enrollments/")
    assert r.status_code == 200
    assert names(r) == ["Ana", "Ben", "Cy", "Di"]


def test_tree_scope_from_the_middle(world):
    r = client_for(world["u"]["registrar"]).get("/enrollments/")
    assert names(r) == ["Ana", "Ben", "Cy"]


def test_org_scope_and_hidden_fields(world):
    r = client_for(world["u"]["prof"]).get("/enrollments/")
    assert names(r) == ["Ana", "Ben"]
    assert set(r.json()[0]) == {"id", "student_name", "grade"}


def test_own_scope(world):
    r = client_for(world["u"]["student"]).get("/enrollments/")
    assert names(r) == ["Ana"]


def test_superuser_sees_all(world):
    r = client_for(world["u"]["root"]).get("/enrollments/")
    assert len(r.json()) == 5


def test_no_policy_means_forbidden(world):
    r = client_for(world["u"]["nobody"]).get("/enrollments/")
    assert r.status_code == 403


def test_anonymous_is_rejected(world):
    assert APIClient().get("/enrollments/").status_code in (401, 403)


def test_out_of_scope_detail_is_404(world):
    pk = world["e"]["other"].pk
    assert client_for(world["u"]["admin"]).get(f"/enrollments/{pk}/").status_code == 404


def test_student_cannot_see_classmate(world):
    pk = world["e"]["dept_s2"].pk
    assert client_for(world["u"]["student"]).get(f"/enrollments/{pk}/").status_code == 404


# ------------------------------------------------------------------ writes

def test_professor_can_only_write_allowed_fields(world):
    e = world["e"]["dept_s1"]
    r = client_for(world["u"]["prof"]).patch(
        f"/enrollments/{e.pk}/", {"grade": "A", "student_name": "Hacked", "tuition": "0"}, format="json"
    )
    assert r.status_code == 200
    e.refresh_from_db()
    assert e.grade == "A"
    assert e.student_name == "Ana"
    assert e.tuition == 100


def test_student_cannot_update(world):
    e = world["e"]["dept_s1"]
    r = client_for(world["u"]["student"]).patch(f"/enrollments/{e.pk}/", {"grade": "A"}, format="json")
    assert r.status_code == 403


def test_create_inside_scope_sets_owner(world):
    r = client_for(world["u"]["registrar"]).post(
        "/enrollments/", {"organization": world["dept"].pk, "student_name": "Fay"}, format="json"
    )
    assert r.status_code == 201, r.json()
    assert Enrollment.objects.get(pk=r.json()["id"]).owner == world["u"]["registrar"]


def test_create_outside_scope_is_forbidden(world):
    r = client_for(world["u"]["registrar"]).post(
        "/enrollments/", {"organization": world["school"].pk, "student_name": "Fay"}, format="json"
    )
    assert r.status_code == 403


def test_create_needs_a_valid_org(world):
    r = client_for(world["u"]["registrar"]).post("/enrollments/", {"student_name": "Fay"}, format="json")
    assert r.status_code == 400
    assert "organization" in r.json()


def test_cannot_move_record_out_of_scope(world):
    e = world["e"]["dept_s1"]
    r = client_for(world["u"]["registrar"]).patch(
        f"/enrollments/{e.pk}/", {"organization": world["other"].pk}, format="json"
    )
    assert r.status_code == 403
    e.refresh_from_db()
    assert e.organization == world["dept"]


def test_org_admin_creates_child_org(world):
    r = client_for(world["u"]["admin"]).post(
        "/organizations/",
        {"name": "Physics", "code": "physics", "type": "department", "parent": world["branch"].pk},
        format="json",
    )
    assert r.status_code == 201, r.json()
    assert r.json()["path"].startswith(world["branch"].path)


# ------------------------------------------------------------------ custom actions

def test_mapped_custom_action(world):
    e = world["e"]["dept_s1"]
    assert client_for(world["u"]["prof"]).post(f"/enrollments/{e.pk}/approve/").status_code == 200
    assert client_for(world["u"]["student"]).post(f"/enrollments/{e.pk}/approve/").status_code == 403


def test_unmapped_custom_action_is_superuser_only(world):
    assert client_for(world["u"]["admin"]).get("/enrollments/export/").status_code == 403
    assert client_for(world["u"]["root"]).get("/enrollments/export/").status_code == 200


# ------------------------------------------------------------------ policy table

def test_new_policy_applies_immediately(world):
    nobody = world["u"]["nobody"]
    Member.objects.create(user=nobody, organization=world["dept"], role="staff")
    assert client_for(nobody).get("/enrollments/").status_code == 403
    rule("staff", Enrollment, "list")
    assert names(client_for(nobody).get("/enrollments/")) == ["Ana", "Ben"]


def test_deleted_policy_applies_immediately(world):
    Policy.objects.filter(role="student").delete()
    assert client_for(world["u"]["student"]).get("/enrollments/").status_code == 403


def test_policy_validation(world):
    p = Policy(
        code="bad", name="bad", role="janitor",
        content_type=ContentType.objects.get_for_model(Enrollment),
        action="list", readable_fields=["salary"],
    )
    with pytest.raises(ValidationError) as exc:
        p.full_clean()
    assert set(exc.value.message_dict) == {"role", "readable_fields"}


def test_engine_outside_drf(world):
    engine = PolicyEngine.for_user(world["u"]["prof"])
    assert engine.can("update", world["e"]["dept_s1"])
    assert not engine.can("update", world["e"]["branch"])
    assert not engine.can("delete", world["e"]["dept_s1"])


# ------------------------------------------------------------------ admin + checks

def test_policy_admin_is_superuser_only(world, client):
    staff = world["u"]["admin"]
    staff.is_staff = True
    staff.save()
    client.force_login(staff)
    assert client.get("/admin/policytree/policy/").status_code in (302, 403)

    client.force_login(world["u"]["root"])
    assert client.get("/admin/policytree/policy/").status_code == 200
    assert "registrar" in client.get("/admin/policytree/policy/add/").content.decode()


def test_system_checks_pass():
    assert [e for e in checks.run_checks() if e.id.startswith("policytree") and e.is_serious()] == []


@override_settings(POLICYTREE_MEMBER_MODEL="testapp.Enrollment")
def test_system_checks_catch_wrong_model():
    ids = {e.id for e in checks.run_checks() if e.id.startswith("policytree")}
    assert "policytree.E002" in ids


@override_settings(POLICYTREE_ORGANIZATION_MODEL="nope.Thing")
def test_system_checks_catch_missing_model():
    ids = {e.id for e in checks.run_checks() if e.id.startswith("policytree")}
    assert "policytree.E001" in ids
