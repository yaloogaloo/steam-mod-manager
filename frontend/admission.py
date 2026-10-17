"""Normal startup admits scoped production commands; test processes stay isolated."""
import os

READONLY_RELEASE = 'P41_VALIDATED_READONLY'

def require_production_admission(*, read_only=False):
    if read_only:return
    if os.environ.get('PYTEST_CURRENT_TEST') or os.environ.get('SMM_TEST_DB'):
        raise RuntimeError('生产可写入口不接受测试数据库环境')
