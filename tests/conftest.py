import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from rest_framework.test import APIClient

from policytree.models import Policy
from tests.testapp.models import Enrollment, Invoice, Member, Organization

User = get_user_model()


@pytest.fixture(autouse=True)
def fresh_cache():
    """Rolled-back test data never fires signals, so start each test with an empty cache."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


# ------------------------------------------------------------------ fixtures

def org(name, type_, parent=None):
    return Organization.objects.create(name=name, code=name.lower().replace(" ", "-"), type=type_, parent=parent)


def rule(role, model, action, scope="org", read=None, write=None, effect="allow", condition=""):
    suffix = "".join(f"-{part}" for part in (effect if effect != "allow" else "", condition) if part)
    return Policy.objects.create(
        code=f"{role}-{model.__name__.lower()}-{action}{suffix}",
        name=f"{role} {action}",
        role=role,
        content_type=ContentType.objects.get_for_model(model),
        action=action,
        scope=scope,
        readable_fields=read or [],
        writable_fields=write or [],
        effect=effect,
        condition=condition,
    )


@pytest.fixture
def world():
    school = org("North School", "school")
    branch = org("North Branch", "branch", school)
    dept = org("Math Dept", "department", branch)
    other = org("South School", "school")

    users = {name: User.objects.create_user(name, password="x") for name in
             ["admin", "registrar", "prof", "student", "student2", "nobody"]}
    users["root"] = User.objects.create_superuser("root", password="x")

    Member.objects.create(user=users["admin"], organization=school, role="admin")
    Member.objects.create(user=users["registrar"], organization=branch, role="registrar")
    Member.objects.create(user=users["prof"], organization=dept, role="professor")
    Member.objects.create(user=users["student"], organization=dept, role="student")
    Member.objects.create(user=users["student2"], organization=dept, role="student")

    for action in ["list", "detail", "create", "update", "delete"]:
        rule("admin", Enrollment, action, "tree")
        rule("admin", Organization, action, "tree")
    for action in ["list", "detail", "create", "update"]:
        rule("registrar", Enrollment, action, "tree")
    rule("professor", Enrollment, "list", "org", read=["id", "student_name", "grade"])
    rule("professor", Enrollment, "detail", "org", read=["id", "student_name", "grade"])
    rule("professor", Enrollment, "update", "org", write=["grade"])
    rule("student", Enrollment, "list", "own")
    rule("student", Enrollment, "detail", "own")

    e = {
        "dept_s1": Enrollment.objects.create(organization=dept, owner=users["student"], student_name="Ana", tuition=100),
        "dept_s2": Enrollment.objects.create(organization=dept, owner=users["student2"], student_name="Ben", tuition=200),
        "branch": Enrollment.objects.create(organization=branch, owner=users["registrar"], student_name="Cy", tuition=300),
        "school": Enrollment.objects.create(organization=school, owner=users["admin"], student_name="Di", tuition=400),
        "other": Enrollment.objects.create(organization=other, owner=users["root"], student_name="Ed", tuition=500),
    }
    return {"school": school, "branch": branch, "dept": dept, "other": other, "u": users, "e": e}


def client_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def names(response):
    return sorted(row["student_name"] for row in response.json())




@pytest.fixture
def billing(world):
    """
    accounting (member of North Branch):
      Enrollment detail  tree
      Invoice create     org   (North Branch only), may set number + amount
      Invoice list       tree  sees id, number, amount
    registrar: also gets Invoice create (tree)
    cashier (member of North School): Invoice create tree, nothing on Enrollment
    """
    u = world["u"]
    u["accounting"] = type(u["nobody"]).objects.create_user("accounting", password="x")
    u["cashier"] = type(u["nobody"]).objects.create_user("cashier", password="x")
    Member.objects.create(user=u["accounting"], organization=world["branch"], role="accounting")
    Member.objects.create(user=u["cashier"], organization=world["school"], role="staff")

    rule("accounting", Enrollment, "detail", "tree")
    rule("accounting", Invoice, "create", "org", write=["number", "amount"])
    rule("accounting", Invoice, "list", "tree", read=["id", "number", "amount"])
    rule("registrar", Invoice, "create", "tree")
    rule("staff", Invoice, "create", "tree")
    return world
