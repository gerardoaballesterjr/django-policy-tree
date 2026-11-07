"""System checks so a misconfigured project fails at startup, not in production."""
from django.conf import settings
from django.core import checks
from django.core.exceptions import ImproperlyConfigured

from .conf import PolicyTreeSettings
from .roles import Roles, RoleTree


class PolicyTreeChecks:
    """
    E001  organization model setting missing, wrong, or not an AbstractOrganization
    E002  member model setting missing, wrong, or not an AbstractMember
    E003  member model has no `role` field
    E004  POLICYTREE_ROLE_INCLUDES contains a cycle
    E005  Member.role is a foreign key, but not a policytree.models.RoleField
    W001  the policy cache is local to each process
    W002  POLICYTREE_ROLE_INCLUDES names unknown roles
    W003  the organization model's default manager allows unsafe bulk path writes
    """

    TAG = "policytree"
    LOCAL_CACHES = (
        "django.core.cache.backends.locmem.LocMemCache",
        "django.core.cache.backends.dummy.DummyCache",
        "django.core.cache.backends.filebased.FileBasedCache",
    )

    @classmethod
    def register(cls):
        # Django tags the registered callable, so register an instance
        # (bound methods can't carry attributes).
        checks.register(cls(), cls.TAG)

    def __call__(self, app_configs=None, **kwargs):
        return self.run(app_configs, **kwargs)

    @classmethod
    def run(cls, app_configs=None, **kwargs):
        messages = []
        organization = cls._check_model(
            PolicyTreeSettings.get_organization_model, "AbstractOrganization", "E001", messages
        )
        member = cls._check_model(
            PolicyTreeSettings.get_member_model, "AbstractMember", "E002", messages
        )
        if member is not None:
            cls._check_role_field(member, messages)
        if organization is not None:
            cls._check_manager(organization, messages)
        cls._check_role_includes(messages)
        cls._check_cache(messages)
        return messages

    @classmethod
    def _check_model(cls, getter, base_name, error_id, messages):
        from . import models

        try:
            model = getter()
        except ImproperlyConfigured as exc:
            messages.append(checks.Error(str(exc), id=f"{cls.TAG}.{error_id}"))
            return None

        if not issubclass(model, getattr(models, base_name)):
            messages.append(
                checks.Error(
                    f"{model.__name__} must subclass policytree.models.{base_name}.",
                    obj=model,
                    id=f"{cls.TAG}.{error_id}",
                )
            )
            return None
        return model

    @classmethod
    def _check_role_field(cls, member, messages):
        try:
            field = member._meta.get_field("role")
        except Exception:
            messages.append(
                checks.Error(
                    f"{member.__name__} needs a `role` field.",
                    hint="Add role = models.CharField(max_length=50, choices=...).",
                    obj=member,
                    id=f"{cls.TAG}.E003",
                )
            )
            return
        if field.is_relation:
            from .models import Role

            if field.related_model is not Role or field.target_field.name != "code":
                messages.append(
                    checks.Error(
                        f"{member.__name__}.role must be a CharField or policytree.models.RoleField().",
                        hint="Use role = RoleField() for roles managed in the admin.",
                        obj=member,
                        id=f"{cls.TAG}.E005",
                    )
                )

    @classmethod
    def _check_manager(cls, organization, messages):
        from .models import OrganizationQuerySet

        queryset = organization._default_manager.get_queryset()
        if not isinstance(queryset, OrganizationQuerySet):
            messages.append(
                checks.Warning(
                    f"{organization.__name__}'s default manager doesn't use OrganizationQuerySet, "
                    "so bulk updates can change parents without fixing paths.",
                    hint="Base your manager on OrganizationQuerySet.as_manager().",
                    obj=organization,
                    id=f"{cls.TAG}.W003",
                )
            )

    @classmethod
    def _check_role_includes(cls, messages):
        includes = PolicyTreeSettings.role_includes()
        if not includes:
            return
        cycle = RoleTree.find_cycle(includes)
        if cycle:
            messages.append(
                checks.Error(
                    f"POLICYTREE_ROLE_INCLUDES has a cycle: {' -> '.join(cycle)}.",
                    id=f"{cls.TAG}.E004",
                )
            )
        # Only roles declared in code: checks run before the database may exist.
        known = {value for value, _ in Roles.static_choices()}
        if known:
            named = set(includes) | {r for roles in includes.values() for r in roles}
            unknown = sorted(named - known)
            if unknown:
                messages.append(
                    checks.Warning(
                        f"POLICYTREE_ROLE_INCLUDES names unknown roles: {', '.join(unknown)}.",
                        id=f"{cls.TAG}.W002",
                    )
                )

    @classmethod
    def _check_cache(cls, messages):
        if settings.DEBUG:
            return
        alias = PolicyTreeSettings.cache_alias()
        backend = settings.CACHES.get(alias, {}).get("BACKEND", "")
        if backend in cls.LOCAL_CACHES:
            messages.append(
                checks.Warning(
                    f"The '{alias}' cache ({backend.rsplit('.', 1)[-1]}) is separate in each process, "
                    "so a policy change may not reach every server or worker.",
                    hint="Use Redis or Memcached, or point POLICYTREE_CACHE_ALIAS at a shared cache.",
                    id=f"{cls.TAG}.W001",
                )
            )
