# Plain Django views

`PolicyViewMixin` works with Django's generic class-based views:

```python
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView
from policytree.views import PolicyViewMixin


class ProjectListView(PolicyViewMixin, ListView):
    model = Project


class ProjectDetailView(PolicyViewMixin, DetailView):
    model = Project


class ProjectCreateView(PolicyViewMixin, CreateView):
    model = Project
    fields = ["name", "budget", "organization"]


class ProjectUpdateView(PolicyViewMixin, UpdateView):
    model = Project
    fields = ["name", "budget", "organization"]


class ProjectDeleteView(PolicyViewMixin, DeleteView):
    model = Project
    success_url = "/projects/"
```

| View | Action | Behavior |
|---|---|---|
| `ListView` | `list` | the queryset is scoped |
| `DetailView` | `detail` | out-of-scope objects return 404, others are checked |
| `CreateView` | `create` | the target from the form must be in scope; the saved object is re-checked |
| `UpdateView` | `update` | checked before, and the saved result re-checked |
| `DeleteView` | `delete` | checked before deleting |

Login is required (the mixin includes `LoginRequiredMixin`). Users with no matching allow get 403 before the view runs.

Set `policy_action` to use another action, for example a `TemplateView` that shows a dashboard of projects:

```python
class ProjectBoard(PolicyViewMixin, ListView):
    model = Project
    policy_action = "detail"
```

## Forms

Fields the user can neither read nor write are removed. Fields they can read but not write are **disabled**, and Django ignores submitted values for disabled fields. Fields they can write but not read are shown empty, so their current value isn't revealed.

Saved records are re-checked like in DRF: fields submitted on create must be writable where the record lands, and a moved record needs `create` rights in its new place.

Read rules aren't applied to the object in templates; show only fields the user may read, for example with `{% policy_fields %}` (below).

## Where creates go

As in DRF, the target comes from the form through the model's organization path (`organization` for projects, `project` for tasks). Override `get_policy_target(self, form)` when the place is decided on the server.

## Templates

```django
{% load policytree %}
{% policy_can "update" project as can_edit %}
{% if can_edit %}<a href="{% url 'project-edit' project.pk %}">Edit</a>{% endif %}
```

```django
{% policy_fields "detail" project as shown %}
{% if shown is None or "budget" in shown %}{{ project.budget }}{% endif %}
```

`policy_fields` returns the readable field names for that record, or `None` for all.

The tags need `request` in the template context (the default `django.template.context_processors.request`).
