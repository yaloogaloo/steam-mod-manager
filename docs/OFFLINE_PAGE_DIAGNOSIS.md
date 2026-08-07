# Offline Page Diagnosis

**Mod:** `[Palworld 1.0] 4x Storage - Bigger Chests and Containers`  
**Mod ID:** `3761838546`  
**Date:** 2026-08-04  
**Scope:** read-only audit + temporary monkeypatch test (no business-logic changes)

---

## Sync path

同步一个 Mod 时，离线页抓取发生在 **Phase 3 网络补全**，不是复制阶段。

```
ModSyncService.sync()
  └─ Phase 1b: delta classify (_is_fully_synced_mod)
  └─ Phase 2: Steam metadata (仅 to_fetch)
  └─ Phase 3:
        _copy_only()                    # 文件复制 / 数字目录改名
        └─ _network_enrich_and_save()
              └─ _network_enrich()
                    └─ OfflinePageArchiver.archive()     # 主路径
                    └─ OfflinePageArchiver.ensure_offline_page()  # 仅当仍无有效页
```

| 项 | 值 |
|----|-----|
| **函数名** | `_network_enrich` → `OfflinePageArchiver.archive` |
| **文件** | `services/sync.py` / `services/archive.py` |
| **代码位置** | `sync.py` L441–508（`need_archive` 为真时 L497–508 调用 `archive`）；硬保底 L511–518 调 `ensure_offline_page` |
| **archive 入口** | `services/archive.py` `archive()` L132+；HTML：`_fetch_main_html` → `_http_get` |

---

## Skip logic

**存在。** 多层跳过，真实条件如下。

### Layer 1 — 整 Mod 早退（不进 metadata / 不进 archive）

`services/sync.py` L151–163：

```text
if overwrite_files:
    → 永远进入 to_fetch（强制覆盖）
elif existing and skip_existing and _is_fully_synced_mod(existing):
    → to_skip（整条跳过，不调用 archive）
else:
    → to_fetch
```

`_is_fully_synced_mod`（L310–324）要求同时满足：

1. 目录存在且**不是**纯数字名  
2. `.info/mod.json` 存在  
3. `_has_valid_offline_page` ≡ `index.html` **存在且 `st_size > 0`**

**关键：stub 页 size>0 也算“有效”，会触发整条跳过，不会重抓。**

### Layer 2 — 网络阶段内跳过 archive

`_network_enrich` L464–475：

```text
if (not overwrite_files) and _has_valid_offline_page(managed):
    need_archive = False
```

即便进了 to_fetch，只要已有非空 `index.html`，仍不调用 `archive`（除非强制覆盖）。

### Layer 3 — archive / ensure 自身

| 函数 | 条件 |
|------|------|
| `archive(..., overwrite=False)` | `index.html` 已存在 → 直接返回（同步主路径传 `overwrite=True`，此层通常不挡） |
| `ensure_offline_page` | `index.html` 存在且 size>0 → 直接返回，不重抓 |

---

## Mod 3761838546 result

### 用户当时看到 stub 时（原始失败同步）

证据：stub 内嵌错误  
`Failed to perform, curl: (35) Recv failure: Connection was reset`  
（仅 `write_fallback_page` / `archive` 失败路径会写入）

| 探针 | 结果 |
|------|------|
| 进入同步 | **YES** |
| `OfflinePageArchiver.archive()` | **YES** |
| `_fetch_main_html()` | **YES** |
| `_http_get()` | **YES** |
| `fallback` | **YES** |
| 最终 | **STUB** |

根因（该次）：直连或非有效代理下 Steam TLS 重置 → **B. archive 抓取失败**。

### 失败后若再普通同步（未勾强制覆盖）

当时 stub `size ≈ 1401 > 0` → `_is_fully_synced_mod == True` → **整条跳过**，不再进 archive。

| 探针 | 结果 |
|------|------|
| archive_called | **NO** |
| fetch_called | **NO** |
| fallback | **NO** |
| 最终 | 保留旧 **STUB** |

根因（后续）：**A. 同步跳过离线页更新**。

### 强制覆盖 + `socks5://127.0.0.1:7897`（临时 monkeypatch 测试，未改源码）

与 GUI「强制覆盖」等价：`overwrite_files=True` + 代理字典传入 `OfflinePageArchiver`。

| 探针 | 结果 |
|------|------|
| archive_called | **YES** |
| fetch_called | **YES** |
| http_get_called | **YES** |
| fallback | **NO** |
| 最终 | **SUCCESS** |
| index_size | ~61354 |
| assets | ~300 |
| banner | True |

### 当前磁盘状态（诊断时复核）

```text
index_size     61354
is_stub        False
has_banner     True
has_curl35     False
assets         300
fully_synced   True
```

---

## Root Cause

**原始失败：B — archive 抓取失败**（无可用代理 / 直连被 Steam reset，写入 stub）。

**用户后续仍看到 stub：A — 同步跳过离线页更新**（stub `size > 0` 被当成有效页）。

**不是架构未接 archive；也不是 curl_cffi 未启用。**  
修复方式（操作层，非改代码）：代理填 `socks5://127.0.0.1:7897`，勾选「强制覆盖/重新生成已存在的 Mod」再同步。已验证该路径可将本 Mod 刷成真实离线页。
