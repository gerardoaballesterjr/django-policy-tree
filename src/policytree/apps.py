from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class PolicyTreeConfig(AppConfig):
    name = "policytree"
    verbose_name = "Policy tree"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .audit import PolicyAudit
        from .checks import PolicyTreeChecks

        PolicyTreeChecks.register()
        PolicyAudit.connect()

        # Each app may register scopes and conditions in a `policies.py` module.
        autodiscover_modules("policies")
