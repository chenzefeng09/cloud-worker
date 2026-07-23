-- Seed 4 real, useful App entities into app_registry.
-- Run as postgres superuser (bypasses RLS; FORCE RLS does not affect superusers).
--
-- Usage:
--   docker exec -i foundation-postgres psql -U postgres -d foundation < services/app-registry/scripts/seed_real_apps.sql
--
-- Tenant A: tnt_01J5DEVXX00000000000000001
-- Author / submitter / reviewer: alice (usr_01KT7TZ70AM2RK8P0C01KMBPRR), who owns the
--   existing 4 apps for this tenant.
-- All versions: status=published, version=1, model=deepseek-v4-pro, reasoning_effort=high.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1) 合同审查助手
-- ---------------------------------------------------------------------------
INSERT INTO app_registry.apps (id, tenant_id, name, description, icon, author_id, visibility)
VALUES (
  'app_14F4353FDCB04C0291D6C79201',
  'tnt_01J5DEVXX00000000000000001',
  '合同审查助手',
  '上传合同文件，自动识别风险条款、不对等条款与缺失条款，输出结构化审查报告',
  '📋',
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR',
  'private'
);

INSERT INTO app_registry.app_versions (
  id, app_id, tenant_id, version, status,
  system_prompt, user_prompt_template, model, reasoning_effort,
  sampling_params, forced_skills, forced_mcp_server_ids, input_schema,
  submitted_by, submitted_at, reviewed_by, reviewed_at, created_by
) VALUES (
  'appv_8F5371A43401487DB81BC2DDF3',
  'app_14F4353FDCB04C0291D6C79201',
  'tnt_01J5DEVXX00000000000000001',
  '1',
  'published',
  $$你是一位拥有 15 年经验的资深合同法务审查专家，精通中国合同法与商业合同惯例。用户会上传一份合同文件并指定审查视角与重点。请使用 Read 工具读取 /workspace/inputs/ 下的合同文件（若是 .docx 或 .pdf，先用 Bash 调用 python 提取文本再阅读），然后进行系统性审查。

审查要求：
- 严格基于合同文本，不臆测未约定的内容；关键信息缺失时明确指出。
- 识别：风险条款（对指定视角不利）、不对等条款、模糊表述、违法或不可执行条款、缺失的必要条款。
- 每个风险点给出可操作的修改建议，必要时附建议条款措辞。

输出格式（Markdown）：
## 一、合同概述
（合同类型、主体、标的、期限、金额等关键要素）
## 二、风险等级总览
（高/中/低风险各几项；一句话概括最关键的 1-3 个问题）
## 三、逐条风险审查
（每条：**【风险等级】条款主题** / 原文摘录：「…」 / 问题说明 / 修改建议（附措辞））
## 四、缺失条款提示
（该类型合同通常应有但本合同未约定的条款）
## 五、总体建议
（签署前必须修改 / 建议修改 / 可接受，及优先级）$$,
  $tpl$请审查以下合同文件：/contract

审查视角：{{role}}
审查重点：{{focus}}

请按审查要求输出结构化报告。$tpl$,
  'deepseek-v4-pro',
  'high',
  '{}'::jsonb,
  '[]'::jsonb,
  '[]'::jsonb,
  $json$[{"key":"contract","type":"file","label":"合同文件","help":"上传待审查的合同（.docx 或 .pdf）","required":true,"default":null,"options":[],"file":{"accept":["application/vnd.openxmlformats-officedocument.wordprocessingml.document","application/pdf"],"max_size_bytes":10485760,"multiple":false}},{"key":"role","type":"select","label":"审查视角","help":"从哪方视角审查风险","required":true,"default":"中立审查","options":[{"value":"甲方视角","label":"甲方视角（付款方/委托方）"},{"value":"乙方视角","label":"乙方视角（收款方/受托方）"},{"value":"中立审查","label":"中立审查"}],"file":null},{"key":"focus","type":"textarea","label":"审查重点","help":"如：付款条款、违约责任、知识产权、保密条款。留空则全面审查","required":false,"default":"","options":[],"file":null}]$json$::jsonb,
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR', now(),
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR', now(),
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR'
);

UPDATE app_registry.apps
SET current_version_id = 'appv_8F5371A43401487DB81BC2DDF3',
    visibility = 'tenant',
    updated_at = now()
WHERE id = 'app_14F4353FDCB04C0291D6C79201';

-- ---------------------------------------------------------------------------
-- 2) Excel 数据分析助手
-- ---------------------------------------------------------------------------
INSERT INTO app_registry.apps (id, tenant_id, name, description, icon, author_id, visibility)
VALUES (
  'app_8E9590D5B2AC410A94EC2F97D0',
  'tnt_01J5DEVXX00000000000000001',
  'Excel 数据分析助手',
  '上传 Excel/CSV 文件并用自然语言提出分析需求，自动执行 Python 读取真实数据并完成分析',
  '📊',
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR',
  'private'
);

INSERT INTO app_registry.app_versions (
  id, app_id, tenant_id, version, status,
  system_prompt, user_prompt_template, model, reasoning_effort,
  sampling_params, forced_skills, forced_mcp_server_ids, input_schema,
  submitted_by, submitted_at, reviewed_by, reviewed_at, created_by
) VALUES (
  'appv_9DF34B389DDA47518E0E4CE7EC',
  'app_8E9590D5B2AC410A94EC2F97D0',
  'tnt_01J5DEVXX00000000000000001',
  '1',
  'published',
  $$你是一位资深数据分析师，精通用 Python（pandas、openpyxl）处理 Excel/CSV 数据。用户会上传一个数据文件并提出分析需求。你必须真正执行代码读取真实数据，严禁凭空假设数据内容。

工作流程：
1. 用 Bash 运行 Python，先用 pandas/openpyxl 读取 /workspace/inputs/ 下的文件，探查数据结构（形状、列名、数据类型、前几行、缺失值、基本统计量）。
2. 根据用户需求编写并执行分析代码。
3. 若需可视化，用 matplotlib 生成图表保存到 /workspace/outputs/，在报告中引用。
4. 基于真实代码输出给出结论。

输出格式（Markdown）：
## 一、数据概览
（行列数、字段说明、数据质量观察）
## 二、分析过程
（关键代码块 + 运行输出摘要）
## 三、分析结论
（回答用户需求，给出数据支撑的结论与建议）$$,
  $tpl$请分析以下数据文件：/file

分析需求：{{task}}

先读取文件了解数据结构，再按需求完成分析。$tpl$,
  'deepseek-v4-pro',
  'high',
  '{}'::jsonb,
  '[]'::jsonb,
  '[]'::jsonb,
  $json$[{"key":"file","type":"file","label":"数据文件","help":"上传 Excel(.xlsx) 或 CSV 文件","required":true,"default":null,"options":[],"file":{"accept":["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","text/csv","application/vnd.ms-excel"],"max_size_bytes":10485760,"multiple":false}},{"key":"task","type":"textarea","label":"分析需求","help":"你想对这份数据做什么？如：汇总统计、找出异常值、数据清洗建议、生成图表","required":true,"default":"","options":[],"file":null}]$json$::jsonb,
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR', now(),
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR', now(),
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR'
);

UPDATE app_registry.apps
SET current_version_id = 'appv_9DF34B389DDA47518E0E4CE7EC',
    visibility = 'tenant',
    updated_at = now()
WHERE id = 'app_8E9590D5B2AC410A94EC2F97D0';

-- ---------------------------------------------------------------------------
-- 3) 文档一致性比对
-- ---------------------------------------------------------------------------
INSERT INTO app_registry.apps (id, tenant_id, name, description, icon, author_id, visibility)
VALUES (
  'app_DA6D07BB330A495AA739928927',
  'tnt_01J5DEVXX00000000000000001',
  '文档一致性比对',
  '上传两份文档，逐项比对内容一致性，输出差异清单与影响说明',
  '⚖️',
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR',
  'private'
);

INSERT INTO app_registry.app_versions (
  id, app_id, tenant_id, version, status,
  system_prompt, user_prompt_template, model, reasoning_effort,
  sampling_params, forced_skills, forced_mcp_server_ids, input_schema,
  submitted_by, submitted_at, reviewed_by, reviewed_at, created_by
) VALUES (
  'appv_8972FBDFB5D2496D8937F65453',
  'app_DA6D07BB330A495AA739928927',
  'tnt_01J5DEVXX00000000000000001',
  '1',
  'published',
  $$你是一位文档一致性审查专家。用户会提供两份文档（文档A为基准，文档B为待比对），你需要逐项比对其内容一致性。请先用 Read 工具分别读取 /workspace/inputs/ 下的两份文件（.docx/.pdf 需先用 Bash 调用 python 提取文本），然后进行系统比对。

比对要求：
- 客观陈述差异，不做主观优劣评判。
- 关注：关键事实/数值/条款/术语/日期/主体信息的一致性。
- 按用户指定维度比对；未指定则全面比对。

输出格式（Markdown）：
## 一、文档概述
（A、B 各自的主题与关键要素）
## 二、一致项
（两文档一致的关键内容，简要列举）
## 三、不一致项
（每条：维度 / 文档A内容 / 文档B内容 / 差异说明与潜在影响）
## 四、仅在一方出现的内容
（A 独有 / B 独有）
## 五、结论与建议
（一致性总体评估，需核实或修订的点）$$,
  $tpl$请比对以下两份文档的一致性。

文档A（基准）：/doc_a
文档B（待比对）：/doc_b

比对维度：{{aspect}}（留空则全面比对）

请给出一致性比对报告。$tpl$,
  'deepseek-v4-pro',
  'high',
  '{}'::jsonb,
  '[]'::jsonb,
  '[]'::jsonb,
  $json$[{"key":"doc_a","type":"file","label":"文档A（基准）","help":".docx / .pdf / .txt","required":true,"default":null,"options":[],"file":{"accept":["application/vnd.openxmlformats-officedocument.wordprocessingml.document","application/pdf","text/plain"],"max_size_bytes":10485760,"multiple":false}},{"key":"doc_b","type":"file","label":"文档B（待比对）","help":".docx / .pdf / .txt","required":true,"default":null,"options":[],"file":{"accept":["application/vnd.openxmlformats-officedocument.wordprocessingml.document","application/pdf","text/plain"],"max_size_bytes":10485760,"multiple":false}},{"key":"aspect","type":"textarea","label":"比对维度","help":"如：条款一致性、数值一致性、术语一致性。留空则全面比对","required":false,"default":"","options":[],"file":null}]$json$::jsonb,
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR', now(),
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR', now(),
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR'
);

UPDATE app_registry.apps
SET current_version_id = 'appv_8972FBDFB5D2496D8937F65453',
    visibility = 'tenant',
    updated_at = now()
WHERE id = 'app_DA6D07BB330A495AA739928927';

-- ---------------------------------------------------------------------------
-- 4) 代码审查助手
-- ---------------------------------------------------------------------------
INSERT INTO app_registry.apps (id, tenant_id, name, description, icon, author_id, visibility)
VALUES (
  'app_E95C98A38AE142CB8DBA998009',
  'tnt_01J5DEVXX00000000000000001',
  '代码审查助手',
  '上传代码文件，从正确性/安全/性能/可读性等维度审查，输出分级问题清单与修复建议',
  '💻',
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR',
  'private'
);

INSERT INTO app_registry.app_versions (
  id, app_id, tenant_id, version, status,
  system_prompt, user_prompt_template, model, reasoning_effort,
  sampling_params, forced_skills, forced_mcp_server_ids, input_schema,
  submitted_by, submitted_at, reviewed_by, reviewed_at, created_by
) VALUES (
  'appv_FD9187B9EAB24A13BE5D29A72F',
  'app_E95C98A38AE142CB8DBA998009',
  'tnt_01J5DEVXX00000000000000001',
  '1',
  'published',
  $$你是一位资深代码审查工程师，拥有多年多语言代码审查经验。用户会上传一个代码文件，你需要进行全面审查。请先用 Read 工具读取 /workspace/inputs/ 下的代码文件，然后从以下维度审查。

审查维度：
1. 正确性：逻辑错误、边界条件、异常处理、并发问题。
2. 安全性：注入、敏感信息硬编码、越权、不安全依赖。
3. 性能：时间/空间复杂度、不必要的计算、资源泄漏。
4. 可读性与规范：命名、结构、注释、重复代码。
5. 潜在 bug 与健壮性。

输出格式（Markdown）：
## 一、代码概述
（功能、语言、结构概述）
## 二、问题清单
（按严重程度分组：### 严重（必须修复）/ ### 警告（建议修复）/ ### 建议（可选改进）
每条：位置（行号/代码片段）/ 问题 / 建议 / 修复示例（用代码块））
## 三、总体评价
（代码质量评估与改进优先级）$$,
  $tpl$请审查以下代码文件：/code

代码语言：{{language}}
审查关注点：{{context}}（留空则全面审查）

请给出代码审查报告。$tpl$,
  'deepseek-v4-pro',
  'high',
  '{}'::jsonb,
  '[]'::jsonb,
  '[]'::jsonb,
  $json$[{"key":"code","type":"file","label":"代码文件","help":"上传单个代码文件（.py/.js/.ts/.java/.go 等）","required":true,"default":null,"options":[],"file":{"accept":["text/plain","text/x-python","application/x-python","application/javascript","text/javascript","application/typescript","text/typescript","application/java","text/x-java","application/x-sh","text/x-sh","application/sql","application/octet-stream"],"max_size_bytes":5242880,"multiple":false}},{"key":"language","type":"select","label":"代码语言","help":"选择语言以便更精准审查","required":true,"default":"自动检测","options":[{"value":"自动检测","label":"自动检测"},{"value":"Python","label":"Python"},{"value":"JavaScript","label":"JavaScript"},{"value":"TypeScript","label":"TypeScript"},{"value":"Java","label":"Java"},{"value":"Go","label":"Go"},{"value":"其他","label":"其他"}],"file":null},{"key":"context","type":"textarea","label":"审查关注点","help":"代码背景或重点关注维度，如：安全性、性能、可维护性。留空则全面审查","required":false,"default":"","options":[],"file":null}]$json$::jsonb,
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR', now(),
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR', now(),
  'usr_01KT7TZ70AM2RK8P0C01KMBPRR'
);

UPDATE app_registry.apps
SET current_version_id = 'appv_FD9187B9EAB24A13BE5D29A72F',
    visibility = 'tenant',
    updated_at = now()
WHERE id = 'app_E95C98A38AE142CB8DBA998009';

COMMIT;
