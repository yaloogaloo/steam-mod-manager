"""Non-sensitive registration choices in the existing game-scoped UI settings."""
import json
from PySide6.QtCore import QSettings

KEYS={
    'kind':{'url','offline','local','workshop'},
    'platform':{'Steam','Nexus','GitHub','mod.io','Other'},
    'localPlatform':{'Steam','Nexus','GitHub','mod.io','Other'},
    'importKind':{'folder','batch','zip'},
}

def clean(value):
    if not isinstance(value,dict):return {}
    return {key:item for key,item in value.items() if key in KEYS and isinstance(item,str) and item in KEYS[key]}

def read(game,*,settings=None):
    store=settings if settings is not None else QSettings('SteamModManager','WorkshopLibrary')
    try:return clean(json.loads(store.value('ui/registration/'+str(int(game)),'{}',str)))
    except (ValueError,TypeError):return {}

def write(game,value,*,settings=None):
    store=settings if settings is not None else QSettings('SteamModManager','WorkshopLibrary')
    safe=clean(value)
    store.setValue('ui/registration/'+str(int(game)),json.dumps(safe))
    store.sync()
