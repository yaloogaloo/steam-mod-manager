"""Direct file success must not hide an incomplete Launcher projection."""
from types import SimpleNamespace

import pytest

from services.deploy import ModDeployer


@pytest.mark.parametrize('active,effective,intended,success', [
    ('', False, ['mod/ugc_42.mod'], False),
    ('existing-playset', False, ['mod/ugc_42.mod'], False),
    ('existing-playset', True, ['mod/ugc_42.mod'], True),
    ('', False, [], True),  # Removing the final member requires no active set.
])
def test_result_distinguishes_direct_and_launcher(monkeypatch, active, effective, intended, success):
    monkeypatch.setattr('services.paradox_activation.consider_paradox_playset_notice',
                        lambda *a, **k: SimpleNamespace(should_prompt=False))
    deployer = object.__new__(ModDeployer)
    monkeypatch.setattr(deployer, '_database', lambda: None)
    report = SimpleNamespace(errors=[], unresolved=[], direct_dlc_load_written=True,
                             enabled_launcher_ids=intended, active_playset_id=active,
                             is_effective=effective, playset_written=bool(active))
    result = {'success': True}
    deployer._annotate_paradox_launcher_result(result, 281990, report)
    assert result['success'] is success
    assert result['direct_dlc_load_written'] is True
    assert result['launcher_effective'] is effective
    assert result['game_load_verified'] is False
    if not success:
        assert result['error_code'] == 'projection_failed'
        assert result['partial_state_saved'] is True
        assert 'Launcher' in result['error']
