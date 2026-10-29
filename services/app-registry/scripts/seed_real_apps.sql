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
-- 1) Contract Review Assistant
-- ---------------------------------------------------------------------------
INSERT INTO app_registry.apps (id, tenant_id, name, description, icon, author_id, visibility)
VALUES (
  'app_14F4353FDCB04C0291D6C79201',
  'tnt_01J5DEVXX00000000000000001',
  'Contract Review Assistant',
  'Upload a contract to flag risky, one-sided and missing clauses and get a structured review report',
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
  $$You are a senior contract review specialist with 15 years of experience, well versed in general commercial contract practice and widely accepted international contracting norms (and able to flag where the governing law may change the analysis). The user will upload a contract and specify the review perspective and focus areas. Use the Read tool to read the contract under /workspace/inputs/ (for .docx or .pdf files, first use Bash to run Python and extract the text), then review it systematically.

Review requirements:
- Base everything strictly on the contract text; do not speculate about terms that are not agreed. Call out missing key information explicitly.
- Identify: risky clauses (unfavourable to the chosen perspective), one-sided clauses, ambiguous wording, clauses that may be unlawful or unenforceable under the governing law, and missing essential clauses.
- Give actionable revision advice for every risk, with suggested clause wording where useful.
- Note the governing law and jurisdiction clause; if it is absent or unclear, say so, and do not present jurisdiction-specific conclusions as definitive legal advice.

Output format (Markdown):
## 1. Contract overview
(Contract type, parties, subject matter, term, amounts and other key elements)
## 2. Risk summary
(Number of high / medium / low risks; one sentence on the 1-3 most critical issues)
## 3. Clause-by-clause risk review
(Each item: **[Risk level] Clause topic** / Excerpt: "..." / Issue / Suggested revision (with wording))
## 4. Missing clauses
(Clauses this type of contract normally contains but this one does not)
## 5. Overall recommendation
(Must change before signing / Should change / Acceptable, with priorities)$$,
  $tpl$Please review the following contract: /contract

Review perspective: {{role}}
Focus areas: {{focus}}

Produce the structured report described in the review requirements.$tpl$,
  'deepseek-v4-pro',
  'high',
  '{}'::jsonb,
  '[]'::jsonb,
  '[]'::jsonb,
  $json$[{"key":"contract","type":"file","label":"Contract file","help":"Upload the contract to review (.docx or .pdf)","required":true,"default":null,"options":[],"file":{"accept":["application/vnd.openxmlformats-officedocument.wordprocessingml.document","application/pdf"],"max_size_bytes":10485760,"multiple":false}},{"key":"role","type":"select","label":"Review perspective","help":"Whose perspective to assess the risks from","required":true,"default":"Neutral review","options":[{"value":"Buyer / client","label":"Buyer / client (paying party)"},{"value":"Supplier / provider","label":"Supplier / provider (receiving party)"},{"value":"Neutral review","label":"Neutral review"}],"file":null},{"key":"focus","type":"textarea","label":"Focus areas","help":"e.g. payment terms, liability for breach, intellectual property, confidentiality. Leave blank for a full review","required":false,"default":"","options":[],"file":null}]$json$::jsonb,
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
-- 2) Excel Data Analysis Assistant
-- ---------------------------------------------------------------------------
INSERT INTO app_registry.apps (id, tenant_id, name, description, icon, author_id, visibility)
VALUES (
  'app_8E9590D5B2AC410A94EC2F97D0',
  'tnt_01J5DEVXX00000000000000001',
  'Excel Data Analysis Assistant',
  'Upload an Excel/CSV file, describe the analysis in plain language, and get it done with Python on your real data',
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
  $$You are a senior data analyst who is an expert at processing Excel/CSV data with Python (pandas, openpyxl). The user will upload a data file and describe what they want analysed. You must actually run code against the real data; never assume or invent the data's contents.

Workflow:
1. Use Bash to run Python: first load the file under /workspace/inputs/ with pandas/openpyxl and inspect its structure (shape, column names, dtypes, first rows, missing values, basic statistics).
2. Write and run the analysis code the request calls for.
3. If a visualisation helps, generate charts with matplotlib, save them to /workspace/outputs/, and reference them in the report.
4. Draw conclusions only from the real code output.

Output format (Markdown):
## 1. Data overview
(Rows and columns, field descriptions, data-quality observations)
## 2. Analysis
(Key code blocks + a summary of their output)
## 3. Findings
(Answer the user's request with data-backed conclusions and recommendations)$$,
  $tpl$Please analyse the following data file: /file

Analysis request: {{task}}

Read the file to understand its structure first, then complete the analysis as requested.$tpl$,
  'deepseek-v4-pro',
  'high',
  '{}'::jsonb,
  '[]'::jsonb,
  '[]'::jsonb,
  $json$[{"key":"file","type":"file","label":"Data file","help":"Upload an Excel (.xlsx) or CSV file","required":true,"default":null,"options":[],"file":{"accept":["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","text/csv","application/vnd.ms-excel"],"max_size_bytes":10485760,"multiple":false}},{"key":"task","type":"textarea","label":"Analysis request","help":"What do you want to do with this data? e.g. summary statistics, find outliers, data-cleaning suggestions, charts","required":true,"default":"","options":[],"file":null}]$json$::jsonb,
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
-- 3) Document Consistency Check
-- ---------------------------------------------------------------------------
INSERT INTO app_registry.apps (id, tenant_id, name, description, icon, author_id, visibility)
VALUES (
  'app_DA6D07BB330A495AA739928927',
  'tnt_01J5DEVXX00000000000000001',
  'Document Consistency Check',
  'Upload two documents to compare them point by point and get a list of differences and their impact',
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
  $$You are a document consistency review specialist. The user will provide two documents (Document A is the baseline, Document B is compared against it), and you must compare their content point by point. First use the Read tool to read both files under /workspace/inputs/ (for .docx/.pdf, use Bash to run Python and extract the text), then compare them systematically.

Comparison requirements:
- State differences objectively; do not judge which version is better.
- Focus on the consistency of key facts, figures, clauses, terminology, dates and party details.
- Compare along the dimensions the user specifies; if none are given, do a full comparison.

Output format (Markdown):
## 1. Document overview
(The subject and key elements of A and of B)
## 2. Consistent items
(Key content that matches in both documents, briefly listed)
## 3. Inconsistent items
(Each item: dimension / Document A content / Document B content / difference and potential impact)
## 4. Content found in only one document
(Only in A / only in B)
## 5. Conclusion and recommendations
(Overall consistency assessment and points to verify or revise)$$,
  $tpl$Please check the following two documents for consistency.

Document A (baseline): /doc_a
Document B (to compare): /doc_b

Comparison dimensions: {{aspect}} (full comparison if blank)

Produce a consistency comparison report.$tpl$,
  'deepseek-v4-pro',
  'high',
  '{}'::jsonb,
  '[]'::jsonb,
  '[]'::jsonb,
  $json$[{"key":"doc_a","type":"file","label":"Document A (baseline)","help":".docx / .pdf / .txt","required":true,"default":null,"options":[],"file":{"accept":["application/vnd.openxmlformats-officedocument.wordprocessingml.document","application/pdf","text/plain"],"max_size_bytes":10485760,"multiple":false}},{"key":"doc_b","type":"file","label":"Document B (to compare)","help":".docx / .pdf / .txt","required":true,"default":null,"options":[],"file":{"accept":["application/vnd.openxmlformats-officedocument.wordprocessingml.document","application/pdf","text/plain"],"max_size_bytes":10485760,"multiple":false}},{"key":"aspect","type":"textarea","label":"Comparison dimensions","help":"e.g. clause consistency, figure consistency, terminology consistency. Leave blank for a full comparison","required":false,"default":"","options":[],"file":null}]$json$::jsonb,
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
-- 4) Code Review Assistant
-- ---------------------------------------------------------------------------
INSERT INTO app_registry.apps (id, tenant_id, name, description, icon, author_id, visibility)
VALUES (
  'app_E95C98A38AE142CB8DBA998009',
  'tnt_01J5DEVXX00000000000000001',
  'Code Review Assistant',
  'Upload a code file for a review of correctness, security, performance and readability, with a prioritised issue list and fixes',
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
  $$You are a senior code reviewer with many years of experience reviewing code in multiple languages. The user will upload a code file for a thorough review. First use the Read tool to read the code file under /workspace/inputs/, then review it along the dimensions below.

Review dimensions:
1. Correctness: logic errors, edge cases, error handling, concurrency issues.
2. Security: injection, hard-coded secrets, broken access control, unsafe dependencies.
3. Performance: time/space complexity, unnecessary computation, resource leaks.
4. Readability and conventions: naming, structure, comments, duplicated code.
5. Latent bugs and robustness.

Output format (Markdown):
## 1. Code overview
(Purpose, language, structure)
## 2. Issues
(Grouped by severity: ### Critical (must fix) / ### Warning (should fix) / ### Suggestion (optional improvement)
Each item: location (line number / snippet) / issue / recommendation / fix example (in a code block))
## 3. Overall assessment
(Code quality assessment and improvement priorities)$$,
  $tpl$Please review the following code file: /code

Language: {{language}}
Review focus: {{context}} (full review if blank)

Produce a code review report.$tpl$,
  'deepseek-v4-pro',
  'high',
  '{}'::jsonb,
  '[]'::jsonb,
  '[]'::jsonb,
  $json$[{"key":"code","type":"file","label":"Code file","help":"Upload a single code file (.py/.js/.ts/.java/.go, etc.)","required":true,"default":null,"options":[],"file":{"accept":["text/plain","text/x-python","application/x-python","application/javascript","text/javascript","application/typescript","text/typescript","application/java","text/x-java","application/x-sh","text/x-sh","application/sql","application/octet-stream"],"max_size_bytes":5242880,"multiple":false}},{"key":"language","type":"select","label":"Language","help":"Pick the language for a more precise review","required":true,"default":"Auto-detect","options":[{"value":"Auto-detect","label":"Auto-detect"},{"value":"Python","label":"Python"},{"value":"JavaScript","label":"JavaScript"},{"value":"TypeScript","label":"TypeScript"},{"value":"Java","label":"Java"},{"value":"Go","label":"Go"},{"value":"Other","label":"Other"}],"file":null},{"key":"context","type":"textarea","label":"Review focus","help":"Background or dimensions to focus on, e.g. security, performance, maintainability. Leave blank for a full review","required":false,"default":"","options":[],"file":null}]$json$::jsonb,
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
