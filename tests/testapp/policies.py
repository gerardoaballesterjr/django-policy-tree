"""Auto-discovered by policytree: the example project's conditions."""
from django.db.models import Q

from policytree.registry import Condition, ConditionRegistry


@ConditionRegistry.register
class Ungraded(Condition):
    name = "ungraded"
    label = "Only enrollments without a grade"
    models = ["testapp.Enrollment"]

    def q(self, user):
        return Q(grade="")


@ConditionRegistry.register
class SmallTuition(Condition):
    name = "small_tuition"
    label = "Only tuition under 1,000"
    models = ["testapp.Enrollment"]
    check_on = Condition.BOTH  # also stops raising tuition past the limit

    def q(self, user):
        return Q(tuition__lt=1000)


@ConditionRegistry.register
class AnyModelNotOwnedByMe(Condition):
    name = "not_mine"
    label = "Records owned by someone else"

    def q(self, user):
        return ~Q(owner=user)


@ConditionRegistry.register
class Archived(Condition):
    """Use with a deny on update/delete to make archived records read-only."""

    name = "archived"
    label = "Archived records"
    models = ["testapp.Organization", "testapp.Member", "testapp.Space", "testapp.Room"]

    def q(self, user):
        return Q(archived_at__isnull=False)
