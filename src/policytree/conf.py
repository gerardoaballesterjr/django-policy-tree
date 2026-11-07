"""Settings and model lookups. Never import the project's models directly."""
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class PolicyTreeSettings:
    """
    Reads the package settings and resolves the project's models.

        PolicyTreeSettings.get_organization_model()
        PolicyTreeSettings.get_member_model()
    """

    ORGANIZATION_SETTING = "POLICYTREE_ORGANIZATION_MODEL"
    MEMBER_SETTING = "POLICYTREE_MEMBER_MODEL"
    CACHE_KEY_SETTING = "POLICYTREE_CACHE_KEY"
    CACHE_ALIAS_SETTING = "POLICYTREE_CACHE_ALIAS"
    CACHE_TIMEOUT_SETTING = "POLICYTREE_CACHE_TIMEOUT"
    ROLE_INCLUDES_SETTING = "POLICYTREE_ROLE_INCLUDES"
    DEFAULT_CACHE_KEY = "policytree:v2"

    # ---- raw settings

    @classmethod
    def _required(cls, name):
        value = getattr(settings, name, None)
        if not value:
            raise ImproperlyConfigured(
                f"{name} must be set, e.g. {name} = 'accounts.Organization'."
            )
        return value

    @classmethod
    def organization_model_label(cls):
        return cls._required(cls.ORGANIZATION_SETTING)

    @classmethod
    def member_model_label(cls):
        return cls._required(cls.MEMBER_SETTING)

    @classmethod
    def cache_key(cls):
        return getattr(settings, cls.CACHE_KEY_SETTING, cls.DEFAULT_CACHE_KEY)

    DEFAULT_CACHE_TIMEOUT = 300
    MAX_CACHE_TIMEOUT = 3600

    @classmethod
    def cache_timeout(cls):
        """Seconds a cached copy may live (1 to 3600; default 300); changes clear it sooner."""
        value = getattr(settings, cls.CACHE_TIMEOUT_SETTING, cls.DEFAULT_CACHE_TIMEOUT)
        if not isinstance(value, int) or value <= 0:
            return cls.DEFAULT_CACHE_TIMEOUT
        return min(value, cls.MAX_CACHE_TIMEOUT)

    @classmethod
    def cache_alias(cls):
        return getattr(settings, cls.CACHE_ALIAS_SETTING, "default")

    @classmethod
    def role_includes(cls):
        """{"admin": ["staff"], "staff": ["viewer"]}: a role also gets the listed roles' policies."""
        return dict(getattr(settings, cls.ROLE_INCLUDES_SETTING, None) or {})

    # ---- models

    @classmethod
    def _get_model(cls, label, setting):
        try:
            return apps.get_model(label, require_ready=False)
        except ValueError:
            raise ImproperlyConfigured(f"{setting} must look like 'app_label.ModelName'.")
        except LookupError:
            raise ImproperlyConfigured(f"{setting} refers to '{label}', which is not installed.")

    @classmethod
    def get_organization_model(cls):
        return cls._get_model(cls.organization_model_label(), cls.ORGANIZATION_SETTING)

    @classmethod
    def get_member_model(cls):
        return cls._get_model(cls.member_model_label(), cls.MEMBER_SETTING)
