from django.core.management.base import BaseCommand

from policytree.conf import PolicyTreeSettings


class Command(BaseCommand):
    help = "Recompute every organization's path from its parent (after imports or raw SQL)."

    def handle(self, **options):
        fixed = PolicyTreeSettings.get_organization_model().rebuild_paths()
        self.stdout.write(f"Fixed {fixed} path(s)." if fixed else "All paths were already correct.")
