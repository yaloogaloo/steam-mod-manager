# Identity Repair Architecture Report

**Date:** 2026-09-08  
**Task:** Phase 2 Task 4 — Identity Repair Architecture Audit  
**Policy:** Audit only — **no deletions**, no compat layer, no schema changes

> **P2-2 (2026-09-10) contract correction:** Frozen Entity Identity is TEXT
> `mods.internal_id`. `mods.mod_id` is the SQLite PK / FK handle, not the
> business Entity Identity. The table below is a historical audit snapshot
> and must not be read as `Internal ID = mods.mod_id`.

**Regression:**

**Regression:**

```text
pytest tests/test_identity_authority_final.py -q  → passed
pytest tests/test_identity_write_boundary.py -q   → passed
```

---

## Identity Contract (reference)

| Field | Role |
|-------|------|
| `mods.mod_id` | Internal unique entity PK |
| `workspace_id` | User-facing / platform association (not entity match key) |
| `published_file_id` | Steam Workshop source ID only (API + association) |
| `external_id` | Platform-native ID (Steam Workshop / Nexus Mod ID, …) |

---

## 1. Module responsibilities

| 模块 | 入口 | 调用方 | 读写数据库范围 | 是否生产路径 |
|------|------|--------|----------------|--------------|
| **`identity_repair.py`** | `plan_identity_repair`, `apply_identity_repair`, `format_repair_plan`, CLI `main` / `run_audit_cli` / `run_apply_cli` | **Tools:** `scripts/identity_governance.py identity-repair`, `scripts/identity_repair_preflight.py`, module CLI. **Tests:** `test_identity_repair*`. **Peer:** `identity_invariants` (helpers). **Not** UI / Sync / Reconcile | **Read:** `mods` identity/path/status; dynamic scan of tables with `mod_id` / `source_mod_id` / `target_mod_id` / `published_file_id`. **Write (apply):** `DELETE mods` (ghost/invalid); rewire FKs; scrub `source_url`; set `identity_status` / `folder_present`; merge user fields onto canonical; `identity_repair_audit` + `identity_audit_log`; FS quarantine. Sidecar `published_file_id` via `sidecar_published_file_id` (Workshop only) | **否** — tooling, gated `--apply` |
| **`mod_identity_repair.py`** | `audit_severity_counts`, `build_repair_plan`, `apply_repair_plan`, `repair_mod_library_identity`, elect helpers | **Tools:** `scripts/identity_governance.py repair`. **Tests:** `test_identity_governance.py`. **Peer:** `identity_repair` imports `audit_severity_counts` only | **Read:** `mods` (+ integrity audit). **Write:** `external_id` / `platform` / `source_url` / `workspace_id`; `DELETE` duplicate via `delete_mod_record`; migrate `deployment_record_items.mod_id`; audit log. Sidecar: **`published_file_id = canonical mod_id`** (⚠ contract risk) | **否** — tooling only |
| **`identity_pollution.py`** | `scan_identity_pollution`, `plan_identity_pollution_repair`, `apply_identity_pollution_repair`, `write_pollution_report` | **Tools:** `tools/identity_pollution_report.py`. **Tests:** `test_identity_authority_final.py` | **Read:** `mods` identity columns. **Write:** `app_id`, `workspace_id` (uniquify), `identity_status='unresolved'`. **No** row delete | **否** — tooling |
| **`status_recovery.py`** | `run_status_recovery`, `run_status_model_cleanup_v2`, `migrate_identity_pollution_from_content`, content/identity re-eval helpers | **Production:** `library_reconcile.reconcile_library`; `db_manager` open (migrate phase). **Tests:** status authority suites | **Read/Write:** `mods.identity_status`, `content_status`, `library_status`; `schema_flags`. **Never** deploy / user conflict fields | **是** — library open / reconcile (idempotent flags) |
| **`library_reconcile.py`** (identity slice) | `reconcile_library`, `start_reconcile_library_async`, orphan emit | **Production UI:** `ui/main_window.py` startup. Many tests | Via `ensure_mod_identity` + `persist_identity`: path, `folder_present`, sanitized platform/`external_id`/`source_url`/`workspace_id`, `identity_status`. **No** create | **是** — bind-only |
| **`mod_identity.py`** | `ensure_mod_identity`, `resolve_existing_mod_id`, `read_internal_id`, `extract_workspace_id`, … | Reconcile, Sync, sidecar, deploy, status_recovery multi-folder, orphan_import, … | **Read-only** DB lookups by `internal_id` | **是** |
| **`identity_service.py`** | `create_mod_identity`, `persist_identity`, `repair_no_allocate_scope`, `sidecar_published_file_id`, lifecycle gates | Importers / Sync / DB / sidecar (create+persist); repair apply (no-allocate gate) | Create via authority; persist identity fields; **no** silent catalog INSERT | **是** (create gated Import/Sync) |
| **`mod_identity_authority.py`** | `resolve_mod_identity`, `create_mod_identity`, `update_platform_identity`, `ensure_non_polluted_workspace`, `log_identity_mutation` | Wrapped by IdentityService; deploy; repair scrub | Create / update platform fields; scrub `workspace_id` | **是** |
| **`orphan_import.py`** | `OrphanCandidate`, `import_orphan_candidates` | Reconcile **emits** candidates. Apply API: **tests only** (no UI consumer) | Bind-only via `ensure_mod_identity` | Emit **是** / apply **否** |
| **`identity_invariants.py`** | Invariant scanners consuming repair helpers | Tests / governance | Read-oriented | tooling / CI |
| **Full rebuild tools** (`tools/identity_full_rebuild_*`, recovery/collision scripts) | Audit / preview / apply scripts | Operator CLI | Own SQL/FS disaster recovery | **否** — disaster tooling |

`ui/widgets/` — N/A for this task. No second repair package beyond the modules above.

---

## 2. Duplicate capability analysis

### Capability map

| 能力 | 主要实现 | 次要/重叠 | 判定 |
|------|----------|-----------|------|
| **Orphan repair / bind** | Reconcile emit + `mod_identity.ensure_mod_identity` | `orphan_import.import_orphan_candidates` (unused apply); `identity_repair` ORPHAN class (quarantine) | Bind path **C**; unused apply API **B** |
| **Identity rebuild** | `tools/identity_full_rebuild_*` | Collision / hybrid recovery tools | **C** (disaster tooling — keep outside day-to-day RepairService) |
| **Pollution detection** | `identity_pollution.scan_*`; pieces of both repair planners; integrity audit | Name clash with `status_recovery.migrate_identity_pollution_from_content` (status axis, not field pollution) | Detect **C**; pollution **apply** **B** (fold into Repair) |
| **Recovery (status peel)** | `status_recovery` | — | **C** (status axis; sibling, not entity merge) |
| **Invalid / duplicate entity retirement** | `identity_repair` (`REMOVE_INVALID_DUPLICATE`, quarantine, FK rewire, audit table) | `mod_identity_repair.retire_duplicate_entity` | Prefer **identity_repair** → old stack **B** |
| **Workspace / external scrub** | Authority `ensure_non_polluted_workspace`; pollution REASSIGN; old repair scrub actions | Three writers | Detect unify; apply via authority + Repair opcodes **B→C** |
| **Sidecar `published_file_id` bind** | `identity_repair` + `sidecar_published_file_id` | `mod_identity_repair` writes **internal mod_id** into PFI | New path **C**; old binder **B** (contract violation) |

### Classification summary

| 类 | 含义 | 模块 / 能力 |
|----|------|-------------|
| **A — 可直接删除** | 本阶段审计后：**无整模块**达 A（均有调用方或测试）。仅潜在死内部（如 apply 未走的 `_apply_merge`）需确认后再动 | （暂无整模块 A） |
| **B — 迁移后删除** | `mod_identity_repair.py`（整体）；`identity_pollution.apply_*`（apply 面）；`orphan_import.import_orphan_candidates`（若决定不接线） | 迁移到 `identity_repair` / 未来 `IdentityRepairService` |
| **C — 必须保留** | `identity_repair.py`；`status_recovery.py`；`library_reconcile` bind；`mod_identity`；`identity_service` + `mod_identity_authority`；full-rebuild tools；pollution **detect** 逻辑 | 生产绑定 / 创建门禁 / 实体修复权威 / 状态恢复 |

---

## 3. Data modification boundaries

| 操作 | `identity_repair` | `mod_identity_repair` | `identity_pollution` | `status_recovery` | Reconcile / `mod_identity` | IdentityService create |
|------|-------------------|------------------------|----------------------|-------------------|----------------------------|------------------------|
| **允许创建 identity** | **否** (`repair_no_allocate_scope`) | **否** | **否** | **否** | **否** (`LIFECYCLE_RECONCILE`) | **是**（仅 Import/Sync） |
| **允许修改 `workspace_id`** | 不改写 canonical；拒绝合并导致 ghost→workspace | **是**（scrub / regenerate） | **是**（uniquify） | **否** | **是**（`persist_identity` 消毒后） | **是**（派生/持久化） |
| **允许修改 `mod_id`** | **否**重写；可 **DELETE** 无效行并迁引用 | **否**重写；可 **DELETE** 重复行 | **否** | **否** | **否** | 仅 **allocate 新 PK** |
| **允许绑定 `published_file_id`** | **是** — Workshop only via `sidecar_published_file_id` | **是** — ⚠ 写成 **canonical `mod_id`**（违反 Contract） | **否** | **否** | **否**（strip legacy；不作为修复写 PFI） | Steam create 路径历史 PK 可与 Workshop 同数字；语义分离 |

### Contract compliance notes

1. **Repair must never mint** Internal entities — satisfied by `identity_repair` + reconcile gates; violated only if someone calls create outside Import/Sync.
2. **`published_file_id` must stay Steam Workshop** — `identity_repair` complies; **`mod_identity_repair._bind_folder_to_canonical` does not** (writes Internal PK into sidecar PFI) → strong reason to migrate away (**B**).
3. **`workspace_id` is association, not PK** — pollution uniquify and authority scrub are allowed repairs; must not be used for entity match.
4. **`mod_id` is immutable identity** — repair may delete invalid rows after FK migrate; never rewrite surviving PK.

---

## 4. Merge proposal → `IdentityRepairService`

**目标架构（概念，本阶段不实现）：**

```
IdentityRepairService
├── detect()    # 只读 findings
├── validate()  # 变更前门禁
├── repair()    # 门禁 apply；永不 allocate
└── report()    # JSON / CLI / VerificationResult
```

### detect
吸收：
- `identity_repair.plan_identity_repair`（ghost / URL scrub / invalid duplicate）— **主引擎**
- `identity_pollution.scan_identity_pollution`（跨游戏 WS、跨平台 external、`app_id=0`）
- integrity audit + `audit_severity_counts`（从旧 `mod_identity_repair` 迁出）
- reconcile orphan 发射 → finding 类型 `ORPHAN_UNBOUND_FOLDER`
- **不吸收** `status_recovery`（状态轴另属 `StatusRecoveryService`）

### validate
集中：
- `repair_no_allocate_scope` / 禁止 `create_mod_identity`
- 禁止改写 canonical 原始 identity 字段为 ghost
- `(platform, app_id, external_id)` 唯一性
- FS quarantine vs 共享目录
- sidecar PFI 仅 Workshop（`sidecar_published_file_id`）
- 禁止重写 `mod_id`

### repair
单一 apply 管线（今日 `apply_identity_repair` + pollution 安全动作 + authority scrub）：
1. Remove / quarantine invalid duplicates  
2. Scrub polluting `source_url`  
3. Scrub polluted `external_id` / `workspace_id` via **authority**  
4. Infer Nexus `app_id`（from pollution）  
5. Mark `identity_status`  
6. Bind sidecar PFI **only** via Workshop helper  
7. Audit → `identity_repair_audit` + `identity_audit_log`  

**Out of scope:** full rebuild tools；content re-eval（调用 `status_recovery`）；Import create。

### report
统一 CLI：
- `identity_governance` 停止双路径（`mod_identity_repair` vs `identity_repair`）
- Pollution JSON 作为 report view，不再是第二套 apply 工具
- 保留 preflight VerificationResult

### Suggested ownership after merge

| Concern | Owner |
|---------|--------|
| Create / allocate | `IdentityService` + `mod_identity_authority` |
| Runtime bind | `mod_identity` + `library_reconcile` |
| Status peel | `status_recovery` |
| Entity pollution / invalid duplicate | **`IdentityRepairService`** |
| Disaster rebuild | `tools/identity_full_rebuild_*` |

### Migration sequence (next phases — not this task)

1. Point `identity_governance repair` → `identity_repair` only.  
2. Move `audit_severity_counts` beside integrity audit; delete `mod_identity_repair.py`.  
3. Fold pollution scan into `detect`; pollution apply → repair opcodes.  
4. Wire or drop `import_orphan_candidates`.  
5. No UI `repair()` until PRODUCTION_VERIFIED protocol.

---

## Verdict

- **Two repair stacks** exist; **`identity_repair` is the entity-repair authority (C)**; **`mod_identity_repair` is redundant and contract-unsafe on sidecar PFI (B)**.  
- **Pollution detect (C) / apply (B)** should fold into a future `IdentityRepairService`.  
- **`status_recovery` stays (C)** on the production reconcile path — status axis, not identity mint.  
- **Runtime create remains Import/Sync only**; Repair must not create identity or rewrite `mod_id`.  

**本阶段未删除任何代码。**
