"""Where a new record will live, worked out from the model's organization path."""
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models

from .registry import FieldPaths


class CreateTarget:
    """
    A new record names its place through the first step of its organization
    path, and the rest of the path leads to the organization:

        Task.policytree_organization_field = "project__organization"
        CreateTarget.resolve(Task, project_id)  ->  that project's organization

    A new organization is created inside its `parent`.
    """

    @staticmethod
    def field(model):
        """The model field (and request/form key) that names the place."""
        return FieldPaths(model).create_field

    @classmethod
    def _relation(cls, model):
        name = cls.field(model)
        try:
            field = model._meta.get_field(name)
        except Exception:
            raise ImproperlyConfigured(f"{model.__name__} has no field '{name}' to create records through.")
        if not field.is_relation or field.many_to_many or field.one_to_many:
            raise ImproperlyConfigured(
                f"{model.__name__}.{name} must be a foreign key on the path to the organization."
            )
        return field

    @classmethod
    def resolve(cls, model, value):
        """The organization for a raw id (or instance) of the create field, or None."""
        if value in (None, ""):
            return None
        field = cls._relation(model)
        if isinstance(value, models.Model):
            record = value
        else:
            lookup = {field.target_field.attname: value}
            try:
                record = field.related_model._default_manager.filter(**lookup).first()
            except (ValueError, TypeError, ValidationError):
                return None  # malformed id, e.g. not a UUID
        if record is None:
            return None
        return FieldPaths.walk(record, FieldPaths(model).create_rest)
