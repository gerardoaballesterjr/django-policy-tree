# Security policy

django-policy-tree controls access to data, so we treat bugs that grant more access than intended as security issues.

## Reporting

Please **do not open a public issue** for a vulnerability.

Use GitHub's private vulnerability reporting: **Security → Report a vulnerability** on the repository. Include:

- the version, Django and DRF versions, and database;
- the policies, memberships and models involved (a minimal reproduction or failing test is ideal);
- what access was gained that shouldn't have been.

You'll get an acknowledgement within 7 days. Confirmed issues are fixed in a patch release, with credit if you'd like it.

## Supported versions

Until 1.0, only the latest release gets security fixes.

## Scope

See [docs/security.md](docs/security.md) for what the package guarantees and what is outside its control (for example, code that bypasses the provided integration points).
