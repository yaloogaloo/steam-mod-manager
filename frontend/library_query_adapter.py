"""Projection of the original pure library rules; JavaScript only selects ranks."""
from ui.library_query import ModFilterIndex,sort_key,matches_status_filter


def query_projection(rows,search_fields=None,tag_flags=None):
    indexes={}
    for pk,row in rows.items():
        indexes[pk]=ModFilterIndex(
            mod_id=str(pk),display_name=row['name'],steam_name='',notes=row.get('user_notes',''),
            game_name=row.get('game_name',''),favorite=row['favorite'],deployed=row['deployed'],
            has_offline=row['has_offline'],mtime=row['mtime'],sort_name=row['name'],
            invalid=row['invalid'],is_invalid=row['invalid'],conflict=row['conflict'],
            conflict_status=row['conflict_status'],enabled=row['enabled'],
            content_status=row['content_status'],identity_status=row['identity_status'],
            local_size_bytes=row['local_size_bytes'],local_size_status=row['local_size_status'])
    result={pk:{'sort_ranks':{},'filter_keys':[]} for pk in indexes}
    for pk,row in rows.items():
        fields=(search_fields or {}).get(str(pk))
        flags=(tag_flags or {}).get(str(pk))
        # Keep separate haystacks, as in matches_search; joined fields can
        # incorrectly match text spanning two unrelated properties.
        result[pk]['search_fields']=[str(value or '') for value in (
            row['name'],getattr(fields,'steam_name',row.get('steam_name','')),
            getattr(fields,'user_notes',row.get('user_notes','')),
            row.get('workspace_id',''),getattr(fields,'game_name',''),
            ' '.join(getattr(flags,'tag_values',())),
            getattr(fields,'category_tags',''),row.get('category',''),
            row.get('source_url',''),row.get('external_id',''),row.get('platform',''))]
    for mode in ('mtime','name','size'):
        for rank,pk in enumerate(sorted(indexes,key=lambda k:sort_key(indexes[k],mode))):
            result[pk]['sort_ranks'][mode]=rank
    for pk,index in indexes.items():
        result[pk]['filter_keys']=[key for key in ('content_missing','invalid','conflict','disabled','anomaly','offline_present','offline_missing') if matches_status_filter(index,key)]
    return result
