"""
Roles come from one of two places:

  - code:  Member.role is a CharField with `choices` (or any string)
  - admin: Member.role is a RoleField, and superusers manage policytree.Role rows

Role inheritance comes from POLICYTREE_ROLE_INCLUDES (code) plus, in admin
mode, each Role's `includes` (editable at runtime). Inactive roles grant nothing.
"""
from .conf import PolicyTreeSettings


class Roles:
    """Answers questions about roles without caring where they are defined."""

    # ---- which mode

    @classmethod
    def field(cls):
        return PolicyTreeSettings.get_member_model()._meta.get_field("role")

    @classmethod
    def uses_model(cls):
        """True when Member.role points at policytree.Role (admin-managed roles)."""
        try:
            field = cls.field()
        except Exception:
            return False
        if not field.is_relation:
            return False
        from .models import Role

        return field.related_model is Role

    @classmethod
    def key(cls, membership):
        """The role code of a membership, without a database query."""
        return membership.role_id if cls.uses_model() else membership.role

    # ---- cached Role table

    @classmethod
    def _cache_key(cls):
        return f"{PolicyTreeSettings.cache_key()}:roles"

    @classmethod
    def _build(cls):
        from .cache import SafeCache
        from .models import Role

        roles = list(Role.objects.using(SafeCache.database()).prefetch_related("includes"))
        return {
            "names": {r.code: r.name for r in roles},
            "active": {r.code for r in roles if r.is_active},
            "includes": {r.code: sorted(i.code for i in r.includes.all()) for r in roles},
        }

    @classmethod
    def data(cls):
        from .cache import SafeCache

        return SafeCache.get(cls._cache_key(), cls._build)

    @classmethod
    def clear(cls, **kwargs):
        from .cache import SafeCache

        SafeCache.changed(cls._cache_key())

    # ---- questions

    @classmethod
    def choices(cls):
        """[(code, label), ...] for dropdowns."""
        if cls.uses_model():
            data = cls.data()
            return [
                (code, name if code in data["active"] else f"{name} (inactive)")
                for code, name in sorted(data["names"].items(), key=lambda item: item[1].lower())
            ]
        return cls.static_choices()

    @classmethod
    def static_choices(cls):
        """Choices declared in code on Member.role; never touches the database."""
        try:
            field = cls.field()
        except Exception:
            return []
        if field.is_relation:
            return []
        return list(field.choices or [])

    @classmethod
    def known(cls):
        """Set of valid role codes, or None when any string is accepted."""
        if cls.uses_model():
            return set(cls.data()["names"])
        values = {value for value, _ in cls.static_choices()}
        return values or None

    @classmethod
    def is_active(cls, code):
        if not cls.uses_model():
            return True
        return code in cls.data()["active"]

    @classmethod
    def includes(cls):
        """Merged inheritance map: settings plus Role.includes in admin mode."""
        merged = {role: list(children) for role, children in PolicyTreeSettings.role_includes().items()}
        if cls.uses_model():
            for role, children in cls.data()["includes"].items():
                merged.setdefault(role, []).extend(children)
        return merged


class RoleTree:
    """
    Expands a role into itself plus every role it includes, transitively.

        POLICYTREE_ROLE_INCLUDES = {"admin": ["manager"], "manager": ["staff"]}
        RoleTree.expand("admin")  ->  {"admin", "manager", "staff"}

    An inactive role is skipped together with everything it would pull in.
    """

    @classmethod
    def expand(cls, role, includes=None):
        includes = Roles.includes() if includes is None else includes
        seen, stack = set(), [role]
        while stack:
            current = stack.pop()
            if current in seen or not Roles.is_active(current):
                continue
            seen.add(current)
            stack.extend(includes.get(current, ()))
        return seen

    @classmethod
    def find_cycle(cls, includes=None):
        """Return a list like ["a", "b", "a"] if roles include each other, else None."""
        includes = PolicyTreeSettings.role_includes() if includes is None else includes
        visiting, done = [], set()
        for role in includes:
            found = cls._visit(role, includes, visiting, done)
            if found:
                return found
        return None

    @classmethod
    def _visit(cls, role, includes, visiting, done):
        if role in done:
            return None
        if role in visiting:
            return visiting[visiting.index(role):] + [role]
        visiting.append(role)
        for child in includes.get(role, ()):
            found = cls._visit(child, includes, visiting, done)
            if found:
                return found
        visiting.pop()
        done.add(role)
        return None
