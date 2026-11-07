"""
Django REST Framework integration. Requires `pip install django-policy-tree[drf]`.

    class ProjectSerializer(PolicyFieldsMixin, serializers.ModelSerializer): ...

    class ProjectViewSet(PolicyQuerysetMixin, viewsets.ModelViewSet):
        queryset = Project.objects.all()
        serializer_class = ProjectSerializer
        permission_classes = [PolicyPermission]
"""
from collections.abc import Mapping
from contextlib import ExitStack
from contextvars import ContextVar

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, models, router, transaction
from django.db.models.signals import post_save, pre_delete
from rest_framework import exceptions, permissions, serializers
from rest_framework.response import Response

from .engine import Action, PolicyEngine
from .registry import FieldPaths
from .targets import CreateTarget

class DRFActionMap:
    """
    Turns a view's current action into policy requirements: (policy action,
    model), where model None means the viewset's own model.

    `policy_actions` maps custom @action names to one of:

        "update"                                   own model, "update"
        ("create", Invoice)                        Invoice, "create"
        ("list", "billing.Invoice")                model by label
        [("update", None), ("create", Invoice)]    all of them required

    A detail action (detail=True) always also needs the object to be visible,
    unless it already requires list, detail, update or delete on it.
    """

    DEFAULTS = {
        "list": Action.LIST,
        "retrieve": Action.DETAIL,
        "create": Action.CREATE,
        "update": Action.UPDATE,
        "partial_update": Action.UPDATE,
        "destroy": Action.DELETE,
    }

    # ---- parsing

    @classmethod
    def _model(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return apps.get_model(value)
            except (LookupError, ValueError):
                raise ImproperlyConfigured(f"policy_actions: unknown model '{value}'.")
        return value

    @classmethod
    def _action(cls, value):
        if value not in Action.values:
            raise ImproperlyConfigured(
                f"policy_actions: '{value}' is not one of {', '.join(Action.values)}."
            )
        return value

    @classmethod
    def normalize(cls, value):
        """Any supported `policy_actions` value -> [(action, model or None), ...]."""
        if isinstance(value, str):
            return [(cls._action(value), None)]
        if isinstance(value, tuple) and len(value) == 2:
            return [(cls._action(value[0]), cls._model(value[1]))]
        if isinstance(value, list) and value:
            result = []
            for item in value:
                if isinstance(item, list):
                    raise ImproperlyConfigured("policy_actions: lists can't be nested.")
                result.extend(cls.normalize(item))
            return result
        raise ImproperlyConfigured(
            "policy_actions values must be an action, an (action, model) tuple, or a list of those."
        )

    # ---- per view

    @staticmethod
    def main_model(view):
        queryset = getattr(view, "queryset", None)
        if queryset is not None:
            return queryset.model
        serializer_class = getattr(view, "serializer_class", None)
        model = getattr(getattr(serializer_class, "Meta", None), "model", None)
        if model is None:
            raise ImproperlyConfigured(
                f"{type(view).__name__} needs a `queryset` (or a model serializer) for policy checks."
            )
        return model

    @classmethod
    def requirements(cls, view):
        """Everything the current action needs. Empty means unmapped (superusers only)."""
        if view is None:
            return []
        name = getattr(view, "action", None)
        custom = getattr(view, "policy_actions", None) or {}
        main = cls.main_model(view)

        if name in custom:
            reqs = cls.normalize(custom[name])
        elif name in cls.DEFAULTS:
            reqs = [(cls.DEFAULTS[name], None)]
        else:
            return []

        # The viewset's own model written explicitly counts as "own model".
        reqs = [(action, None if model is main else model) for action, model in reqs]

        # A detail action loads the object, so it must be visible.
        if getattr(view, "detail", False) and not any(
            model is None and action != Action.CREATE for action, model in reqs
        ):
            reqs.insert(0, (Action.DETAIL, None))
        return reqs

    @classmethod
    def resolve(cls, view):
        """The policy action that decides which of the viewset's own records are reachable."""
        for action, model in cls.requirements(view):
            if model is None:
                return action
        return None

    @classmethod
    def action_for(cls, view, model):
        """The policy action the current view requires for `model`, if any."""
        if view is None:
            return None
        main = cls.main_model(view)
        for action, req_model in cls.requirements(view):
            if (req_model or main) is model:
                return action
        return None


class WriteTracker:
    """
    While a PolicyQuerysetMixin request runs, records every save of the
    viewset's models; each saved record must be checked after its last save
    (serializer saves, policy_created, policy_changed). Deletes of those models,
    cascades included, are checked as they happen.
    """

    _current = ContextVar("policytree_write_tracker", default=None)

    def __init__(self, engine, tracked):
        self.engine = engine
        self.models = {m._meta.concrete_model for m in tracked}
        self.written = set()
        self.checked = set()
        self.deletable = set()  # records the user may delete (a soft delete saves them)

    @staticmethod
    def _key(instance):
        return type(instance)._meta.concrete_model, instance.pk

    @classmethod
    def current(cls):
        return cls._current.get()

    @classmethod
    def mark(cls, instance):
        """This record (as it is now) has been checked."""
        tracker = cls.current()
        if tracker is not None and getattr(instance, "pk", None) is not None:
            tracker.checked.add(cls._key(instance))

    @classmethod
    def allow_delete(cls, instance):
        tracker = cls.current()
        if tracker is not None and getattr(instance, "pk", None) is not None:
            key = cls._key(instance)
            tracker.deletable.add(key)
            tracker.checked.add(key)

    @property
    def unchecked(self):
        """Saved records nobody checked. A deletable record saved (soft delete) passes
        only if the user may still delete it as stored now, i.e. it wasn't moved."""
        left = set()
        for model, pk in self.written - self.checked:
            if (model, pk) in self.deletable:
                stored = model._base_manager.filter(pk=pk).first()
                if stored is not None and self.engine.can(Action.DELETE, stored):
                    continue
            left.add((model, pk))
        return left

    @classmethod
    def on_save(cls, sender, instance, raw=False, **kwargs):
        tracker = cls.current()
        if tracker is None or raw or sender._meta.concrete_model not in tracker.models:
            return
        key = cls._key(instance)
        tracker.written.add(key)
        tracker.checked.discard(key)  # any earlier check was of an older state

    @classmethod
    def on_delete(cls, sender, instance, **kwargs):
        tracker = cls.current()
        if tracker is None or sender._meta.concrete_model not in tracker.models:
            return
        if cls._key(instance) in tracker.checked:
            return
        if not tracker.engine.can(Action.DELETE, instance):
            raise exceptions.PermissionDenied(f"You may not delete this {sender.__name__}.")

    @classmethod
    def connect(cls):
        post_save.connect(cls.on_save, dispatch_uid="policytree_track_save")
        pre_delete.connect(cls.on_delete, dispatch_uid="policytree_track_delete")


WriteTracker.connect()


class PolicyPermission(permissions.BasePermission):
    """
    Checks the Policy table for the current action, including any second
    models named in the view's `policy_actions`. Unmapped custom actions
    are superuser-only.

    Some answers can't be settled before the action runs (a conditional rule,
    a per-record scope like "own", or which second-model records an action
    touches). With PolicyQuerysetMixin those are recorded as owed and must be
    confirmed by the action, or the request is refused and rolled back.
    Without the mixin nothing could enforce that, so they are refused here.
    """

    @staticmethod
    def _builtin(view):
        return getattr(view, "action", None) in ("update", "partial_update")

    @staticmethod
    def _enforced(view):
        # The mixin's lifecycle must actually be running (an initial() override
        # that skips super() would otherwise drop enforcement silently).
        return isinstance(view, PolicyQuerysetMixin) and getattr(view, "_policy_initialised", False)

    def _owe(self, engine, view, model, action):
        if not self._enforced(view):
            return False
        engine.owe(model, action)
        return True

    def has_permission(self, request, view):
        engine = PolicyEngine.for_request(request)
        if not engine.is_authenticated:
            return False
        if engine.is_superuser:
            return True

        reqs = DRFActionMap.requirements(view)
        if not reqs:
            return False

        main = DRFActionMap.main_model(view)
        on_object = getattr(view, "detail", False)
        for action, model in reqs:
            target = model or main
            # Creates without a main object: the place comes from the request.
            if action == Action.CREATE and (model is None or not on_object):
                allowed, provisional = engine.assess(target, Action.CREATE, self._target(request, view, target))
                if not allowed:
                    return False
                # The pre-check only covers the place named in the request: the saved
                # record must be checked too (serializer saves and policy_created do).
                if not self._owe(engine, view, target, Action.CREATE) and provisional:
                    return False
            elif not engine.has_any(target, action):
                return False
            elif model is None and action == Action.UPDATE and not self._builtin(view):
                # A custom action changing the viewset's own rows: the results must be checked.
                if not self._owe(engine, view, target, action):
                    return False
            elif model is not None and not on_object:
                # Which records the action touches is unknown: it must check them.
                if not self._owe(engine, view, target, action):
                    return False
        return True

    def has_object_permission(self, request, view, obj):
        engine = PolicyEngine.for_request(request)
        if engine.is_superuser:
            return True
        reqs = DRFActionMap.requirements(view)
        if not reqs:
            return False

        org = engine.org_of(obj)
        for action, model in reqs:
            if model is None:
                if action != Action.CREATE and not engine.can(action, obj):
                    return False
                if action == Action.DELETE:
                    WriteTracker.allow_delete(obj)
                continue
            # Second model: the user must reach the main object's organization...
            allowed, provisional = engine.assess(model, action, org)
            if not allowed:
                return False
            # ...and the records the action touches must be checked by the action.
            if not self._owe(engine, view, model, action) and (action != Action.CREATE or provisional):
                return False
        return True

    @staticmethod
    def _target(request, view, model):
        getter = getattr(view, "get_policy_target", None)
        if getter is not None:
            return getter(model)
        return PolicyQuerysetMixin.target_from_request(request, model)


class PolicySaveCheck:
    """Re-checks saved records inside the saving transaction (shared by the serializers)."""

    @staticmethod
    def engine_for(serializer):
        request = serializer.context.get("request")
        if request is None:
            return None
        engine = PolicyEngine.for_request(request)
        return None if engine.is_superuser else engine

    @staticmethod
    def refuse(action):
        raise exceptions.PermissionDenied(f"The result would be outside what you're allowed to {action}.")

    @staticmethod
    def submitted(serializer, data):
        """Keys of `data` the client actually sent (not hidden server-side defaults)."""
        fields = getattr(serializer, "fields", {})
        hidden = {f.source for f in fields.values() if isinstance(f, serializers.HiddenField)}
        return {key for key in data if key not in hidden}

    @classmethod
    def created(cls, engine, instance, submitted):
        if not isinstance(instance, models.Model) or instance.pk is None:
            cls.refuse(Action.CREATE)
        if not engine.can_create(instance, submitted):
            cls.refuse(Action.CREATE)
        engine.confirm(type(instance), Action.CREATE)
        WriteTracker.mark(instance)

    @classmethod
    def changed(cls, engine, instance, before, submitted=()):
        if not isinstance(instance, models.Model) or not engine.can_change(instance, before):
            cls.refuse(Action.UPDATE)
        if submitted:
            writable = engine.fields(type(instance), Action.UPDATE, "write", obj=instance)
            if writable is not None and not set(submitted) <= writable:
                cls.refuse(Action.UPDATE)
        engine.confirm(type(instance), Action.UPDATE)
        WriteTracker.mark(instance)


class PolicyQuerysetMixin:
    """
    Viewset mixin: scopes querysets (including overridden get_queryset()),
    works out where creates go, re-checks saved results, runs each request in
    a transaction, and refuses (rolling back) when a pre-check owed by
    PolicyPermission was never confirmed by the action.
    """

    policy_actions = {}  # custom @action name -> requirements (see DRFActionMap)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Whatever get_queryset the class ends up with (its own, or one from
        # another mixin earlier in the MRO) returns scoped rows.
        own = getattr(cls, "get_queryset", None)
        if own is not None and not getattr(own, "_policytree_scoped", False):
            def get_queryset(self, *args, _own=own, **kw):
                qs = _own(self, *args, **kw)
                if qs is getattr(self, "_policy_scoped", None):
                    return qs
                self._policy_scoped = self._policy_scope(qs)
                return self._policy_scoped

            get_queryset._policytree_scoped = True
            get_queryset.__doc__ = own.__doc__
            cls.get_queryset = get_queryset

    @property
    def policy(self):
        return PolicyEngine.for_request(self.request)

    # ---- where creates go

    @staticmethod
    def target_from_request(request, model):
        field = CreateTarget.field(model)
        if not isinstance(request.data, Mapping):
            raise exceptions.ValidationError({field: ["Send a single object."]})
        org = CreateTarget.resolve(model, request.data.get(field))
        if org is None:
            raise exceptions.ValidationError({field: ["A valid value is required."]})
        return org

    def get_policy_target(self, model):
        """
        The organization a new `model` record will belong to. By default it is
        read from the request through the model's organization path; override
        it when the place is decided on the server (e.g. from request.user).
        """
        return self.target_from_request(self.request, model)

    # ---- querysets

    def _policy_actions_on_rows(self):
        """Every own-model action the current request needs on existing rows."""
        actions = []
        for action, model in DRFActionMap.requirements(self):
            if model is None and action != Action.CREATE and action not in actions:
                actions.append(action)
        return actions

    def _policy_scope(self, qs):
        if getattr(self, "request", None) is None:
            return qs.none()  # e.g. schema generation: no user, no rows
        if self.policy.is_superuser:
            return qs
        actions = self._policy_actions_on_rows()
        if not actions:
            return qs.none()  # creating (or an unknown action) needs no existing rows
        query = qs.query
        if query.is_sliced or query.combinator:
            raise ImproperlyConfigured(
                f"{type(self).__name__}.get_queryset() must return an unsliced, uncombined queryset."
            )
        if qs._fields is not None:  # values()/values_list(): scope through the primary keys
            for action in actions:
                qs = qs.filter(pk__in=self.policy.filter(qs.model._default_manager.all(), action).values("pk"))
            return qs
        joins = FieldPaths(qs.model).related_joins()
        qs = qs.select_related(*joins) if joins else qs
        for action in actions:  # rows must allow every action, e.g. list and delete
            qs = self.policy.filter(qs, action)
        return qs

    def get_queryset(self):
        self._policy_scoped = self._policy_scope(super().get_queryset())
        return self._policy_scoped

    get_queryset._policytree_scoped = True

    def filter_queryset(self, queryset):
        # Safety net: anything not produced by our get_queryset() is scoped here.
        if queryset is not getattr(self, "_policy_scoped", None):
            queryset = self._policy_scope(queryset)
        return super().filter_queryset(queryset)

    # ---- helpers for custom actions (each confirms what it checked)

    def policy_queryset(self, queryset, action=None):
        """
        Scope a queryset of any model; the action defaults to the one mapped for
        that model in `policy_actions`, otherwise "list". Confirms that model
        and action: use the returned queryset for everything the action reads.
        """
        action = action or DRFActionMap.action_for(self, queryset.model) or Action.LIST
        scoped = self.policy.filter(queryset, action)
        if action in (Action.LIST, Action.DETAIL):
            # Reads only: changes are confirmed by the helpers that make them.
            self.policy.confirm(queryset.model, action)
        return scoped

    def policy_prefetch(self, lookup, queryset, action=Action.LIST):
        """A scoped Prefetch for nested serializers, checked without extra queries per row."""
        joins = FieldPaths(queryset.model).related_joins()
        queryset = queryset.select_related(*joins) if joins else queryset
        return models.Prefetch(lookup, queryset=self.policy_queryset(queryset, action))

    def policy_require(self, action, obj):
        """
        Raise 403 unless the user may `action` this object (any model). Confirms
        reads (list, detail) of that object. Changes are confirmed by the helpers
        that make them: policy_delete, policy_update, policy_created, policy_changed.
        """
        if not self.policy.can(action, obj):
            raise exceptions.PermissionDenied(f"You may not {action} this {type(obj).__name__}.")
        if action in (Action.LIST, Action.DETAIL):
            self.policy.confirm(type(obj), action)

    def policy_delete(self, queryset):
        """Delete exactly the rows of `queryset` the user may delete; confirms delete. Returns the count."""
        allowed = self.policy.filter(queryset, Action.DELETE)
        tracker = WriteTracker.current()
        if tracker is not None:
            model = queryset.model._meta.concrete_model
            tracker.checked.update((model, pk) for pk in allowed.values_list("pk", flat=True))
        count = self.policy.bulk_delete(queryset)
        self.policy.confirm(queryset.model, Action.DELETE)
        return count

    def policy_skip(self, model, action):
        """
        Waive an owed check for a branch that touched no records (e.g. "nothing
        to do"). Writes the request did make are still checked one by one.
        """
        self.policy.confirm(model, action)

    def policy_update(self, queryset, **values):
        """queryset.update(**values) limited to what the user may update and write; confirms update."""
        count = self.policy.bulk_update(queryset, **values)
        self.policy.confirm(queryset.model, Action.UPDATE)
        return count

    def policy_created(self, obj, submitted=()):
        """Re-check a record your action created without a serializer (raises 403, rolls back)."""
        PolicySaveCheck.created(self.policy, obj, submitted)

    def policy_changed(self, obj, before):
        """Re-check a record your action changed without a serializer; `before` = policy.place_of(obj) earlier."""
        PolicySaveCheck.changed(self.policy, obj, before)

    # ---- every request: one transaction; refused or unconfirmed requests keep nothing

    def initial(self, request, *args, **kwargs):
        if getattr(self, "_policy_atomic", None) is None:
            raise ImproperlyConfigured(
                f"{type(self).__name__}.dispatch() must call super(): PolicyQuerysetMixin needs it."
            )
        if not getattr(type(self).get_queryset, "_policytree_scoped", False):
            raise ImproperlyConfigured(
                f"{type(self).__name__}.get_queryset was replaced after the class was created; define it in the class."
            )
        # A fresh engine per view call: owed checks never leak between nested views.
        setattr(request, PolicyEngine.REQUEST_ATTR, None)
        self._policy_failed = False
        self._policy_initialised = True
        engine = PolicyEngine.for_request(request)
        if engine.is_authenticated and not engine.is_superuser:
            self._policy_tracker = WriteTracker(engine, self._policy_tracked_models())
            self._policy_token = WriteTracker._current.set(self._policy_tracker)
        super().initial(request, *args, **kwargs)

    def _policy_tracked_models(self):
        tracked = set()
        queryset = getattr(self, "queryset", None)
        if queryset is not None:
            tracked.add(queryset.model)
        else:
            try:
                tracked.add(DRFActionMap.main_model(self))
            except ImproperlyConfigured:
                pass
        for value in (self.policy_actions or {}).values():
            tracked.update(m for _, m in DRFActionMap.normalize(value) if m is not None)
        return tracked

    def dispatch(self, request, *args, **kwargs):
        if getattr(self, "view_is_async", False):
            raise ImproperlyConfigured("PolicyQuerysetMixin doesn't support async views yet.")
        self._policy_finalized = False
        self._policy_tracker = None
        self._policy_token = None
        with ExitStack() as stack:
            self._policy_atomic = []
            for alias in self._policy_databases_for(request):
                stack.enter_context(transaction.atomic(using=alias))
                self._policy_atomic.append(alias)
            try:
                response = super().dispatch(request, *args, **kwargs)
            finally:
                if self._policy_token is not None:
                    WriteTracker._current.reset(self._policy_token)
            if not self._policy_finalized:
                self._policy_rollback()
                raise ImproperlyConfigured(
                    f"{type(self).__name__}.finalize_response() must call super(): PolicyQuerysetMixin needs it."
                )
            return response

    def _policy_databases_for(self, request):
        # The action isn't known before DRF resolves it; cover the default database
        # and every database any of this viewset's models write to.
        models_seen = set()
        queryset = getattr(self, "queryset", None)
        if queryset is not None:
            models_seen.add(queryset.model)
        for value in (self.policy_actions or {}).values():
            try:
                models_seen.update(m for _, m in DRFActionMap.normalize(value) if m is not None)
            except ImproperlyConfigured:
                pass
        return sorted({router.db_for_write(m) for m in models_seen} | {DEFAULT_DB_ALIAS})

    def handle_exception(self, exc):
        self._policy_failed = True  # whatever the handler answers, keep nothing
        return super().handle_exception(exc)

    def _policy_rollback(self):
        for alias in getattr(self, "_policy_atomic", []):
            if transaction.get_connection(alias).in_atomic_block:
                transaction.set_rollback(True, using=alias)

    def finalize_response(self, request, response, *args, **kwargs):
        status = getattr(response, "status_code", 500)
        if status >= 400 or getattr(self, "_policy_failed", False):
            self._policy_rollback()
        engine = getattr(request, PolicyEngine.REQUEST_ATTR, None)
        tracker = getattr(self, "_policy_tracker", None)
        problems = []
        if engine is not None and engine.owed:
            problems += sorted(f"{action} {model.__name__}" for model, action in engine.owed)
        if tracker is not None and tracker.unchecked:
            problems += sorted({f"saved {model.__name__}" for model, _ in tracker.unchecked})
        if problems and status < 400:
            # The action used records it never checked: undo everything and refuse.
            self._policy_rollback()
            response = Response({"detail": f"Not checked against the records involved: {', '.join(problems)}."},
                                status=403)
        self._policy_finalized = True
        return super().finalize_response(request, response, *args, **kwargs)

    # ---- re-check saved results for serializers without PolicyFieldsMixin

    def get_serializer(self, *args, **kwargs):
        serializer = super().get_serializer(*args, **kwargs)
        self._policy_serializer = serializer
        instance = args[0] if args else kwargs.get("instance")
        if isinstance(instance, models.Model) and not self.policy.is_superuser:
            self._policy_before = self.policy.place_of(instance)
        return serializer

    def _policy_verify(self, action):
        """Re-check the saved record, even if the serializer did (later code may have changed it)."""
        if self.policy.is_superuser:
            return
        serializer = getattr(self, "_policy_serializer", None)
        instance = getattr(serializer, "instance", None)
        if not isinstance(instance, models.Model):
            return  # many=True: PolicyListSerializer checks each row
        data = getattr(serializer, "validated_data", None) or {}
        if action == Action.CREATE:
            PolicySaveCheck.created(self.policy, instance, PolicySaveCheck.submitted(serializer, data))
        else:
            PolicySaveCheck.changed(self.policy, instance, getattr(self, "_policy_before", None))

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        self._policy_verify(Action.CREATE)
        return response

    def update(self, request, *args, **kwargs):
        response = super().update(request, *args, **kwargs)
        self._policy_verify(Action.UPDATE)
        return response


class PolicyListSerializer(serializers.ListSerializer):
    """
    Used automatically by PolicyFieldsMixin with many=True:
      - nested lists only show related rows in scope (querysets are filtered in
        the database; prefetched rows and plain lists are checked in memory);
      - per-record field rules are worked out for all rows at once;
      - bulk creates are re-checked like single ones.
    """

    def to_representation(self, data):
        request = self.context.get("request")
        child = self.child
        model = getattr(getattr(child, "Meta", None), "model", None)
        engine = PolicyEngine.for_request(request) if request is not None else None
        if engine is None or engine.is_superuser or model is None:
            return super().to_representation(data)

        if isinstance(data, models.Manager):
            data = data.all()
        nested = self.parent is not None
        if nested:
            if isinstance(data, models.QuerySet) and data._result_cache is None:
                data = engine.filter(data, Action.LIST)
            else:
                items = list(data)
                own = engine.filter_objects([obj for obj in items if isinstance(obj, model)], Action.LIST)
                keep = {id(obj) for obj in own}
                data = [obj for obj in items if not isinstance(obj, model) or id(obj) in keep]

        items = list(data)
        if isinstance(child, PolicyFieldsMixin):
            if nested:
                child._policy_read_action = Action.LIST  # rows were picked by "list": fields too
            read_action = child._policy_read()
            # Uniform field rules are free only when the rows were picked by the same action.
            try:
                picked_by = Action.LIST if nested else DRFActionMap.resolve(self.context.get("view"))
            except ImproperlyConfigured:
                picked_by = None
            child._policy_fields = engine.fields_map(
                model, read_action, "read", [obj for obj in items if isinstance(obj, model)],
                prefiltered=(picked_by == read_action),
            )
        return super().to_representation(items)

    def save(self, **kwargs):
        engine = PolicySaveCheck.engine_for(self)
        if engine is None:
            return super().save(**kwargs)
        items = [PolicySaveCheck.submitted(self.child, item) for item in self.validated_data]
        if self.instance is None:
            with transaction.atomic():
                instances = list(super().save(**kwargs))
                if len(instances) != len(items):
                    PolicySaveCheck.refuse(Action.CREATE)
                for instance, fields in zip(instances, items):
                    PolicySaveCheck.created(engine, instance, fields)
            return instances

        before = {obj.pk: engine.place_of(obj) for obj in self.instance if isinstance(obj, models.Model)}
        sent = set().union(*items) if items else set()
        model = self.child.Meta.model
        database = router.db_for_write(model)
        with transaction.atomic(using=database):
            instances = list(super().save(**kwargs))
            # Re-check every row we were given, as stored now, whatever update() returned.
            stored = model._base_manager.using(database).in_bulk(list(before))
            if set(stored) != set(before):
                PolicySaveCheck.refuse(Action.UPDATE)
            for pk, place in before.items():
                PolicySaveCheck.changed(engine, stored[pk], place, sent)
            for instance in instances:
                if getattr(instance, "pk", None) not in before:
                    PolicySaveCheck.created(engine, instance, sent)
        return instances


class PolicyFieldsMixin:
    """
    Serializer mixin:
      - drops fields the user can't read and locks fields they can't write;
      - applies field rules per record, so a row only shows what the rules
        reaching that row allow;
      - when nested with many=True, shows only related rows in scope;
      - re-checks every save (also in custom actions) inside a transaction:
        place, conditions, moves, and fields submitted on create.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        meta = cls.__dict__.get("Meta")
        if meta is not None and not hasattr(meta, "list_serializer_class"):
            meta.list_serializer_class = PolicyListSerializer

    def _policy_context(self):
        engine = PolicySaveCheck.engine_for(self)
        if engine is None:
            return None, None, None
        view = self.context.get("view")
        model = self.Meta.model
        try:
            action = DRFActionMap.action_for(view, model) or DRFActionMap.resolve(view)
        except ImproperlyConfigured:
            action = None  # a view without a model: judge by this serializer's model
        return engine, model, action or self._policy_default_action()

    def _policy_default_action(self):
        """Without a mapped view action: writing with data is a create or update, otherwise a read."""
        root = self.parent if isinstance(self.parent, serializers.ListSerializer) else self
        if hasattr(root, "initial_data"):
            return Action.CREATE if root.instance is None else Action.UPDATE
        return Action.DETAIL

    def _policy_read(self):
        """The action whose readable fields apply to this serializer's rows."""
        forced = getattr(self, "_policy_read_action", None)
        if forced:
            return forced
        _, _, action = self._policy_context()
        return Action.LIST if action == Action.LIST else Action.DETAIL

    def get_fields(self):
        fields = super().get_fields()
        engine, model, action = self._policy_context()
        if engine is None:
            return fields

        instance = self.instance if isinstance(self.instance, models.Model) else None
        readable = engine.fields(model, self._policy_read(), "read")
        writable = (
            engine.fields(model, action, "write", obj=instance)
            if action in (Action.CREATE, Action.UPDATE)
            else set()
        )

        for name, field in list(fields.items()):
            can_read = readable is None or name in readable
            can_write = writable is None or name in writable
            if not can_read and not can_write:
                del fields[name]
            elif not can_write:
                field.read_only = True
                field.required = False
            elif not can_read:
                field.write_only = True
        return fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        engine, model, _ = self._policy_context()
        if engine is None or not isinstance(instance, model):
            return data
        known = getattr(self, "_policy_fields", None) or {}
        if instance.pk in known:
            readable = known[instance.pk]
        else:
            readable = engine.fields(model, self._policy_read(), "read", obj=instance)
        if readable is None:
            return data
        return type(data)((key, value) for key, value in data.items() if key in readable)

    def save(self, **kwargs):
        engine = PolicySaveCheck.engine_for(self)
        if engine is None:
            return super().save(**kwargs)
        creating = self.instance is None
        before = None if creating else engine.place_of(self.instance)
        submitted = PolicySaveCheck.submitted(self, self.validated_data)
        with transaction.atomic():
            instance = super().save(**kwargs)
            if creating:
                PolicySaveCheck.created(engine, instance, submitted)
            else:
                PolicySaveCheck.changed(engine, instance, before)
        return instance
