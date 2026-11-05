# Contributing

Issues and patches are welcome.

- Do not put names, email addresses, phone numbers, tokens, machine paths, or workplace records in a patch.
- Keep API keys out of config files. Use `HAILPORTS_API_KEY` or the OS keychain.
- Plug tokens belong in the environment names listed in `.env.example`.
- Run `python -m pytest -q` and `python -m compileall -q hailports hailports_setup setup.py scripts` before you open a patch.
- `hailports setup --dry-run --non-interactive` should still print a plan and write nothing.

There is no sponsor URL yet. Do not add one unless it is a real link you were given.
