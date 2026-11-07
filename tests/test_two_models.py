"""Custom actions that work on a second model through `policy_actions`."""
import pytest
from django.core.exceptions import ImproperlyConfigured

from policytree.drf import DRFActionMap
from tests.conftest import client_for
from tests.testapp.models import Invoice

pytestmark = pytest.mark.django_db


def bill(user, enrollment, **data):
    payload = {"number": "INV-1", "amount": "50.00", **data}
    return client_for(user).post(f"/enrollments/{enrollment.pk}/bill/", payload, format="json")


# ------------------------------------------------------------ detail actions

def test_second_model_policy_is_required(billing):
    # admin can do everything on Enrollment but has no Invoice policy
    assert bill(billing["u"]["admin"], billing["e"]["dept_s1"]).status_code == 403
    assert not Invoice.objects.exists()


def test_second_model_scope_uses_main_objects_org(billing):
    acc = billing["u"]["accounting"]
    # Invoice create is "org" at North Branch: Math Dept is below it, so no
    assert bill(acc, billing["e"]["dept_s1"]).status_code == 403
    # an enrollment that sits in North Branch itself: yes
    r = bill(acc, billing["e"]["branch"], notes="ignored")
    assert r.status_code == 201
    inv = Invoice.objects.get()
    assert inv.organization == billing["branch"]
    assert inv.owner == acc
    assert inv.notes == ""  # not in writable_fields


def test_detail_action_still_needs_the_main_object(billing):
    # cashier may create invoices anywhere in the school, but can't see enrollments
    assert bill(billing["u"]["cashier"], billing["e"]["branch"]).status_code == 403


def test_main_object_out_of_scope_is_404(billing):
    # accounting's Enrollment scope starts at North Branch; this one is at the school
    assert bill(billing["u"]["accounting"], billing["e"]["school"]).status_code == 404


def test_superuser_can_bill(billing):
    assert bill(billing["u"]["root"], billing["e"]["dept_s1"]).status_code == 201


# ------------------------------------------------------------ lists of a second model

def test_second_model_list_is_scoped_and_fields_filtered(billing):
    e = billing["e"]["dept_s1"]
    root = billing["u"]["root"]
    Invoice.objects.create(enrollment=e, organization=billing["dept"], owner=root, number="IN", amount=1)
    Invoice.objects.create(enrollment=e, organization=billing["other"], owner=root, number="OUT", amount=2)

    r = client_for(billing["u"]["accounting"]).get(f"/enrollments/{e.pk}/invoices/")
    assert r.status_code == 200
    assert [row["number"] for row in r.json()] == ["IN"]
    assert set(r.json()[0]) == {"id", "number", "amount"}


def test_second_model_list_needs_a_policy(billing):
    # registrar sees enrollments but has no Invoice "list" policy
    e = billing["e"]["dept_s1"]
    assert client_for(billing["u"]["registrar"]).get(f"/enrollments/{e.pk}/invoices/").status_code == 403


# ------------------------------------------------------------ actions without an object

def test_create_without_object_reads_org_from_request(billing):
    c = client_for(billing["u"]["accounting"])
    payload = {"number": "X", "amount": "5.00"}
    assert c.post("/enrollments/issue/", {**payload, "organization": billing["dept"].pk}, format="json").status_code == 403
    assert c.post("/enrollments/issue/", {**payload, "organization": billing["branch"].pk}, format="json").status_code == 201
    assert c.post("/enrollments/issue/", payload, format="json").status_code == 400


# ------------------------------------------------------------ several requirements

def test_all_requirements_must_pass(billing):
    e = billing["e"]["dept_s1"]
    payload = {"grade": "B", "amount": "10.00"}
    # professor may update the enrollment but can't create invoices
    assert client_for(billing["u"]["prof"]).post(f"/enrollments/{e.pk}/grade_and_bill/", payload, format="json").status_code == 403
    # registrar may do both
    assert client_for(billing["u"]["registrar"]).post(f"/enrollments/{e.pk}/grade_and_bill/", payload, format="json").status_code == 200
    e.refresh_from_db()
    assert e.grade == "B"
    assert e.invoices.count() == 1


# ------------------------------------------------------------ configuration

@pytest.mark.parametrize("value", [
    "fly",
    ("fly", Invoice),
    ("list", "nope.Thing"),
    [["list", None]],
    [],
    42,
])
def test_bad_policy_actions_are_rejected(value):
    with pytest.raises(ImproperlyConfigured):
        DRFActionMap.normalize(value)


def test_policy_actions_forms():
    assert DRFActionMap.normalize("update") == [("update", None)]
    assert DRFActionMap.normalize(("list", "testapp.Invoice")) == [("list", Invoice)]
    assert DRFActionMap.normalize([("update", None), ("create", Invoice)]) == [
        ("update", None), ("create", Invoice),
    ]
