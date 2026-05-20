import os

import tools.acceptance_pywiim as acceptance


def test_acceptance_adapter_has_main() -> None:
    assert callable(acceptance.main)


def test_scripts_are_executable() -> None:
    for path in ("scripts/run.sh", "scripts/run-dev.sh", "scripts/acceptance-open-source-client.sh"):
        assert os.access(path, os.X_OK), path
