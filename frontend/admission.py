"""Release gate for the opt-in production frontend.

P4's historical writer remains unknown. P41 separately validated the current
readonly path. Writable admission stays blocked, with no command-line override.
"""

# Separately admitted read-only path: native RO connection + SQL authorizer,
# process capability guard, no mutation context and read-only Adapter.
# P41 controlled production probe: main files/settings unchanged (2026-10-14).
READONLY_RELEASE = 'P41_VALIDATED_READONLY'
BLOCK_REASON = (
    'P4 生产安全门禁未通过：数据库指纹发生变化，原因尚未确认。'
    '新版生产入口暂不启动；未执行恢复、迁移或 checkpoint。'
    '详情见 D:\\smm-product-redesign-p4\\runtime_validation.md。'
)


def require_production_admission(*, read_only=False):
    if read_only and READONLY_RELEASE == 'P41_VALIDATED_READONLY':
        return
    raise RuntimeError(BLOCK_REASON if read_only else
                       '可写生产准入继续阻断；仅 --read-only 预览可在独立安全验证后启动。')
