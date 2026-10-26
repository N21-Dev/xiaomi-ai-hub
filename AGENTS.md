# Contributor guidance

Application code lives under `services/`, and the optional native Android app
under `apps/android/`. Use Python 3.11+, Node.js 22+ for browser checks, and
Docker Compose v2. Run the commands in README.md from the repository root.

Keep Xiaomi credentials, model API keys, account/session databases, health
records, device identifiers, logs, APK signing keys and local deployment files
out of Git. Tests must use temporary storage and synthetic upstream responses;
never mount production data or call real devices from tests.

Login is required by default. Do not introduce default accounts, weaken CSRF
checks or silently enable the health assistant's data access. Add tests for
authentication, permission or data-flow changes. For UI changes, check desktop
and mobile layouts and use screenshots containing synthetic data only.

Update the relevant README and documentation when behavior changes. Verify
`python3 ops/audit_public.py`, the backend suites and appropriate browser checks
before proposing a release. Keep pull requests focused and report validation.
