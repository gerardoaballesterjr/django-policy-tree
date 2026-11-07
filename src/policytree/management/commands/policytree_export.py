import json

from django.core.management.base import BaseCommand

from policytree.models import Policy, Role
from policytree.roles import Roles


class Command(BaseCommand):
    help = (
        "Write policies as JSON, in the format policytree_seed reads. With admin-managed "
        'roles (RoleField) the output is {"roles": [...], "policies": [...]}.'
    )

    def add_arguments(self, parser):
        parser.add_argument("path", nargs="?", help="Output file. Omit to print.")
        parser.add_argument("--with-roles", action="store_true", help="Include roles even without a RoleField.")

    def handle(self, path=None, with_roles=False, **options):
        policies = [p.snapshot() for p in Policy.objects.select_related("content_type").order_by("code")]
        if with_roles or Roles.uses_model():
            roles = [r.snapshot() for r in Role.objects.prefetch_related("includes").order_by("code")]
            data = {"roles": roles, "policies": policies}
            summary = f"{len(roles)} roles and {len(policies)} policies"
        else:
            data = policies
            summary = f"{len(policies)} policies"

        text = json.dumps(data, indent=2) + "\n"
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            self.stdout.write(f"Exported {summary} to {path}.")
        else:
            self.stdout.write(text, ending="")
