"""Edge cases: misconfiguration, admin bulk actions, unusual inputs, extension points."""
import pytest
from django.contrib.contenttypes.models import ContentType
from django.core import checks
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db.models import Q
from django.test import RequestFactory, override_settings

from policytree.checks import PolicyTreeChecks
from policytree.conf import PolicyTreeSettings
from policytree.drf import DRFActionMap
from policytree.engine import PolicyEngine
from policytree.models import Policy, Role
from policytree.registry import (
    Condition, ConditionRegistry, FieldPaths, OwnScope, Scope, ScopeRegistry,
)
from policytree.roles import Roles
from policytree.templatetags.policytree import PolicyTemplateTags
from tests.conftest import client_for, rule
from tests.testapp.models import Enrollment, Invoice, Organization, Payment

pytestmark = pytest.mark.django_db


def ids(**override):
    with override_settings(**override):
        return {m.id for m in checks.run_checks() if m.id.startswith("policytree")}


# =============================================================== settings and checks

@pytest.mark.parametrize("value", [None, "", "nodot", "nope.Missing"])
def test_bad_member_setting(value):
    assert "policytree.E002" in ids(POLICYTREE_MEMBER_MODEL=value)


def test_missing_setting_message():
    with override_settings(POLICYTREE_ORGANIZATION_MODEL=None):
        with pytest.raises(ImproperlyConfigured, match="must be set"):
            PolicyTreeSettings.organization_model_label()


def test_manager_warning():
    messages = []
    PolicyTreeChecks._check_manager(Enrollment, messages)  # a plain manager
    assert [m.id for m in messages] == ["policytree.W003"]


def test_cache_warning_skipped_in_debug():
    assert "policytree.W001" not in ids(DEBUG=True)


def test_roles_without_member_model():
    with override_settings(POLICYTREE_MEMBER_MODEL="nope.Missing"):
        assert not Roles.uses_model()
        assert Roles.static_choices() == []
        assert Roles.known() is None


# =============================================================== registries and scopes

def test_registry_rejects_bad_items():
    with pytest.raises(ImproperlyConfigured):
        ScopeRegistry.register(object())
    with pytest.raises(ImproperlyConfigured):
        ScopeRegistry.register(type("Nameless", (Scope,), {}))

    class OtherOwn(OwnScope):
        name = "own"

    with pytest.raises(ImproperlyConfigured, match="already registered"):
        ScopeRegistry.register(OtherOwn)
    with pytest.raises(ImproperlyConfigured, match="Unknown scope"):
        ScopeRegistry.get("galaxy")


def test_base_classes_must_be_implemented():
    with pytest.raises(NotImplementedError):
        Scope().covers(None, None, None, None, None)
    with pytest.raises(NotImplementedError):
        Scope().q(None, None, None)
    with pytest.raises(NotImplementedError):
        Condition().q(None)


def test_condition_is_false_for_unsaved_objects():
    class Any(Condition):
        name = "any"

        def q(self, user):
            return Q()

    assert Any().check(None, Enrollment()) is False


def test_owner_reached_through_another_model(world):
    paths = FieldPaths(Payment)
    assert paths.related_joins() == ["invoice", "invoice__organization"]
    inv = Invoice(organization=world["dept"], number="1", amount=1)  # no owner yet
    assert paths.owner_id_of(Payment(invoice=inv, amount=1)) is None
    assert paths.owner_id_of(Payment(amount=1)) is None


def test_below_and_ancestors_with_empty_paths(world):
    stray = Organization.objects.create(name="Stray", code="stray", type="school")
    Organization._base_manager.filter(pk=stray.pk).update(path="")
    user = world["u"]["nobody"]
    from tests.testapp.models import Member

    Member.objects.create(user=user, organization=stray, role="staff")
    for scope in ("below", "ancestors"):
        Policy.objects.filter(role="staff").delete()
        rule("staff", Enrollment, "list", scope)
        engine = PolicyEngine.for_user(user)
        assert list(engine.filter(Enrollment.objects.all(), "list")) == []


# =============================================================== engine details

def test_require_raises(world):
    engine = PolicyEngine.for_user(world["u"]["student"])
    engine.require("detail", world["e"]["dept_s1"])
    with pytest.raises(PermissionDenied):
        engine.require("detail", world["e"]["dept_s2"])


def test_superuser_shortcuts(world):
    engine = PolicyEngine.for_user(world["u"]["root"])
    qs = Enrollment.objects.all()
    assert engine.filter(qs, "list") is qs
    assert engine.fields(Enrollment, "detail", "read") is None
    assert engine.fields_map(Enrollment, "list", "read", list(qs)) == {e.pk: None for e in qs}
    assert engine.filter_objects(list(qs), "delete") == list(qs)


def test_fields_without_an_organization(world):
    engine = PolicyEngine.for_user(world["u"]["prof"])
    assert engine.fields(Enrollment, "detail", "read", obj=Enrollment(student_name="x")) == set()


def test_after_only_deny_does_not_hide_records(world):
    class Late(Condition):
        name = "late"
        check_on = Condition.AFTER

        def q(self, user):
            return Q(grade="F")

    ConditionRegistry.register(Late)
    try:
        rule("registrar", Enrollment, "list", "tree", effect="deny", condition="late")
        Enrollment.objects.filter(pk=world["e"]["dept_s1"].pk).update(grade="F")
        engine = PolicyEngine.for_user(world["u"]["registrar"])
        assert engine.filter(Enrollment.objects.filter(grade="F"), "list").count() == 1
        assert engine.filter_objects(list(Enrollment.objects.filter(grade="F")), "list")
        rows = list(Enrollment.objects.filter(grade="F"))
        assert engine.fields_map(Enrollment, "list", "read", rows)
    finally:
        ConditionRegistry.unregister("late")


def test_fields_map_skips_rules_that_reach_nothing(world):
    nobody = world["u"]["nobody"]
    from tests.testapp.models import Member

    Member.objects.create(user=nobody, organization=world["dept"], role="professor")
    Member.objects.create(user=nobody, organization=world["dept"], role="staff")
    rule("staff", Enrollment, "list", "org", read=["tuition"])
    engine = PolicyEngine.for_user(nobody)
    result = engine.fields_map(Enrollment, "list", "read", list(Enrollment.objects.all()))
    ana = world["e"]["dept_s1"]
    assert result[ana.pk] == {"id", "student_name", "grade", "tuition"}
    assert result[world["e"]["other"].pk] == set()


# =============================================================== DRF details

def test_action_map_edges(world):
    assert DRFActionMap.requirements(None) == []
    assert DRFActionMap.resolve(None) is None

    class View:
        action = "list"
        detail = False
        queryset = Enrollment.objects.all()

    assert DRFActionMap.action_for(View(), Invoice) is None
    assert DRFActionMap.action_for(View(), Enrollment) == "list"


def test_unmapped_detail_action_is_superuser_only(world):
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from policytree.drf import PolicyPermission

    class View:
        action = "mystery"
        detail = True
        queryset = Enrollment.objects.all()

    request = Request(APIRequestFactory().get("/"))
    request.user = world["u"]["admin"]
    assert not PolicyPermission().has_object_permission(request, View(), world["e"]["school"])
    request.user = world["u"]["root"]
    assert PolicyPermission().has_object_permission(request, View(), world["e"]["school"])


def test_permission_class_without_the_mixin(world):
    """PolicyPermission alone still finds the create target from the request."""
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from policytree.drf import PolicyPermission

    class View:
        action = "create"
        detail = False
        queryset = Enrollment.objects.all()

    for org, expected in ((world["dept"], True), (world["school"], False)):
        from rest_framework.parsers import JSONParser

        raw = APIRequestFactory().post("/", {"organization": str(org.pk)}, format="json")
        request = Request(raw, parsers=[JSONParser()])
        request.user = world["u"]["registrar"]
        assert PolicyPermission().has_permission(request, View()) is expected


def test_policy_require_in_viewsets(world):
    from rest_framework.exceptions import PermissionDenied as DRFDenied

    from tests.testapp.api import EnrollmentViewSet

    view = EnrollmentViewSet()
    view.request = RequestFactory().get("/")
    view.request.user = world["u"]["student"]
    view.policy_require("detail", world["e"]["dept_s1"])
    with pytest.raises(DRFDenied):
        view.policy_require("detail", world["e"]["dept_s2"])


def test_serializer_without_request_shows_everything(world):
    from tests.testapp.api import EnrollmentSerializer

    data = EnrollmentSerializer(world["e"]["dept_s1"]).data
    assert "tuition" in data


# =============================================================== plain views

def test_view_needs_an_action(world):
    from django.views.generic import View

    from policytree.views import PolicyViewMixin

    class Odd(PolicyViewMixin, View):
        model = Enrollment

    view = Odd()
    with pytest.raises(PermissionDenied):
        view.get_policy_action()
    Odd.policy_action = "list"
    assert view.get_policy_action() == "list"


def test_view_model_from_queryset():
    from django.views.generic import ListView

    from policytree.views import PolicyViewMixin

    class ByQueryset(PolicyViewMixin, ListView):
        queryset = Enrollment.objects.all()

    assert ByQueryset().get_policy_model() is Enrollment


def test_superuser_web_list_is_unfiltered(world):
    from django.test import Client

    c = Client()
    c.force_login(world["u"]["root"])
    assert len(c.get("/web/").context["object_list"]) == 5


def test_web_detail_denies_out_of_reach(world):
    from django.test import Client

    rule("professor", Enrollment, "detail", "tree", effect="deny", condition="not_mine")
    c = Client()
    c.force_login(world["u"]["prof"])
    assert c.get(f"/web/{world['e']['dept_s1'].pk}/").status_code == 404


def test_template_tag_without_request(world):
    assert PolicyTemplateTags.can({}, "update", world["e"]["dept_s1"]) is False
    assert PolicyTemplateTags.can({"request": object()}, "update", None) is False


# =============================================================== admin bulk actions

def test_policy_admin_bulk_delete_is_audited(world, client):
    from policytree.models import PolicyChange

    client.force_login(world["u"]["root"])
    pks = list(Policy.objects.filter(role="student").values_list("pk", flat=True))
    r = client.post("/admin/policytree/policy/", {"action": "delete_selected", "_selected_action": pks, "post": "yes"})
    assert r.status_code == 302
    assert not Policy.objects.filter(role="student").exists()
    assert PolicyChange.objects.filter(kind="deleted", source="admin").count() == len(pks)


@override_settings(POLICYTREE_MEMBER_MODEL="testapp.RoleMember")
def test_role_admin_list_and_bulk_delete(world, client):
    used = Role.objects.create(code="teacher", name="Teacher")
    Role.objects.create(code="spare", name="Spare").includes.add(used)
    rule("teacher", Enrollment, "list")
    client.force_login(world["u"]["root"])

    page = client.get("/admin/policytree/role/").content.decode()
    assert "Teacher" in page and "Spare" in page

    # A selection that includes a role in use is refused as a whole.
    pks = list(Role.objects.values_list("pk", flat=True))
    r = client.post("/admin/policytree/role/", {"action": "delete_selected", "_selected_action": pks, "post": "yes"})
    assert r.status_code == 403
    assert Role.objects.count() == 2

    spare = Role.objects.get(code="spare").pk
    r = client.post("/admin/policytree/role/", {"action": "delete_selected", "_selected_action": [spare], "post": "yes"})
    assert r.status_code == 302
    assert list(Role.objects.values_list("code", flat=True)) == ["teacher"]


@override_settings(POLICYTREE_MEMBER_MODEL="testapp.RoleMember")
def test_role_admin_refuses_non_superusers(world, client):
    staff = world["u"]["admin"]
    staff.is_staff = True
    staff.save()
    role = Role.objects.create(code="teacher", name="Teacher")
    client.force_login(staff)
    assert client.get(f"/admin/policytree/role/{role.pk}/delete/").status_code in (302, 403)


# =============================================================== models

def test_string_forms(world):
    assert str(world["dept"]) == "Math Dept"
    member = world["dept"].members.first()
    assert "professor" in str(member) or "student" in str(member)
    p = Policy.objects.filter(effect="allow").first()
    assert " may " in str(p)


def test_field_lists_must_be_lists(world):
    p = Policy(code="x", name="x", role="admin", action="list",
               content_type=ContentType.objects.get_for_model(Enrollment), readable_fields="grade")
    with pytest.raises(ValidationError) as exc:
        p.full_clean()
    assert "readable_fields" in exc.value.message_dict


def test_organization_clean_rejects_cycles(world):
    school = world["school"]
    school.parent = world["dept"]
    with pytest.raises(ValidationError):
        school.clean()


def test_bulk_update_of_paths_is_blocked(world):
    for org in Organization.objects.all():
        org.path = "x/"
    with pytest.raises(ValueError):
        Organization.objects.bulk_update(list(Organization.objects.all()), ["path"])
    Organization.objects.bulk_update(list(Organization.objects.all()), ["name"])  # fine


# =============================================================== seed errors

@pytest.mark.parametrize("content, message", [
    ("not json", "Can't read"),
    ('{"roles": "x", "policies": []}', "`roles` must be a list"),
    ('"just a string"', "must be a list of policies"),
    ('[{"model": "testapp.Enrollment"}]', "needs a valid `code` and `model`"),
    ('[{"code": "x", "model": "nope.Missing"}]', "needs a valid `code` and `model`"),
    ('{"roles": [{"code": "Bad Code!"}], "policies": []}', "Role 'Bad Code!'"),
])
def test_seed_reports_bad_files(tmp_path, content, message):
    from django.core.management import CommandError, call_command

    path = tmp_path / "p.json"
    path.write_text(content)
    with pytest.raises(CommandError, match=message):
        call_command("policytree_seed", str(path))


def test_seed_missing_file():
    from django.core.management import CommandError, call_command

    with pytest.raises(CommandError, match="Can't read"):
        call_command("policytree_seed", "/nonexistent/policies.json")


def test_seed_roles_without_role_field_notes_it(tmp_path):
    import io

    from django.core.management import call_command

    path = tmp_path / "p.json"
    path.write_text('{"roles": [{"code": "teacher"}, {"code": "head", "includes": ["teacher"]}], "policies": []}')
    out = io.StringIO()
    call_command("policytree_seed", str(path), stdout=out)
    assert "aren't used yet" in out.getvalue()
    assert Role.objects.get(code="teacher").name == "Teacher"

    path.write_text('{"roles": [{"code": "teacher", "name": "Lecturer"}, {"code": "head", "includes": []}], "policies": []}')
    out = io.StringIO()
    call_command("policytree_seed", str(path), stdout=out)
    assert "roles: 0 created, 2 updated" in out.getvalue()


def test_policy_fields_tag(world):
    from django.template import Context, Template
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    template = Template('{% load policytree %}{% policy_fields "detail" obj as shown %}'
                        '{% if "tuition" in shown %}T{% endif %}{% if "grade" in shown %}G{% endif %}')
    ana = world["e"]["dept_s1"]
    request.user = world["u"]["prof"]
    assert template.render(Context({"request": request, "obj": ana})) == "G"
    request = RequestFactory().get("/")
    request.user = world["u"]["admin"]
    assert Template('{% load policytree %}{% policy_fields "detail" obj as s %}{% if s is None %}ALL{% endif %}'
                    ).render(Context({"request": request, "obj": ana})) == "ALL"
    assert PolicyTemplateTags.fields({}, "detail", ana) == set()
