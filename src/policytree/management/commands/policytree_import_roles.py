from django.core.management.base import BaseCommand
from django.db import transaction

from policytree.audit import PolicyAudit
from policytree.conf import PolicyTreeSettings
from policytree.models import Policy, PolicyChange, Role
from policytree.roles import Roles


class Command(BaseCommand):
    help = (
        "Create Role rows for every role already in use: the member model's `choices`, "
        "values stored on members and policies, and POLICYTREE_ROLE_INCLUDES. Run this "
        "before switching Member.role from a CharField to a RoleField."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--with-includes", action="store_true",
            help="Also copy POLICYTREE_ROLE_INCLUDES into each role's `includes`.",
        )

    def handle(self, with_includes=False, **options):
        labels = dict(Roles.static_choices())
        codes = set(labels) | set(Policy.objects.values_list("role", flat=True))

        member = PolicyTreeSettings.get_member_model()
        column = "role_id" if Roles.uses_model() else "role"
        codes |= set(member._base_manager.values_list(column, flat=True))

        includes = PolicyTreeSettings.role_includes()
        codes |= set(includes) | {child for children in includes.values() for child in children}
        codes.discard("")
        codes.discard(None)

        created = linked = 0
        with transaction.atomic():
            for code in sorted(codes):
                if Role.objects.filter(code=code).exists():
                    continue
                role = Role(code=code, name=str(labels.get(code) or code.replace("_", " ").title()))
                PolicyAudit.set_actor(role, source="import")
                role.save()
                created += 1

            if with_includes:
                for code, children in includes.items():
                    role = Role.objects.get(code=code)
                    before = set(role.includes.values_list("code", flat=True))
                    wanted = before | set(children)
                    if wanted != before:
                        PolicyAudit.set_actor(role, source="import", defer=True)
                        role.includes.set(Role.objects.filter(code__in=wanted))
                        PolicyAudit.record_role(role, PolicyChange.Kind.UPDATED)
                        linked += 1

        Roles.clear()
        message = f"Created {created} role(s)."
        if with_includes:
            message += f" Updated includes on {linked} role(s)."
        self.stdout.write(message)
