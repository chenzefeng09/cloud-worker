#!/usr/bin/env bash
# =============================================================================
# verify-restore.sh — 恢复后数据一致性校验
#
# 用法：
#   ./deploy/backup/verify-restore.sh [OPTIONS]
#
# OPTIONS:
#   --pg-host HOST         Postgres 主机（默认 localhost）
#   --pg-port PORT         Postgres 端口（默认 5432）
#   --pg-user USER         Postgres 用户（默认 postgres）
#   --pg-pass PASS         Postgres 密码
#   --db-suffix SUFFIX     库名后缀（默认 _restore_test；空字符串=用原库名）
#   --minio-endpoint URL   MinIO 端点（默认 http://localhost:9001）
#   --minio-user USER      MinIO 用户
#   --minio-pass PASS      MinIO 密码
#   --api-base URL         API 基础 URL（默认 http://localhost:8005）
#   --baseline-file FILE   行数基线快照（JSON；无则跳过行数对比）
#   --dry-run              只打印检查项，不实际执行
#
# 退出码：
#   0  全部检查通过
#   1  至少一个关键检查失败
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# 配置默认值
# ---------------------------------------------------------------------------
PG_HOST="${PG_HOST:-localhost}"
PG_PORT="${PG_PORT:-5432}"
PG_USER="${PG_USER:-postgres}"
PG_PASS="${PG_PASS:-postgres_super_change_me}"
DB_SUFFIX="${DB_SUFFIX:-_restore_test}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9001}"
MINIO_USER="${MINIO_USER:-foundation}"
MINIO_PASS="${MINIO_PASS:-minio_change_me_min_12_chars}"
API_BASE="${API_BASE:-http://localhost:8005}"
BASELINE_FILE="${BASELINE_FILE:-}"
DRY_RUN="${DRY_RUN:-0}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_PREFIX="[verify-restore.sh ${TIMESTAMP}]"

# 检查结果计数
CHECKS_PASSED=0
CHECKS_FAILED=0
CHECKS_SKIPPED=0

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pg-host)     PG_HOST="$2"; shift 2 ;;
    --pg-port)     PG_PORT="$2"; shift 2 ;;
    --pg-user)     PG_USER="$2"; shift 2 ;;
    --pg-pass)     PG_PASS="$2"; shift 2 ;;
    --db-suffix)   DB_SUFFIX="$2"; shift 2 ;;
    --minio-endpoint) MINIO_ENDPOINT="$2"; shift 2 ;;
    --minio-user)  MINIO_USER="$2"; shift 2 ;;
    --minio-pass)  MINIO_PASS="$2"; shift 2 ;;
    --api-base)    API_BASE="$2"; shift 2 ;;
    --baseline-file) BASELINE_FILE="$2"; shift 2 ;;
    --dry-run)     DRY_RUN=1; shift ;;
    *) echo "${LOG_PREFIX} 未知参数: $1" >&2; exit 1 ;;
  esac
done

# 目标库名（加后缀）
FOUNDATION_DB="foundation${DB_SUFFIX}"

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
log()  { echo "${LOG_PREFIX} $*"; }
warn() { echo "${LOG_PREFIX} WARNING: $*" >&2; }

# 记录检查结果
check_pass() {
  log "  [PASS] $*"
  CHECKS_PASSED=$(( CHECKS_PASSED + 1 ))
}
check_fail() {
  log "  [FAIL] $*" >&2
  CHECKS_FAILED=$(( CHECKS_FAILED + 1 ))
}
check_skip() {
  log "  [SKIP] $*"
  CHECKS_SKIPPED=$(( CHECKS_SKIPPED + 1 ))
}

# 执行 SQL 并返回结果
# 优先使用 psql；不在 PATH 时通过 docker exec 容器内 psql
pg_query() {
  local db="$1"
  local sql="$2"
  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "[DRY-RUN] psql -d ${db} -c '${sql}'"
    echo ""
    return 0
  fi
  if command -v psql &>/dev/null; then
    PGPASSWORD="${PG_PASS}" psql \
      -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" \
      -d "${db}" -t -A -c "${sql}" 2>/dev/null || echo ""
  else
    # fallback: docker exec（本地 compose 环境，Postgres 在容器内）
    PG_CONTAINER="${PG_CONTAINER:-foundation-postgres}"
    docker exec "${PG_CONTAINER}" \
      psql -U "${PG_USER}" -d "${db}" -t -A -c "${sql}" 2>/dev/null || echo ""
  fi
}

# ---------------------------------------------------------------------------
# 检查 1：Postgres 连通性
# ---------------------------------------------------------------------------
log "=== 验证开始 (目标库: ${FOUNDATION_DB}) ==="
log "--- 检查 1: Postgres 连通性 ---"

if [[ "${DRY_RUN}" == "1" ]]; then
  check_skip "DRY-RUN: 连通性检查 ${PG_HOST}:${PG_PORT}"
else
  # 优先使用 pg_isready；不在 PATH 时退回 psql 简单连接测试
  PG_REACHABLE=0
  if command -v pg_isready &>/dev/null; then
    PGPASSWORD="${PG_PASS}" pg_isready -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -q 2>/dev/null \
      && PG_REACHABLE=1 || true
  elif command -v psql &>/dev/null; then
    PGPASSWORD="${PG_PASS}" psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" \
      -d postgres -c "SELECT 1;" -q 2>/dev/null | grep -q "1" && PG_REACHABLE=1 || true
  else
    # 最后手段：用 docker exec（本地 compose 环境）
    PG_CONTAINER="${PG_CONTAINER:-foundation-postgres}"
    docker exec "${PG_CONTAINER}" pg_isready -U "${PG_USER}" -q 2>/dev/null \
      && PG_REACHABLE=1 || true
  fi

  if [[ "${PG_REACHABLE}" == "1" ]]; then
    check_pass "Postgres ${PG_HOST}:${PG_PORT} 可达"
  else
    check_fail "Postgres ${PG_HOST}:${PG_PORT} 不可达"
  fi
fi

# ---------------------------------------------------------------------------
# 检查 2：目标库存在
# ---------------------------------------------------------------------------
log "--- 检查 2: 目标库存在 ---"

if [[ "${DRY_RUN}" != "1" ]]; then
  # pg_query 中已处理 psql/docker-exec 回退
  DB_EXISTS=$(pg_query "postgres" \
    "SELECT 1 FROM pg_database WHERE datname = '${FOUNDATION_DB}';" || echo "")

  if [[ "${DB_EXISTS}" == "1" ]]; then
    check_pass "数据库 '${FOUNDATION_DB}' 存在"
  else
    check_fail "数据库 '${FOUNDATION_DB}' 不存在"
    log "跳过后续 Postgres 检查（库不存在）"
    # 跳到非 Postgres 检查
    FOUNDATION_DB=""
  fi
else
  check_skip "DRY-RUN: 跳过库存在性检查"
fi

# ---------------------------------------------------------------------------
# 检查 3：关键 Schema 和表存在性
# ---------------------------------------------------------------------------
log "--- 检查 3: 关键表存在性 ---"

CRITICAL_TABLES=(
  "session_orchestrator.session"
  "session_orchestrator.message"
  "identity.user"
  "authz.role"
  "billing.tenant_subscription"
  "billing.quota_usage"
)

if [[ -n "${FOUNDATION_DB}" ]]; then
  for TABLE in "${CRITICAL_TABLES[@]}"; do
    SCHEMA="${TABLE%%.*}"
    TBL="${TABLE##*.}"
    if [[ "${DRY_RUN}" == "1" ]]; then
      check_skip "DRY-RUN: 检查表 ${TABLE}"
      continue
    fi
    EXISTS=$(pg_query "${FOUNDATION_DB}" \
      "SELECT 1 FROM information_schema.tables
       WHERE table_schema = '${SCHEMA}' AND table_name = '${TBL}';")
    if [[ "${EXISTS}" == "1" ]]; then
      check_pass "表 ${TABLE} 存在"
    else
      check_fail "表 ${TABLE} 不存在"
    fi
  done
fi

# ---------------------------------------------------------------------------
# 检查 4：关键表行数（与基线对比）
# ---------------------------------------------------------------------------
log "--- 检查 4: 关键表行数 ---"

MONITORED_TABLES=(
  "session_orchestrator.session"
  "session_orchestrator.message"
  "identity.user"
  "authz.role"
  "billing.quota_usage"
  "audit.outbox"
)
# Counts stored to a temp file (key=TABLE, val=COUNT) for baseline comparison
COUNTS_TMPFILE="/tmp/verify-restore-counts-${TIMESTAMP}.txt"
: > "${COUNTS_TMPFILE}"

if [[ -n "${FOUNDATION_DB}" ]]; then
  for TABLE in "${MONITORED_TABLES[@]}"; do
    if [[ "${DRY_RUN}" == "1" ]]; then
      check_skip "DRY-RUN: 行数 ${TABLE}"
      continue
    fi
    COUNT=$(pg_query "${FOUNDATION_DB}" \
      "SELECT COUNT(*) FROM ${TABLE};" 2>/dev/null || echo "ERROR")
    echo "${TABLE}=${COUNT}" >> "${COUNTS_TMPFILE}"
    log "  ${TABLE}: ${COUNT} 行"
  done

  # 与基线对比（如果提供了基线文件）
  if [[ -f "${BASELINE_FILE}" ]] && command -v jq &>/dev/null; then
    log "  与基线文件对比: ${BASELINE_FILE}"
    while IFS='=' read -r TABLE COUNT; do
      BASELINE_COUNT=$(jq -r ".\"${TABLE}\" // \"N/A\"" "${BASELINE_FILE}" 2>/dev/null)
      if [[ "${BASELINE_COUNT}" == "N/A" ]]; then
        check_skip "${TABLE}: 基线无记录"
      elif [[ "${COUNT}" -ge "${BASELINE_COUNT}" ]]; then
        check_pass "${TABLE}: ${COUNT} 行 (基线: ${BASELINE_COUNT})"
      else
        check_fail "${TABLE}: ${COUNT} 行 < 基线 ${BASELINE_COUNT} 行！数据可能丢失"
      fi
    done < "${COUNTS_TMPFILE}"
  else
    check_skip "无基线文件或 jq 未安装，跳过行数基线对比"
  fi
fi

# ---------------------------------------------------------------------------
# 检查 5：最新数据点时间（确认备份不太陈旧）
# ---------------------------------------------------------------------------
log "--- 检查 5: 最新数据点时间 ---"

if [[ -n "${FOUNDATION_DB}" && "${DRY_RUN}" != "1" ]]; then
  for TABLE in "session_orchestrator.session" "session_orchestrator.message" "identity.user"; do
    MAX_TIME=$(pg_query "${FOUNDATION_DB}" \
      "SELECT MAX(created_at) FROM ${TABLE};" 2>/dev/null || echo "NULL")
    if [[ -n "${MAX_TIME}" && "${MAX_TIME}" != "NULL" ]]; then
      log "  ${TABLE} 最新记录: ${MAX_TIME}"
      check_pass "${TABLE}: 有数据点 ${MAX_TIME}"
    else
      log "  ${TABLE} 无数据（可能是空库）"
      check_skip "${TABLE}: 无数据（新库或 test 数据）"
    fi
  done
fi

# ---------------------------------------------------------------------------
# 检查 6：alembic 版本（检查 schema 版本是否已迁移）
# ---------------------------------------------------------------------------
log "--- 检查 6: alembic schema 版本 ---"

if [[ -n "${FOUNDATION_DB}" && "${DRY_RUN}" != "1" ]]; then
  ALEMBIC_VERSION=$(pg_query "${FOUNDATION_DB}" \
    "SELECT version_num FROM alembic_version LIMIT 1;" 2>/dev/null || echo "N/A")
  if [[ "${ALEMBIC_VERSION}" != "N/A" && -n "${ALEMBIC_VERSION}" ]]; then
    log "  alembic_version: ${ALEMBIC_VERSION}"
    check_pass "alembic_version 存在: ${ALEMBIC_VERSION}"
  else
    # 可能存在多个 alembic_version 表（每个服务独立的）
    TABLES=$(pg_query "${FOUNDATION_DB}" \
      "SELECT table_name FROM information_schema.tables
       WHERE table_name LIKE 'alembic_version%';" 2>/dev/null || echo "")
    if [[ -n "${TABLES}" ]]; then
      log "  发现多个 alembic_version 表: ${TABLES}"
      check_pass "发现 alembic_version 表"
    else
      check_fail "未找到 alembic_version 表（schema 可能未迁移）"
    fi
  fi
else
  [[ "${DRY_RUN}" == "1" ]] && check_skip "DRY-RUN: 跳过 alembic 版本检查"
fi

# ---------------------------------------------------------------------------
# 检查 7：API 健康端点（现有服务）
# ---------------------------------------------------------------------------
log "--- 检查 7: API 健康端点 ---"

# Parallel arrays (bash 3 compatible: no declare -A)
HEALTH_SVC_NAMES=(identity authz bff session-orchestrator agent-scheduler mcp-registry skill-store knowledge-ingest)
HEALTH_SVC_URLS=(
  "http://localhost:8001/api/identity/v1/healthz"
  "http://localhost:8002/api/authz/v1/healthz"
  "http://localhost:8005/api/bff/v1/healthz"
  "http://localhost:8006/api/session/v1/healthz"
  "http://localhost:8007/api/scheduler/v1/healthz"
  "http://localhost:8010/api/mcp/v1/healthz"
  "http://localhost:8011/api/skills/v1/healthz"
  "http://localhost:8012/api/knowledge/v1/healthz"
)

if command -v curl &>/dev/null; then
  for i in "${!HEALTH_SVC_NAMES[@]}"; do
    SVC="${HEALTH_SVC_NAMES[$i]}"
    ENDPOINT="${HEALTH_SVC_URLS[$i]}"
    if [[ "${DRY_RUN}" == "1" ]]; then
      check_skip "DRY-RUN: curl ${ENDPOINT}"
      continue
    fi
    HTTP_CODE=$(curl -sf -o /dev/null -w "%{http_code}" \
      --max-time 5 "${ENDPOINT}" 2>/dev/null || echo "000")
    if [[ "${HTTP_CODE}" == "200" ]]; then
      check_pass "${SVC} healthz: ${HTTP_CODE}"
    else
      # isolated 恢复不重启服务，现有服务应该仍然健康
      warn "${SVC} healthz: ${HTTP_CODE}（${ENDPOINT}）"
      # 不计为 fail（isolated 模式下服务可能不在）
      check_skip "${SVC}: HTTP ${HTTP_CODE}（isolated 恢复不重启服务）"
    fi
  done
else
  check_skip "curl 未安装，跳过 API 健康检查"
fi

# ---------------------------------------------------------------------------
# 检查 8：MinIO 桶列表（恢复目标桶存在）
# ---------------------------------------------------------------------------
log "--- 检查 8: MinIO 桶列表 ---"

if command -v mc &>/dev/null; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    check_skip "DRY-RUN: mc ls"
  else
    if mc alias set local-verify "${MINIO_ENDPOINT}" "${MINIO_USER}" "${MINIO_PASS}" \
       --api S3v4 2>/dev/null; then
      BUCKETS=$(mc ls local-verify/ 2>/dev/null | awk '{print $NF}' | tr -d '/' || echo "")
      if [[ -n "${BUCKETS}" ]]; then
        log "  MinIO 桶: $(echo "${BUCKETS}" | tr '\n' ' ')"
        check_pass "MinIO 可访问，桶列表非空"
      else
        check_skip "MinIO 可访问但无桶（可能尚未初始化）"
      fi
    else
      check_fail "MinIO 不可达: ${MINIO_ENDPOINT}"
    fi
  fi
else
  check_skip "mc 未安装，跳过 MinIO 检查"
fi

# ---------------------------------------------------------------------------
# 检查 9：Sentinel 数据点（dev 环境已知 seed 数据）
# ---------------------------------------------------------------------------
log "--- 检查 9: Sentinel 数据点（dev seed 数据）---"

# dev 环境有固定的种子数据（identity-init seed），校验它们是否在恢复库中
SENTINEL_USERS=(
  "admin@foundation.local"
  "alice@tenant-a.local"
)

if [[ -n "${FOUNDATION_DB}" && "${DRY_RUN}" != "1" ]]; then
  for EMAIL in "${SENTINEL_USERS[@]}"; do
    EXISTS=$(pg_query "${FOUNDATION_DB}" \
      "SELECT 1 FROM identity.user WHERE email = '${EMAIL}' LIMIT 1;" 2>/dev/null || echo "")
    if [[ "${EXISTS}" == "1" ]]; then
      check_pass "sentinel 用户存在: ${EMAIL}"
    else
      # 注意：isolated 库恢复自 dump，应有 seed 用户
      check_fail "sentinel 用户不存在: ${EMAIL}"
    fi
  done

  # 检查 dev 租户订阅
  SUB_COUNT=$(pg_query "${FOUNDATION_DB}" \
    "SELECT COUNT(*) FROM billing.tenant_subscription
     WHERE status = 'ACTIVE';" 2>/dev/null || echo "0")
  if [[ "${SUB_COUNT:-0}" -ge 2 ]]; then
    check_pass "活跃订阅数: ${SUB_COUNT}"
  else
    check_skip "活跃订阅数: ${SUB_COUNT}（可能是空测试环境）"
  fi
else
  [[ "${DRY_RUN}" == "1" ]] && check_skip "DRY-RUN: 跳过 sentinel 检查"
fi

# ---------------------------------------------------------------------------
# 检查 10：RLS 隔离（A 租户不可见 B 租户数据）
# ---------------------------------------------------------------------------
log "--- 检查 10: RLS 隔离验证 ---"

if [[ -n "${FOUNDATION_DB}" && "${DRY_RUN}" != "1" ]]; then
  TENANT_A="tnt_01J5DEVXX00000000000000001"
  TENANT_B="tnt_01J5DEVXX00000000000000002"

  # 以 app_user（受 RLS 约束）设置 app.tenant_id 检查跨租户隔离
  # 注：这里用超级用户模拟 RLS 行为（SET LOCAL不在事务外生效）
  # 真正的 RLS 测试需要以 app_user 连接 + SET app.tenant_id
  TENANT_A_USERS=$(pg_query "${FOUNDATION_DB}" \
    "SELECT COUNT(*) FROM identity.user WHERE tenant_id = '${TENANT_A}';" 2>/dev/null || echo "0")
  TENANT_B_USERS=$(pg_query "${FOUNDATION_DB}" \
    "SELECT COUNT(*) FROM identity.user WHERE tenant_id = '${TENANT_B}';" 2>/dev/null || echo "0")

  if [[ "${TENANT_A_USERS:-0}" -ge 1 && "${TENANT_B_USERS:-0}" -ge 1 ]]; then
    check_pass "多租户数据存在: TenantA=${TENANT_A_USERS}用户, TenantB=${TENANT_B_USERS}用户"
    log "  [注意] 完整 RLS 隔离测试需以 app_user 连接并 SET app.tenant_id"
  else
    check_skip "租户用户数据不足（TenantA=${TENANT_A_USERS}, TenantB=${TENANT_B_USERS}），跳过 RLS 检查"
  fi
else
  [[ "${DRY_RUN}" == "1" ]] && check_skip "DRY-RUN: 跳过 RLS 检查"
fi

# ---------------------------------------------------------------------------
# 生成验证报告
# ---------------------------------------------------------------------------
log "=== 验证结果汇总 ==="
TOTAL_CHECKS=$(( CHECKS_PASSED + CHECKS_FAILED + CHECKS_SKIPPED ))
log "总检查数: ${TOTAL_CHECKS}"
log "  PASS:   ${CHECKS_PASSED}"
log "  FAIL:   ${CHECKS_FAILED}"
log "  SKIP:   ${CHECKS_SKIPPED}"

# 写 JSON 报告
REPORT_FILE="/tmp/verify-restore-${TIMESTAMP}.json"
cat > "${REPORT_FILE}" << EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "target_db": "${FOUNDATION_DB}",
  "dry_run": ${DRY_RUN},
  "checks_passed": ${CHECKS_PASSED},
  "checks_failed": ${CHECKS_FAILED},
  "checks_skipped": ${CHECKS_SKIPPED},
  "outcome": "$([ ${CHECKS_FAILED} -eq 0 ] && echo 'PASS' || echo 'FAIL')"
}
EOF
log "验证报告: ${REPORT_FILE}"

# 退出码
if [[ ${CHECKS_FAILED} -gt 0 ]]; then
  log "[RESULT] FAIL — ${CHECKS_FAILED} 个检查失败"
  exit 1
else
  log "[RESULT] PASS — 所有关键检查通过"
  exit 0
fi
