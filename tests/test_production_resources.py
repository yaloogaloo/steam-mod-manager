"""Production composition preserves read/write boundaries without production mutations."""
import pytest
from core.db_manager import DatabaseManager
from services.production_resources import ProductionResources
from services.mutation_context import activate_test_context,reset_mutation_context


def test_production_folder_copy_preserves_original_optional_install_path(tmp_path):
    from tools.isolated_deploy_validation import initialize
    isolated,source,target,identity=initialize(tmp_path/'folder-application')
    try:
        isolated.db.update_game_deploy_config(42,install_path='')
        resources=ProductionResources(isolated.root,isolated.db,isolated.path('mod'))
        assert resources.validate_deploy(identity.internal_id).mod_pk==int(identity.mod_id)
        assert resources.validate_write(target)==target
        with pytest.raises(PermissionError):resources.validate_write(tmp_path/'unconfigured/game.exe')
    finally:isolated.db.close()


def test_production_wh3_native_activation_does_not_require_copy_mod_path(tmp_path):
    from tools.wh3_deploy_validation import initialize
    isolated,source,target,identity=initialize(tmp_path/'wh3-application')
    try:
        isolated.db.update_game_deploy_config(1142710,mod_path='')
        resources=ProductionResources(isolated.root,isolated.db,isolated.path('mod'))
        assert resources.validate_deploy(identity.internal_id).mod_pk==int(identity.mod_id)
        config=isolated.db.get_game_deploy_config(1142710)
        assert resources.validate_write(__import__('pathlib').Path(config.install_path)/'used_mods.txt')
        with pytest.raises(PermissionError):resources.validate_write(target)
    finally:isolated.db.close()


def test_configured_paths_and_selected_input_are_not_general_write_grants(tmp_path):
    root=tmp_path/'application';(root/'data').mkdir(parents=True)
    library=tmp_path/'original-library';library.mkdir()
    game=tmp_path/'game';(game/'data').mkdir(parents=True)
    workshop=tmp_path/'steamapps/workshop/content/1142710';workshop.mkdir(parents=True)
    db=DatabaseManager(root/'data/mod_manager.db')
    db.update_game_deploy_config(1142710,install_path=str(game),mod_path=str(game/'data'),workshop_path=str(workshop))
    resources=ProductionResources(root,db,library)
    try:
        assert resources.path('mod')==library
        assert resources.validate(workshop/'123/test.pack')==workshop/'123/test.pack'
        with pytest.raises(PermissionError):resources.validate_write(workshop/'123/test.pack')
        assert resources.validate_write(game/'used_mods.txt')==game/'used_mods.txt'
        with pytest.raises(PermissionError):resources.validate_write(game/'Warhammer3.exe')
        source=tmp_path/'user-input/mod.zip';source.parent.mkdir();source.write_bytes(b'input')
        with pytest.raises(PermissionError):resources.validate(source)
        resources.select_input(source)
        assert resources.input_allowed(source)
        with pytest.raises(PermissionError):resources.validate_write(source)
        with pytest.raises(PermissionError):resources.path('../outside')
        token=activate_test_context([tmp_path])
        try:
            with pytest.raises(PermissionError,match='正式应用入口'):
                with resources.bind():pass
        finally:reset_mutation_context(token)
    finally:db.close()


def test_default_main_routes_to_writable_webengine(monkeypatch):
    import main
    calls=[]
    monkeypatch.setattr(main,'_configure_logging',lambda **kw:None)
    monkeypatch.setattr(main,'launch_gui',lambda **kw:calls.append(kw) or 0)
    assert main.main([])==0
    assert calls==[{'next_frontend':True,'read_only':False,'validation_root':None}]


def test_source_network_is_not_a_general_production_capability(tmp_path,monkeypatch):
    from services.proxy_resolver import ResolvedProxy
    monkeypatch.setattr('services.proxy_resolver.resolve_proxy',lambda **kw:ResolvedProxy())
    root=tmp_path/'app';(root/'data').mkdir(parents=True);lib=tmp_path/'mods';lib.mkdir()
    db=DatabaseManager(root/'data/mod_manager.db');r=ProductionResources(root,db,lib)
    try:
        public=('socket',('8.8.8.8',443));private=('socket',('127.0.0.1',80))
        assert not r.allow_source_external('socket.connect',public)
        with r.source_network('sync'):
            assert r.allow_source_external('socket.connect',public)
            assert not r.allow_source_external('socket.connect',private)
            assert not r.allow_source_external('subprocess.Popen',('cmd.exe',[],None,None))
        assert not r.allow_source_external('socket.connect',public)
    finally:db.close()


@pytest.mark.parametrize('production_scope', [False, True])
def test_steam_proxy_requires_explicit_production_source_scope(monkeypatch, production_scope):
    """A configured proxy works in full sync, never in isolated API admission."""
    import requests
    from types import SimpleNamespace
    from core.steam_api import SteamWorkshopClient, STEAM_PUBLISHED_FILE_DETAILS_URL
    from services import business_resources
    from services.production_resources import _source_operation
    resources = object.__new__(ProductionResources)
    object.__setattr__(resources, 'steam_public', True)
    monkeypatch.setattr(business_resources, 'current_resources', lambda: resources)
    scope_token = business_resources._scope.set(SimpleNamespace(network=[]))
    source_token = _source_operation.set(('sync', {('127.0.0.1', 7890)}) if production_scope else '')
    calls = []
    def transport(session, method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"response":{"publishedfiledetails":[]}}'
        response._content_consumed = True
        return response
    monkeypatch.setattr(requests.Session, 'request', transport)
    try:
        with SteamWorkshopClient(db=object(), proxy_url='http://127.0.0.1:7890', enable_scrape_fallback=False) as client:
            if production_scope:
                response = client._request_without_adapter_retries('POST', STEAM_PUBLISHED_FILE_DETAILS_URL, timeout=1)
                assert response.status_code == 200
                assert calls[0][2]['proxies']['https'] == 'http://127.0.0.1:7890'
                assert calls[0][2]['allow_redirects'] is False
                with pytest.raises(PermissionError):
                    client._request_without_adapter_retries('POST', 'https://example.com', timeout=1)
            else:
                with pytest.raises(PermissionError):
                    client._request_without_adapter_retries('POST', STEAM_PUBLISHED_FILE_DETAILS_URL, timeout=1)
                assert not calls
    finally:
        _source_operation.reset(source_token)
        business_resources._scope.reset(scope_token)

