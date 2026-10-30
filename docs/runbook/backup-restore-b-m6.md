# 备份恢复 Runbook — B-M6

> 计划版本：B-M6（medium profile）  
> 最后更新：2026-10-30（新增 §3.7 恢复后重放 GDPR 擦除）  
> RTO 目标：< 30 分钟  
> RPO 目标：< 1 小时（WAL 归档间隔）  
> 适用范围：agent-platform + saas-foundation（跨两仓库）

---

## 1. 数据存储拓扑

### 1.1 数据源总表

| 数据源 | 容器 | 逻辑库/桶/DB | 写入方 | 内容摘要 | 是否 PII | 保留期 | RTO 优先级 |
|---|---|---|---|---|---|---|---|
| **Postgres** `foundation` | `foundation-postgres` | DB: `foundation` | 全部服务 | 身份/权限/账单/审计 outbox/会话/技能/知识/MCP 注册 | **是**（用户、租户、发票） | 依资源类型 7y（发票）～180d（用户） | **P0—最先恢复** |
| **Postgres** `keycloak` | `foundation-postgres` | DB: `keycloak` | Keycloak | IdP 用户/客户端/realm 配置 | **是**（账号） | 跟随 identity.user | P0（依赖登录） |
| **Postgres** `unleash` | `foundation-postgres` | DB: `unleash` | Unleash | Feature flag | 否 | 90 天 | P2 |
| **MinIO** agent-platform | `foundation-minio` | 桶: `agent-platform` | session-orchestrator, skill-store, knowledge-ingest, agent-scheduler | 会话附件、Skill 包、知识文档、沙箱工作区产物 | **部分是**（用户上传文档） | 90 天 | P1 |
| **MinIO** audit-worm | `foundation-minio` | 桶: `foundation-audit-worm` | audit-worker | 审计事件 WORM 归档（Object Lock COMPLIANCE 7y） | **是** | **7 年**（COMPLIANCE 不可改） | P1（合规强制） |
| **MinIO** privacy-ledger | `foundation-minio` | 桶: `agent-platform-privacy-ledger` | bff | GDPR 擦除台账：每次擦除请求一条 JSON（租户、用户 ID、请求时间、发起方，不含被擦除的内容），擦除执行前写入（Object Lock COMPLIANCE） | **是**（仅用户 ID） | 默认 365 天（COMPLIANCE，按条目锁定） | **不还原**；恢复后、开放流量前据此重放擦除（§3.7） |
| **ClickHouse** `audit` | `foundation-clickhouse` | DB: `audit` | audit-worker（Postgres outbox → ClickHouse） | 审计事件热查询副本（可从 Postgres outbox 重建） | **是**（操作日志） | 7 年 | P2（可重建） |
| **Redis** DB 0 | `foundation-redis` | DB 0 | identity-service, agent sandbox | Session token 缓存、agent run 热状态（plan/step/cursor stream） | 缓存 PII token（短生命周期） | 无；TTL 控制 | **不备份**（见 §1.2） |
| **Vault** | `foundation-vault` | transit secrets engine | crypto-service | KEK（备份 KEK + 业务 KEK）、Vault 配置 | **关键基础设施** | 无限 | P0（KEK 丢失=数据丢失） |
| **K8s 资源 + PV** | 生产 K8s | 所有 namespace | Helm/GitOps | 应用配置、Secrets、PV 数据卷 | **是** | 同 namespace 内容 | P1 |

### 1.2 Redis 备份决策

Redis 仅作**缓存 + 临时热状态**使用，不备份，理由：

1. Session token：TTL < 24h；Keycloak 是权威源，登录后自动重建。
2. Agent run Stream（`run:{run_id}:events`）：已完成的 run 事件 SSE 已推送到前端，且持久化于 Postgres `session.message`；进行中的 run 重连后 session-orchestrator 通过 `reconcile_run` 重建。
3. Redis `maxmemory-policy allkeys-lru`：显式设计为可丢失。
4. 恢复成本：`docker compose up` 后服务自动重建所需 key；无需人工干预。

---

## 2. 备份方案

### 2.1 Postgres — pgBackRest（生产）/ pg_dump（本地/dev）

#### 生产方案（medium profile，K8s 部署）

```
pgBackRest stanza: agent-platform
  全量备份:   每周日 02:00 UTC (cron: 0 2 * * 0)
  增量备份:   每小时 :05 (cron: 5 * * * *)
  WAL 归档:   实时（wal_level=replica, archive_mode=on）
  目标存储:   s3://agent-platform-backup/pgbackrest/
  加密:       --cipher-type=aes-256-cbc（pgBackRest 内置 + 独立 KEK）
  保留:       全量 4 次（约 4 周）; 增量 7 天; WAL 7 天
  PITR 窗口:  7 天内任意时间点
  地理约束:   主桶与异地副本都在 EU/EEA（例：eu-central-1 + eu-west-1）；放在 EU/EEA 之外的区域属于 GDPR 第五章跨境传输，需要有充分性认定或 SCC 并记入 ROPA
```

关键 pgBackRest 配置（`/etc/pgbackrest/pgbackrest.conf`）：

```ini
[global]
repo1-path=/var/lib/pgbackrest
repo1-s3-bucket=agent-platform-backup
repo1-s3-endpoint=s3.eu-central-1.amazonaws.com
repo1-s3-region=eu-central-1
repo1-s3-key=<PGBACKREST_S3_KEY>
repo1-s3-key-secret=<PGBACKREST_S3_SECRET>
repo1-type=s3
repo1-cipher-type=aes-256-cbc
repo1-cipher-pass=${PGBACKREST_CIPHER_PASS}
repo1-retention-full=4
repo1-retention-diff=7
start-fast=y
log-level-console=info

[agent-platform]
pg1-path=/var/lib/postgresql/data
pg1-host=postgres
pg1-host-user=postgres
```

执行备份：

```bash
# 全量
pgbackrest --stanza=agent-platform backup --type=full
# 增量
pgbackrest --stanza=agent-platform backup --type=incr
# 检查备份信息
pgbackrest --stanza=agent-platform info
```

#### dev/本地方案（pg_dump，备份脚本见 §5）

```bash
pg_dump -h localhost -U postgres -Fc foundation > foundation_$(date +%Y%m%d_%H%M%S).dump
pg_dump -h localhost -U postgres -Fc keycloak   > keycloak_$(date +%Y%m%d_%H%M%S).dump
```

### 2.2 MinIO/S3 — 对象存储复制

```
普通桶 (agent-platform):
  方案:  mc mirror + 桶版本控制
  频率:  实时（mc mirror --watch 后台守护）
  目标:  s3://agent-platform-backup/minio-mirror/agent-platform/
  加密:  桶级 SSE-KMS（独立 backup-bucket-kek）
  保留:  noncurrent version 90 天后 lifecycle 删除

WORM 桶 (foundation-audit-worm):
  方案:  Object Lock COMPLIANCE 7y 自身就是不可变副本
         额外：mc mirror 到只读目标桶（不覆盖；Object Lock 阻止覆写）
         注意：不能直接删除/覆盖对象，备份只是额外读取副本
  读取副本目标: s3://agent-platform-backup-worm/audit-worm-replica/
  加密:  同 WORM 桶 SSE；副本桶也开 Object Lock COMPLIANCE

擦除台账桶 (agent-platform-privacy-ledger，GDPR Art. 17):
  作用:  bff 每次执行擦除前先写一条记录；恢复备份后据此重放擦除（§3.7）
  方案:  Object Lock COMPLIANCE，bff 按条目设保留期（BFF_PRIVACY_LEDGER_RETENTION_DAYS，默认 365 天）
         建桶时必须开锁（mc mb --with-lock），事后无法开启；写不进台账时擦除直接拒绝（HTTP 503，什么都不删）
         额外：mc mirror 到只读副本桶（副本桶同样开 Object Lock）
  副本:  s3://agent-platform-backup-worm/privacy-ledger-replica/
  恢复:  永不从备份还原覆写——它记录的正是备份之后发生的擦除；只有 MinIO 整体丢失时才从副本取回
  保留:  须长于任何仍可能被恢复的备份（当前最长：MinIO noncurrent 90 天）；到期后可由 ILM 删除
  权限:  生产给 bff 单独的访问密钥，只授予该桶的 s3:PutObject / s3:GetObject / s3:ListBucket
```

```bash
# 设置 alias
mc alias set prod http://minio:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD
mc alias set backup s3://... $BACKUP_ACCESS_KEY $BACKUP_SECRET_KEY

# 实时镜像（后台运行）
mc mirror --watch prod/agent-platform backup/agent-platform-backup/minio-mirror/agent-platform
mc mirror --watch prod/agent-platform-privacy-ledger backup/agent-platform-backup-worm/privacy-ledger-replica

# 一次性全量同步
mc mirror prod/agent-platform backup/agent-platform-backup/minio-mirror/agent-platform
mc mirror prod/foundation-audit-worm backup/agent-platform-backup-worm/audit-worm-replica
mc mirror prod/agent-platform-privacy-ledger backup/agent-platform-backup-worm/privacy-ledger-replica
```

### 2.3 ClickHouse — clickhouse-backup

```
工具:     Altinity/clickhouse-backup
频率:     每日 03:00 UTC (cron: 0 3 * * *)
目标:     s3://agent-platform-backup/clickhouse/
加密:     clickhouse-backup 内置 AES-256（独立 KEK）
保留:     7 天（与 Postgres 增量对齐）
注意:     ClickHouse audit 数据可从 Postgres outbox 重建（见 §3.3）
          因此 ClickHouse 备份为辅助；Postgres 是权威源
```

```bash
# 创建备份
clickhouse-backup create --config /etc/clickhouse-backup/config.yaml
# 上传到 S3
clickhouse-backup upload --config /etc/clickhouse-backup/config.yaml <backup-name>
# 列出备份
clickhouse-backup list --config /etc/clickhouse-backup/config.yaml
```

`config.yaml` 关键配置：

```yaml
clickhouse:
  host: clickhouse
  port: 9000
  username: foundation
  password: ${CLICKHOUSE_PASSWORD}
  database: audit

s3:
  bucket: agent-platform-backup
  path: clickhouse/
  region: eu-central-1
  access_key: ${BACKUP_S3_KEY}
  secret_key: ${BACKUP_S3_SECRET}
  compression_format: gz
  compression_level: 1
  encryption_key: ${CLICKHOUSE_BACKUP_ENCRYPTION_KEY}

general:
  backups_to_keep_local: 2
  backups_to_keep_remote: 7
```

### 2.4 Vault — Snapshot

```
工具:    vault operator raft snapshot（生产 raft 后端）
         或 vault snapshot save（dev 模式）
频率:    每日 01:00 UTC
目标:    s3://agent-platform-backup/vault/
加密:    Vault 自签（snapshot 本身加密）
保留:    30 天
```

```bash
# 保存 snapshot
VAULT_ADDR=http://vault:8200 VAULT_TOKEN=${VAULT_TOKEN} \
  vault operator raft snapshot save /tmp/vault_$(date +%Y%m%d).snap

# 上传
aws s3 cp /tmp/vault_$(date +%Y%m%d).snap s3://agent-platform-backup/vault/
```

### 2.5 K8s 资源 + PV — Velero（生产）

```
工具:    Velero + AWS/CSI plugin
频率:    每日 04:00 UTC（所有 namespace；PV snapshot 开启）
目标:    s3://agent-platform-backup/velero/
加密:    CSI volume snapshot 加密 + Velero restic 加密
保留:    7 天（PV snapshot）; K8s 清单 30 天
```

```bash
# 安装 Velero（一次性，Helm）
velero install \
  --provider aws \
  --plugins velero/velero-plugin-for-aws:v1.10 \
  --bucket agent-platform-backup \
  --prefix velero/ \
  --backup-location-config region=eu-central-1 \
  --snapshot-location-config region=eu-central-1 \
  --use-node-agent \
  --secret-file ./velero-credentials

# 创建定时备份计划
velero schedule create daily-full \
  --schedule="0 4 * * *" \
  --ttl 168h \
  --include-namespaces agent-platform,saas-foundation \
  --snapshot-volumes=true

# 手动触发一次备份
velero backup create manual-$(date +%Y%m%d) --from-schedule daily-full

# 查看备份状态
velero backup get
velero backup describe manual-$(date +%Y%m%d) --details
```

### 2.6 保留期 / 加密 / 地理约束汇总

| 数据源 | 备份保留期 | 加密方式 | 地理约束（EU/EEA） |
|---|---|---|---|
| Postgres（业务数据） | 全量 4 周；WAL 7 天 | pgBackRest AES-256 + 独立备份 KEK | 桶在 EU/EEA 区域（例：eu-central-1） |
| MinIO agent-platform | 90 天（noncurrent） | SSE-KMS 独立 KEK | 副本桶同区域 |
| MinIO audit-worm | 7 年（COMPLIANCE Object Lock） | SSE-KMS | 副本桶同区域；不可删除 |
| MinIO privacy-ledger | 默认 365 天（COMPLIANCE Object Lock，按条目） | 桶级 SSE（同 WORM 桶） | 副本桶同区域；保留期内不可删除 |
| ClickHouse | 7 天 | AES-256 独立 KEK | 同 Postgres 桶区域 |
| Vault | 30 天 | Vault 自签 | 同区域 |
| K8s/PV（Velero） | 7 天 PV；30 天清单 | CSI + restic | 同区域 snapshot |

**KEK 独立原则**：备份 KEK（`backup-encryption-<env>`）与业务 KEK 完全隔离，存于 Vault transit 引擎。业务 KEK 销毁后，备份内对应字段密文同时失效（双保护），但备份本身的外层加密 KEK 不受影响（确保可解密外壳以执行销毁流程）。

---

## 3. 恢复流程（RTO < 30 分钟目标）

### 3.1 依赖顺序（必须按此顺序恢复）

```
1. Vault（KEK 权威源；其他一切依赖）
2. Postgres（所有服务的数据后端）
3. alembic migration（确保 schema 与代码一致）
4. MinIO/S3（附件/知识文档；擦除台账桶不还原，见 §3.7）
5. ClickHouse（审计查询；可选—可重建）
6. 应用服务（foundation 先，agent-platform 后；bff 先不启动，它是用户流量入口）
7. 重放恢复点之后的 GDPR 擦除（§3.7）——exit 0 之后才启动 bff、开放流量
8. Redis 缓存自动重建（无需干预）
```

### 3.2 场景 A：单库 PITR（部分数据损坏）

**目标 RTO：< 15 分钟**

```bash
# 1. 确定目标恢复时间点（UTC）
RECOVERY_TARGET="2026-06-05 10:30:00+00"
# 擦除重放起点（§3.7）：恢复点再提前 10 分钟，带时区
REPLAY_SINCE="2026-06-05T10:20:00Z"

# 2. 停止所有写入服务（防止脑裂）
docker compose -f agent-platform.yml stop session-orchestrator bff agent-scheduler
cd ../saas-foundation/deploy/docker-compose
docker compose -f dev.yml stop identity authz org billing audit audit-worker crypto

# 3. 停止 Postgres
docker compose -f dev.yml stop postgres pgbouncer

# 4. pgBackRest PITR 恢复到新实例（不覆盖生产！先到 staging 验证）
# [生产命令—需真实 pgBackRest 环境]
pgbackrest --stanza=agent-platform restore \
  --target="${RECOVERY_TARGET}" \
  --target-action=promote \
  --target-exclusive \
  --pg1-path=/var/lib/postgresql/data-restore
# 等待 promote 完成（通常 < 2 分钟）

# 5. 验证恢复结果（见 §3.5）
# 6. 重启服务——bff 先不启动（它是用户流量入口，cron-runner 也经它触发定时任务）
docker compose -f dev.yml up -d postgres pgbouncer
docker compose -f dev.yml up -d identity authz org billing crypto audit
cd ../../agent-platform/deploy/docker-compose
docker compose -f agent-platform.yml up -d session-orchestrator agent-scheduler

# 7. 检查 alembic 版本（恢复后确认 schema 版本）—见 §3.6
docker compose -f agent-platform.yml run --rm bff-migrate

# 8. 重放恢复点之后的 GDPR 擦除（见 §3.7）；必须 exit 0 才能继续
docker compose -f agent-platform.yml run --rm --no-deps -T --name agent-bff-erasure-replay bff \
  python -m agent_bff.erasure_replay --since "${REPLAY_SINCE}" --actor "usr_<执行人用户ID>" \
  > erasure-replay-report.json
echo "exit=$?"

# 9. 启动 bff；验证通过后切换流量
docker compose -f agent-platform.yml up -d bff
```

**时间预算：**

| 步骤 | 预计耗时 |
|---|---|
| 停止服务 | 1 分钟 |
| pgBackRest restore（增量基础 + WAL replay） | 5-8 分钟 |
| schema 版本校验 + migration | 2 分钟 |
| 启动服务 + 健康检查 | 3 分钟 |
| GDPR 擦除重放（视待重放用户数） | 1-2 分钟 |
| 人工验证 | 2 分钟 |
| **合计** | **≈ 14-18 分钟** |

### 3.3 场景 B：整站灾难恢复

**目标 RTO：< 30 分钟**

```bash
# 前提：已有新环境（新 K8s namespace 或新 compose 环境）

# ---- 步骤 1：恢复 Vault（~2 分钟）----
vault operator raft snapshot restore /path/to/vault_latest.snap
# 验证
VAULT_ADDR=http://vault:8200 VAULT_TOKEN=<root> vault secrets list

# ---- 步骤 2：恢复 Postgres（~10 分钟）----
# 方法 A：pgBackRest（生产）
pgbackrest --stanza=agent-platform restore \
  --target-action=promote \
  --pg1-path=/var/lib/postgresql/data

# 方法 B：pg_dump restore（dev/staging）
createdb -h localhost -U postgres foundation
pg_restore -h localhost -U postgres -d foundation foundation_latest.dump
createdb -h localhost -U postgres keycloak
pg_restore -h localhost -U postgres -d keycloak keycloak_latest.dump

# ---- 步骤 3：运行 alembic migration（~2 分钟）----
# 见 §3.6

# ---- 步骤 4：恢复 MinIO（~3 分钟）----
mc alias set backup s3://agent-platform-backup $KEY $SECRET
mc alias set local http://minio:9000 $MINIO_USER $MINIO_PASS
mc mirror backup/minio-mirror/agent-platform local/agent-platform
# WORM 桶：副本桶已有 Object Lock，直接映射（不还原覆写）
# 擦除台账桶 agent-platform-privacy-ledger：不还原覆写（§3.7）。
# 只有 MinIO 整体丢失、台账桶已不存在时才从副本取回——必须在步骤 7 之前（minio-init 会建一个空桶）：
#   mc alias set backup-worm s3://agent-platform-backup-worm $KEY $SECRET
#   mc mb --with-lock local/agent-platform-privacy-ledger
#   mc mirror backup-worm/privacy-ledger-replica local/agent-platform-privacy-ledger

# ---- 步骤 5：恢复 ClickHouse（可选，~3 分钟，可跳过用重建）----
# 方法 A：clickhouse-backup restore
clickhouse-backup download <backup-name>
clickhouse-backup restore <backup-name>
# 方法 B：从 Postgres outbox 重建（适合 < 7 天数据丢失）
# audit-worker 重启后会自动处理 outbox 中 status='PENDING' 的事件

# ---- 步骤 6：启动 foundation 服务（~3 分钟）----
cd /path/to/saas-foundation/deploy/docker-compose
docker compose -f dev.yml up -d postgres pgbouncer vault redis minio keycloak
docker compose -f dev.yml up -d identity authz org billing crypto audit audit-worker

# ---- 步骤 7：启动 agent-platform 服务，bff / web 先不启动（~2 分钟）----
# bff 是用户流量入口，cron-runner 也经它触发定时任务：擦除重放完成前都不能起
cd /path/to/agent-platform/deploy/docker-compose
docker compose -f agent-platform.yml up -d session-orchestrator agent-scheduler knowledge-ingest
docker compose -f agent-platform.yml run --rm bff-migrate
docker compose -f agent-platform.yml run --rm cron-runner-migrate
docker compose -f agent-platform.yml up -d --no-deps cron-runner   # --no-deps：不连带拉起 bff

# ---- 步骤 8：重放恢复点之后的 GDPR 擦除（~1-2 分钟，见 §3.7）；必须 exit 0 才能继续 ----
docker compose -f agent-platform.yml run --rm --no-deps -T --name agent-bff-erasure-replay bff \
  python -m agent_bff.erasure_replay --since "<恢复点前 10 分钟，带时区>" --actor "usr_<执行人用户ID>" \
  > erasure-replay-report.json
echo "exit=$?"

# ---- 步骤 9：启动其余服务（含 bff / web）----
docker compose -f agent-platform.yml up -d

# ---- 步骤 10：运行恢复验证（~2 分钟）----
bash /path/to/deploy/backup/verify-restore.sh

# ---- 步骤 11：Redis 缓存自动重建 ----
# 无需操作：identity-service 重启后重建 session token；
# agent run 状态由 session-orchestrator 通过 reconcile_run 从 Postgres 重建
```

**时间预算（整站灾难恢复）：**

| 步骤 | 预计耗时 |
|---|---|
| Vault 恢复 | 2 分钟 |
| Postgres 恢复（pgBackRest full restore） | 8-10 分钟 |
| alembic migration | 2 分钟 |
| MinIO 恢复（视数据量，假设 < 10GB） | 3-5 分钟 |
| ClickHouse 恢复（可选）或跳过 | 0-3 分钟 |
| 服务启动 + 健康检查 | 3-4 分钟 |
| GDPR 擦除重放（视待重放用户数） | 1-2 分钟 |
| 验证脚本 | 2 分钟 |
| **合计** | **≈ 21-30 分钟** |

> 如 Postgres 数据量大（> 100GB），pgBackRest restore 可能超时，需提前评估 —— 增加 I/O 并行度（`--process-max=4`）可显著提速。

### 3.4 场景 C：误删恢复

```bash
# Postgres 行误删：PITR 到误删操作前 1 秒
# 1. 确认误删时间戳（从 audit 日志查）
psql -h localhost -U postgres -d foundation \
  -c "SELECT created_at, actor, event_type, resource_id FROM audit.event_outbox
      WHERE resource_type = 'session.session' ORDER BY created_at DESC LIMIT 20;"

# 2. 恢复到隔离库
pgbackrest --stanza=agent-platform restore \
  --target="2026-06-05 11:29:59+00" \
  --target-action=promote \
  --pg1-path=/var/lib/postgresql/data-isolated

# 3. 从隔离库复制目标行
pg_dump -h localhost-isolated -U postgres -d foundation \
  -t 'session.session' \
  --where="id = 'ses_01J5...'" \
  -Fc > recovered_rows.dump
pg_restore -h localhost -U postgres -d foundation recovered_rows.dump

# MinIO 对象误删：从版本控制恢复
mc ls --versions prod/agent-platform/path/to/object
mc cp --version-id <version-id> prod/agent-platform/path/to/object prod/agent-platform/path/to/object
```

### 3.5 恢复后数据一致性校验

参见 `deploy/backup/verify-restore.sh`（§5.3）。手动校验要点：

```sql
-- 关键表行数校验（与备份前快照对比）
SELECT schemaname, tablename, n_live_tup
FROM pg_stat_user_tables
WHERE schemaname IN ('session_orchestrator','identity','billing','authz')
ORDER BY schemaname, tablename;

-- session 最新数据点
SELECT MAX(created_at) FROM session_orchestrator.session;
SELECT MAX(created_at) FROM session_orchestrator.message;

-- identity 用户数
SELECT tenant_id, COUNT(*) FROM identity.user GROUP BY tenant_id;

-- audit outbox 积压（>0 说明 audit-worker 尚未处理完）
SELECT COUNT(*) FROM audit.outbox WHERE status = 'PENDING';
```

### 3.6 恢复后 alembic Migration

恢复后**必须**检查 schema 版本，确保与当前代码一致：

```bash
# 检查 foundation 各服务 migration 版本
for svc in identity authz org billing crypto audit; do
  echo "=== $svc ==="
  psql -h localhost -U postgres -d foundation \
    -c "SELECT version_num FROM alembic_version_${svc} 2>/dev/null
        UNION ALL
        SELECT version_num FROM alembic_version 2>/dev/null LIMIT 1;"
done

# 检查 agent-platform 服务版本
psql -h localhost -U postgres -d foundation \
  -c "SELECT version_num FROM alembic_version;"

# 如果版本落后于代码，跑 migration（以 session-orchestrator 为例）
cd /path/to/agent-platform/services/session-orchestrator
SESSION_ORCHESTRATOR_DB_DSN_MIGRATOR=postgresql+asyncpg://app_migrator:xxx@localhost:5432/foundation \
  python -m alembic upgrade head

# foundation 各服务 migration（在 saas-foundation migration-runner 容器中执行）
docker compose -f dev.yml run --rm migration-runner
```

### 3.7 恢复后重放 GDPR 擦除（Art. 17）

**为什么**：备份里仍留着备份之后才被擦除的用户的数据，恢复备份就把它们带了回来；按 GDPR 第 17 条，擦除不能因为恢复备份而失效。每个擦除请求（用户自助 `POST /me/privacy/erase`、管理员 `POST /admin/privacy/users/{user_id}/erase`）在删除任何数据之前，都会先写入擦除台账桶 `agent-platform-privacy-ledger`（§2.2）。这个桶不在任何恢复范围内；恢复后从台账取出恢复点之后的擦除，重新执行一遍。

> 为什么不用审计日志当台账：foundation-audit 的事件虽然能按类型和时间查询，但 ① 只能按单个租户查；② 它的 Postgres outbox 与 ClickHouse 都在恢复范围内，会随恢复一起回退——丢掉的恰好是需要重放的那一段；③ 隐私事件级别是 NOTICE，不进 WORM 桶；④ 写审计是 best-effort，失败不阻断擦除。

**时机**：所有数据源恢复完成、migration 跑完（§3.6），foundation 服务（含 llm-gateway）与 session-orchestrator / agent-scheduler / knowledge-ingest / cron-runner / app-registry 已启动之后；**bff 启动、对外开放流量之前**。

**确定 `--since`**：

- 恢复点：场景 A 为 PITR 目标时间（`RECOVERY_TARGET`）；场景 B 取恢复后库中最新的数据点（§3.5 的 `SELECT MAX(created_at) FROM session_orchestrator.message;`）。多个数据源恢复到不同时间点时，取最早的那个。
- `--since` = 恢复点**再提前 10 分钟**，必须带时区（如 `2026-06-05T10:20:00Z`）。台账在删除之前写入，一次擦除从写台账到各服务删完最长要几分钟，再加上主机时钟偏差：台账时间略早于恢复点、删除却提交在恢复点之后的擦除，也必须重放。
- 不要无故提前太多：重放会删除该用户在恢复数据里的**全部**数据，包括他提出擦除之后又新产生的数据。

**命令**（docker compose，在 `agent-platform/deploy/docker-compose` 下执行；用 bff 镜像起一次性容器，读取 bff 自己的配置）：

```bash
# 1) 预览：列出要重放的用户，不删除任何数据
docker compose -f agent-platform.yml run --rm --no-deps -T --name agent-bff-erasure-replay bff \
  python -m agent_bff.erasure_replay --since "2026-06-05T10:20:00Z" --actor "usr_<执行人用户ID>" --dry-run

# 2) 正式重放；JSON 报告随恢复记录一起存档
docker compose -f agent-platform.yml run --rm --no-deps -T --name agent-bff-erasure-replay bff \
  python -m agent_bff.erasure_replay --since "2026-06-05T10:20:00Z" --actor "usr_<执行人用户ID>" \
  > erasure-replay-report.json
echo "exit=$?"
```

K8s：Ingress 恢复对外之前，在 bff Pod 里执行同样的命令：

```bash
kubectl -n agent-platform exec deploy/ap-bff -- \
  python -m agent_bff.erasure_replay --since "2026-06-05T10:20:00Z" --actor "usr_<执行人用户ID>"
```

- `--actor`：执行恢复的超级管理员自己的平台用户 ID（`usr_` 开头），记入审计。
- 对台账里的每个用户，重放执行与管理员擦除相同的流程，以 `X-Actor: ADMIN` 进入该用户所在的租户：先做一遍不改任何数据的预检（各服务的 dry run / 列表），全部通过后再按顺序删除（定时任务 → 会话 / 工作区 / 文件 → 记忆 → LLM 模型偏好 → App → bff 本地数据）。同一用户在台账里出现多次只擦一次；每个用户写一条审计事件 `admin.privacy.user.erasure_replayed`（只含计数）。
- App：只有作者自己能用的 App（私有，或从未发布）整个删除；已共享给租户的 App 保留，作者和审核记录里的该用户改为 `erased-user`。
- 每一步都是幂等的，重复执行无害。
- 预览时检查：报告里 `ledger_entries` 为 0 时，先确认台账桶是原来的桶（或已从副本取回），而不是 minio-init 刚建的空桶——空台账会让重放"成功"，却什么都没擦。

**退出码**：

| 退出码 | 含义 | 处理 |
|---|---|---|
| 0 | 台账里每个用户都已重新擦除（`--dry-run`：没有删除任何数据） | 启动 bff，开放流量 |
| 1 | 至少一个用户没擦完：报告里该用户 `status` 为 `incomplete`，`failed` 是失败的步骤，`error` 是原因；带 `"phase": "preflight"` 的是预检没通过，该用户什么都没删 | 修好对应服务后用**同样的参数**重跑；exit 0 之前不得开放流量 |
| 2 | 参数错误、未配置台账（`BFF_PRIVACY_LEDGER_S3_BUCKET` 为空）或台账读不了 | 没有删除任何数据；修好后重跑 |

**已知缺口**（重放覆盖不到，需另行处理）：

- **登录账号本身**：擦除不删 Keycloak / identity 账号（由管理员另行关闭）；恢复 keycloak 库后，已关闭的账号可能重新出现，需按管理员的关闭记录复核。
- **MinIO 镜像里的旧对象**：`mc mirror` 不带 `--remove`，副本保留了源桶里已删除的对象。整站恢复（§3.3 步骤 4）会把恢复点**之前**就已擦除的用户文件作为无引用对象带回来；擦除按数据库行的引用删除文件，重放删不到它们。需要调整镜像策略（跟进项）。
- **未配置台账期间的擦除**：`BFF_PRIVACY_LEDGER_S3_BUCKET` 为空时擦除照常执行但不记台账（bff 记 warning 日志，审计 payload 带 `"ledger": "not_configured"`），这些擦除无法重放。

---

## 4. RPO 配置

**WAL 归档配置（Postgres）：**

```sql
-- 在 postgresql.conf 中设置
wal_level = replica
archive_mode = on
archive_command = 'pgbackrest --stanza=agent-platform archive-push %p'
archive_timeout = 300  -- 5 分钟：即使无事务也归档 WAL，保证 RPO < 5 分钟
```

**RPO 验证：**

```bash
# 查看最近一次 WAL 归档时间
pgbackrest --stanza=agent-platform info --output=json | \
  jq '.[] | .archive | .last'

# 预期：距当前 < 5 分钟（archive_timeout=300s）
```

**各数据源 RPO：**

| 数据源 | RPO | 说明 |
|---|---|---|
| Postgres | < 5 分钟 | WAL 归档 archive_timeout=300s |
| MinIO | 近实时 | mc mirror --watch |
| ClickHouse | < 24 小时 | 日备；可从 PG outbox 补 |
| Vault | < 24 小时 | 日备；KEK 在内存中连续服务 |
| K8s PV | < 24 小时 | Velero 日快照 |

---

## 5. 演练机制（M3 起每月自动）

### 5.1 演练目标

1. 验证备份可恢复（非仅备份成功）
2. 验证 RTO < 30 分钟
3. 验证恢复后数据一致性
4. 结果写入 audit 归档（`lifecycle.backup_run.status = VERIFIED`）

### 5.2 演练步骤

```
每月第一个周一 05:00 UTC（低峰）：
  1. 触发：POST /internal/dr-drill（lifecycle-service 端点）
     或手动：bash deploy/backup/restore.sh --target=isolated
  2. 恢复最新全量备份到 isolated 环境
     （独立 Docker network + 独立 DB 实例，不影响生产）
  3. 运行 verify-restore.sh
  4. 记录结果：
     - 成功 → 写 lifecycle.backup_run (status=VERIFIED) + audit event
     - 失败 → P0 告警 + 人工介入
  5. 销毁 isolated 环境
  6. 生成月度演练报告（PDF）存入 audit-worm 桶
```

### 5.3 演练验证内容

```bash
# verify-restore.sh 执行（详见 deploy/backup/verify-restore.sh）
1. Postgres 健康检查（连接 + pg_isready）
2. 关键表存在性（session.session, identity.user, billing.quota_usage）
3. 行数与基线对比（sentinel_counts.json 预先快照）
4. 最新数据点时间（MAX created_at 距备份时间 < RPO）
5. API 健康端点（/healthz 全绿）
6. 登录流程（能拿到 token）
7. Session 列表（能查到数据）
8. RLS 隔离（A 租户不可见 B 租户数据）
```

### 5.4 演练结果归档

```json
{
  "drill_id": "drill_2026-07-07",
  "started_at": "2026-07-07T05:00:00Z",
  "completed_at": "2026-07-07T05:21:37Z",
  "rto_minutes": 21.6,
  "rto_target_minutes": 30,
  "rto_passed": true,
  "checks": {
    "postgres_healthy": true,
    "critical_tables_exist": true,
    "row_counts_match": true,
    "api_healthz_all_green": true,
    "login_flow": true,
    "rls_isolation": true
  },
  "backup_run_id": "bkp_run_20260706_full",
  "outcome": "VERIFIED"
}
```

---

## 6. 脚本清单

| 脚本 | 位置 | 说明 |
|---|---|---|
| `backup.sh` | `deploy/backup/backup.sh` | 本地栈一键备份（pg_dump + mc mirror）；生产见注释 |
| `restore.sh` | `deploy/backup/restore.sh` | 从备份恢复到测试库；含 migration + 健康检查 |
| `verify-restore.sh` | `deploy/backup/verify-restore.sh` | 恢复后一致性校验 |
| `erasure_replay` | `python -m agent_bff.erasure_replay`（bff 镜像内；源码 `services/bff/src/agent_bff/erasure_replay.py`） | 恢复后重放恢复点之后的 GDPR 擦除（§3.7） |

---

## 7. 应急联系与升级路径

| 场景 | 操作人 | 升级 |
|---|---|---|
| 恢复 > 30 分钟 | On-call SRE | → Platform Lead |
| Vault KEK 丢失 | Platform Lead + Security Lead | → 紧急纸质 escrow |
| WORM 桶数据丢失 | Security Lead | → 合规负责人 + 律师 |
| 擦除重放 exit ≠ 0 或擦除台账丢失 | On-call SRE | → DPO（数据保护官）；exit 0 之前不开放流量 |
| 演练失败 | On-call SRE | P0，当周修复 |

---

## 8. 参考资料

- [pgBackRest 文档](https://pgbackrest.org/user-guide.html)
- [Altinity clickhouse-backup](https://github.com/Altinity/clickhouse-backup)
- [Velero 文档](https://velero.io/docs/)
- [MinIO mc mirror](https://min.io/docs/minio/linux/reference/minio-mc/mc-mirror.html)
- [saas-foundation lifecycle-v1.md](../../../saas-foundation/docs/design/lifecycle-v1.md)
- [saas-foundation data-protection-v1.md](../../../saas-foundation/docs/design/data-protection-v1.md)
