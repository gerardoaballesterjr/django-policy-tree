"""Branch-level resources: spaces belong to a branch, rooms belong to a space."""
import pytest

from policytree.engine import PolicyEngine
from tests.conftest import client_for, org, rule
from tests.testapp.models import Member, Room, Space

pytestmark = pytest.mark.django_db


@pytest.fixture
def campus(world):
    """
    North School > North Branch > Math Dept      (world)
    South School > South Branch                   (added)

    staff at North Branch: manage spaces and rooms in their branch tree
    professor at Math Dept: see rooms of the branch above them (ancestors)
    """
    south_branch = org("South Branch", "branch", world["other"])
    lab = Space.objects.create(name="Computer Lab", code="lab", branch=world["branch"])
    hall = Space.objects.create(name="Lecture Space", code="hall", branch=south_branch)
    rooms = {
        "lab1": Room.objects.create(name="Lab 1", code="lab-1", capacity=30, space=lab),
        "hall1": Room.objects.create(name="Hall 1", code="hall-1", capacity=200, space=hall),
    }

    staff = world["u"]["nobody"]
    Member.objects.create(user=staff, organization=world["branch"], role="staff")
    for action in ["list", "detail", "create", "update", "delete"]:
        rule("staff", Space, action, "tree")
        rule("staff", Room, action, "tree")
    rule("professor", Room, "list", "ancestors")
    rule("professor", Room, "detail", "ancestors")

    return {**world, "south_branch": south_branch, "lab": lab, "hall": hall, "rooms": rooms, "staff": staff}


def test_space_create_uses_branch(campus):
    c = client_for(campus["staff"])
    ok = c.post("/spaces/", {"name": "Gym", "code": "gym", "branch": campus["branch"].pk}, format="json")
    assert ok.status_code == 201
    other = c.post("/spaces/", {"name": "Gym", "code": "gym", "branch": campus["south_branch"].pk}, format="json")
    assert other.status_code == 403


def test_room_create_reaches_branch_through_space(campus):
    c = client_for(campus["staff"])
    payload = {"name": "Lab 2", "code": "lab-2", "capacity": 25}
    assert c.post("/rooms/", {**payload, "space": campus["lab"].pk}, format="json").status_code == 201
    assert c.post("/rooms/", {**payload, "space": campus["hall"].pk}, format="json").status_code == 403
    missing = c.post("/rooms/", payload, format="json")
    assert missing.status_code == 400 and "space" in missing.json()


def test_rooms_are_scoped_through_space(campus):
    rows = client_for(campus["staff"]).get("/rooms/").json()
    assert [r["code"] for r in rows] == ["lab-1"]


def test_cannot_move_room_to_another_branch(campus):
    room = campus["rooms"]["lab1"]
    r = client_for(campus["staff"]).patch(f"/rooms/{room.pk}/", {"space": campus["hall"].pk}, format="json")
    assert r.status_code == 403
    room.refresh_from_db()
    assert room.space == campus["lab"]


def test_department_member_sees_rooms_of_branch_above(campus):
    prof = campus["u"]["prof"]
    c = client_for(prof)
    assert [r["code"] for r in c.get("/rooms/").json()] == ["lab-1"]
    assert c.get(f"/rooms/{campus['rooms']['hall1'].pk}/").status_code == 404
    assert c.patch(f"/rooms/{campus['rooms']['lab1'].pk}/", {"capacity": 99}, format="json").status_code == 403


def test_ancestors_scope_in_engine(campus):
    engine = PolicyEngine.for_user(campus["u"]["prof"])
    assert engine.can("detail", campus["rooms"]["lab1"])
    assert not engine.can("detail", campus["rooms"]["hall1"])
