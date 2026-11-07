"""Roles created and managed by superusers at runtime (Member.role = RoleField())."""
import io
import json

import pytest
from django.core import checks
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db.models import ProtectedError
from django.test import override_settings

from policytree.engine import PolicyEngine
from policytree.models import Policy, PolicyChange, Role
from policytree.roles import Roles
from tests.conftest import client_for, names, rule
from tests.testapp.models import Enrollment, Room, RoleMember

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("role_mode"),
]


@pytest.fixture
def role_mode():
    with override_settings(POLICYTREE_MEMBER_MODEL="testapp.RoleMember"):
        yield


def make_role(code, *includes, active=True):
    role = Role.objects.create(code=code, name=code.title(), is_active=active)
    if includes:
        role.includes.set(Role.objects.filter(code__in=includes))
    return role


@pytest.fixture
def staffed(world):
    """A runtime 'librarian' role at Math Dept, with an Enrollment list policy."""
    make_role("librarian")
    user = world["u"]["nobody"]
    RoleMember.objects.create(user=user, organization=world["dept"], role_id="librarian")
    rule("librarian", Enrollment, "list")
    return {**world, "user": user}


def run(command, *args):
    out = io.StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue()


# =============================================================== basics

def test_mode_is_detected():
    assert Roles.uses_model()
    with override_settings(POLICYTREE_MEMBER_MODEL="testapp.Member"):
        assert not Roles.uses_model()


def test_runtime_role_grants_access(staffed):
    assert names(client_for(staffed["user"]).get("/enrollments/")) == ["Ana", "Ben"]


def test_role_created_now_works_immediately(world):
    user = world["u"]["nobody"]
    assert client_for(user).get("/enrollments/").status_code == 403
    make_role("auditor")
    RoleMember.objects.create(user=user, organization=world["branch"], role_id="auditor")
    rule("auditor", Enrollment, "list", "tree")
    assert names(client_for(user).get("/enrollments/")) == ["Ana", "Ben", "Cy"]


def test_inactive_role_grants_nothing(staffed):
    role = Role.objects.get(code="librarian")
    role.is_active = False
    role.save()
    assert client_for(staffed["user"]).get("/enrollments/").status_code == 403
    role.is_active = True
    role.save()
    assert client_for(staffed["user"]).get("/enrollments/").status_code == 200


# =============================================================== inheritance at runtime

def test_includes_are_editable_at_runtime(world):
    make_role("teacher")
    head = make_role("dept_head")
    user = world["u"]["nobody"]
    RoleMember.objects.create(user=user, organization=world["dept"], role_id="dept_head")
    rule("teacher", Enrollment, "list")

    assert client_for(user).get("/enrollments/").status_code == 403
    head.includes.add(Role.objects.get(code="teacher"))
    assert names(client_for(user).get("/enrollments/")) == ["Ana", "Ben"]
    head.includes.clear()
    assert client_for(user).get("/enrollments/").status_code == 403


def test_inactive_included_role_is_not_inherited(world):
    teacher = make_role("teacher")
    make_role("dept_head", "teacher")
    user = world["u"]["nobody"]
    RoleMember.objects.create(user=user, organization=world["dept"], role_id="dept_head")
    rule("teacher", Enrollment, "list")
    assert client_for(user).get("/enrollments/").status_code == 200
    teacher.is_active = False
    teacher.save()
    assert client_for(user).get("/enrollments/").status_code == 403


@override_settings(POLICYTREE_ROLE_INCLUDES={"dept_head": ["teacher"]})
def test_settings_and_admin_includes_combine(world):
    make_role("teacher")
    make_role("clerk")
    make_role("dept_head", "clerk")
    user = world["u"]["nobody"]
    RoleMember.objects.create(user=user, organization=world["dept"], role_id="dept_head")
    rule("teacher", Enrollment, "list")
    rule("clerk", Enrollment, "detail")
    engine = PolicyEngine.for_user(user)
    assert engine.has_any(Enrollment, "list") and engine.has_any(Enrollment, "detail")


# =============================================================== validation and protection

def test_policy_role_must_exist(world):
    from django.contrib.contenttypes.models import ContentType

    p = Policy(code="x", name="x", role="ghost", action="list",
               content_type=ContentType.objects.get_for_model(Room))
    with pytest.raises(ValidationError) as exc:
        p.full_clean()
    assert "role" in exc.value.message_dict
    make_role("ghost")
    p.full_clean()


def test_code_cannot_change():
    role = make_role("teacher")
    role.code = "lecturer"
    with pytest.raises(ValidationError):
        role.save()


def test_roles_in_use_cannot_be_deleted(staffed):
    role = Role.objects.get(code="librarian")
    with pytest.raises(ProtectedError):
        role.delete()  # used by a policy
    Policy.objects.filter(role="librarian").delete()
    with pytest.raises(ProtectedError):
        role.delete()  # used by a member
    RoleMember.objects.all().delete()
    role.delete()
    assert not Role.objects.filter(code="librarian").exists()


def test_member_role_must_exist(world):
    member = RoleMember(user=world["u"]["nobody"], organization=world["dept"], role_id="ghost")
    with pytest.raises(ValidationError) as exc:
        member.full_clean()
    assert "role" in exc.value.message_dict


# =============================================================== admin

def admin_client(world, client):
    client.force_login(world["u"]["root"])
    return client


def test_role_admin_creates_role_with_includes_and_one_audit_row(world, client):
    teacher = make_role("teacher")
    c = admin_client(world, client)
    before = PolicyChange.objects.filter(target="role").count()
    r = c.post("/admin/policytree/role/add/", {
        "code": "dept_head", "name": "Department head", "description": "",
        "is_active": "on", "includes": [teacher.pk],
    })
    assert r.status_code == 302, r.content.decode()[:500]
    role = Role.objects.get(code="dept_head")
    assert list(role.includes.values_list("code", flat=True)) == ["teacher"]

    rows = PolicyChange.objects.filter(target="role", policy_code="dept_head")
    assert PolicyChange.objects.filter(target="role").count() == before + 1
    change = rows.get()
    assert (change.kind, change.source, change.changed_by) == ("created", "admin", world["u"]["root"])
    assert change.data["includes"] == ["teacher"]


def test_role_admin_rejects_cycles(world, client):
    head = make_role("dept_head")
    make_role("teacher", "dept_head")
    c = admin_client(world, client)
    r = c.post(f"/admin/policytree/role/{head.pk}/change/", {
        "name": "Dept head", "description": "", "is_active": "on",
        "includes": [Role.objects.get(code="teacher").pk],
    })
    assert r.status_code == 200
    assert "include each other" in r.content.decode()
    assert not head.includes.exists()


def test_role_admin_code_is_read_only_after_creation(world, client):
    role = make_role("teacher")
    c = admin_client(world, client)
    r = c.post(f"/admin/policytree/role/{role.pk}/change/", {
        "code": "lecturer", "name": "Teacher", "description": "", "is_active": "on",
    })
    assert r.status_code == 302
    role.refresh_from_db()
    assert role.code == "teacher"


def test_role_admin_hides_delete_for_roles_in_use(staffed, client):
    c = admin_client(staffed, client)
    role = Role.objects.get(code="librarian")
    assert c.get(f"/admin/policytree/role/{role.pk}/delete/").status_code == 403
    unused = make_role("unused")
    assert c.post(f"/admin/policytree/role/{unused.pk}/delete/", {"post": "yes"}).status_code == 302
    assert PolicyChange.objects.filter(target="role", policy_code="unused", kind="deleted").exists()


def test_policy_admin_offers_runtime_roles(world, client):
    make_role("librarian")
    page = admin_client(world, client).get("/admin/policytree/policy/add/").content.decode()
    assert 'value="librarian"' in page


def test_role_admin_hidden_without_role_field(world, client):
    c = admin_client(world, client)
    assert c.get("/admin/policytree/role/").status_code == 200
    with override_settings(POLICYTREE_MEMBER_MODEL="testapp.Member"):
        assert c.get("/admin/policytree/role/").status_code == 403


# =============================================================== seed / export / import

def test_seed_roles_and_policies(world, tmp_path):
    data = {
        "roles": [
            {"code": "teacher", "name": "Teacher"},
            {"code": "dept_head", "name": "Department head", "includes": ["teacher"]},
        ],
        "policies": [
            {"code": "teacher-enr-list", "role": "teacher", "model": "testapp.Enrollment", "action": "list"},
        ],
    }
    path = tmp_path / "policies.json"
    path.write_text(json.dumps(data))
    out = run("policytree_seed", str(path))
    assert "roles: 2 created" in out and "Applied: 1 created" in out
    assert list(Role.objects.get(code="dept_head").includes.values_list("code", flat=True)) == ["teacher"]
    assert PolicyChange.objects.filter(target="role", source="seed").count() == 2

    assert "roles: 0 created, 0 updated, 2 unchanged" in run("policytree_seed", str(path))

    user = world["u"]["nobody"]
    RoleMember.objects.create(user=user, organization=world["dept"], role_id="dept_head")
    assert names(client_for(user).get("/enrollments/")) == ["Ana", "Ben"]

    exported = json.loads(run("policytree_export"))
    assert {r["code"] for r in exported["roles"]} == {"teacher", "dept_head"}
    assert "teacher-enr-list" in {p["code"] for p in exported["policies"]}


@pytest.mark.parametrize("roles, message", [
    ([{"code": "a", "includes": ["b"]}, {"code": "b", "includes": ["a"]}], "include each other"),
    ([{"code": "a", "includes": ["nope"]}], "unknown roles"),
    ([{"code": "a", "includes": ["a"]}], "itself"),
    ([{"name": "no code"}], "needs a `code`"),
])
def test_seed_rejects_bad_roles(roles, message, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"roles": roles, "policies": []}))
    with pytest.raises(CommandError, match=message):
        run("policytree_seed", str(path))
    assert not Role.objects.exists()


def test_seed_prune_keeps_roles_in_use(staffed, tmp_path):
    make_role("unused")
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"roles": [], "policies": []}))
    out = run("policytree_seed", str(path), "--prune")
    assert "Kept role 'librarian'" in out
    assert not Role.objects.filter(code="unused").exists()
    assert Role.objects.filter(code="librarian").exists()  # member still uses it


@override_settings(POLICYTREE_MEMBER_MODEL="testapp.Member", POLICYTREE_ROLE_INCLUDES={"admin": ["staff"]})
def test_import_roles_from_choices_and_settings(world):
    out = run("policytree_import_roles", "--with-includes")
    assert "Created 6 role(s)" in out
    assert set(Role.objects.values_list("code", flat=True)) == {
        "admin", "staff", "accounting", "registrar", "professor", "student"}
    assert Role.objects.get(code="registrar").name == "Registrar"
    assert list(Role.objects.get(code="admin").includes.values_list("code", flat=True)) == ["staff"]
    assert "Created 0 role(s)" in run("policytree_import_roles")


# =============================================================== checks

def test_checks_pass_in_role_mode():
    assert not [m for m in checks.run_checks() if m.id.startswith("policytree") and m.is_serious()]


@override_settings(POLICYTREE_MEMBER_MODEL="testapp.BadRoleMember")
def test_check_rejects_a_plain_foreign_key():
    assert "policytree.E005" in {m.id for m in checks.run_checks() if m.id.startswith("policytree")}
