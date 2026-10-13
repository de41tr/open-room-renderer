# Repository instructions

## Bug fixes and upstream contributions

- Implement focused fixes and perform only the bare minimum checks needed for the change.
- Do not run smoke tests. Run larger test selections or the full suite only when the user explicitly requests them.
- Prefer an existing targeted test when necessary; do not add tests that merely mirror the implementation.
- Commit minimally verified bug fixes to a dedicated `fix/...` branch, push that branch, and create a pull request against `main`.
- The maintainer authorizes this workflow for bug fixes; do not ask again solely to push a fix branch or open its pull request.
- Describe the concrete failure, resulting behavior, and validation in each pull request.
- Keep pull requests open for review; merge them only when the user requests it.
- Keep credentials, private runtime configuration, identities, and local device data out of commits and pull requests.
