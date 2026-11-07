"""
    {% load policytree %}
    {% policy_can "update" project as can_edit %}
    {% if can_edit %}<a href="...">Edit</a>{% endif %}

    {% policy_fields "detail" project as shown %}
    {% if shown is None or "budget" in shown %}{{ project.budget }}{% endif %}
"""
from django import template

from policytree.engine import PolicyEngine

register = template.Library()


class PolicyTemplateTags:
    @staticmethod
    def _engine(context):
        request = context.get("request")
        return PolicyEngine.for_request(request) if request is not None else None

    @staticmethod
    def can(context, action, obj):
        if obj is None:
            return False
        engine = PolicyTemplateTags._engine(context)
        if engine is None:
            return False
        return engine.can(action, obj)

    @staticmethod
    def fields(context, action, obj):
        """Readable field names for this record, None for all; an empty set when unknown."""
        if obj is None:
            return set()
        engine = PolicyTemplateTags._engine(context)
        if engine is None:
            return set()
        return engine.fields(type(obj), action, "read", obj=obj)


register.simple_tag(PolicyTemplateTags.can, takes_context=True, name="policy_can")
register.simple_tag(PolicyTemplateTags.fields, takes_context=True, name="policy_fields")
