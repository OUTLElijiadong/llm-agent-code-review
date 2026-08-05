# 自动生成的项目事实

> 本文件由 `scripts/generate_project_facts.py` 生成，请勿手工修改。

## 摘要

| 事实 | 数量/值 |
| --- | ---: |
| 业务 HTTP 路由 | 238 |
| HTTP 操作 | 238 |
| WebSocket 路由 | 1 |
| ORM 表 | 73 |
| Agent | 17 |
| Vue 页面 | 60 |
| 后端 Python 模块 | 280 |
| 后端测试文件 | 117 |
| 前端测试文件 | 23 |
| Alembic 迁移 | 27 |
| Alembic head | 027 |

## HTTP 路由

| 方法 | 路径 | 名称 |
| --- | --- | --- |
| GET | `/api/admin/agent-releases` | list_release_approvals |
| GET | `/api/admin/agent-releases/agents` | list_custom_agents |
| POST | `/api/admin/agent-releases/agents/{agent_id}/disable` | disable_agent |
| POST | `/api/admin/agent-releases/agents/{agent_id}/rollback/{release_id}` | rollback_agent |
| POST | `/api/admin/agent-releases/{approval_id}/approve` | approve_release |
| POST | `/api/admin/agent-releases/{approval_id}/reject` | reject_release |
| POST | `/api/admin/agent-releases/{approval_id}/revise` | admin_revise |
| GET | `/api/admin/approvals` | list_approvals |
| POST | `/api/admin/approvals/{item_id}/approve` | approve_item |
| POST | `/api/admin/approvals/{item_id}/reject` | reject_item |
| GET | `/api/admin/audit` | list_audit_logs |
| GET | `/api/admin/beta-codes` | list_beta_codes |
| POST | `/api/admin/beta-codes` | generate_beta_codes |
| POST | `/api/admin/beta-codes/{invite_id}/revoke` | revoke_beta_code |
| GET | `/api/admin/governance/agents` | list_governance_agents |
| GET | `/api/admin/governance/agents/{agent_code}` | get_governance_agent |
| PUT | `/api/admin/governance/agents/{agent_code}` | update_governance_agent |
| GET | `/api/admin/governance/agents/{agent_code}/knowledge` | list_agent_knowledge |
| GET | `/api/admin/governance/agents/{agent_code}/memory` | list_agent_memory |
| POST | `/api/admin/governance/agents/{agent_code}/memory` | create_agent_memory |
| POST | `/api/admin/governance/knowledge/crawl` | crawl_agent_knowledge_sources |
| POST | `/api/admin/governance/knowledge/docs` | create_agent_knowledge_doc |
| POST | `/api/admin/governance/knowledge/docs/{doc_id}/activate` | activate_agent_knowledge_doc |
| GET | `/api/admin/governance/knowledge/sources` | list_agent_knowledge_sources |
| POST | `/api/admin/governance/knowledge/sources` | upsert_agent_knowledge_source |
| GET | `/api/admin/governance/overview` | governance_overview |
| GET | `/api/admin/jobs` | list_jobs |
| GET | `/api/admin/jobs/runs` | list_job_runs |
| PUT | `/api/admin/jobs/{job_id}` | update_job |
| POST | `/api/admin/jobs/{job_id}/run` | run_job |
| GET | `/api/admin/llm/config` | get_config |
| PUT | `/api/admin/llm/config` | update_config |
| POST | `/api/admin/llm/test` | test_config |
| GET | `/api/admin/observability/alerts` | list_alerts |
| GET | `/api/admin/observability/alerts/unread` | list_unread_alerts |
| POST | `/api/admin/observability/alerts/{alert_id}/read` | mark_alert_read |
| POST | `/api/admin/observability/alerts/{alert_id}/resolve` | resolve_alert |
| GET | `/api/admin/observability/overview` | observability_overview |
| POST | `/api/admin/observability/security/run-monitor` | run_security_monitor_endpoint |
| GET | `/api/admin/observability/security/status` | security_status |
| GET | `/api/admin/overview/agents-activity` | overview_agents_activity |
| GET | `/api/admin/overview/geo` | overview_geo |
| GET | `/api/admin/overview/security` | overview_security |
| GET | `/api/admin/overview/system` | overview_system |
| GET | `/api/admin/policies` | list_policies |
| POST | `/api/admin/policies` | upsert_policy |
| GET | `/api/admin/policies/decisions` | list_policy_decisions |
| POST | `/api/admin/policies/evaluate` | evaluate_policy |
| GET | `/api/admin/rewards/events` | list_rewards |
| POST | `/api/admin/rewards/events` | create_reward |
| GET | `/api/admin/rollback/versions` | list_versions |
| POST | `/api/admin/rollback/versions` | create_version |
| POST | `/api/admin/rollback/versions/{version_id}/rollback` | rollback_version |
| GET | `/api/admin/tools/calls` | list_tool_calls |
| GET | `/api/admin/tools/permissions` | list_tool_permissions |
| POST | `/api/admin/tools/permissions` | upsert_tool_permission |
| GET | `/api/agent-catalog` | list_catalog |
| POST | `/api/agent-catalog/{agent_code}/invoke` | invoke_agent |
| GET | `/api/agent-responses/session` | get_agent_response_session |
| POST | `/api/agent-responses/stream` | stream_agent_response |
| GET | `/api/agent-studio/agent-versions/{version_id}` | get_agent_version |
| POST | `/api/agent-studio/agent-versions/{version_id}/skills` | bind_skill |
| POST | `/api/agent-studio/agent-versions/{version_id}/submit` | submit_version |
| POST | `/api/agent-studio/agent-versions/{version_id}/test` | test_version |
| POST | `/api/agent-studio/agent-versions/{version_id}/withdraw` | withdraw_version |
| GET | `/api/agent-studio/agents` | list_owned_agents |
| POST | `/api/agent-studio/agents` | create_agent |
| GET | `/api/agent-studio/agents/{agent_id}/versions` | list_agent_versions |
| POST | `/api/agent-studio/agents/{agent_id}/versions` | revise_agent |
| DELETE | `/api/agent-studio/bindings/{binding_id}` | unbind_skill |
| GET | `/api/agent-studio/skill-versions/{version_id}` | get_skill_version |
| GET | `/api/agent-studio/skills` | list_owned_skills |
| POST | `/api/agent-studio/skills` | create_skill |
| GET | `/api/agent-studio/skills/{skill_id}/versions` | list_skill_versions |
| POST | `/api/agent-studio/skills/{skill_id}/versions` | revise_skill |
| GET | `/api/agents` | list_agents |
| POST | `/api/agents/clarify` | submit_clarification |
| GET | `/api/agents/events` | stream_agent_events |
| GET | `/api/agents/metagpt/info` | get_metagpt_info |
| GET | `/api/agents/metagpt/preview` | preview_metagpt_environment |
| GET | `/api/agents/overview` | get_overview |
| GET | `/api/agents/runtime` | list_runtime_agents |
| GET | `/api/agents/runtime/summary` | get_runtime_summary |
| GET | `/api/agents/situation` | get_situation |
| GET | `/api/agents/skill-records` | list_skill_records |
| GET | `/api/agents/type-mappings` | list_type_mappings |
| GET | `/api/agents/usage` | get_usage |
| GET | `/api/agents/{agent_name}/skills` | list_agent_skills |
| POST | `/api/agents/{agent_name}/skills/{skill_name}/invoke` | invoke_agent_skill |
| GET | `/api/ai-logs` | list_logs |
| GET | `/api/ai-logs/{log_id}` | get_log |
| POST | `/api/ai-prompt/issue` | generate_for_issue |
| POST | `/api/ai-prompt/project` | generate_for_project |
| POST | `/api/ai-prompt/task` | generate_for_task |
| GET | `/api/ai-prompt/tools` | list_tools |
| POST | `/api/ai/analyze-folder` | agent_analyze_folder |
| POST | `/api/ai/detect-language` | agent_detect_language |
| DELETE | `/api/api-config` | delete_config |
| GET | `/api/api-config` | get_config |
| PUT | `/api/api-config` | save_config |
| POST | `/api/api-config/test` | test_connection |
| GET | `/api/auth/captcha` | get_captcha |
| POST | `/api/auth/change-password` | change_password |
| POST | `/api/auth/login` | login |
| POST | `/api/auth/logout` | logout |
| GET | `/api/auth/me` | me |
| POST | `/api/auth/register` | register |
| GET | `/api/code-files` | list_files |
| POST | `/api/code-files` | create_file |
| POST | `/api/code-files/upload` | upload_code |
| POST | `/api/code-files/upload-folder` | upload_folder |
| DELETE | `/api/code-files/{file_id}` | delete_file |
| GET | `/api/code-files/{file_id}` | get_file |
| PUT | `/api/code-files/{file_id}` | update_file |
| GET | `/api/code-files/{file_id}/download` | download_binary_file |
| GET | `/api/code-files/{file_id}/meta` | get_file_meta |
| POST | `/api/code-files/{file_id}/rename` | rename_file |
| GET | `/api/code-files/{file_id}/versions` | list_versions |
| GET | `/api/code-files/{file_id}/versions/{version_no}` | get_version |
| POST | `/api/code-files/{file_id}/versions/{version_no}/restore` | restore_version |
| GET | `/api/dashboard/issue-type-statistics` | issue_type_statistics |
| GET | `/api/dashboard/review-frequency` | review_frequency |
| GET | `/api/dashboard/risk-distribution` | risk_distribution |
| GET | `/api/dashboard/score-trend` | score_trend |
| GET | `/api/dashboard/summary` | summary |
| GET | `/api/discuss/start` | start_discussion |
| GET | `/api/evolution/eval-cases` | list_eval_cases |
| GET | `/api/evolution/experiences` | list_experiences |
| GET | `/api/evolution/feedback` | feedback_summary |
| GET | `/api/evolution/proposals` | list_proposals |
| GET | `/api/evolution/proposals/{proposal_id}` | get_proposal |
| POST | `/api/evolution/proposals/{proposal_id}/approve` | approve_proposal |
| POST | `/api/evolution/proposals/{proposal_id}/evaluate` | evaluate_proposal |
| POST | `/api/evolution/proposals/{proposal_id}/reject` | reject_proposal |
| POST | `/api/evolution/proposals/{proposal_id}/rollback` | rollback_proposal |
| POST | `/api/evolution/run` | run_evolution |
| POST | `/api/evolution/trigger` | trigger_evolution |
| GET | `/api/feedback` | list_feedback |
| POST | `/api/feedback` | create_feedback |
| GET | `/api/feedback/stats` | feedback_stats |
| GET | `/api/feedback/{feedback_id}` | get_feedback |
| PUT | `/api/feedback/{feedback_id}/reply` | reply_feedback |
| POST | `/api/forum/assist` | assist_draft |
| GET | `/api/forum/posts` | list_posts |
| POST | `/api/forum/posts` | create_post |
| DELETE | `/api/forum/posts/{post_id}` | delete_post |
| GET | `/api/forum/posts/{post_id}` | get_post |
| PUT | `/api/forum/posts/{post_id}` | update_post |
| PUT | `/api/forum/posts/{post_id}/pin` | pin_post |
| POST | `/api/forum/posts/{post_id}/replies` | create_reply |
| DELETE | `/api/forum/replies/{reply_id}` | delete_reply |
| GET | `/api/issues` | list_issues |
| POST | `/api/issues/batch-status` | batch_update_status |
| GET | `/api/issues/{issue_id}` | get_issue |
| PUT | `/api/issues/{issue_id}/status` | update_status |
| GET | `/api/knowledge/docs` | list_docs |
| POST | `/api/knowledge/docs` | add_doc |
| DELETE | `/api/knowledge/docs/{doc_id}` | delete_doc |
| GET | `/api/knowledge/embedding-config` | get_embedding_config |
| PUT | `/api/knowledge/embedding-config` | update_embedding_config |
| POST | `/api/knowledge/search` | search |
| GET | `/api/knowledge/stats` | stats |
| POST | `/api/knowledge/sync` | sync |
| GET | `/api/maintenance` | list_tickets |
| POST | `/api/maintenance` | create_ticket |
| GET | `/api/maintenance/stats` | ticket_stats |
| GET | `/api/maintenance/{ticket_id}` | get_ticket |
| POST | `/api/maintenance/{ticket_id}/close` | close_ticket |
| PUT | `/api/maintenance/{ticket_id}/handle` | handle_ticket |
| GET | `/api/me/profile` | get_profile |
| PUT | `/api/me/profile` | update_profile |
| POST | `/api/me/profile/relearn` | relearn_profile |
| GET | `/api/projects` | list_projects |
| POST | `/api/projects` | create_project |
| POST | `/api/projects/import-remote` | import_remote_project |
| DELETE | `/api/projects/{project_id}` | delete_project |
| GET | `/api/projects/{project_id}` | get_project |
| PUT | `/api/projects/{project_id}` | update_project |
| GET | `/api/projects/{project_id}/audit-source-archive` | get_audit_source_archive |
| POST | `/api/projects/{project_id}/audit-source-archive` | upload_audit_source_archive |
| GET | `/api/projects/{project_id}/audit-source-archive/result` | get_audit_source_archive_result |
| GET | `/api/projects/{project_id}/members` | list_members |
| POST | `/api/projects/{project_id}/members` | add_member |
| DELETE | `/api/projects/{project_id}/members/{user_id}` | remove_member |
| PUT | `/api/projects/{project_id}/members/{user_id}` | update_member_role |
| GET | `/api/projects/{project_id}/source-archive` | download_project_source |
| GET | `/api/rbac/menus` | list_menus_tree |
| GET | `/api/rbac/permissions` | list_permissions |
| GET | `/api/rbac/roles` | list_roles |
| POST | `/api/rbac/roles` | create_role |
| GET | `/api/rbac/roles/{role_code}/users` | list_users_by_role |
| DELETE | `/api/rbac/roles/{role_id}` | delete_role |
| PUT | `/api/rbac/roles/{role_id}` | update_role |
| GET | `/api/rbac/roles/{role_id}/data-scope` | get_role_data_scope |
| PUT | `/api/rbac/roles/{role_id}/data-scope` | update_role_data_scope |
| GET | `/api/rbac/roles/{role_id}/permissions` | list_role_permissions |
| PUT | `/api/rbac/roles/{role_id}/permissions` | assign_role_permissions |
| GET | `/api/rbac/users/{user_id}/data-scope` | get_user_data_scope |
| GET | `/api/rbac/users/{user_id}/menus` | list_user_menus |
| GET | `/api/rbac/users/{user_id}/permissions` | list_user_permissions |
| GET | `/api/rbac/users/{user_id}/roles` | list_user_roles |
| POST | `/api/rbac/users/{user_id}/roles` | assign_user_roles |
| GET | `/api/reports` | list_reports |
| POST | `/api/reports/generate` | generate_report |
| GET | `/api/reports/tasks/{task_id}` | preview_report |
| GET | `/api/reports/tasks/{task_id}/export` | export_report |
| GET | `/api/reports/templates` | list_templates |
| POST | `/api/reports/templates` | create_template |
| DELETE | `/api/reports/templates/{template_id}` | delete_template |
| PUT | `/api/reports/templates/{template_id}` | update_template |
| DELETE | `/api/reports/{task_id}` | delete_report |
| GET | `/api/reports/{task_id}` | get_report |
| GET | `/api/reports/{task_id}/export/pdf` | export_pdf |
| GET | `/api/reports/{task_id}/export/word` | export_word |
| POST | `/api/review/start` | start |
| GET | `/api/review/tasks` | list_tasks |
| DELETE | `/api/review/tasks/{task_id}` | delete_task |
| GET | `/api/review/tasks/{task_id}` | get_task |
| POST | `/api/review/tasks/{task_id}/cancel` | cancel_task |
| GET | `/api/review/tasks/{task_id}/issues` | list_task_issues |
| GET | `/api/rules` | list_rules |
| POST | `/api/rules` | create_rule |
| DELETE | `/api/rules/{rule_id}` | delete_rule |
| PUT | `/api/rules/{rule_id}` | update_rule |
| POST | `/api/rules/{rule_id}/toggle` | toggle_rule |
| GET | `/api/security/checklist` | get_checklist |
| GET | `/api/security/dashboard-summary` | dashboard_summary |
| GET | `/api/security/findings` | list_findings |
| POST | `/api/security/fullchain-audit` | fullchain_audit |
| POST | `/api/security/scan-all-projects` | scan_all_projects |
| POST | `/api/security/scan-file` | scan_file |
| POST | `/api/security/scan-project` | scan_project |
| POST | `/api/security/scan-task` | scan_task |
| GET | `/api/users` | list_users |
| DELETE | `/api/users/{user_id}` | delete_user |
| POST | `/api/users/{user_id}/reset-password` | reset_password |
| POST | `/api/users/{user_id}/role` | set_role |
| POST | `/api/users/{user_id}/toggle-status` | toggle_status |

## WebSocket

| 路径 | 名称 |
| --- | --- |
| `/api/ws/discuss/{session_id}` | ws_discuss |

## ORM 表

| 表 | 列数 |
| --- | ---: |
| `admin_chat_message` | 12 |
| `admin_chat_session` | 8 |
| `agent_alert` | 15 |
| `agent_artifact_version` | 9 |
| `agent_capability_alias` | 9 |
| `agent_job` | 10 |
| `agent_job_run` | 9 |
| `agent_knowledge_chunk` | 9 |
| `agent_knowledge_doc` | 13 |
| `agent_knowledge_source` | 9 |
| `agent_mcp_binding` | 9 |
| `agent_memory` | 10 |
| `agent_metric_snapshot` | 8 |
| `agent_profile` | 16 |
| `agent_reflection` | 9 |
| `agent_response_run` | 10 |
| `agent_reward_event` | 8 |
| `agent_skill_binding` | 9 |
| `agent_skill_record` | 12 |
| `agent_tool_execution` | 12 |
| `agent_tool_permission` | 9 |
| `ai_call_log` | 16 |
| `approval_item` | 15 |
| `audit_log` | 10 |
| `beta_invite_code` | 11 |
| `code_file` | 15 |
| `code_version` | 7 |
| `custom_agent` | 10 |
| `custom_agent_release` | 14 |
| `custom_agent_skill_binding` | 7 |
| `custom_agent_version` | 18 |
| `custom_skill` | 9 |
| `custom_skill_version` | 15 |
| `data_scope` | 6 |
| `eval_case` | 10 |
| `evolution_proposal` | 17 |
| `forum_post` | 11 |
| `forum_reply` | 7 |
| `knowledge_chunk` | 9 |
| `knowledge_doc` | 10 |
| `maintenance_ticket` | 12 |
| `malware_scan_log` | 9 |
| `mcp_server` | 16 |
| `mcp_tool` | 13 |
| `menu` | 12 |
| `ops_execution` | 15 |
| `permission` | 8 |
| `policy_decision_log` | 12 |
| `policy_rule` | 13 |
| `project` | 8 |
| `project_member` | 6 |
| `project_source_archive` | 24 |
| `report_template` | 9 |
| `review_experience` | 15 |
| `review_issue` | 29 |
| `review_report` | 7 |
| `review_rule` | 13 |
| `review_task` | 23 |
| `review_task_agent_release` | 8 |
| `review_task_file` | 4 |
| `role` | 9 |
| `role_permission` | 5 |
| `sandbox_artifact` | 11 |
| `sandbox_environment` | 27 |
| `sandbox_event` | 7 |
| `sandbox_worker` | 19 |
| `system_config` | 5 |
| `tool_call_log` | 17 |
| `user` | 12 |
| `user_api_config` | 9 |
| `user_feedback` | 11 |
| `user_profile` | 14 |
| `user_role` | 5 |

## Agent

| Code | 分类 | Skill 数 | 描述 |
| --- | --- | ---: | --- |
| `ai_prompt` | output | 3 | 把审查问题翻译成可粘贴给 Cursor/Copilot/ChatGPT/Claude Code 的修复提示词 |
| `chat_assistant` | frontline | 2 | PRISM 平台智能聊天助手, 可通过对话调控所有 Agent |
| `code_file_manager` | manager | 3 | 查询项目代码文件列表和详情 |
| `code_reviewer` | reviewer | 4 | 对代码片段执行智能审查,检测 Bug、安全、性能等问题 |
| `dashboard` | analytics | 3 | 获取平台统计数据: 汇总指标/风险分布/评分趋势/审查频次 |
| `evolution` | meta | 4 | 自进化代理:从审查反馈蒸馏规则进化提案,经闸门+审批后生效 |
| `language_detector` | analyzer | 3 | 根据项目名称和描述智能识别编程语言 |
| `operations` | operations | 19 | 最高管理员管理 Agent：全域巡检、安全监控与攻击溯源、备份治理、受批准变更、验证和回滚 |
| `orchestrator` | meta | 4 | 主调度 Agent, 协调所有子 Agent 完成全平台功能 |
| `project_analyzer` | analyzer | 4 | 根据文件夹名称和文件列表智能分析项目元数据 |
| `project_manager` | manager | 3 | 管理项目: 创建/查询/编辑/删除项目 |
| `reporter` | output | 4 | 查询审查报告列表和详情 |
| `review_orchestrator` | orchestrator | 4 | 启动代码审查任务/查询审查记录/列出审查问题 |
| `rule_manager` | manager | 3 | 管理审查规则: 列出/创建/启用/禁用 |
| `sandbox_deployer` | operations | 5 | 在隔离 worker 部署项目并管理预览、续期和关闭生命周期 |
| `security_sentinel` | security | 4 | 网络安全深度审查: OWASP Top10 / CWE / 敏感信息 / 项目级威胁建模 |
| `test_verifier` | review | 4 | 调用隔离 worker 执行项目级动态白盒、黑盒或组合测试 |

## Vue 页面

- `frontend/src/views/admin/AdminOverview.vue`
- `frontend/src/views/admin/AgentGovernance.vue`
- `frontend/src/views/admin/AgentReleaseAdmin.vue`
- `frontend/src/views/admin/AiLogList.vue`
- `frontend/src/views/admin/ApprovalCenter.vue`
- `frontend/src/views/admin/BetaCodeAdmin.vue`
- `frontend/src/views/admin/EmbeddingConfig.vue`
- `frontend/src/views/admin/EvolutionCenter.vue`
- `frontend/src/views/admin/GovernanceWorkstation.vue`
- `frontend/src/views/admin/JobCenter.vue`
- `frontend/src/views/admin/KnowledgeGovernance.vue`
- `frontend/src/views/admin/LlmConfig.vue`
- `frontend/src/views/admin/McpWorkerGovernance 2.vue`
- `frontend/src/views/admin/McpWorkerGovernance.vue`
- `frontend/src/views/admin/ObservabilityCenter.vue`
- `frontend/src/views/admin/PermissionList.vue`
- `frontend/src/views/admin/PolicyCenter.vue`
- `frontend/src/views/admin/RewardCenter.vue`
- `frontend/src/views/admin/RoleManage.vue`
- `frontend/src/views/admin/RollbackCenter.vue`
- `frontend/src/views/admin/SkillManager.vue`
- `frontend/src/views/admin/SystemAudit.vue`
- `frontend/src/views/admin/ToolGovernance.vue`
- `frontend/src/views/admin/UserManage.vue`
- `frontend/src/views/admin/UserRoleAssign.vue`
- `frontend/src/views/agent/AgentCenter.vue`
- `frontend/src/views/agent/AgentStudio.vue`
- `frontend/src/views/auth/Login.vue`
- `frontend/src/views/auth/Register.vue`
- `frontend/src/views/code/CodeEditor.vue`
- `frontend/src/views/code/CodeFileList.vue`
- `frontend/src/views/code/CodeHub.vue`
- `frontend/src/views/code/VersionHistory.vue`
- `frontend/src/views/dashboard/Dashboard.vue`
- `frontend/src/views/error/Forbidden.vue`
- `frontend/src/views/error/NotFound.vue`
- `frontend/src/views/forum/ForumList.vue`
- `frontend/src/views/forum/ForumPostDetail.vue`
- `frontend/src/views/forum/ForumPostEdit.vue`
- `frontend/src/views/issue/IssueHub.vue`
- `frontend/src/views/knowledge/KnowledgeBase.vue`
- `frontend/src/views/profile/ApiConfig.vue`
- `frontend/src/views/profile/ChangePassword.vue`
- `frontend/src/views/profile/PersonalizationCenter.vue`
- `frontend/src/views/profile/ProfileCenter.vue`
- `frontend/src/views/project/ProjectDetail.vue`
- `frontend/src/views/project/ProjectForm.vue`
- `frontend/src/views/project/ProjectList.vue`
- `frontend/src/views/report/ReportDetail.vue`
- `frontend/src/views/report/ReportList.vue`
- `frontend/src/views/report/ReportTemplateManage.vue`
- `frontend/src/views/review/ReviewStart.vue`
- `frontend/src/views/review/ReviewTaskDetail.vue`
- `frontend/src/views/review/ReviewTaskList.vue`
- `frontend/src/views/rule/RuleConfig.vue`
- `frontend/src/views/sandbox/SandboxWorkstation 2.vue`
- `frontend/src/views/sandbox/SandboxWorkstation.vue`
- `frontend/src/views/security/SecurityCenter.vue`
- `frontend/src/views/support/FeedbackCenter.vue`
- `frontend/src/views/support/MaintenanceCenter.vue`

## Alembic 迁移

| Revision | Down revision | 文件 |
| --- | --- | --- |
| `001` | `-` | `backend/alembic/versions/001_add_user_api_config.py` |
| `002` | `001` | `backend/alembic/versions/002_agent_governance_platform.py` |
| `003` | `002` | `backend/alembic/versions/003_review_issue_vuln_metadata.py` |
| `004` | `003` | `backend/alembic/versions/004_project_member.py` |
| `005` | `004` | `backend/alembic/versions/005_agent_skill_evolution.py` |
| `006` | `005` | `backend/alembic/versions/006_review_issue_vuln_metadata_full.py` |
| `007` | `006` | `backend/alembic/versions/007_rbac_tables.py` |
| `008` | `007` | `backend/alembic/versions/008_report_template_malware_log.py` |
| `009` | `008` | `backend/alembic/versions/009_enlarge_review_issue_owasp_cwe.py` |
| `010` | `009` | `backend/alembic/versions/010_seed_report_template_manage.py` |
| `011` | `010` | `backend/alembic/versions/011_add_user_last_login_ip.py` |
| `012` | `011` | `backend/alembic/versions/012_add_copilot_request_id.py` |
| `013` | `012` | `backend/alembic/versions/013_add_direct_copilot_request_id.py` |
| `014` | `013` | `backend/alembic/versions/014_beta_invite_codes.py` |
| `015` | `014` | `backend/alembic/versions/015_custom_agent_studio.py` |
| `016` | `015` | `backend/alembic/versions/016_admin_copilot_ops.py` |
| `017` | `016` | `backend/alembic/versions/017_agent_response_runs.py` |
| `018` | `017` | `backend/alembic/versions/018_expand_agent_response_payloads.py` |
| `019` | `018` | `backend/alembic/versions/019_server_ops_permissions.py` |
| `020` | `019` | `backend/alembic/versions/020_manager_admin_capability_contract.py` |
| `021` | `020` | `backend/alembic/versions/021_grant_project_import_to_users.py` |
| `022` | `021` | `backend/alembic/versions/022_unique_super_admin.py` |
| `023` | `022` | `backend/alembic/versions/023_agent_capabilities_mcp_sandbox.py` |
| `024` | `023` | `backend/alembic/versions/024_quarantined_source_archives.py` |
| `025` | `024` | `backend/alembic/versions/025_seed_capability_aliases.py` |
| `026` | `025` | `backend/alembic/versions/026_expand_source_binary_capacity.py` |
| `027` | `026` | `backend/alembic/versions/027_add_security_alert_fields.py` |
