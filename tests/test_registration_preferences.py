import json
from PySide6.QtCore import QSettings
from frontend.registration_preferences import read,write,clean

def test_game_scoped_choices_survive_new_settings_instance_without_identity(tmp_path):
    path=str(tmp_path/'preferences.ini');store=QSettings(path,QSettings.IniFormat)
    choices={'kind':'offline','platform':'Nexus','localPlatform':'Other','importKind':'zip','source':'C:/temp/private.html','api_key':'secret','workshop_id':'123'}
    write(916440,choices,settings=store)
    restarted=QSettings(path,QSettings.IniFormat)
    assert read(916440,settings=restarted)=={k:v for k,v in choices.items() if k in ('kind','platform','localPlatform','importKind')}
    assert read(1623730,settings=restarted)=={}
    contents=(tmp_path/'preferences.ini').read_text()
    assert 'secret' not in contents and 'private.html' not in contents and '123' not in contents
    assert clean({'kind':'invalid','platform':'Other','source':'bad'})=={'platform':'Other'}
