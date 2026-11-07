"""
Plain Django class-based views.

    class ProjectListView(PolicyViewMixin, ListView):
        model = Project

    class ProjectUpdateView(PolicyViewMixin, UpdateView):
        model = Project
        fields = ["name", "budget", "organization"]

The action is picked from the view type (ListView -> list, DetailView -> detail,
CreateView -> create, UpdateView -> update, DeleteView -> delete), or set
`policy_action` yourself. Field paths come from the model.
"""
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import transaction
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .engine import Action, PolicyEngine
from .registry import FieldPaths
from .targets import CreateTarget


class PolicyViewMixin(LoginRequiredMixin):
    policy_action = None  # set to override the automatic choice

    VIEW_ACTIONS = (
        (CreateView, Action.CREATE),
        (UpdateView, Action.UPDATE),
        (DeleteView, Action.DELETE),
        (DetailView, Action.DETAIL),
        (ListView, Action.LIST),
    )

    # ---- basics

    @property
    def policy(self):
        return PolicyEngine.for_request(self.request)

    def get_policy_action(self):
        if self.policy_action:
            return self.policy_action
        for view_class, action in self.VIEW_ACTIONS:
            if isinstance(self, view_class):
                return action
        raise PermissionDenied("This view has no policy action; set `policy_action`.")

    def get_policy_model(self):
        model = getattr(self, "model", None)
        if model is None and getattr(self, "queryset", None) is not None:
            model = self.queryset.model
        if model is None:
            raise ImproperlyConfigured(f"{type(self).__name__} needs a `model` or `queryset` for policy checks.")
        return model

    def get_policy_target(self, form):
        """
        The organization a new record will belong to: by default read from the
        form through the model's organization path. Override it when the place
        is decided on the server.
        """
        model = self.get_policy_model()
        field = CreateTarget.field(model)
        value = form.cleaned_data.get(field) if field in form.cleaned_data else getattr(form.instance, field, None)
        return CreateTarget.resolve(model, value)

    @staticmethod
    def policy_submitted(form):
        """Fields the user sent: every enabled field present in the posted data or files."""
        sent = set()
        for name, field in form.fields.items():
            key = form.add_prefix(name)
            if not field.disabled and (key in form.data or key in form.files):
                sent.add(name)
        return sent

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            if not self.policy.has_any(self.get_policy_model(), self.get_policy_action()):
                raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    # ---- objects and lists

    def get_queryset(self):
        qs = super().get_queryset()
        action = self.get_policy_action()
        if action == Action.CREATE or self.policy.is_superuser:
            return qs
        joins = FieldPaths(qs.model).related_joins()
        return self.policy.filter(qs.select_related(*joins) if joins else qs, action)

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not self.policy.can(self.get_policy_action(), obj):
            raise PermissionDenied
        # Where the record is now, to recognise a move when it is saved.
        self._policy_before = self.policy.place_of(obj)
        return obj

    # ---- forms

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if not hasattr(form, "fields"):
            return form
        action = self.get_policy_action()
        if action not in (Action.CREATE, Action.UPDATE):
            return form

        model = self.get_policy_model()
        instance = getattr(self, "object", None)
        readable = self.policy.fields(model, Action.DETAIL, "read", obj=instance)
        writable = self.policy.fields(model, action, "write", obj=instance)
        for name in list(form.fields):
            can_read = readable is None or name in readable
            can_write = writable is None or name in writable
            if not can_read and not can_write:
                del form.fields[name]
            elif not can_write:
                form.fields[name].disabled = True
            elif not can_read:
                # May set it, may not see it: don't show the current value.
                form.initial.pop(name, None)
                form.fields[name].initial = None
        return form

    def form_valid(self, form):
        action = self.get_policy_action()
        if action == Action.CREATE:
            org = self.get_policy_target(form)
            # Provisional answers (conditions, "own") are settled by can_create() after saving.
            allowed, _ = self.policy.assess(self.get_policy_model(), Action.CREATE, org)
            if not allowed:
                raise PermissionDenied
        if action not in (Action.CREATE, Action.UPDATE):
            return super().form_valid(form)

        # Re-check the saved result, so place, conditions and fields apply to the final state.
        with transaction.atomic():
            response = super().form_valid(form)
            if action == Action.CREATE:
                ok = self.policy.can_create(self.object, submitted=self.policy_submitted(form))
            else:
                ok = self.policy.can_change(self.object, getattr(self, "_policy_before", None))
            if not ok:
                raise PermissionDenied
        return response
