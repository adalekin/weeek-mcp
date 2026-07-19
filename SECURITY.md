# Security

## Supported versions

Security fixes are applied to the latest released minor version where practical. Use the current release from PyPI or the default branch of this repository.

## Reporting a vulnerability

Please **do not** open a public issue for security-sensitive reports.

Instead, contact the maintainers privately (for example via the security advisory feature on GitHub for this repository, or email listed in package metadata on PyPI if available). Include:

- A short description of the issue and its impact
- Steps to reproduce or a proof of concept, if possible
- Suggested severity, if you have an opinion

We aim to acknowledge reports within a few business days and coordinate disclosure once a fix is ready.

## Handling credentials

This server talks to Weeek on your behalf. Keep the following out of version control
(they are covered by `.gitignore`):

- `WEEEK_API_TOKEN` and login credentials — provide them via environment or `.env`.
- The cached Playwright session (`storage_state.json`) — it grants access to your
  Weeek account.
