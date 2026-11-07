# Recipe: SaaS workspaces

Each customer is a tenant with workspaces, and workspaces have projects. Customers' admins manage their own roles.

```
Tenant (customer)        owner
└─ Workspace             editor, viewer
   └─ Project            (documents live here)
```

## Models

```python
class Organization(AbstractOrganization):
    type = models.CharField(max_length=20, choices=[
        ("tenant", "Tenant"), ("workspace", "Workspace"), ("project", "Project")])


class Member(AbstractMember):
    role = RoleField()

    class Meta(AbstractMember.Meta):
        pass


class Document(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)  # a project
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    body = models.TextField()
```

## Roles and policies

```json
{
  "roles": [
    {"code": "viewer", "name": "Viewer"},
    {"code": "editor", "name": "Editor", "includes": ["viewer"]},
    {"code": "owner",  "name": "Owner",  "includes": ["editor"]}
  ],
  "policies": [
    {"code": "viewer-doc-list",   "role": "viewer", "model": "app.document", "action": "list",   "scope": "tree"},
    {"code": "viewer-doc-detail", "role": "viewer", "model": "app.document", "action": "detail", "scope": "tree"},
    {"code": "editor-doc-create", "role": "editor", "model": "app.document", "action": "create", "scope": "tree"},
    {"code": "editor-doc-update", "role": "editor", "model": "app.document", "action": "update", "scope": "tree"},
    {"code": "owner-doc-delete",  "role": "owner",  "model": "app.document", "action": "delete", "scope": "tree"},
    {"code": "owner-org-create",  "role": "owner",  "model": "app.organization", "action": "create", "scope": "tree"}
  ]
}
```

A member of a workspace reaches every project in it (`tree`), and nothing in other tenants: tenants are separate branches of the tree, so isolation comes from the scopes, with no extra filtering.

## Notes

- Only superusers create tenants (top-level organizations). Tenant owners create workspaces and projects inside their tenant (`create` on the organization model with scope `tree`).
- Role definitions are global to the project. If each tenant needs its own role names, model that in your own code; this package has one set of roles.
