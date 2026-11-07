"""Deny rules, conditions, role inheritance, scopes, indirect links, per-record fields,
nested scoping, bulk helpers, path guards, audit, seed/export, checks, plain views."""
import io
import json

import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core import checks
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db.models import Q
from django.test import override_settings

from policytree.engine import PolicyEngine
from policytree.models import Policy, PolicyChange
from policytree.registry import Scope, ScopeRegistry
from tests.conftest import client_for, names, org, rule
from tests.testapp.models import Enrollment, Invoice, Member, Organization, Payment

pytestmark = pytest.mark.django_db


def seg(org):
    return org.path_segment(org.pk)
User = get_user_model()


def patch(user, enrollment, **data):
    return client_for(user).patch(f"/enrollments/{enrollment.pk}/", data, format="json")


def set_policy(role, action, model=Enrollment, **changes):
    """Edit a policy through save() so the cache and audit log see it."""
    policy = Policy.objects.get(role=role, action=action, content_type=ContentType.objects.get_for_model(model))
    for key, value in changes.items():
        setattr(policy, key, value)
    policy.save()
    return policy


def check_ids():
    return {m.id for m in checks.run_checks() if m.id.startswith("policytree")}


# =============================================================== deny rules

def test_deny_overrides_allow_inside_its_scope(world):
    rule("admin", Enrollment, "list", "below", effect="deny")
    assert names(client_for(world["u"]["admin"]).get("/enrollments/")) == ["Di"]


def test_deny_wins_across_roles(world):
    nobody = world["u"]["nobody"]
    Member.objects.create(user=nobody, organization=world["branch"], role="registrar")
    Member.objects.create(user=nobody, organization=world["dept"], role="staff")
    assert names(client_for(nobody).get("/enrollments/")) == ["Ana", "Ben", "Cy"]
    rule("staff", Enrollment, "list", "org", effect="deny")
    assert names(client_for(nobody).get("/enrollments/")) == ["Cy"]


def test_deny_blocks_create_in_its_scope(world):
    rule("registrar", Enrollment, "create", "below", effect="deny")
    c = client_for(world["u"]["registrar"])
    assert c.post("/enrollments/", {"organization": world["dept"].pk, "student_name": "X"}, format="json").status_code == 403
    assert c.post("/enrollments/", {"organization": world["branch"].pk, "student_name": "X"}, format="json").status_code == 201


def test_conditional_deny_is_judged_per_record(world):
    Enrollment.objects.filter(pk=world["e"]["branch"].pk).update(tuition=5000)
    rule("registrar", Enrollment, "update", "tree", effect="deny", condition="small_tuition")
    reg = world["u"]["registrar"]
    assert patch(reg, world["e"]["dept_s1"], grade="A").status_code == 404  # small tuition: denied
    assert patch(reg, world["e"]["branch"], grade="A").status_code == 200


def test_deny_policies_cannot_list_fields(world):
    p = Policy(code="d", name="d", role="admin", content_type=ContentType.objects.get_for_model(Enrollment),
               action="list", effect="deny", readable_fields=["grade"])
    with pytest.raises(ValidationError) as exc:
        p.full_clean()
    assert "readable_fields" in exc.value.message_dict


# =============================================================== conditions

def test_condition_limits_which_records_can_be_updated(world):
    set_policy("professor", "update", condition="ungraded")
    prof, ana = world["u"]["prof"], world["e"]["dept_s1"]
    assert patch(prof, ana, grade="A").status_code == 200
    assert patch(prof, ana, grade="B").status_code == 404  # graded now: out of reach
    ana.refresh_from_db()
    assert ana.grade == "A"


def test_condition_is_checked_on_the_saved_result(world):
    set_policy("registrar", "update", condition="small_tuition")
    reg, cy = world["u"]["registrar"], world["e"]["branch"]
    assert patch(reg, cy, tuition="5000").status_code == 403
    cy.refresh_from_db()
    assert cy.tuition == 300  # rolled back
    assert patch(reg, cy, tuition="900").status_code == 200


def test_condition_on_create_is_checked_after_saving(world):
    set_policy("registrar", "create", condition="small_tuition")
    c = client_for(world["u"]["registrar"])
    payload = {"organization": world["dept"].pk, "student_name": "Big"}
    assert c.post("/enrollments/", {**payload, "tuition": "5000"}, format="json").status_code == 403
    assert not Enrollment.objects.filter(student_name="Big").exists()
    assert c.post("/enrollments/", {**payload, "tuition": "10"}, format="json").status_code == 201


def test_condition_filters_lists(world):
    set_policy("registrar", "list", condition="ungraded")
    Enrollment.objects.filter(pk=world["e"]["dept_s2"].pk).update(grade="A")
    assert names(client_for(world["u"]["registrar"]).get("/enrollments/")) == ["Ana", "Cy"]


def test_condition_validation(world):
    def policy(condition, model):
        return Policy(code="c", name="c", role="admin", action="list", condition=condition,
                      content_type=ContentType.objects.get_for_model(model))

    with pytest.raises(ValidationError):
        policy("nope", Enrollment).full_clean()
    with pytest.raises(ValidationError):
        policy("ungraded", Invoice).full_clean()  # limited to Enrollment
    policy("not_mine", Invoice).full_clean()       # usable on any model


# =============================================================== role inheritance

def test_role_includes_other_roles(world):
    nobody = world["u"]["nobody"]
    Member.objects.create(user=nobody, organization=world["dept"], role="staff")
    assert client_for(nobody).get("/enrollments/").status_code == 403
    with override_settings(POLICYTREE_ROLE_INCLUDES={"staff": ["professor"]}):
        r = client_for(nobody).get("/enrollments/")
        assert names(r) == ["Ana", "Ben"]
        assert set(r.json()[0]) == {"id", "student_name", "grade"}  # professor's fields


def test_role_includes_are_transitive(world):
    nobody = world["u"]["nobody"]
    Member.objects.create(user=nobody, organization=world["branch"], role="staff")
    with override_settings(POLICYTREE_ROLE_INCLUDES={"staff": ["accounting"], "accounting": ["registrar"]}):
        assert names(client_for(nobody).get("/enrollments/")) == ["Ana", "Ben", "Cy"]


@override_settings(POLICYTREE_ROLE_INCLUDES={"staff": ["janitor"], "janitor": ["staff"]})
def test_role_cycles_and_unknown_roles_are_reported():
    ids = check_ids()
    assert "policytree.E004" in ids
    assert "policytree.W002" in ids


# =============================================================== scopes

def test_siblings_scope(world):
    dept2 = org("Physics", "department", world["branch"])
    Enrollment.objects.create(organization=dept2, owner=world["u"]["root"], student_name="Gus")
    nobody = world["u"]["nobody"]
    Member.objects.create(user=nobody, organization=world["dept"], role="staff")
    rule("staff", Enrollment, "list", "siblings")
    assert names(client_for(nobody).get("/enrollments/")) == ["Ana", "Ben", "Gus"]


class ParentOnly(Scope):
    name = "parent_only"
    label = "Only the parent organization"

    def covers(self, user, membership, org, obj, paths):
        return org.pk == membership.organization.parent_id

    def q(self, user, membership, paths):
        return Q(**{paths.id: membership.organization.parent_id})


def test_custom_scope(world):
    ScopeRegistry.register(ParentOnly)
    try:
        nobody = world["u"]["nobody"]
        Member.objects.create(user=nobody, organization=world["dept"], role="staff")
        rule("staff", Enrollment, "list", "parent_only")
        rule("staff", Enrollment, "detail", "parent_only")
        c = client_for(nobody)
        assert names(c.get("/enrollments/")) == ["Cy"]
        assert c.get(f"/enrollments/{world['e']['branch'].pk}/").status_code == 200
        assert c.get(f"/enrollments/{world['e']['dept_s1'].pk}/").status_code == 404
    finally:
        ScopeRegistry.unregister("parent_only")


def test_unknown_scope_is_rejected(world):
    p = Policy(code="s", name="s", role="admin", action="list", scope="galaxy",
               content_type=ContentType.objects.get_for_model(Enrollment))
    with pytest.raises(ValidationError) as exc:
        p.full_clean()
    assert "scope" in exc.value.message_dict


# =============================================================== indirect links

def test_models_linked_through_another_model(world):
    root, student = world["u"]["root"], world["u"]["student"]
    inv_in = Invoice.objects.create(organization=world["dept"], owner=root, number="A", amount=1)
    inv_out = Invoice.objects.create(organization=world["other"], owner=root, number="B", amount=1)
    inv_mine = Invoice.objects.create(organization=world["dept"], owner=student, number="C", amount=1)
    p_in = Payment.objects.create(invoice=inv_in, amount=1)
    p_out = Payment.objects.create(invoice=inv_out, amount=1)
    p_mine = Payment.objects.create(invoice=inv_mine, amount=1)

    rule("registrar", Payment, "list", "tree")
    engine = PolicyEngine.for_user(world["u"]["registrar"])
    assert set(engine.filter(Payment.objects.all(), "list")) == {p_in, p_mine}
    assert engine.can("list", p_in) and not engine.can("list", p_out)

    rule("student", Payment, "detail", "own")  # owner reached through invoice__owner
    engine = PolicyEngine.for_user(student)
    assert engine.can("detail", p_mine)
    assert not engine.can("detail", p_in)
    assert list(engine.filter(Payment.objects.all(), "detail")) == [p_mine]


# =============================================================== per-record fields

def test_field_rules_follow_each_record(world):
    nobody = world["u"]["nobody"]
    Member.objects.create(user=nobody, organization=world["dept"], role="professor")
    Member.objects.create(user=nobody, organization=world["other"], role="accounting")
    rule("accounting", Enrollment, "list", "org")
    rule("accounting", Enrollment, "detail", "org")
    c = client_for(nobody)

    rows = {row["student_name"]: set(row) for row in c.get("/enrollments/").json()}
    assert rows["Ana"] == {"id", "student_name", "grade"}
    assert "tuition" in rows["Ed"]

    assert "tuition" not in c.get(f"/enrollments/{world['e']['dept_s1'].pk}/").json()
    assert "tuition" in c.get(f"/enrollments/{world['e']['other'].pk}/").json()


# =============================================================== nested rows

def test_nested_rows_are_scoped(billing):
    e, root = billing["e"]["dept_s1"], billing["u"]["root"]
    Invoice.objects.create(enrollment=e, organization=billing["dept"], owner=root, number="IN", amount=1)
    Invoice.objects.create(enrollment=e, organization=billing["other"], owner=root, number="OUT", amount=2)
    rule("accounting", Invoice, "detail", "tree", read=["id", "number"])

    # Nested rows are a list: rows and fields both follow the "list" rules.
    data = client_for(billing["u"]["accounting"]).get(f"/enrollments/{e.pk}/full/").json()
    assert [row["number"] for row in data["invoices"]] == ["IN"]
    assert set(data["invoices"][0]) == {"id", "number", "amount"}

    data = client_for(root).get(f"/enrollments/{e.pk}/full/").json()
    assert sorted(row["number"] for row in data["invoices"]) == ["IN", "OUT"]


# =============================================================== bulk helpers

def test_bulk_update_only_touches_allowed_rows(world):
    engine = PolicyEngine.for_user(world["u"]["prof"])
    assert engine.bulk_update(Enrollment.objects.all(), grade="Z") == 2
    assert Enrollment.objects.get(pk=world["e"]["branch"].pk).grade == ""
    with pytest.raises(ValueError):
        engine.bulk_update(Enrollment.objects.all(), organization=world["other"])


def test_bulk_delete_respects_deny(world):
    rule("admin", Enrollment, "delete", "below", effect="deny")
    engine = PolicyEngine.for_user(world["u"]["admin"])
    assert engine.bulk_delete(Enrollment.objects.all()) == 1  # only the school-level one
    assert Enrollment.objects.count() == 4


# =============================================================== tree safety

def test_bulk_parent_changes_are_blocked(world):
    with pytest.raises(ValueError):
        Organization.objects.filter(pk=world["branch"].pk).update(parent=world["other"])
    Organization.objects.filter(pk=world["branch"].pk).update(name="Renamed")  # fine


def test_rebuild_paths_command(world):
    dept = world["dept"]
    Organization._base_manager.filter(pk=dept.pk).update(path="999/")
    out = io.StringIO()
    call_command("policytree_rebuild_paths", stdout=out)
    assert "Fixed 1" in out.getvalue()
    dept.refresh_from_db()
    assert dept.path == f"{seg(world['school'])}/{seg(world['branch'])}/{seg(dept)}/"


def test_bulk_create_builds_paths(world):
    Organization.objects.bulk_create([Organization(name="Chem", code="chem", type="department", parent=world["branch"])])
    chem = Organization.objects.get(code="chem")
    assert chem.path == f"{world['branch'].path}{seg(chem)}/"


# =============================================================== audit log

def test_admin_changes_are_audited(world, client):
    root = world["u"]["root"]
    client.force_login(root)
    ct = ContentType.objects.get_for_model(Invoice)
    r = client.post("/admin/policytree/policy/add/", {
        "code": "acct-invoice-list", "name": "x", "role": "accounting", "content_type": ct.pk,
        "action": "list", "effect": "allow", "scope": "tree", "condition": "",
        "readable_fields": "[]", "writable_fields": "[]",
    })
    assert r.status_code == 302, r.content
    change = PolicyChange.objects.first()
    assert (change.policy_code, change.kind, change.source, change.changed_by) == (
        "acct-invoice-list", "created", "admin", root)

    pk = Policy.objects.get(code="acct-invoice-list").pk
    assert client.post(f"/admin/policytree/policy/{pk}/delete/", {"post": "yes"}).status_code == 302
    change = PolicyChange.objects.first()
    assert (change.kind, change.source, change.data["scope"]) == ("deleted", "admin", "tree")


def test_code_changes_are_audited(world):
    before = PolicyChange.objects.count()
    rule("staff", Invoice, "list")
    change = PolicyChange.objects.first()
    assert PolicyChange.objects.count() == before + 1
    assert (change.source, change.changed_by) == ("code", None)


def test_audit_log_is_read_only_in_admin(world, client):
    client.force_login(world["u"]["root"])
    assert client.get("/admin/policytree/policychange/").status_code == 200
    assert client.get("/admin/policytree/policychange/add/").status_code == 403


# =============================================================== seed / export

def run(command, *args):
    out = io.StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue()


def test_seed_and_export(world, tmp_path):
    rows = [
        {"code": "acct-inv-list", "role": "accounting", "model": "testapp.Invoice", "action": "list", "scope": "tree"},
        {"code": "acct-inv-detail", "role": "accounting", "model": "testapp.invoice", "action": "detail"},
    ]
    path = tmp_path / "policies.json"
    path.write_text(json.dumps(rows))

    assert "2 created" in run("policytree_seed", str(path))
    assert "2 unchanged" in run("policytree_seed", str(path))
    assert Policy.objects.get(code="acct-inv-list").scope == "tree"
    assert PolicyChange.objects.filter(source="seed").count() == 2

    rows[1]["scope"] = "tree"
    path.write_text(json.dumps(rows))
    assert "Would apply: 0 created, 1 updated" in run("policytree_seed", str(path), "--dry-run")
    assert Policy.objects.get(code="acct-inv-detail").scope == "org"
    assert "1 updated" in run("policytree_seed", str(path))

    exported = json.loads(run("policytree_export"))
    assert {"acct-inv-list", "acct-inv-detail"} <= {p["code"] for p in exported}

    before = Policy.objects.count()
    assert f"{before - 2} deleted" in run("policytree_seed", str(path), "--prune")
    assert Policy.objects.count() == 2


def test_seed_rejects_bad_policies(world, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"code": "x", "role": "admin", "model": "testapp.Invoice", "action": "list", "scope": "galaxy"}]))
    with pytest.raises(CommandError):
        run("policytree_seed", str(path))
    assert not Policy.objects.filter(code="x").exists()


def test_export_is_seed_compatible(world, tmp_path):
    path = tmp_path / "all.json"
    run("policytree_export", str(path))
    assert "0 created" in run("policytree_seed", str(path)) and "unchanged" in run("policytree_seed", str(path))


# =============================================================== checks

def test_local_cache_warning():
    assert "policytree.W001" in check_ids()
    with override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": "redis://x"}}):
        assert "policytree.W001" not in check_ids()


# =============================================================== plain Django views

def web(user):
    from django.test import Client
    c = Client()
    c.force_login(user)
    return c


def test_web_list_is_scoped(world):
    r = web(world["u"]["prof"]).get("/web/")
    assert sorted(e.student_name for e in r.context["object_list"]) == ["Ana", "Ben"]


def test_web_access_rules(world, client):
    assert web(world["u"]["prof"]).get(f"/web/{world['e']['branch'].pk}/").status_code == 404
    assert web(world["u"]["nobody"]).get("/web/").status_code == 403
    assert client.get("/web/").status_code == 302  # login required


def test_web_update_form_follows_field_rules(world):
    c, ana = web(world["u"]["prof"]), world["e"]["dept_s1"]
    form = c.get(f"/web/{ana.pk}/edit/").context["form"]
    assert set(form.fields) == {"student_name", "grade"}
    assert form.fields["student_name"].disabled and not form.fields["grade"].disabled

    assert c.post(f"/web/{ana.pk}/edit/", {"student_name": "Hacked", "grade": "A"}).status_code == 302
    ana.refresh_from_db()
    assert (ana.student_name, ana.grade) == ("Ana", "A")


def test_web_create_checks_target_organization(world):
    c = web(world["u"]["registrar"])
    data = {"student_name": "Web", "grade": "", "tuition": "10"}
    assert c.post("/web/new/", {**data, "organization": world["school"].pk}).status_code == 403
    assert c.post("/web/new/", {**data, "organization": world["dept"].pk}).status_code == 302
    assert Enrollment.objects.get(student_name="Web").owner == world["u"]["registrar"]


def test_web_update_rechecks_the_result(world):
    set_policy("registrar", "update", condition="small_tuition")
    cy = world["e"]["branch"]
    data = {"organization": cy.organization_id, "student_name": "Cy", "grade": "", "tuition": "5000"}
    assert web(world["u"]["registrar"]).post(f"/web/{cy.pk}/edit/", data).status_code == 403
    cy.refresh_from_db()
    assert cy.tuition == 300


def test_web_delete(world):
    ana = world["e"]["dept_s1"]
    assert web(world["u"]["prof"]).post(f"/web/{ana.pk}/delete/").status_code == 403
    assert web(world["u"]["admin"]).post(f"/web/{ana.pk}/delete/").status_code == 302
    assert not Enrollment.objects.filter(pk=ana.pk).exists()


def test_template_tag(world):
    ana = world["e"]["dept_s1"]
    assert "can_edit=True" in web(world["u"]["prof"]).get(f"/web/{ana.pk}/").content.decode()
    assert "can_edit=False" in web(world["u"]["student"]).get(f"/web/{ana.pk}/").content.decode()


# =============================================================== condition stages

def test_after_only_condition_checks_results_but_not_lists(world):
    from policytree.registry import Condition, ConditionRegistry

    class CapAfter(Condition):
        name = "cap_after"
        models = ["testapp.Enrollment"]
        check_on = Condition.AFTER

        def q(self, user):
            return Q(tuition__lt=1000)

    ConditionRegistry.register(CapAfter)
    try:
        Enrollment.objects.filter(pk=world["e"]["branch"].pk).update(tuition=5000)
        set_policy("registrar", "list", condition="cap_after")
        set_policy("registrar", "update", condition="cap_after")
        reg, cy = world["u"]["registrar"], world["e"]["branch"]
        assert "Cy" in names(client_for(reg).get("/enrollments/"))   # lists ignore it
        assert patch(reg, cy, tuition="800").status_code == 200       # result under the cap
        assert patch(reg, cy, tuition="2000").status_code == 403      # result over the cap
        cy.refresh_from_db()
        assert cy.tuition == 800
    finally:
        ConditionRegistry.unregister("cap_after")
