# Contributing

## Reporting a problem

Flight records are personal data, and a raw one can be decrypted by other people. Please never attach a
flight record (`FlightRecord_*.txt`), a file from `keychains/` or an API key to a public issue.

What helps instead:

- the command you ran and everything it printed;
- the output of `uv run flyscribe --info --anon LOG`: log format, app version and the record types in
  the log, without serial numbers or position (it does include the date and time of the flight);
- if a value looks wrong, the affected rows from an `--anon` export and what you expected to see.

## Privacy and security problems

If an `--anon` export lets through something that identifies the pilot, the equipment or the place, if
a `--no-ids` export lets through a name or a serial number, or if flyscribe sends anything other than
the wrapped keys to DJI, please report it privately and not in a public issue: use **Report a
vulnerability** on the repository's [Security page](https://github.com/gibbsoft/flyscribe/security).

## Changing the code

- flyscribe is one module, `flyscribe.py`, that needs only the standard library and `cryptography`, and
  runs on Python 3.10 and newer. Lines are up to 120 characters long.
- The project is managed with [uv](https://docs.astral.sh/uv/): `uv run flyscribe` runs your working
  copy. If you change the dependencies in `pyproject.toml`, run `uv lock` and include the new `uv.lock`.
- There is no test suite yet. Check a change by exporting the same logs before and after it, in every
  format, plain, with `--anon` and with `--no-ids`: the output must be identical byte for byte unless
  the change is meant to alter it. Say in the pull request which app version and aircraft your logs
  came from.
- A newly decoded field that identifies the equipment or the pilot must be added to `IDS`, so that
  `--no-ids` and `--anon` leave it out. One that names a place must be added to `IDENTIFYING`, which
  only `--anon` leaves out.
- By contributing you agree that your contribution is released under the project's MIT licence.

## Checks on GitHub

Every push and every pull request runs the workflows in `.github/workflows/`:

- `ci.yml` starts the tool on the oldest supported Python and on a current one, with the packages from
  `uv.lock`, then builds the archives and compares what they contain with the list of release files. It
  converts no log: a change still needs the check described above.
- `secrets.yml` goes through every commit of every branch. It fails if one holds a file that `.gitignore`
  keeps out (a flight record, a key file, an export), or a secret that
  [gitleaks](https://github.com/gitleaks/gitleaks) finds with the rules in `.gitleaks.toml`: its own for
  the keys and tokens of many services, one for an unlocked key under any file name and one for a DJI
  API key that follows `DJI_API_KEY` or `--api-key`.

The workflows run after a push, and by then a public repository has shown the push to everyone. Before
you push, run the two checks of `secrets.yml` in your working copy:

```sh
git rev-list --all | xargs -n 1 git ls-tree -r --name-only | sort -u | git check-ignore --no-index --stdin
gitleaks git --redact
```

The first must print nothing, the second must find no leaks.

## Releasing

1. `uv version --bump patch` (or `minor`, `major`) sets the new version in `pyproject.toml` and
   `uv.lock`.
2. `uv build` writes the source archive and the wheel to `dist/`. A working copy also holds flight
   records, keys and exports, so list what the archives contain (`tar tf`, `unzip -l`) before going on.
   `pyproject.toml` limits them to `flyscribe.py`, the README, the licence files and the packaging
   files (`pyproject.toml`, `.gitignore` and generated metadata).
3. `uv publish` uploads them to PyPI.
