"""
Scopes and conditions: the pluggable parts of a policy.

Register your own in an app module named `policies.py` (auto-discovered):

    from django.db.models import Q
    from policytree.registry import Condition, ConditionRegistry

    @ConditionRegistry.register
    class Unpaid(Condition):
        name = "unpaid"
        label = "Only unpaid invoices"
        models = ["billing.Invoice"]

        def q(self, user):
            return Q(status="unpaid")
"""
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.db.models import Q

NOTHING = Q(pk__in=[])
EVERYTHING = Q(pk__isnull=False)  # unlike Q(), this really excludes everything


class FieldPaths:
    """
    How a model reaches its organization and owner. Declared once, on the model:

        class Task(models.Model):
            policytree_organization_field = "project__organization"   # default "organization"
            policytree_owner_field = "assignee"                         # default "owner"

    The organization model itself is its own organization; new organizations
    are created inside their `parent`.
    """

    _cache = {}

    def __new__(cls, model):
        from .conf import PolicyTreeSettings

        key = (model, PolicyTreeSettings.organization_model_label())
        if key not in cls._cache:
            instance = super().__new__(cls)
            instance._setup(model, PolicyTreeSettings.get_organization_model())
            cls._cache[key] = instance
        return cls._cache[key]

    def _setup(self, model, organization_model):
        self.model = model
        self.is_organization = issubclass(model, organization_model)
        self.owner = getattr(model, "policytree_owner_field", "owner")
        if self.is_organization:
            self.organization = ""
            self.id, self.path, self.parent = "pk", "path", "parent"
        else:
            self.organization = getattr(model, "policytree_organization_field", "organization")
            self.id = f"{self.organization}__pk"
            self.path = f"{self.organization}__path"
            self.parent = f"{self.organization}__parent"

    @classmethod
    def clear(cls):
        cls._cache.clear()

    # ---- walking objects

    @staticmethod
    def walk(obj, path):
        """Follow "a__b__c" from obj; None when any step is missing."""
        if not path:
            return obj
        for part in path.split("__"):
            if obj is None:
                return None
            try:
                obj = getattr(obj, part)
            except ObjectDoesNotExist:
                return None
        return obj

    def org_of(self, obj):
        return obj if self.is_organization else self.walk(obj, self.organization)

    def owner_id_of(self, obj):
        *parents, last = self.owner.split("__")
        target = self.walk(obj, "__".join(parents))
        if target is None:
            return None
        value = getattr(target, f"{last}_id", None)
        if value is None:
            value = getattr(getattr(target, last, None), "pk", None)
        return value

    # ---- creating

    @property
    def create_field(self):
        """The field a new record names its place with: "parent" for organizations,
        otherwise the first step of the organization path ("project" for "project__organization")."""
        return "parent" if self.is_organization else self.organization.split("__")[0]

    @property
    def create_rest(self):
        """The rest of the path after `create_field` ("organization" for "project__organization")."""
        return "" if self.is_organization else self.organization.partition("__")[2]

    def related_joins(self):
        """select_related() arguments that make org_of / owner_id_of query-free."""
        joins = set()
        if self.organization:
            joins.add(self.organization)
        owner_parents = self.owner.rpartition("__")[0]
        if owner_parents:
            joins.add(owner_parents)
        return sorted(joins)


# ---------------------------------------------------------------- scopes

class Scope:
    """
    Which organizations, relative to a membership, a policy reaches.
    `covers()` and `q()` must agree: an object is covered exactly when the
    queryset filter returns it.
    """

    name = ""
    label = ""
    needs_object = False  # True when coverage depends on the record itself (e.g. its owner)

    def covers(self, user, membership, org, obj, paths):
        """Does the membership reach `org` (and `obj`, if given)?"""
        raise NotImplementedError

    def q(self, user, membership, paths):
        """The same rule as a queryset filter."""
        raise NotImplementedError

    @staticmethod
    def has_path(org):
        """An organization without a path (e.g. loaded by raw SQL) reaches nothing."""
        return bool(getattr(org, "path", ""))


class OrgScope(Scope):
    name = "org"
    label = "Own organization"

    def covers(self, user, membership, org, obj, paths):
        return membership.organization_id == org.pk

    def q(self, user, membership, paths):
        return Q(**{paths.id: membership.organization_id})


class TreeScope(Scope):
    name = "tree"
    label = "Own organization and everything below it"

    def covers(self, user, membership, org, obj, paths):
        home = membership.organization
        return self.has_path(home) and self.has_path(org) and org.path.startswith(home.path)

    def q(self, user, membership, paths):
        home = membership.organization
        if not self.has_path(home):
            return NOTHING
        return Q(**{f"{paths.path}__startswith": home.path})


class BelowScope(Scope):
    name = "below"
    label = "Everything below own organization (not itself)"

    def covers(self, user, membership, org, obj, paths):
        home = membership.organization
        return (
            self.has_path(home)
            and self.has_path(org)
            and org.pk != home.pk
            and org.path.startswith(home.path)
        )

    def q(self, user, membership, paths):
        home = membership.organization
        if not self.has_path(home):
            return NOTHING
        return Q(**{f"{paths.path}__startswith": home.path}) & ~Q(**{paths.id: home.pk})


class AncestorsScope(Scope):
    name = "ancestors"
    label = "Own organization and everything above it"

    def covers(self, user, membership, org, obj, paths):
        home = membership.organization
        return self.has_path(home) and self.has_path(org) and home.path.startswith(org.path)

    def q(self, user, membership, paths):
        home = membership.organization
        if not self.has_path(home):
            return NOTHING
        return Q(**{f"{paths.id}__in": home.ancestor_ids()})


class SiblingsScope(Scope):
    """
    Organizations sharing the member's parent. A top-level organization has no
    parent, so it reaches only itself (never the other top-level organizations,
    which may be other tenants).
    """

    name = "siblings"
    label = "Own organization and those sharing its parent"

    def covers(self, user, membership, org, obj, paths):
        parent_id = membership.organization.parent_id
        if parent_id is None:
            return org.pk == membership.organization_id
        return org.parent_id == parent_id

    def q(self, user, membership, paths):
        parent_id = membership.organization.parent_id
        if parent_id is None:
            return Q(**{paths.id: membership.organization_id})
        return Q(**{f"{paths.parent}__pk": parent_id})


class OwnScope(Scope):
    name = "own"
    label = "Own records in own organization"
    needs_object = True

    def covers(self, user, membership, org, obj, paths):
        if membership.organization_id != org.pk:
            return False
        # No object yet (create): the record will be owned by the user.
        return obj is None or paths.owner_id_of(obj) == user.pk

    def q(self, user, membership, paths):
        return Q(**{paths.id: membership.organization_id, paths.owner: user})


# ---------------------------------------------------------------- conditions

class Condition:
    """
    An extra rule on the record itself. Implement `q()`; `check()` reuses it
    for single objects. Limit it to some models with `models`.

    `check_on` says which state of an updated record is tested:
      "before"  the record as it is (lists, detail, which records may be changed)
      "after"   the saved result of a change ("amount must stay under 10,000")
      "both"    both of the above
    Newly created records are always tested.
    """

    BEFORE, AFTER, BOTH = "before", "after", "both"

    name = ""
    label = ""
    models = ()  # "app_label.ModelName" labels; empty = any model
    check_on = BEFORE

    def used_at(self, stage):
        """stage: "before", "after" or "new"."""
        return stage == "new" or self.check_on in (stage, self.BOTH)

    def q(self, user):
        raise NotImplementedError

    def check(self, user, obj):
        if obj.pk is None:
            return False
        return type(obj)._base_manager.filter(pk=obj.pk).filter(self.q(user)).exists()

    def applies_to(self, model):
        labels = {label.lower() for label in self.models}
        return not labels or model._meta.label_lower in labels


# ---------------------------------------------------------------- registries

class BaseRegistry:
    kind = ""
    base = object

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._items = {}

    @classmethod
    def register(cls, item):
        """Use as a decorator on a class, or pass an instance."""
        instance = item() if isinstance(item, type) else item
        if not isinstance(instance, cls.base):
            raise ImproperlyConfigured(f"{item!r} is not a {cls.base.__name__}.")
        if not instance.name:
            raise ImproperlyConfigured(f"{type(instance).__name__} needs a `name`.")
        existing = cls._items.get(instance.name)
        if existing is not None and type(existing).__qualname__ != type(instance).__qualname__:
            raise ImproperlyConfigured(f"A {cls.kind} named '{instance.name}' is already registered.")
        cls._items[instance.name] = instance
        return item

    @classmethod
    def unregister(cls, name):
        cls._items.pop(name, None)

    @classmethod
    def get(cls, name):
        try:
            return cls._items[name]
        except KeyError:
            raise ImproperlyConfigured(f"Unknown {cls.kind} '{name}'. Is it registered?")

    @classmethod
    def names(cls):
        return list(cls._items)

    @classmethod
    def choices(cls):
        return [(name, item.label or name) for name, item in cls._items.items()]


class ScopeRegistry(BaseRegistry):
    kind = "scope"
    base = Scope

    @classmethod
    def register_defaults(cls):
        for scope in (OrgScope, TreeScope, OwnScope, BelowScope, AncestorsScope, SiblingsScope):
            cls.register(scope)


class ConditionRegistry(BaseRegistry):
    kind = "condition"
    base = Condition


ScopeRegistry.register_defaults()
