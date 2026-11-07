# Contributing

Thanks for helping. A few things matter more here than in most packages, because this one decides who can see and change data.

## Setup

```bash
pip install -e ".[test]"
pytest
```

`tests/testapp` holds the test fixtures: one sample domain (it happens to be a school, with an indirect `Space`/`Room` path) using a UUID and soft-delete base model, with both string roles (`Member`) and admin-managed roles (`RoleMember`).

## Rules for changes

- **Fail closed.** When something is missing or unexpected, deny. Never fall back to more access.
- **Keep `can()` and `filter()` in agreement.** Any change to scopes, conditions, roles or rule evaluation must keep `tests/test_properties.py` passing. Add your case to the generators if it isn't covered.
- **Test the denial, not just the success.** For every "X can do Y" test, add "Z can't".
- **Watch query counts.** `tests/test_hardening.py` asserts that list endpoints don't grow queries with rows. Don't add per-row queries.
- **Run the matrix** for changes to models, migrations or queries: `DB=postgres pytest`, `DB=mysql pytest`, and `tox` for Django versions.
- **Migrations:** `makemigrations --check` must be clean.
- **Docs:** update `docs/` and `CHANGELOG.md`; `mkdocs build --strict` must pass.

## Security issues

Don't open a public issue; see [SECURITY.md](SECURITY.md).

## Docs examples

The package is domain-agnostic, and the docs should read that way. General pages (README, quickstart, concepts, guides, reference) use the neutral company example (`core.Organization`, `Project`, `Task`, roles admin/manager/member/viewer). Domain-specific examples belong in `docs/recipes/`, one page per domain.
