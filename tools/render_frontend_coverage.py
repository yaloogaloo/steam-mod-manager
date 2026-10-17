"""Render the existing parity document from its single editable coverage registry."""
import json
from pathlib import Path


def render(document):
    lines=['# 旧前端业务接管清单','',
        '权威记录：`frontend_coverage.json`；基线：`'+document['baseline']+'`。',
        document['note'],
        'P1 原状态为 78 implemented、8 partial、7 legacy_unreachable、16 code_only_no_entry。原版没有可达入口的项目保留记录，但不冒充后端不支持。',
        'EQUIVALENT 需要正式 UI、权威业务和持久化证据；临时数据回归和静态接线检查不能替代正式验收。','',
        '| ID / 旧功能 | 旧入口与权威实现 | 合同 | 新入口与调用链 | 状态及缺口 | 正式证据 |',
        '|---|---|---|---|---|---|']
    for row in document['features']:
        cells=[str(row['id'])+'. '+row['name'],row['legacy_entry_and_service'],row['contract'],row['new_entry_and_chain'],
            '**'+row['status']+'**：'+row['remaining'],row['formal_result']]
        lines.append('| '+' | '.join(c.replace('\n','<br>').replace('|','\\|') for c in cells)+' |')
    lines+=['','## 历史信号绑定源码索引','',
        '<details><summary>展开 '+str(len(document['legacy_handlers']))+' 个绑定处理函数（不等于独立功能数量）</summary>','',
        '| 旧源码 | 处理函数 | 权威调用 |','|---|---|---|']
    for row in document['legacy_handlers']:
        lines.append('| `'+row['legacy_file']+':'+str(row['line'])+'` | `'+row['handler']+'` | '+row['authority_calls']+' |')
    lines+=['','</details>','']
    return '\n'.join(lines)


if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    directory=root/'docs/migration'
    document=json.loads((directory/'frontend_coverage.json').read_text('utf8'))
    (directory/'legacy_frontend_parity.md').write_text(render(document),encoding='utf8')
