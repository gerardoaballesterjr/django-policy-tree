"""UUID primary keys, soft delete and archiving through the project's BaseModel."""
import uuid

import pytest

from policytree.engine import PolicyEngine
from tests.conftest import client_for, org, rule
from tests.test_spaces import campus  # noqa: F401  (fixture)
from tests.testapp.models import Enrollment, Member, Organization, Room

pytestmark = pytest.mark.django_db


def test_uuid_paths(world):
    dept = world["dept"]
    assert isinstance(dept.pk, uuid.UUID)
    assert dept.path == "".join(f"{o.pk.hex}/" for o in (world["school"], world["branch"], dept))
    assert dept.ancestor_ids() == [world["school"].pk, world["branch"].pk, dept.pk]
    assert set(dept.ancestors()) == {world["school"], world["branch"], dept}


def test_soft_deleted_membership_grants_nothing(world):
    prof = world["u"]["prof"]
    assert client_for(prof).get("/enrollments/").status_code == 200
    Member.objects.get(user=prof).delete()
    assert Member.all_objects.filter(user=prof).exists()  # still in the table
    assert client_for(prof).get("/enrollments/").status_code == 403


def test_soft_deleting_an_organization_removes_its_subtree_and_access(world):
    world["branch"].delete()
    assert not Organization.objects.filter(pk__in=[world["branch"].pk, world["dept"].pk]).exists()
    assert Organization.all_objects.filter(pk=world["dept"].pk).exists()

    # Members of the deleted branch and department lose access.
    assert client_for(world["u"]["registrar"]).get("/enrollments/").status_code == 403
    assert client_for(world["u"]["prof"]).get("/enrollments/").status_code == 403
    # The school admin can no longer create inside it.
    r = client_for(world["u"]["admin"]).post(
        "/enrollments/", {"organization": str(world["dept"].pk), "student_name": "X"}, format="json")
    assert r.status_code == 400


def test_codes_can_be_reused_after_soft_delete(world):
    world["dept"].delete()
    again = org("Math Dept", "department", world["branch"])  # same code "math-dept"
    assert again.code == "math-dept"


def test_archived_records_are_read_only(campus):  # noqa: F811
    rule("staff", Room, "update", "tree", effect="deny", condition="archived")
    room = campus["rooms"]["lab1"]
    c = client_for(campus["staff"])
    assert c.patch(f"/rooms/{room.pk}/", {"capacity": 40}, format="json").status_code == 200

    room.archive()
    assert c.patch(f"/rooms/{room.pk}/", {"capacity": 50}, format="json").status_code == 404
    assert "lab-1" in [r["code"] for r in c.get("/rooms/").json()]  # still listed
    room.refresh_from_db()
    assert room.capacity == 40


def test_soft_delete_through_the_api_and_bulk_helper(campus):  # noqa: F811
    c = client_for(campus["staff"])
    room = campus["rooms"]["lab1"]
    assert c.delete(f"/rooms/{room.pk}/").status_code == 204
    assert not Room.objects.filter(pk=room.pk).exists()
    assert Room.all_objects.get(pk=room.pk).deleted_at is not None

    Room.objects.create(name="Lab 3", code="lab-3", capacity=10, space=campus["lab"])
    engine = PolicyEngine.for_user(campus["staff"])
    assert engine.bulk_delete(Room.objects.all()) == 1      # only rooms in their branch
    assert Room.objects.count() == 1                         # South hall room remains
    assert Room.all_objects.count() == 3                     # nothing hard-deleted


def test_malformed_uuid_is_a_validation_error(campus):  # noqa: F811
    c = client_for(campus["staff"])
    r = c.post("/rooms/", {"name": "X", "code": "x", "capacity": 1, "space": "not-a-uuid"}, format="json")
    assert r.status_code == 400 and "space" in r.json()
    r = c.post("/spaces/", {"name": "X", "code": "x", "branch": "not-a-uuid"}, format="json")
    assert r.status_code == 400 and "branch" in r.json()


def test_integer_records_still_work_under_uuid_organizations(world):
    # Enrollment keeps an integer pk while its organization uses a UUID.
    e = world["e"]["dept_s1"]
    assert isinstance(e.pk, int)
    assert PolicyEngine.for_user(world["u"]["prof"]).can("update", e)
    assert Enrollment.objects.filter(organization__path__startswith=world["branch"].path).count() == 3
