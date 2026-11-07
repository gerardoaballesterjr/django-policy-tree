"""
Rough timings, not part of the normal test run:

    pytest benchmarks -q -s

Uses the example project with 10,000 enrollments in one department.
"""
import time

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from policytree.engine import PolicyEngine
from tests.conftest import client_for, rule
from tests.testapp.models import Enrollment, Member

pytestmark = pytest.mark.django_db

ROWS = 10_000


def timed(label, fn, repeat=3):
    best = None
    for _ in range(repeat):
        with CaptureQueriesContext(connection) as ctx:
            start = time.perf_counter()
            result = fn()
            elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    print(f"  {label:<58} {best * 1000:8.1f} ms  {len(ctx):3d} queries")
    return result


def test_benchmark(world):
    root = world["u"]["root"]
    Enrollment.objects.bulk_create(
        Enrollment(organization=world["dept"], owner=root, student_name=f"S{i}", tuition=i % 2000)
        for i in range(ROWS)
    )
    nobody = world["u"]["nobody"]
    Member.objects.create(user=nobody, organization=world["dept"], role="professor")
    Member.objects.create(user=nobody, organization=world["other"], role="accounting")
    rule("accounting", Enrollment, "list", "org")
    rule("registrar", Enrollment, "list", "tree", effect="deny", condition="small_tuition")

    print(f"\n{ROWS:,} enrollments")
    for name in ("registrar", "prof"):
        engine = PolicyEngine.for_user(world["u"][name])
        timed(f"engine.filter(...).count()  [{name}]",
              lambda: engine.filter(Enrollment.objects.all(), "list").count())

    objs = list(Enrollment.objects.select_related("organization")[:1000])
    engine = PolicyEngine.for_user(world["u"]["prof"])
    timed("engine.can() on 1,000 objects (no conditions)",
          lambda: [engine.can("detail", o) for o in objs])
    engine = PolicyEngine.for_user(world["u"]["registrar"])
    timed("engine.can() on 1,000 objects (with a conditional deny)",
          lambda: [engine.can("list", o) for o in objs])

    timed("engine.filter_objects() on 1,000 objects (conditional deny)",
          lambda: engine.filter_objects(objs, "list"))

    engine = PolicyEngine.for_user(nobody)
    timed("engine.fields_map() on 1,000 objects (mixed field rules)",
          lambda: engine.fields_map(Enrollment, "list", "read", objs))

    for name, label in (("prof", "uniform field rules"), ("nobody", "mixed field rules")):
        user = nobody if name == "nobody" else world["u"][name]
        c = client_for(user)
        timed(f"GET /enrollments/ all rows, {label}", lambda: c.get("/enrollments/"), repeat=1)
