"""
Permission logic. Framework-agnostic; the DRF and view layers wrap this.

    engine = PolicyEngine.for_user(user)          # in views: PolicyEngine.for_request(request)
    engine.can("update", project)                 # one object
    engine.can_in(Project, "create", team)        # a model inside an organization
    engine.filter(Project.objects.all(), "list")  # a queryset
    engine.fields(Project, "detail", "read", p)   # field names (None = all)
    engine.can_create(p) / can_change(p, before)   # re-check a saved record

Rules: superusers can do everything; otherwise a matching deny wins, then
any matching allow grants; no match means no access. Anything unknown or
missing (an unregistered scope, a record without an organization) fails closed.
"""
import logging
from functools import reduce
from operator import or_

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import router, transaction

from .cache import SafeCache
from .conf import PolicyTreeSettings
from .models import Policy
from .registry import EVERYTHING, NOTHING, ConditionRegistry, FieldPaths, ScopeRegistry
from .roles import Roles, RoleTree

Action = Policy.Action
Effect = Policy.Effect
logger = logging.getLogger("policytree")


class Stage:
    """Which state of a record is being judged (see Condition.check_on)."""

    BEFORE = "before"  # the record as it is
    AFTER = "after"    # the saved result of an update
    NEW = "new"        # a freshly created record


class PolicyCache:
    """The Policy table as {(role, content_type_id, action): [rule, ...]}, cached."""

    @classmethod
    def _build(cls):
        data = {}
        for p in Policy.objects.using(router.db_for_write(Policy)).all():
            data.setdefault((p.role, p.content_type_id, p.action), []).append({
                "id": p.pk,
                "effect": p.effect,
                "scope": p.scope,
                "condition": p.condition,
                "read": list(p.readable_fields),
                "write": list(p.writable_fields),
            })
        return data

    @classmethod
    def all(cls):
        return SafeCache.get(PolicyTreeSettings.cache_key(), cls._build)

    @classmethod
    def rules(cls, role, content_type_id, action):
        return cls.all().get((role, content_type_id, action), [])

    @classmethod
    def clear(cls, **kwargs):
        SafeCache.changed(PolicyTreeSettings.cache_key())


class PolicyEngine:
    """All permission checks for one user."""

    REQUEST_ATTR = "_policytree_engine"

    def __init__(self, user):
        self.user = user
        self._memberships = None
        self._roles = {}
        self._owed = set()  # (model, action) pre-checks that still need a check on real records

    @classmethod
    def for_user(cls, user):
        return cls(user)

    @classmethod
    def for_request(cls, request):
        engine = getattr(request, cls.REQUEST_ATTR, None)
        if engine is None or engine.user is not request.user:
            engine = cls(request.user)
            setattr(request, cls.REQUEST_ATTR, engine)
        return engine

    # ---- who the user is

    @property
    def is_active(self):
        return getattr(self.user, "is_active", True) is not False

    @property
    def is_authenticated(self):
        return bool(getattr(self.user, "is_authenticated", False)) and self.is_active

    @property
    def is_superuser(self):
        return bool(getattr(self.user, "is_superuser", False)) and self.is_authenticated

    @property
    def memberships(self):
        if self._memberships is None:
            if not self.is_authenticated:
                self._memberships = []
            else:
                # Default managers decide what counts: a soft-deleted membership,
                # or one in a soft-deleted organization, grants nothing.
                organizations = PolicyTreeSettings.get_organization_model()._default_manager
                self._memberships = list(
                    PolicyTreeSettings.get_member_model()
                    ._default_manager.filter(user=self.user, organization__in=organizations.values("pk"))
                    .select_related("organization")
                )
        return self._memberships

    def _roles_of(self, membership):
        role = Roles.key(membership)
        if role not in self._roles:
            self._roles[role] = RoleTree.expand(role)
        return self._roles[role]

    # ---- rules

    def rules(self, model, action, effect=None):
        """Yield (membership, rule) for every policy that applies to the user's roles."""
        if model is None:
            raise ImproperlyConfigured("A model is needed to check permissions.")
        ct_id = ContentType.objects.get_for_model(model).id
        for m in self.memberships:
            seen = set()
            for role in self._roles_of(m):
                for rule in PolicyCache.rules(role, ct_id, action):
                    if rule["id"] in seen or (effect and rule["effect"] != effect):
                        continue
                    seen.add(rule["id"])
                    yield m, rule

    @staticmethod
    def _lookup(registry, name):
        """A registered scope or condition, or None (logged) if it no longer exists."""
        try:
            return registry.get(name)
        except ImproperlyConfigured:
            logger.error("Policy refers to unknown %s '%s'; treating it as matching nothing for allows "
                         "and everything for denies.", registry.kind, name)
            return None

    def org_of(self, obj):
        return FieldPaths(type(obj)).org_of(obj)

    def place_of(self, obj):
        """
        The organization a record is placed in: its parent for an organization,
        otherwise its organization. Creating or moving a record here needs a
        `create` allow that reaches this place.
        """
        paths = FieldPaths(type(obj))
        if paths.is_organization:
            return FieldPaths.walk(obj, "parent")
        return paths.org_of(obj)

    def _reach_org(self, obj, action):
        """The organization scopes are measured against: an organization is created in its parent."""
        if action == Action.CREATE:
            return self.place_of(obj)
        return self.org_of(obj)

    def _condition_holds(self, rule, obj, stage):
        """True / False, or None when it can't be judged: no saved object, or not used at this stage."""
        if not rule["condition"]:
            return True
        condition = self._lookup(ConditionRegistry, rule["condition"])
        if condition is None:
            return rule["effect"] == Effect.DENY  # fail closed
        if obj is None or obj.pk is None or not condition.used_at(stage):
            return None
        return condition.check(self.user, obj)

    def _matches(self, model, action, effect, org, obj=None, stage=Stage.BEFORE):
        """Yield (membership, rule) whose scope reaches `org` and whose condition holds."""
        paths = FieldPaths(model)
        for m, rule in self.rules(model, action, effect):
            scope = self._lookup(ScopeRegistry, rule["scope"])
            if scope is None:
                if effect == Effect.DENY:
                    yield m, rule  # fail closed
                continue
            if obj is None and scope.needs_object and action != Action.CREATE:
                continue  # e.g. "own": can't tell whose records without one
            if not scope.covers(self.user, m, org, obj, paths):
                continue
            holds = self._condition_holds(rule, obj, stage)
            if holds is None:
                # Can't judge the condition here: a conditional allow might
                # apply (it is judged where it applies); a conditional deny
                # must not block what it doesn't cover.
                if effect == Effect.ALLOW:
                    yield m, rule
                continue
            if holds:
                yield m, rule

    def _decide(self, model, action, org, obj=None, stage=Stage.BEFORE):
        if any(True for _ in self._matches(model, action, Effect.DENY, org, obj, stage)):
            return False
        return any(True for _ in self._matches(model, action, Effect.ALLOW, org, obj, stage))

    # ---- checks still owed on real records

    def owe(self, model, action):
        """Record that a pre-check must be followed by a check on the real records."""
        self._owed.add((model, action))

    def confirm(self, model, action):
        """
        Mark an owed check as done. Only call this right after checking the very
        records the action uses (the DRF helpers policy_queryset, policy_require,
        policy_created and policy_changed do).
        """
        self._owed.discard((model, action))

    @property
    def owed(self):
        """(model, action) pairs whose pre-check passed provisionally and was never confirmed."""
        return set(self._owed)

    def assess(self, model, action, org):
        """
        (allowed, provisional) for `model` records inside `org`, without a record.
        Provisional means the answer depends on something only a real record can
        settle (a condition, or a per-record scope such as "own"): the caller must
        follow up with can(), filter(), can_create() or can_change().
        """
        if self.is_superuser:
            return True, False
        if org is None or not self.is_authenticated:
            return False, False
        paths = FieldPaths(model)
        creating = action == Action.CREATE
        unsure_deny = False
        for m, rule in self.rules(model, action, Effect.DENY):
            scope = self._lookup(ScopeRegistry, rule["scope"])
            if scope is None:
                return False, False
            if scope.needs_object and not creating:
                unsure_deny = True
                continue
            if not scope.covers(self.user, m, org, None, paths):
                continue
            if not rule["condition"]:
                return False, False
            if self._lookup(ConditionRegistry, rule["condition"]) is None:
                return False, False
            unsure_deny = True
        sure = unsure = False
        for m, rule in self.rules(model, action, Effect.ALLOW):
            scope = self._lookup(ScopeRegistry, rule["scope"])
            if scope is None:
                continue
            if scope.needs_object:
                # e.g. "own": who owns the record decides, so only a real record can settle it
                if creating and not scope.covers(self.user, m, org, None, paths):
                    continue
                unsure = True
                continue
            if not scope.covers(self.user, m, org, None, paths):
                continue
            if rule["condition"]:
                if self._lookup(ConditionRegistry, rule["condition"]) is not None:
                    unsure = True
                continue
            sure = True
        allowed = sure or unsure
        return allowed, allowed and (not sure or unsure_deny)

    # ---- single decisions

    def has_any(self, model, action):
        """Is there any allow policy for this user, model and action? (a quick gate)"""
        if self.is_superuser:
            return True
        return self.is_authenticated and any(True for _ in self.rules(model, action, Effect.ALLOW))

    def can_in(self, model, action, org):
        """
        May the user do `action` on any `model` record inside `org`? True only
        when that is certain without a record; see assess() for provisional answers.
        """
        allowed, provisional = self.assess(model, action, org)
        return allowed and not provisional

    def can(self, action, obj, stage=Stage.BEFORE):
        """May the user do `action` on this object?"""
        if self.is_superuser:
            return True
        if not self.is_authenticated:
            return False
        org = self._reach_org(obj, action)
        if org is None:
            return False
        return self._decide(type(obj), action, org, obj, stage)

    def require(self, action, obj, stage=Stage.BEFORE):
        """Raise PermissionDenied unless can(...)."""
        if not self.can(action, obj, stage):
            raise PermissionDenied(f"You may not {action} this {type(obj).__name__}.")

    # ---- re-checking saved records

    def can_create(self, obj, submitted=()):
        """
        Is this freshly saved record allowed? Checks its place, its conditions,
        and that every field in `submitted` was writable where it was created.
        Call inside the transaction that saved it.
        """
        if self.is_superuser:
            return True
        if not self.can(Action.CREATE, obj, Stage.NEW):
            return False
        return self._fields_ok(obj, Action.CREATE, submitted)

    def can_change(self, obj, before):
        """
        Is this saved update allowed? `before` is place_of(obj) taken before the
        change. A record moved to another place also needs `create` there.
        """
        if self.is_superuser:
            return True
        if not self.can(Action.UPDATE, obj, Stage.AFTER):
            return False
        after = self.place_of(obj)
        if getattr(after, "pk", None) != getattr(before, "pk", None):
            if after is None or not self._decide(type(obj), Action.CREATE, after, obj, Stage.NEW):
                return False
        return True

    def _fields_ok(self, obj, action, submitted):
        if not submitted:
            return True
        allowed = self.fields(type(obj), action, "write", obj=obj)
        return allowed is None or set(submitted) <= allowed

    # ---- many records

    def _rule_q(self, membership, rule, paths, stage=Stage.BEFORE):
        """
        Queryset filter for one rule at a stage. None means the rule doesn't
        apply to existing records here (an allow grants nothing, a deny blocks nothing).
        """
        scope = self._lookup(ScopeRegistry, rule["scope"])
        if scope is None:
            return EVERYTHING if rule["effect"] == Effect.DENY else None
        q = scope.q(self.user, membership, paths)
        if rule["condition"]:
            condition = self._lookup(ConditionRegistry, rule["condition"])
            if condition is None:
                return EVERYTHING if rule["effect"] == Effect.DENY else None
            if condition.used_at(stage):
                q &= condition.q(self.user)
            elif rule["effect"] == Effect.DENY:
                return None  # a deny for another stage never hides records here
        return q

    def _filter(self, queryset, action, stage):
        if self.is_superuser:
            return queryset
        if not self.is_authenticated:
            return queryset.none()
        paths = FieldPaths(queryset.model)
        allow = [self._rule_q(m, r, paths, stage) for m, r in self.rules(queryset.model, action, Effect.ALLOW)]
        allow = [q for q in allow if q is not None]
        if not allow:
            return queryset.none()
        deny = [self._rule_q(m, r, paths, stage) for m, r in self.rules(queryset.model, action, Effect.DENY)]
        deny = [q for q in deny if q is not None]
        queryset = queryset.filter(reduce(or_, allow))
        if deny:
            queryset = queryset.exclude(reduce(or_, deny))
        return queryset

    def filter(self, queryset, action):
        """Only the records the user may `action`."""
        return self._filter(queryset, action, Stage.BEFORE)

    def filter_objects(self, objects, action):
        """
        The same as filter(), for objects already in memory (e.g. prefetched).
        Checked in memory, or with one query when a conditional rule applies.
        """
        objects = list(objects)
        if self.is_superuser or not objects:
            return objects
        model = type(objects[0])
        conditional = any(rule["condition"] for _, rule in self.rules(model, action))
        if not conditional:
            return [obj for obj in objects if self.can(action, obj)]
        pks = [obj.pk for obj in objects if obj.pk is not None]
        allowed = set(self.filter(model._base_manager.filter(pk__in=pks), action).values_list("pk", flat=True))
        return [obj for obj in objects if obj.pk in allowed]

    def bulk_update(self, queryset, **values):
        """
        queryset.update(**values) on the records the user may update, with the
        same rules as single updates: only writable fields, and every changed
        record must still be allowed afterwards (else nothing changes).
        Returns how many rows changed.
        """
        model = queryset.model
        paths = FieldPaths(model)
        if {paths.create_field, f"{paths.create_field}_id"} & set(values):
            raise ValueError("Moving records between organizations in bulk isn't supported.")
        for name in values:
            try:
                field = model._meta.get_field(name)
            except Exception:
                field = next((f for f in model._meta.concrete_fields if f.attname == name), None)
            if field is None or not field.concrete or field.primary_key or not field.editable:
                raise ValueError(f"'{name}' can't be changed in bulk.")
        joins = paths.related_joins()
        scoped = self.filter(queryset, Action.UPDATE)
        rows = list(scoped.select_related(*joins) if joins else scoped)
        if not self.is_superuser:
            writable = self.fields_map(model, Action.UPDATE, "write", rows, prefiltered=True)
            rows = [r for r in rows if writable[r.pk] is None or set(values) <= writable[r.pk]]
        pks = [r.pk for r in rows]
        if not pks:
            return 0
        with transaction.atomic():
            count = model._default_manager.filter(pk__in=pks).update(**values)
            if not self.is_superuser:
                still = self._filter(model._base_manager.filter(pk__in=pks), Action.UPDATE, Stage.AFTER)
                if still.count() != len(pks):
                    raise PermissionDenied("Some results would be outside what you're allowed to update.")
        return count

    def bulk_delete(self, queryset):
        """Delete the records the user may delete. Returns how many rows went."""
        result = self.filter(queryset, Action.DELETE).delete()
        return result[0] if isinstance(result, tuple) else result  # soft-delete querysets return a count

    # ---- fields

    @staticmethod
    def _merge(rules, kind):
        result = set()
        for rule in rules:
            if not rule[kind]:  # an empty list on a policy means "all fields"
                return None
            result |= set(rule[kind])
        return result

    def fields(self, model, action, kind, obj=None):
        """
        Field names the user may `kind` ("read" or "write"): None means all,
        an empty set means none. With `obj`, only rules that reach it count.
        """
        if self.is_superuser:
            return None
        if obj is None:
            return self._merge((r for _, r in self.rules(model, action, Effect.ALLOW)), kind)
        org = self._reach_org(obj, action)
        if org is None:
            return set()
        stage = Stage.NEW if action == Action.CREATE else Stage.BEFORE
        return self._merge((r for _, r in self._matches(model, action, Effect.ALLOW, org, obj, stage)), kind)

    def fields_map(self, model, action, kind, objects, prefiltered=False):
        """
        {pk: fields} for many records at once (see fields()). Pass
        prefiltered=True only when `objects` came from filter(..., action) with
        this same action; then uniform rules cost no queries. Otherwise one
        query per rule, never one per record.
        """
        objects = [obj for obj in objects if getattr(obj, "pk", None) is not None]
        if self.is_superuser:
            return {obj.pk: None for obj in objects}
        rules = list(self.rules(model, action, Effect.ALLOW))
        if not rules:
            return {obj.pk: set() for obj in objects}
        distinct = {None if not r[kind] else frozenset(r[kind]) for _, r in rules}
        if prefiltered and len(distinct) == 1:
            value = self._merge((r for _, r in rules), kind)
            return {obj.pk: (None if value is None else set(value)) for obj in objects}

        pks = [obj.pk for obj in objects]
        result = {pk: set() for pk in pks}
        paths = FieldPaths(model)
        for membership, rule in rules:
            q = self._rule_q(membership, rule, paths)
            if q is None:
                continue
            for pk in model._base_manager.filter(pk__in=pks).filter(q).values_list("pk", flat=True):
                if result[pk] is not None:
                    result[pk] = None if not rule[kind] else result[pk] | set(rule[kind])
        return result


# re-exported for custom scopes that need an empty filter
__all__ = ["Action", "Effect", "EVERYTHING", "NOTHING", "PolicyCache", "PolicyEngine", "Stage"]
