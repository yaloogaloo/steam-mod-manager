"""Agent artifacts are constrained without changing production runtime paths."""
from pathlib import Path
import tempfile
import pytest
from tools.agent_workspace import require_workspace
from services.business_resources import checked_sandbox_root


def test_agent_workspace_boundaries(tmp_path):
    root = Path(__file__).resolve().parents[1]
    workspace = root / '_tmp'
    assert tmp_path.resolve().is_relative_to(workspace / 'tests')
    assert Path(tempfile.gettempdir()).resolve().is_relative_to(workspace / 'tests')
    assert require_workspace(tmp_path) == tmp_path.resolve()
    with tempfile.TemporaryDirectory() as temporary:
        assert Path(temporary).resolve().is_relative_to(workspace / 'tests')
        (Path(temporary) / 'probe.txt').write_text('workspace check', encoding='utf8')
    for rejected in (root, root / 'data', workspace, workspace / 'protected_recovery' / 'test'):
        with pytest.raises((ValueError, PermissionError)):
            require_workspace(rejected)
    with pytest.raises(ValueError):
        require_workspace(root.anchor + 'smm-forbidden-output')
    with pytest.raises(PermissionError):
        checked_sandbox_root(workspace / 'tests' / '..' / 'escape')
