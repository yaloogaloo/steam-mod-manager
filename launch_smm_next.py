"""Explicit new SMM entry; legacy main.py remains the default."""
import argparse
import os
import sys

# Also keep imports read-only when invoked directly without Python's -B flag.
sys.dont_write_bytecode = True


def main():
    parser = argparse.ArgumentParser(description='SMM 新版资产库（部署与导入待迁移）')
    parser.add_argument('--read-only', action='store_true', help='禁止收藏和合集写入')
    args = parser.parse_args()
    if os.environ.get('SMM_TEST_DB') or os.environ.get('PYTEST_CURRENT_TEST'):
        parser.error('生产入口不接受测试数据库环境；请使用隔离验证入口')
    from frontend.admission import require_production_admission
    try:
        require_production_admission(read_only=args.read_only)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    from main import launch_gui
    return launch_gui(next_frontend=True, read_only=args.read_only)


if __name__ == '__main__':
    sys.exit(main())
