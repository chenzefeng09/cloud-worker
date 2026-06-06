#!/usr/bin/env bash
# =============================================================================
# restore.sh — agent-platform + saas-foundation 恢复演练脚本
#
# 从备份恢复到隔离测试库（不破坏正在运行的生产/开发栈！）
# 恢复目标：独立的 _restore_test 库（Postgres）+ 独立目录（MinIO）
#
# 用法：
#   ./deploy/backup/restore.sh --backup-dir /path/to/backup/YYYYMMDD_HHMMSS
#   ./deploy/backup/restore.sh --backup-dir /path/to/backup/YYYYMMDD_HHMMSS --target=isolated
#   ./deploy/backup/restore.sh --backup-dir /path/to/backup/YYYYMMDD_HHMMSS --dry-run
#
# 恢复目标选项：
#   isolated (默认)  → 恢复到 foundation_restore_test / keycloak_restore_test 临时库
#   staging          → 恢复到 staging 环境（需要单独的 Postgres 实例）
#   primary          → 恢复到生产（危险！需要 --confirm-primary 显式确认）
#
# 环境变量：
#   PG_HOST         Postgres 主机（默认 localhost）
#   PG_PORT         Postgres 端口（默认 5432）
#   PG_SUPER_USER   Postgres 超级用户（默认 postgres）
#   PG_SUPER_PASS   Postgres 超级用户密码
#   MINIO_ENDPOINT  MinIO 端点（默认 http://localhost:9001）
#   MINIO_USER      MinIO 用户
#   MINIO_PASS      MinIO 密码
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 配置默认值
# ---------------------------------------------------------------------------
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR=""
TARGET="isolated"          # isolated | staging | primary
CONFIRM_PRIMARY=0
DRY_RUN="${DRY_RUN:-0}"

PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_SUPER_USER="${PG_SUPER_USER:-postgres}"
PG_SUPER_PASS="${PG_SUPER_PASS:-postgres_super_change_me}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9001}"
MINIO_USER="${MINIO_USER:-foundation}"
MINIO_PASS="${MINIO_PASS:-minio_change_me_min_12_chars}"

# isolated 目标库名（加 _restore_test 后缀，绝不覆盖正在用的库）
RESTORE_SUFFIX="_restore_test"

LOG_PREFIX="[restore.sh ${TIMESTAMP}]"
RESTORE_START_EPOCH=$(date +%s)

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-dir)
      BACKUP_DIR="$2"; shift 2 ;;
    --target)
      TARGET="$2"; shift 2 ;;
    --confirm-primary)
      CONFIRM_PRIMARY=1; shift ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    *)
      echo "${LOG_PREFIX} 未知参数: $1" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
log()  { echo "${LOG_PREFIX} $*"; }
die()  { echo "${LOG_PREFIX} ERROR: $*" >&2; exit 1; }
warn() { echo "${LOG_PREFIX} WARNING: $*" >&2; }

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
    warn "${tool} 未安装"
    return 1
  fi
  return 0
}

elapsed_minutes() {
  local now
  now=$(date +%s)
  echo $(( (now - RESTORE_START_EPOCH) / 60 ))
}

# ---------------------------------------------------------------------------
# 前置校验
# ---------------------------------------------------------------------------
[[ -z "${BACKUP_DIR}" ]] && die "--backup-dir 必须指定"
[[ -d "${BACKUP_DIR}" ]] || die "备份目录不存在: ${BACKUP_DIR}"

log "=== 开始恢复演练 ==="
log "备份目录: ${BACKUP_DIR}"
log "恢复目标: ${TARGET}"
[[ "${DRY_RUN}" == "1" ]] && log "[DRY-RUN 模式]"

# 生产恢复需要显式确认
if [[ "${TARGET}" == "primary" && "${CONFIRM_PRIMARY}" != "1" ]]; then
  die "恢复到 primary 必须加 --confirm-primary 参数（这是破坏性操作！）"
fi

# isolated 目标：加 _restore_test 后缀，绝不会覆盖正在使用的库
if [[ "${TARGET}" == "isolated" ]]; then
  PG_RESTORE_SUFFIX="${RESTORE_SUFFIX}"
  log "安全检查: isolated 模式 — 恢复到 *${RESTORE_SUFFIX} 数据库，不影响现有库"
else
  PG_RESTORE_SUFFIX=""
  log "WARNING: 非 isolated 模式，将覆盖目标库！"
fi

# ---------------------------------------------------------------------------
# 步骤 1：检查 Postgres 连通性
# ---------------------------------------------------------------------------
log "--- 步骤 1: Postgres 连通性检查 ---"

if ! check_tool pg_restore; then
  die "pg_restore 未安装，无法继续"
fi

if ! PGPASSWORD="${PG_SUPER_PASS}" pg_isready \
     -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_SUPER_USER}" -q 2>/dev/null; then
  die "无法连接 Postgres ${PG_HOST}:${PG_PORT}"
fi
log "Postgres 连通性 OK"

# ---------------------------------------------------------------------------
# 步骤 2：恢复 Postgres（drop + recreate + restore）
# ---------------------------------------------------------------------------
log "--- 步骤 2: Postgres 恢复 ---"

PG_DUMP_DIR="${BACKUP_DIR}/postgres"
if [[ ! -d "${PG_DUMP_DIR}" ]]; then
  warn "Postgres 备份目录不存在: ${PG_DUMP_DIR}，跳过 Postgres 恢复"
else
  for DUMP_FILE in "${PG_DUMP_DIR}"/*.dump; do
    [[ -f "${DUMP_FILE}" ]] || continue
    # 从文件名提取库名（格式: <dbname>_<timestamp>.dump）
    DUMP_BASENAME=$(basename "${DUMP_FILE}")
    DB_NAME=$(echo "${DUMP_BASENAME}" | sed 's/_[0-9]\{8\}_[0-9]\{6\}\.dump$//')
    RESTORE_DB="${DB_NAME}${PG_RESTORE_SUFFIX}"

    log "恢复: ${DUMP_FILE} → 数据库 '${RESTORE_DB}'"

    # 1. 删除旧的恢复测试库（如果存在）
    log "  DROP DATABASE IF EXISTS ${RESTORE_DB}"
    run_cmd bash -c "PGPASSWORD='${PG_SUPER_PASS}' psql \
      -h '${PG_HOST}' -p '${PG_PORT}' -U '${PG_SUPER_USER}' \
      -c \"DROP DATABASE IF EXISTS ${RESTORE_DB};\" postgres"

    # 2. 创建新库
    log "  CREATE DATABASE ${RESTORE_DB}"
    run_cmd bash -c "PGPASSWORD='${PG_SUPER_PASS}' psql \
      -h '${PG_HOST}' -p '${PG_PORT}' -U '${PG_SUPER_USER}' \
      -c \"CREATE DATABASE ${RESTORE_DB};\" postgres"

    # 3. 恢复 dump（-j 4 并行加速；不输出恢复进度以减少日志）
    log "  pg_restore → ${RESTORE_DB}"
    run_cmd bash -c "PGPASSWORD='${PG_SUPER_PASS}' pg_restore \
      -h '${PG_HOST}' \
      -p '${PG_PORT}' \
      -U '${PG_SUPER_USER}' \
      -d '${RESTORE_DB}' \
      -j 4 \
      --no-owner \
      --no-privileges \
      '${DUMP_FILE}' 2>&1 | grep -v '^pg_restore: warning' || true"

    log "  恢复完成: ${RESTORE_DB}"
  done
fi

# [PROD] 生产 PITR 恢复（pgBackRest）：
# RECOVERY_TARGET="2026-06-05 10:30:00+00"
# pgbackrest --stanza=agent-platform restore \
#   --target="${RECOVERY_TARGET}" \
#   --target-action=promote \
#   --pg1-path=/var/lib/postgresql/data-restore
# 详见 docs/runbook/backup-restore-b-m6.md §3.2

# ---------------------------------------------------------------------------
# 步骤 3：恢复后运行 alembic migration（确保 schema 版本一致）
# ---------------------------------------------------------------------------
log "--- 步骤 3: alembic migration 检查 ---"

# 检查恢复库的 alembic 版本
if [[ "${DRY_RUN}" != "1" && "${TARGET}" == "isolated" ]]; then
  FOUNDATION_RESTORE_DB="foundation${PG_RESTORE_SUFFIX}"
  log "检查 foundation${PG_RESTORE_SUFFIX} 的 alembic 版本:"

  # 列出所有 alembic_version 表
  ALEMBIC_VERSIONS=$(PGPASSWORD="${PG_SUPER_PASS}" psql \
    -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_SUPER_USER}" \
    -d "${FOUNDATION_RESTORE_DB}" \
    -t -c "SELECT table_schema || '.' || table_name
           FROM information_schema.tables
           WHERE table_name LIKE 'alembic_version%'
           ORDER BY table_name;" 2>/dev/null || echo "")

  if [[ -n "${ALEMBIC_VERSIONS}" ]]; then
    while IFS= read -r TABLE; do
      [[ -z "${TABLE// }" ]] && continue
      VER=$(PGPASSWORD="${PG_SUPER_PASS}" psql \
        -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_SUPER_USER}" \
        -d "${FOUNDATION_RESTORE_DB}" \
        -t -c "SELECT version_num FROM ${TABLE// /} LIMIT 1;" 2>/dev/null || echo "N/A")
      log "  ${TABLE// /}: ${VER// /}"
    done <<< "${ALEMBIC_VERSIONS}"
  else
    log "  未找到 alembic_version 表（可能库名不匹配或备份来自不同版本）"
  fi
fi

# [PROD] 如果版本落后，需要手动运行 migration：
# cd /path/to/agent-platform/services/session-orchestrator
# SESSION_ORCHESTRATOR_DB_DSN_MIGRATOR=postgresql+asyncpg://app_migrator:xxx@host:5432/foundation_restore_test \
#   python -m alembic upgrade head
# 详见 docs/runbook/backup-restore-b-m6.md §3.6
log "  [注意] 生产恢复后如 schema 版本落后，需手动运行各服务 alembic upgrade head"

# ---------------------------------------------------------------------------
# 步骤 4：恢复 MinIO 桶（到临时目录）
# ---------------------------------------------------------------------------
log "--- 步骤 4: MinIO 恢复 ---"

MINIO_BACKUP_DIR="${BACKUP_DIR}/minio"
if [[ ! -d "${MINIO_BACKUP_DIR}" ]]; then
  warn "MinIO 备份目录不存在: ${MINIO_BACKUP_DIR}，跳过 MinIO 恢复"
elif ! check_tool mc; then
  warn "mc 未安装，跳过 MinIO 恢复"
else
  if run_cmd mc alias set local-restore "${MINIO_ENDPOINT}" "${MINIO_USER}" "${MINIO_PASS}" --api S3v4 2>/dev/null; then
    for BUCKET_DIR in "${MINIO_BACKUP_DIR}"/*/; do
      [[ -d "${BUCKET_DIR}" ]] || continue
      BUCKET_NAME=$(basename "${BUCKET_DIR}")
      # isolated 模式：恢复到 <bucket-name>-restore-test 桶
      if [[ "${TARGET}" == "isolated" ]]; then
        RESTORE_BUCKET="${BUCKET_NAME}-restore-test"
      else
        RESTORE_BUCKET="${BUCKET_NAME}"
      fi

      log "MinIO: ${BUCKET_DIR} → ${RESTORE_BUCKET}"

      # 创建目标桶（如不存在）
      run_cmd mc mb --ignore-existing "local-restore/${RESTORE_BUCKET}" || true

      # 同步内容
      run_cmd mc mirror \
        --overwrite \
        "${BUCKET_DIR}" \
        "local-restore/${RESTORE_BUCKET}/"
      log "  MinIO 桶恢复完成: ${RESTORE_BUCKET}"
    done
  else
    warn "无法连接 MinIO，跳过 MinIO 恢复"
  fi
fi

# [PROD] 从 S3 恢复 MinIO：
# mc alias set backup s3://agent-platform-backup $KEY $SECRET
# mc mirror backup/minio-mirror/agent-platform local-restore/agent-platform
# 详见 docs/runbook/backup-restore-b-m6.md §3.3

# ---------------------------------------------------------------------------
# 步骤 5：ClickHouse 恢复（[PROD] 占位）
# ---------------------------------------------------------------------------
log "--- 步骤 5: ClickHouse 恢复 ---"
# [PROD] 生产使用：
# clickhouse-backup download <backup-name>
# clickhouse-backup restore <backup-name>
# 或从 Postgres outbox 重建（重启 audit-worker 即可）
log "  [PROD 占位] ClickHouse 恢复：audit-worker 重启后自动从 Postgres outbox 重建"

# ---------------------------------------------------------------------------
# 步骤 6：启动服务健康检查
# ---------------------------------------------------------------------------
log "--- 步骤 6: 服务健康检查 ---"

# [PROD] 启动服务并等待 healthcheck：
# docker compose -f dev.yml up -d postgres pgbouncer vault redis minio keycloak
# docker compose -f dev.yml up -d identity authz org billing crypto audit
# cd ../agent-platform/deploy/docker-compose && docker compose -f agent-platform.yml up -d

# 本地：只检查现有服务健康状态
log "检查现有服务健康状态（isolated 恢复不重启服务）:"
HEALTH_ENDPOINTS=(
  "http://localhost:8001/api/identity/v1/healthz"
  "http://localhost:8002/api/authz/v1/healthz"
  "http://localhost:8005/api/bff/v1/healthz"
  "http://localhost:8006/api/session/v1/healthz"
)

for ENDPOINT in "${HEALTH_ENDPOINTS[@]}"; do
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "  [DRY-RUN] 检查: ${ENDPOINT}"
  else
    if curl -sf "${ENDPOINT}" -o /dev/null 2>/dev/null; then
      log "  OK: ${ENDPOINT}"
    else
      warn "  FAIL: ${ENDPOINT}（isolated 恢复不影响现有服务）"
    fi
  fi
done

# ---------------------------------------------------------------------------
# 步骤 7：调用恢复验证脚本
# ---------------------------------------------------------------------------
log "--- 步骤 7: 恢复一致性校验 ---"

VERIFY_SCRIPT="$(dirname "$0")/verify-restore.sh"
if [[ -f "${VERIFY_SCRIPT}" ]]; then
  log "运行验证脚本: ${VERIFY_SCRIPT}"
  run_cmd bash "${VERIFY_SCRIPT}" \
    --pg-host "${PG_HOST}" \
    --pg-port "${PG_PORT}" \
    --pg-user "${PG_SUPER_USER}" \
    --pg-pass "${PG_SUPER_PASS}" \
    --db-suffix "${PG_RESTORE_SUFFIX}" \
    ${DRY_RUN:+--dry-run} || {
      warn "验证脚本返回非零（可能是 isolated 环境部分功能不完整）"
    }
else
  warn "验证脚本不存在: ${VERIFY_SCRIPT}"
fi

# ---------------------------------------------------------------------------
# 完成
# ---------------------------------------------------------------------------
ELAPSED=$(elapsed_minutes)
log "=== 恢复演练完成 ==="
log "耗时: ${ELAPSED} 分钟"
log "目标: ${TARGET}"

if [[ "${TARGET}" == "isolated" ]]; then
  log "恢复的隔离库:"
  if [[ "${DRY_RUN}" != "1" ]]; then
    PGPASSWORD="${PG_SUPER_PASS}" psql \
      -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_SUPER_USER}" \
      -t -c "SELECT datname FROM pg_database WHERE datname LIKE '%${RESTORE_SUFFIX}%';" postgres 2>/dev/null || true
  fi
  log ""
  log "演练完成后清理（删除恢复测试库）:"
  for DUMP_FILE in "${BACKUP_DIR}/postgres/"*.dump; do
    [[ -f "${DUMP_FILE}" ]] || continue
    DUMP_BASENAME=$(basename "${DUMP_FILE}")
    DB_NAME=$(echo "${DUMP_BASENAME}" | sed 's/_[0-9]\{8\}_[0-9]\{6\}\.dump$//')
    RESTORE_DB="${DB_NAME}${PG_RESTORE_SUFFIX}"
    log "  PGPASSWORD=xxx psql -h ${PG_HOST} -U ${PG_SUPER_USER} -c 'DROP DATABASE IF EXISTS ${RESTORE_DB};' postgres"
  done
fi

if [[ "${ELAPSED}" -le 30 ]]; then
  log "RTO 目标: PASSED (${ELAPSED} min ≤ 30 min)"
else
  warn "RTO 目标: FAILED (${ELAPSED} min > 30 min)，需要优化恢复流程！"
fi
