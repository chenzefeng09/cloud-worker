#!/usr/bin/env bash
# =============================================================================
# backup.sh — agent-platform + saas-foundation 备份脚本
#
# 本地 docker-compose 栈：pg_dump 各库 + mc mirror MinIO 桶
# 生产环境 (pgBackRest/Velero/clickhouse-backup) 部分以注释 [PROD] 标注
#
# 用法：
#   ./deploy/backup/backup.sh [--target-dir /path/to/backup] [--dry-run]
#
# 环境变量（可通过 .env 或 export 设置）：
#   BACKUP_DIR        备份目标目录（默认 /tmp/agent-platform-backup/YYYYMMDD_HHMMSS）
#   PG_HOST           Postgres 主机（默认 localhost）
#   PG_PORT           Postgres 端口（默认 5432）
#   PG_SUPER_USER     Postgres 超级用户（默认 postgres）
#   PG_SUPER_PASS     Postgres 超级用户密码
#   MINIO_ENDPOINT    MinIO 端点（默认 http://localhost:9001）
#   MINIO_USER        MinIO 用户
#   MINIO_PASS        MinIO 密码
#   DRY_RUN           设为 1 则只打印不执行
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 配置默认值
# ---------------------------------------------------------------------------
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="${BACKUP_DIR:-/tmp/agent-platform-backup/${TIMESTAMP}}"
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_SUPER_USER="${PG_SUPER_USER:-postgres}"
PG_SUPER_PASS="${PG_SUPER_PASS:-postgres_super_change_me}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9001}"
MINIO_USER="${MINIO_USER:-foundation}"
MINIO_PASS="${MINIO_PASS:-minio_change_me_min_12_chars}"
DRY_RUN="${DRY_RUN:-0}"

# 要备份的 Postgres 数据库
PG_DATABASES=("foundation" "keycloak")

# 要备份的 MinIO 桶
MINIO_BUCKETS=("agent-platform" "foundation-audit-worm")

LOG_PREFIX="[backup.sh ${TIMESTAMP}]"

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-dir)
      BACKUP_DIR="$2"; shift 2 ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    *)
      echo "${LOG_PREFIX} 未知参数: $1" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
log() { echo "${LOG_PREFIX} $*"; }
die() { echo "${LOG_PREFIX} ERROR: $*" >&2; exit 1; }

run_cmd() {
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "${LOG_PREFIX} [DRY-RUN] $*"
  else
    "$@"
  fi
}

check_tool() {
  local tool="$1"
  if ! command -v "${tool}" &>/dev/null; then
    log "WARNING: ${tool} 未安装，跳过相关备份步骤"
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
log "=== 开始备份 ==="
log "目标目录: ${BACKUP_DIR}"
[[ "${DRY_RUN}" == "1" ]] && log "[DRY-RUN 模式：只打印命令，不实际执行]"

run_cmd mkdir -p "${BACKUP_DIR}/postgres"
run_cmd mkdir -p "${BACKUP_DIR}/minio"
run_cmd mkdir -p "${BACKUP_DIR}/metadata"

# ---------------------------------------------------------------------------
# 步骤 1：Postgres pg_dump（本地栈可用）
# ---------------------------------------------------------------------------
log "--- 步骤 1: Postgres 备份 ---"

if ! check_tool pg_dump; then
  log "WARNING: pg_dump 未找到。如在 Docker 环境中，请在 postgres 容器内执行或安装 postgresql-client"
  PG_AVAILABLE=0
else
  # 测试连通性
  if PGPASSWORD="${PG_SUPER_PASS}" pg_isready -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_SUPER_USER}" -q 2>/dev/null; then
    PG_AVAILABLE=1
    log "Postgres 连通性 OK (${PG_HOST}:${PG_PORT})"
  else
    log "WARNING: 无法连接 Postgres ${PG_HOST}:${PG_PORT}，跳过 Postgres 备份"
    PG_AVAILABLE=0
  fi
fi

if [[ "${PG_AVAILABLE}" == "1" ]]; then
  for DB in "${PG_DATABASES[@]}"; do
    DUMP_FILE="${BACKUP_DIR}/postgres/${DB}_${TIMESTAMP}.dump"
    log "备份数据库: ${DB} → ${DUMP_FILE}"
    run_cmd bash -c "PGPASSWORD='${PG_SUPER_PASS}' pg_dump \
      -h '${PG_HOST}' \
      -p '${PG_PORT}' \
      -U '${PG_SUPER_USER}' \
      -Fc \
      -Z 6 \
      '${DB}' > '${DUMP_FILE}'"
    # 验证 dump 文件
    if [[ "${DRY_RUN}" != "1" ]]; then
      DUMP_SIZE=$(du -sh "${DUMP_FILE}" 2>/dev/null | cut -f1 || echo "unknown")
      log "  完成: ${DUMP_FILE} (${DUMP_SIZE})"
    fi
  done
  log "Postgres 备份完成"
fi

# [PROD] 生产环境使用 pgBackRest（WAL 归档 + 全量/增量）：
# pgbackrest --stanza=agent-platform backup --type=full
# pgbackrest --stanza=agent-platform backup --type=incr
# pgbackrest --stanza=agent-platform info
# 详见 docs/runbook/backup-restore-b-m6.md §2.1

# ---------------------------------------------------------------------------
# 步骤 2：MinIO 桶备份（本地栈可用）
# ---------------------------------------------------------------------------
log "--- 步骤 2: MinIO 桶备份 ---"

if ! check_tool mc; then
  log "WARNING: mc (MinIO Client) 未安装，跳过 MinIO 备份"
  MINIO_AVAILABLE=0
else
  # 配置 mc alias
  if run_cmd mc alias set local-backup "${MINIO_ENDPOINT}" "${MINIO_USER}" "${MINIO_PASS}" --api S3v4 2>/dev/null; then
    MINIO_AVAILABLE=1
    log "MinIO 连通性 OK (${MINIO_ENDPOINT})"
  else
    log "WARNING: 无法连接 MinIO ${MINIO_ENDPOINT}，跳过 MinIO 备份"
    MINIO_AVAILABLE=0
  fi
fi

if [[ "${MINIO_AVAILABLE}" == "1" ]]; then
  # 列出桶（用于验证连通性和记录现状）
  log "当前 MinIO 桶列表："
  run_cmd mc ls local-backup/ || true

  for BUCKET in "${MINIO_BUCKETS[@]}"; do
    BUCKET_BACKUP_DIR="${BACKUP_DIR}/minio/${BUCKET}"
    log "镜像桶: ${BUCKET} → ${BUCKET_BACKUP_DIR}"

    # 检查桶是否存在
    if run_cmd mc ls "local-backup/${BUCKET}" &>/dev/null; then
      run_cmd mkdir -p "${BUCKET_BACKUP_DIR}"
      # mc mirror：本地镜像（--overwrite 确保增量更新）
      run_cmd mc mirror \
        --overwrite \
        "local-backup/${BUCKET}" \
        "${BUCKET_BACKUP_DIR}/"
      if [[ "${DRY_RUN}" != "1" ]]; then
        BUCKET_SIZE=$(du -sh "${BUCKET_BACKUP_DIR}" 2>/dev/null | cut -f1 || echo "unknown")
        log "  完成: ${BUCKET_BACKUP_DIR} (${BUCKET_SIZE})"
      fi
    else
      log "  桶 ${BUCKET} 不存在，跳过"
    fi
  done
  log "MinIO 备份完成"
fi

# [PROD] 生产环境使用 mc mirror 到 S3 远端，实时同步：
# mc alias set backup s3://agent-platform-backup $BACKUP_S3_KEY $BACKUP_S3_SECRET
# mc mirror --watch local-backup/agent-platform backup/agent-platform-backup/minio-mirror/agent-platform
# WORM 桶：mc mirror local-backup/foundation-audit-worm backup/agent-platform-backup-worm/audit-worm-replica
# 详见 docs/runbook/backup-restore-b-m6.md §2.2

# ---------------------------------------------------------------------------
# 步骤 3：ClickHouse 备份（[PROD] 占位）
# ---------------------------------------------------------------------------
log "--- 步骤 3: ClickHouse 备份 ---"
# [PROD] 生产使用 clickhouse-backup：
# clickhouse-backup create --config /etc/clickhouse-backup/config.yaml
# clickhouse-backup upload --config /etc/clickhouse-backup/config.yaml <backup-name>
#
# 注：ClickHouse audit DB 可从 Postgres outbox 重建（audit-worker 重处理）
# 因此本地备份中跳过 ClickHouse，降低复杂度。
log "  [PROD 占位] ClickHouse: 生产使用 clickhouse-backup + S3。本地跳过（可从 Postgres outbox 重建）"

# ---------------------------------------------------------------------------
# 步骤 4：Vault 快照（[PROD] 占位）
# ---------------------------------------------------------------------------
log "--- 步骤 4: Vault 快照 ---"
VAULT_ADDR="${VAULT_ADDR:-http://localhost:8200}"
VAULT_TOKEN="${VAULT_TOKEN:-dev-root-token-change-me}"

if check_tool vault; then
  VAULT_SNAP_FILE="${BACKUP_DIR}/vault_${TIMESTAMP}.snap"
  log "Vault 快照 → ${VAULT_SNAP_FILE}"
  # dev 模式支持 vault operator raft snapshot save
  if VAULT_ADDR="${VAULT_ADDR}" VAULT_TOKEN="${VAULT_TOKEN}" \
     run_cmd vault operator raft snapshot save "${VAULT_SNAP_FILE}" 2>/dev/null; then
    log "  Vault 快照完成"
  else
    # dev 模式不支持 raft，尝试导出 secrets 列表（不导出 secret 值）
    log "  Vault dev 模式：导出 secrets 引擎列表（不含 KEK 明文）"
    run_cmd bash -c "VAULT_ADDR='${VAULT_ADDR}' VAULT_TOKEN='${VAULT_TOKEN}' \
      vault secrets list -format=json 2>/dev/null > '${BACKUP_DIR}/vault_secrets_list.json'" || true
  fi
else
  log "  vault CLI 未安装，跳过 Vault 快照"
fi

# [PROD] 生产使用：
# vault operator raft snapshot save /tmp/vault_$(date +%Y%m%d).snap
# aws s3 cp /tmp/vault_$(date +%Y%m%d).snap s3://agent-platform-backup/vault/
# 详见 docs/runbook/backup-restore-b-m6.md §2.4

# ---------------------------------------------------------------------------
# 步骤 5：K8s Velero（[PROD] 占位）
# ---------------------------------------------------------------------------
log "--- 步骤 5: K8s/Velero 备份 ---"
# [PROD] 生产使用：
# velero backup create manual-$(date +%Y%m%d) --from-schedule daily-full
# velero backup describe manual-$(date +%Y%m%d) --details
# 详见 docs/runbook/backup-restore-b-m6.md §2.5
log "  [PROD 占位] Velero: 生产 K8s 部署使用 Velero。本地 docker-compose 跳过"

# ---------------------------------------------------------------------------
# 步骤 6：写备份元数据清单
# ---------------------------------------------------------------------------
log "--- 步骤 6: 写备份元数据 ---"
MANIFEST_FILE="${BACKUP_DIR}/metadata/backup_manifest.json"
run_cmd bash -c "cat > '${MANIFEST_FILE}' <<EOF
{
  \"backup_id\": \"bkp_${TIMESTAMP}\",
  \"timestamp\": \"${TIMESTAMP}\",
  \"created_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
  \"dry_run\": ${DRY_RUN},
  \"sources\": {
    \"postgres\": {
      \"host\": \"${PG_HOST}\",
      \"port\": ${PG_PORT},
      \"databases\": $(printf '%s\n' "${PG_DATABASES[@]}" | jq -R . | jq -s .),
      \"available\": $([ \"${PG_AVAILABLE:-0}\" = \"1\" ] && echo true || echo false)
    },
    \"minio\": {
      \"endpoint\": \"${MINIO_ENDPOINT}\",
      \"buckets\": $(printf '%s\n' "${MINIO_BUCKETS[@]}" | jq -R . | jq -s .),
      \"available\": $([ \"${MINIO_AVAILABLE:-0}\" = \"1\" ] && echo true || echo false)
    }
  },
  \"target_dir\": \"${BACKUP_DIR}\",
  \"note\": \"本地 dev 备份。生产使用 pgBackRest+Velero+clickhouse-backup。\"
}
EOF"
log "元数据写入: ${MANIFEST_FILE}"

# ---------------------------------------------------------------------------
# 完成
# ---------------------------------------------------------------------------
log "=== 备份完成 ==="
log "备份目录: ${BACKUP_DIR}"
if [[ "${DRY_RUN}" != "1" ]]; then
  TOTAL_SIZE=$(du -sh "${BACKUP_DIR}" 2>/dev/null | cut -f1 || echo "unknown")
  log "总大小: ${TOTAL_SIZE}"
fi
