"""Fixes from the 1.0 cleanup: simpler API, security edges, nested lists, query counts."""
import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from policytree.engine import PolicyEngine
from policytree.models import Policy
from policytree.registry import FieldPaths
from policytree.targets import CreateTarget
from tests.conftest import client_for, org, rule
from tests.test_spaces import campus  # noqa: F401  (fixture)
from tests.testapp.models import Enrollment, Member, Organization, Room, Space

pytestmark = pytest.mark.django_db


# =============================================================== create targets

def test_create_targets_come_from_the_model():
    assert CreateTarget.field(Organization) == "parent"
    assert CreateTarget.field(Enrollment) == "organization"
    assert CreateTarget.field(Space) == "branch"
    assert CreateTarget.field(Room) == "space"


def test_create_target_resolves_through_related_records(campus):  # noqa: F811
    assert CreateTarget.resolve(Room, campus["lab"].pk) == campus["branch"]
    assert CreateTarget.resolve(Room, str(campus["lab"].pk)) == campus["branch"]
    assert CreateTarget.resolve(Room, "not-a-uuid") is None
    assert CreateTarget.resolve(Room, None) is None


@pytest.mark.parametrize("path", ["nothing__branch", "name__branch"])
def test_bad_create_path_is_reported(monkeypatch, path):
    """The first step of the organization path must be a foreign key on the model."""
    monkeypatch.setattr(Space, "policytree_organization_field", path, raising=False)
    FieldPaths.clear()
    try:
        with pytest.raises(ImproperlyConfigured):
            CreateTarget.resolve(Space, "x")
    finally:
        monkeypatch.undo()
        FieldPaths.clear()


def test_custom_target_hook(world):
    """A viewset can decide the place on the server instead of reading the request."""
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from policytree.drf import PolicyPermission

    class View:
        action = "create"
        detail = False
        queryset = Enrollment.objects.all()
        policy_actions = {}

        def get_policy_target(self, model):
            return world["school"]

    request = Request(APIRequestFactory().post("/x/", {}, format="json"))
    request.user = world["u"]["registrar"]  # registrar reaches North Branch, not the school
    assert PolicyPermission().has_permission(request, View()) is False
    request = Request(APIRequestFactory().post("/x/", {}, format="json"))
    request.user = world["u"]["admin"]
    assert PolicyPermission().has_permission(request, View()) is True


# =============================================================== security edges

def test_superuser_can_create_a_top_level_organization(world):
    r = client_for(world["u"]["root"]).post(
        "/organizations/", {"name": "East", "code": "east", "type": "school"}, format="json")
    assert r.status_code == 201, r.json()
    assert Organization.objects.get(code="east").parent is None


def test_non_superuser_cannot_create_a_top_level_organization(world):
    r = client_for(world["u"]["admin"]).post(
        "/organizations/", {"name": "East", "code": "east", "type": "school"}, format="json")
    assert r.status_code == 400 and "parent" in r.json()


def test_empty_path_reaches_nothing(world):
    """An organization saved without path upkeep (e.g. loaddata, raw SQL) must not open the whole tree."""
    stray = Organization.objects.create(name="Stray", code="stray", type="school")
    Organization._base_manager.filter(pk=stray.pk).update(path="")
    user = world["u"]["nobody"]
    Member.objects.create(user=user, organization=stray, role="admin")  # admin: tree scope
    engine = PolicyEngine.for_user(user)
    assert list(engine.filter(Enrollment.objects.all(), "list")) == []
    assert not engine.can("list", world["e"]["dept_s1"])
    assert not engine.can_in(Enrollment, "create", world["dept"])


def test_inactive_users_get_nothing(world):
    for name in ("admin", "root"):
        user = world["u"][name]
        user.is_active = False
        user.save()
        engine = PolicyEngine.for_user(user)
        assert not engine.is_superuser
        assert list(engine.filter(Enrollment.objects.all(), "list")) == []
        assert not engine.can("detail", world["e"]["school"])


def test_anonymous_gets_nothing(world):
    from django.contrib.auth.models import AnonymousUser

    engine = PolicyEngine.for_user(AnonymousUser())
    assert list(engine.filter(Enrollment.objects.all(), "list")) == []
    assert not engine.can("detail", world["e"]["school"])
    assert not engine.can_in(Enrollment, "create", world["school"])


def test_records_without_an_organization_are_refused(world):
    e = Enrollment(owner=world["u"]["admin"], student_name="Orphan")
    assert not PolicyEngine.for_user(world["u"]["admin"]).can("detail", e)


# =============================================================== nested lists

def nested_codes(response):
    return {row["code"]: ([r["code"] for r in row["rooms"]], [r["code"] for r in row["room_list"]])
            for row in response.json()}


def test_nested_rows_filtered_with_prefetch_lists_and_policy_prefetch(campus):  # noqa: F811
    Room.objects.create(name="Lab 9", code="lab-9", capacity=5, space=campus["lab"]).archive()
    rule("staff", Room, "list", "tree", effect="deny", condition="archived")
    c = client_for(campus["staff"])
    expected = {"lab": (["lab-1"], ["lab-1"])}
    assert nested_codes(c.get("/spaces/scoped/")) == expected
    assert nested_codes(c.get("/spaces/plain/")) == expected


def test_nested_prefetch_does_not_query_per_row(campus):  # noqa: F811
    for i in range(15):
        Room.objects.create(name=f"R{i}", code=f"r-{i}", capacity=1, space=campus["lab"])
    for i in range(5):
        Space.objects.create(name=f"S{i}", code=f"s-{i}", branch=campus["branch"])
    c = client_for(campus["staff"])
    c.get("/spaces/scoped/")  # warm caches
    with CaptureQueriesContext(connection) as small:
        c.get("/spaces/scoped/")
    for i in range(30):
        Room.objects.create(name=f"T{i}", code=f"t-{i}", capacity=1, space=campus["lab"])
    for i in range(10):
        Space.objects.create(name=f"U{i}", code=f"u-{i}", branch=campus["branch"])
    with CaptureQueriesContext(connection) as large:
        c.get("/spaces/scoped/")
    assert len(large) == len(small), [q["sql"] for q in large.captured_queries]


# =============================================================== query counts

def make_enrollments(world, count):
    root = world["u"]["root"]
    Enrollment.objects.bulk_create(
        Enrollment(organization=world["dept"], owner=root, student_name=f"S{i}") for i in range(count))


def list_queries(client, url="/enrollments/"):
    client.get(url)  # warm caches
    with CaptureQueriesContext(connection) as ctx:
        assert client.get(url).status_code == 200
    return len(ctx)


@pytest.mark.parametrize("user", ["admin", "registrar", "prof"])
def test_list_queries_do_not_grow_with_rows(world, user):
    c = client_for(world["u"][user])
    small = list_queries(c)
    make_enrollments(world, 60)
    assert list_queries(c) == small


def test_mixed_field_rules_cost_one_query_per_rule_not_per_row(world):
    """A user whose roles grant different fields in different places."""
    nobody = world["u"]["nobody"]
    Member.objects.create(user=nobody, organization=world["dept"], role="professor")
    Member.objects.create(user=nobody, organization=world["other"], role="accounting")
    rule("accounting", Enrollment, "list", "org")
    c = client_for(nobody)
    small = list_queries(c)
    make_enrollments(world, 60)
    assert list_queries(c) == small
    rows = c.get("/enrollments/").json()
    assert all(set(r) == {"id", "student_name", "grade"} for r in rows if r["student_name"] != "Ed")
    assert "tuition" in next(r for r in rows if r["student_name"] == "Ed")


def test_detail_does_not_query_through_relations(campus):  # noqa: F811
    c = client_for(campus["staff"])
    url = f"/rooms/{campus['rooms']['lab1'].pk}/"
    c.get(url)
    with CaptureQueriesContext(connection) as ctx:
        assert c.get(url).status_code == 200
    # memberships + the room (with space and branch joined); the request's own
    # transaction adds SAVEPOINT/RELEASE inside tests, which aren't data queries
    data = [q["sql"] for q in ctx.captured_queries if "SAVEPOINT" not in q["sql"]]
    assert len(data) <= 4, data


@pytest.mark.django_db(transaction=True)
def test_policy_cache_is_used(world):
    """With committed data (as in production) the shared cache serves every request."""
    c = client_for(world["u"]["admin"])
    c.get("/enrollments/")
    with CaptureQueriesContext(connection) as ctx:
        c.get("/enrollments/")
    assert not any("policytree_policy" in q["sql"] for q in ctx.captured_queries)
    Policy.objects.filter(role="admin").first().save()  # any change clears the cache
    with CaptureQueriesContext(connection) as ctx:
        c.get("/enrollments/")
    assert any("policytree_policy" in q["sql"] for q in ctx.captured_queries)


def test_api_is_unauthenticated_safe(world):
    assert APIClient().get("/rooms/").status_code in (401, 403)
