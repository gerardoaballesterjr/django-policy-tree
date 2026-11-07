"""
Property-based tests: for random trees, memberships, records and policies,
every way of asking the engine must give the same answer.

  - can(action, obj) is True exactly for the objects filter(queryset, action) returns
  - filter_objects() agrees with filter()
  - fields(obj) agrees with fields_map()
  - can_in(model, action, org) agrees with can() for unconditional rules
  - nobody without a matching allow ever sees anything
"""
import pytest
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import transaction
from django.test import override_settings
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from policytree.engine import PolicyEngine
from policytree.models import Policy
from tests.testapp.models import Enrollment, Member, Organization

pytestmark = pytest.mark.django_db

User = get_user_model()

ROLES = ["admin", "staff", "registrar", "professor", "student", "accounting"]
SCOPES = ["org", "tree", "below", "ancestors", "siblings", "own"]
CONDITIONS = ["", "ungraded", "small_tuition", "not_mine"]
ACTIONS = ["list", "update"]
FIELDS = ["id", "student_name", "grade", "tuition", "organization", "owner"]
USERS = 3


@st.composite
def worlds(draw):
    size = draw(st.integers(min_value=1, max_value=6))
    # parents[i] is None (a root) or an earlier index, so the result is a forest.
    parents = [None] + [draw(st.one_of(st.none(), st.integers(0, i - 1))) for i in range(1, size)]
    memberships = draw(st.lists(
        st.tuples(st.integers(0, USERS - 1), st.integers(0, size - 1), st.sampled_from(ROLES)),
        max_size=6, unique=True))
    records = draw(st.lists(
        st.tuples(st.integers(0, size - 1), st.integers(0, USERS - 1),
                  st.sampled_from(["", "A"]), st.sampled_from([10, 5000])),
        min_size=1, max_size=8))
    policies = draw(st.lists(
        st.tuples(st.sampled_from(ROLES), st.sampled_from(ACTIONS), st.sampled_from(["allow", "deny"]),
                  st.sampled_from(SCOPES), st.sampled_from(CONDITIONS),
                  st.lists(st.sampled_from(FIELDS), max_size=3, unique=True)),
        max_size=8, unique_by=lambda p: (p[0], p[1], p[2], p[4])))
    includes = draw(st.dictionaries(st.sampled_from(ROLES), st.lists(st.sampled_from(ROLES), max_size=2), max_size=2))
    return parents, memberships, records, policies, includes


def build(parents, memberships, records, policies):
    orgs = []
    for i, parent in enumerate(parents):
        orgs.append(Organization.objects.create(
            name=f"O{i}", code=f"o{i}", type="school", parent=orgs[parent] if parent is not None else None))
    users = [User.objects.create_user(f"u{i}") for i in range(USERS)]
    for user, org, role in memberships:
        Member.objects.create(user=users[user], organization=orgs[org], role=role)
    for org, owner, grade, tuition in records:
        Enrollment.objects.create(organization=orgs[org], owner=users[owner], student_name="x",
                                  grade=grade, tuition=tuition)
    ct = ContentType.objects.get_for_model(Enrollment)
    for n, (role, action, effect, scope, condition, fields) in enumerate(policies):
        Policy.objects.create(
            code=f"p{n}", name="p", role=role, content_type=ct, action=action, effect=effect,
            scope=scope, condition=condition,
            readable_fields=fields if effect == "allow" else [],
        )
    return orgs, users


def check(orgs, users, policies):
    every = list(Enrollment.objects.select_related("organization", "owner"))
    for user in users:
        engine = PolicyEngine.for_user(user)
        for action in ACTIONS:
            visible = set(engine.filter(Enrollment.objects.all(), action).values_list("pk", flat=True))

            # can() and filter() agree on every record
            for e in every:
                assert engine.can(action, e) == (e.pk in visible), (user.username, action, e.pk)

            # filter_objects() agrees with filter()
            assert {e.pk for e in engine.filter_objects(every, action)} == visible

            # fields(obj) agrees with fields_map() for visible records
            shown = [e for e in every if e.pk in visible]
            batch = engine.fields_map(Enrollment, action, "read", shown)
            for e in shown:
                assert engine.fields(Enrollment, action, "read", obj=e) == batch[e.pk]

            # no allow policy reaching the user's roles: nothing at all
            if not engine.has_any(Enrollment, action):
                assert not visible

            # can_in() matches can() when no conditions are involved
            if not any(p[4] for p in policies) and not any(p[3] == "own" for p in policies):
                for org in orgs:
                    in_org = [e for e in every if e.organization_id == org.pk]
                    for e in in_org:
                        assert engine.can_in(Enrollment, action, org) == engine.can(action, e)


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(worlds())
def test_engine_answers_agree(world_data):
    parents, memberships, records, policies, includes = world_data
    with override_settings(POLICYTREE_ROLE_INCLUDES=includes), transaction.atomic():
        cache.clear()
        orgs, users = build(parents, memberships, records, policies)
        check(orgs, users, policies)
        transaction.set_rollback(True)
    cache.clear()


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(worlds(), st.integers(0, 5), st.integers(0, 5))
def test_moving_organizations_keeps_answers_consistent(world_data, node, new_parent):
    """After moving part of the tree, paths and permissions still agree."""
    parents, memberships, records, policies, includes = world_data
    with override_settings(POLICYTREE_ROLE_INCLUDES=includes), transaction.atomic():
        cache.clear()
        orgs, users = build(parents, memberships, records, policies)
        if node < len(orgs) and new_parent < len(orgs):
            moving, target = orgs[node], orgs[new_parent]
            if not target.is_within(moving):  # moving under its own subtree is refused
                moving.parent = target
                moving.save()
            orgs = [Organization.objects.get(pk=o.pk) for o in orgs]
            for o in orgs:  # every path equals the chain of parents
                chain, cur = [], o
                while cur is not None:
                    chain.insert(0, cur.path_segment(cur.pk))
                    cur = cur.parent
                assert o.path == "".join(f"{seg}/" for seg in chain)
        check(orgs, users, policies)
        transaction.set_rollback(True)
    cache.clear()
