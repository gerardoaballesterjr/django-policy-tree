import json

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from policytree.audit import PolicyAudit
from policytree.models import Policy, PolicyChange, Role
from policytree.roles import Roles, RoleTree


class Command(BaseCommand):
    help = (
        "Create or update roles and policies from a JSON file. The file is either a list of "
        'policies, or {"roles": [...], "policies": [...]}. Keep it in git so changes are reviewed like code.'
    )

    POLICY_FIELDS = ["name", "role", "action", "effect", "scope", "condition", "readable_fields", "writable_fields"]
    ROLE_FIELDS = ["name", "description", "is_active"]

    def add_arguments(self, parser):
        parser.add_argument("path", help="JSON file, e.g. policies.json")
        parser.add_argument(
            "--prune", action="store_true",
            help="Delete policies not in the file, and unused roles not in the file (when it lists roles).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Show what would change, change nothing.")

    # ---- entry point

    def handle(self, path, prune=False, dry_run=False, **options):
        roles, policies = self._read(path)
        role_counts = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0}
        policy_counts = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0}

        with transaction.atomic():
            if roles is not None:
                self._apply_roles(roles, role_counts)

            codes = set()
            for index, row in enumerate(policies, start=1):
                policy, state = self._apply_policy(row, index)
                codes.add(policy.code)
                policy_counts[state] += 1

            if prune:
                for policy in Policy.objects.exclude(code__in=codes):
                    PolicyAudit.set_actor(policy, source="seed")
                    policy.delete()
                    policy_counts["deleted"] += 1
                if roles is not None:
                    self._prune_roles({row["code"] for row in roles}, role_counts)

            if dry_run:
                transaction.set_rollback(True)

        prefix = "Would apply" if dry_run else "Applied"
        if roles is not None:
            self.stdout.write(f"{prefix} roles: {self._summary(role_counts)}.")
            if not Roles.uses_model():
                self.stdout.write("Note: the member model's `role` isn't a RoleField, so these roles aren't used yet.")
        self.stdout.write(f"{prefix}: {self._summary(policy_counts)}.")

    @staticmethod
    def _summary(counts):
        return ", ".join(f"{counts[k]} {k}" for k in ("created", "updated", "unchanged", "deleted"))

    def _read(self, path):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise CommandError(f"Can't read {path}: {exc}")
        if isinstance(data, list):
            return None, data
        if isinstance(data, dict) and isinstance(data.get("policies", []), list):
            roles = data.get("roles")
            if roles is not None and not isinstance(roles, list):
                raise CommandError("`roles` must be a list.")
            return roles, data.get("policies", [])
        raise CommandError('The file must be a list of policies or {"roles": [...], "policies": [...]}.')

    # ---- roles

    def _apply_roles(self, rows, counts):
        found, before = [], {}
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict) or not row.get("code"):
                raise CommandError(f"Role #{index}: needs a `code`.")
            role = Role.objects.filter(code=row["code"]).first()
            before[row["code"]] = role.snapshot() if role else None
            role = role or Role(code=row["code"])
            for field in self.ROLE_FIELDS:
                if field in row:
                    setattr(role, field, row[field])
            if not role.name:
                role.name = row["code"].replace("_", " ").replace("-", " ").title()
            try:
                role.full_clean(exclude=["includes"])
            except ValidationError as exc:
                raise CommandError(f"Role '{row['code']}': {exc.message_dict}")
            PolicyAudit.set_actor(role, source="seed", defer=True)
            role.save()
            found.append((role, row))

        for role, row in found:
            if "includes" in row:
                wanted = list(row["includes"] or [])
                included = list(Role.objects.filter(code__in=wanted))
                missing = sorted(set(wanted) - {r.code for r in included})
                if missing:
                    raise CommandError(f"Role '{role.code}' includes unknown roles: {', '.join(missing)}.")
                if role.code in wanted:
                    raise CommandError(f"Role '{role.code}' can't include itself.")
                role.includes.set(included)

        graph = {r.code: list(r.includes.values_list("code", flat=True)) for r in Role.objects.all()}
        cycle = RoleTree.find_cycle(graph)
        if cycle:
            raise CommandError(f"Roles include each other: {' -> '.join(cycle)}.")

        for role, row in found:
            old = before[role.code]
            if old is None:
                PolicyAudit.record_role(role, PolicyChange.Kind.CREATED)
                counts["created"] += 1
            elif role.snapshot() != old:
                PolicyAudit.record_role(role, PolicyChange.Kind.UPDATED)
                counts["updated"] += 1
            else:
                setattr(role, PolicyAudit.DEFER_ATTR, False)
                counts["unchanged"] += 1
        Roles.clear()

    def _prune_roles(self, keep, counts):
        for role in Role.objects.exclude(code__in=keep):
            if role.in_use():
                self.stdout.write(f"Kept role '{role.code}': still in use.")
                continue
            PolicyAudit.set_actor(role, source="seed")
            role.delete()
            counts["deleted"] += 1

    # ---- policies

    def _apply_policy(self, row, index):
        try:
            code = row["code"]
            model = apps.get_model(row["model"])
        except (KeyError, LookupError, ValueError, TypeError) as exc:
            raise CommandError(f"Policy #{index}: needs a valid `code` and `model` ({exc}).")

        values = {field: row[field] for field in self.POLICY_FIELDS if field in row}
        values.setdefault("name", code)
        values["content_type"] = ContentType.objects.get_for_model(model)

        policy = Policy.objects.filter(code=code).first()
        state = "created" if policy is None else "updated"
        policy = policy or Policy(code=code)
        before = policy.snapshot() if policy.pk else None

        for field, value in values.items():
            setattr(policy, field, value)
        if before is not None and policy.snapshot() == before:
            return policy, "unchanged"

        try:
            policy.full_clean()
        except ValidationError as exc:
            raise CommandError(f"Policy '{code}': {exc.message_dict}")
        PolicyAudit.set_actor(policy, source="seed")
        policy.save()
        return policy, state
