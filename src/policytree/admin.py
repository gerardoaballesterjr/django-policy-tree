from django import forms
from django.contrib import admin

from .audit import PolicyAudit
from .models import Policy, PolicyChange, Role
from .registry import ConditionRegistry, ScopeRegistry
from .roles import Roles, RoleTree


class PolicyForm(forms.ModelForm):
    class Meta:
        model = Policy
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        roles = Roles.choices()
        if roles or Roles.uses_model():
            self.fields["role"] = forms.ChoiceField(choices=[("", "---------")] + roles)
        self.fields["scope"] = forms.ChoiceField(choices=ScopeRegistry.choices(), initial="org")
        self.fields["condition"] = forms.ChoiceField(
            choices=[("", "— none —")] + ConditionRegistry.choices(), required=False
        )


class RoleForm(forms.ModelForm):
    class Meta:
        model = Role
        fields = ["code", "name", "description", "is_active", "includes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "includes" in self.fields and self.instance.pk:
            self.fields["includes"].queryset = Role.objects.exclude(pk=self.instance.pk)

    def clean(self):
        cleaned = super().clean()
        code = cleaned.get("code") or self.instance.code
        includes = cleaned.get("includes")
        if code and includes is not None:
            graph = {
                role.code: list(role.includes.values_list("code", flat=True))
                for role in Role.objects.exclude(code=code)
            }
            graph[code] = [role.code for role in includes]
            cycle = RoleTree.find_cycle(graph)
            if cycle:
                self.add_error("includes", f"That would make roles include each other: {' -> '.join(cycle)}.")
        return cleaned


class SuperuserOnlyAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_module_permission(request)


@admin.register(Role)
class RoleAdmin(SuperuserOnlyAdmin):
    """Shown only when the member model's `role` is a RoleField."""

    form = RoleForm
    list_display = ["name", "code", "is_active", "included_roles", "policies", "members", "updated_at"]
    list_filter = ["is_active"]
    search_fields = ["code", "name"]
    filter_horizontal = ["includes"]
    readonly_fields = ["updated_at"]

    def has_module_permission(self, request):
        return super().has_module_permission(request) and Roles.uses_model()

    def has_delete_permission(self, request, obj=None):
        if not super().has_delete_permission(request, obj):
            return False
        return obj is None or not obj.in_use()

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + ["code"] if obj is not None else fields

    @admin.display(description="Includes")
    def included_roles(self, obj):
        return ", ".join(obj.includes.values_list("code", flat=True)) or "—"

    @admin.display(description="Policies")
    def policies(self, obj):
        return obj.policy_count()

    @admin.display(description="Members")
    def members(self, obj):
        return obj.member_count()

    def save_model(self, request, obj, form, change):
        PolicyAudit.set_actor(obj, request.user, source="admin", defer=True)
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        kind = PolicyChange.Kind.UPDATED if change else PolicyChange.Kind.CREATED
        PolicyAudit.record_role(form.instance, kind)

    def delete_model(self, request, obj):
        PolicyAudit.set_actor(obj, request.user, source="admin")
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # The admin refuses bulk deletes that include roles in use (see
        # has_delete_permission), and Role.delete() refuses them too.
        for obj in queryset:
            self.delete_model(request, obj)


@admin.register(Policy)
class PolicyAdmin(SuperuserOnlyAdmin):
    form = PolicyForm
    list_display = ["code", "role", "content_type", "action", "effect", "scope", "condition", "updated_at"]
    list_filter = ["effect", "role", "content_type", "action", "scope"]
    search_fields = ["code", "name", "role"]
    readonly_fields = ["updated_at"]

    def save_model(self, request, obj, form, change):
        PolicyAudit.set_actor(obj, request.user, source="admin")
        super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        PolicyAudit.set_actor(obj, request.user, source="admin")
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self.delete_model(request, obj)


@admin.register(PolicyChange)
class PolicyChangeAdmin(SuperuserOnlyAdmin):
    list_display = ["changed_at", "target", "policy_code", "kind", "source", "changed_by"]
    list_filter = ["target", "kind", "source"]
    search_fields = ["policy_code"]
    readonly_fields = ["target", "policy_code", "kind", "data", "source", "changed_by", "changed_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
