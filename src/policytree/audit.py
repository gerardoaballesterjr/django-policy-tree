"""Records every policy and role change, and keeps the caches fresh."""
from django.db.models.signals import m2m_changed, post_delete, post_save

from .engine import PolicyCache
from .models import Policy, PolicyChange, Role
from .roles import Roles


class PolicyAudit:
    """
    Signal handlers for Policy and Role. To say who made a change, set an
    actor on the instance before saving or deleting it:

        PolicyAudit.set_actor(policy, request.user, source="admin")

    With defer=True, saves of a role are not logged automatically; call
    PolicyAudit.record_role(role, kind) once its `includes` are saved, so one
    change produces one log row.
    """

    ACTOR_ATTR = "_policytree_actor"
    DEFER_ATTR = "_policytree_defer"

    # ---- actor

    @classmethod
    def set_actor(cls, obj, user=None, source="code", defer=False):
        setattr(obj, cls.ACTOR_ATTR, (user, source))
        setattr(obj, cls.DEFER_ATTR, defer)
        return obj

    @classmethod
    def _actor(cls, obj):
        user, source = getattr(obj, cls.ACTOR_ATTR, (None, "code"))
        if user is not None and not getattr(user, "pk", None):
            user = None
        return user, source

    @classmethod
    def _deferred(cls, obj):
        return getattr(obj, cls.DEFER_ATTR, False)

    @classmethod
    def _record(cls, target, obj, kind, data=None):
        user, source = cls._actor(obj)
        PolicyChange.objects.create(
            target=target,
            policy_code=obj.code,
            kind=kind,
            data=data if data is not None else obj.snapshot(),
            source=source,
            changed_by=user,
        )

    # ---- policies

    @classmethod
    def on_save(cls, sender, instance, created, **kwargs):
        PolicyCache.clear()
        kind = PolicyChange.Kind.CREATED if created else PolicyChange.Kind.UPDATED
        cls._record(PolicyChange.Target.POLICY, instance, kind)

    @classmethod
    def on_delete(cls, sender, instance, **kwargs):
        PolicyCache.clear()
        cls._record(PolicyChange.Target.POLICY, instance, PolicyChange.Kind.DELETED)

    # ---- roles

    @classmethod
    def record_role(cls, role, kind):
        cls._record(PolicyChange.Target.ROLE, role, kind)
        setattr(role, cls.DEFER_ATTR, False)

    @classmethod
    def on_role_save(cls, sender, instance, created, **kwargs):
        Roles.clear()
        if not cls._deferred(instance):
            kind = PolicyChange.Kind.CREATED if created else PolicyChange.Kind.UPDATED
            cls._record(PolicyChange.Target.ROLE, instance, kind)

    @classmethod
    def on_role_delete(cls, sender, instance, **kwargs):
        Roles.clear()
        data = {**instance.snapshot(), "includes": getattr(instance, "_policytree_includes", [])}
        cls._record(PolicyChange.Target.ROLE, instance, PolicyChange.Kind.DELETED, data)

    @classmethod
    def on_role_pre_delete_snapshot(cls, sender, instance, **kwargs):
        # The includes rows are gone by post_delete, so remember them first.
        instance._policytree_includes = sorted(instance.includes.values_list("code", flat=True))

    @classmethod
    def on_role_includes(cls, sender, instance, action, reverse, **kwargs):
        if action not in ("post_add", "post_remove", "post_clear"):
            return
        Roles.clear()
        if not reverse and not cls._deferred(instance):
            cls._record(PolicyChange.Target.ROLE, instance, PolicyChange.Kind.UPDATED)

    # ---- wiring

    @classmethod
    def connect(cls):
        from django.db.models.signals import pre_delete

        post_save.connect(cls.on_save, sender=Policy, dispatch_uid="policytree_policy_save")
        post_delete.connect(cls.on_delete, sender=Policy, dispatch_uid="policytree_policy_delete")
        post_save.connect(cls.on_role_save, sender=Role, dispatch_uid="policytree_role_save")
        pre_delete.connect(cls.on_role_pre_delete_snapshot, sender=Role, dispatch_uid="policytree_role_pre_delete")
        post_delete.connect(cls.on_role_delete, sender=Role, dispatch_uid="policytree_role_delete")
        m2m_changed.connect(
            cls.on_role_includes, sender=Role.includes.through, dispatch_uid="policytree_role_includes"
        )
