# Settings

| Setting | Required | Default | Meaning |
|---|---|---|---|
| `POLICYTREE_ORGANIZATION_MODEL` | yes | | `"app.Model"` subclassing `AbstractOrganization` |
| `POLICYTREE_MEMBER_MODEL` | yes | | `"app.Model"` subclassing `AbstractMember` |
| `POLICYTREE_ROLE_INCLUDES` | no | `{}` | `{"admin": ["manager"]}`: a role also gets the listed roles' policies |
| `POLICYTREE_CACHE_ALIAS` | no | `"default"` | which `CACHES` entry holds policies and roles; must be shared across processes |
| `POLICYTREE_CACHE_KEY` | no | `"policytree:v2"` | cache key prefix; give each project its own when they share a cache (or use `CACHES[...]["KEY_PREFIX"]`) |
| `POLICYTREE_CACHE_TIMEOUT` | no | `300` | seconds a cached copy may live; changes clear it sooner |

## Model attributes

| Attribute | Default | Meaning |
|---|---|---|
| `policytree_organization_field` | `"organization"` | path from the model to its organization, e.g. `"project__organization"` |
| `policytree_owner_field` | `"owner"` | path to the owning user, used by the `own` scope, e.g. `"invoice__owner"` |

The first step of the organization path is also the field that places a new record (`project` for `"project__organization"`).
