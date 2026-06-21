SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS code_review
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE code_review;

CREATE TABLE IF NOT EXISTS user (
    id           BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    username     VARCHAR(50)  NOT NULL                COMMENT '用户名',
    password     VARCHAR(255) NOT NULL                COMMENT 'bcrypt加密',
    email        VARCHAR(100) DEFAULT NULL            COMMENT '邮箱',
    nickname     VARCHAR(50)  DEFAULT NULL            COMMENT '昵称',
    role         VARCHAR(20)  NOT NULL DEFAULT 'user' COMMENT 'admin/user/reviewer',
    status       TINYINT      NOT NULL DEFAULT 1      COMMENT '1=启用,0=禁用',
    last_login   DATETIME     DEFAULT NULL            COMMENT '最后登录时间',
    token_version INT         NOT NULL DEFAULT 0      COMMENT '令牌版本:改密/禁用/重置时递增使旧JWT失效',
    create_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_username (username),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表';

CREATE TABLE IF NOT EXISTS project (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    user_id       BIGINT       NOT NULL                 COMMENT '所属用户',
    project_name  VARCHAR(100) NOT NULL                 COMMENT '项目名',
    description   VARCHAR(500) DEFAULT NULL,
    language      VARCHAR(50)  DEFAULT NULL             COMMENT '主语言',
    status        VARCHAR(20)  NOT NULL DEFAULT 'active' COMMENT 'active/archived/deleted',
    create_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_proj_name (user_id, project_name),
    KEY idx_user_status (user_id, status),
    KEY idx_create_time (create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目表';

CREATE TABLE IF NOT EXISTS code_file (
    id           BIGINT       NOT NULL AUTO_INCREMENT,
    project_id   BIGINT       NOT NULL,
    file_name    VARCHAR(255) NOT NULL                  COMMENT '文件名',
    file_path    VARCHAR(500) DEFAULT NULL              COMMENT '逻辑路径',
    language     VARCHAR(50)  NOT NULL                  COMMENT '语言标识',
    size_bytes   INT          NOT NULL DEFAULT 0        COMMENT '字节数',
    line_count   INT          NOT NULL DEFAULT 0        COMMENT '行数',
    version_no   INT          NOT NULL DEFAULT 1        COMMENT '当前版本号',
    content      LONGTEXT     NOT NULL                  COMMENT '代码内容UTF-8',
    status       VARCHAR(20)  NOT NULL DEFAULT 'active' COMMENT 'active/deleted',
    create_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_project_status (project_id, status),
    KEY idx_project_lang (project_id, language),
    KEY idx_create_time (create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代码文件表';

CREATE TABLE IF NOT EXISTS code_version (
    id           BIGINT     NOT NULL AUTO_INCREMENT,
    file_id      BIGINT     NOT NULL,
    version_no   INT        NOT NULL,
    content      LONGTEXT   NOT NULL,
    change_desc  VARCHAR(255) DEFAULT NULL              COMMENT '修改说明',
    operator_id  BIGINT     DEFAULT NULL                COMMENT '操作人',
    create_time  DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_file_version (file_id, version_no),
    KEY idx_create_time (create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代码文件历史版本';

CREATE TABLE IF NOT EXISTS review_rule (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    user_id       BIGINT       DEFAULT NULL              COMMENT 'NULL表示系统内置规则',
    rule_code     VARCHAR(50)  NOT NULL                  COMMENT '机器标识',
    rule_name     VARCHAR(100) NOT NULL                  COMMENT '展示名称',
    rule_type     VARCHAR(50)  NOT NULL                  COMMENT '类型分组',
    rule_content  TEXT         NOT NULL                  COMMENT 'Prompt片段',
    enabled       TINYINT      NOT NULL DEFAULT 1        COMMENT '1=启用,0=禁用',
    is_builtin    TINYINT      NOT NULL DEFAULT 0        COMMENT '1=内置不可删',
    sort_order    INT          NOT NULL DEFAULT 0,
    create_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_rule_code (user_id, rule_code),
    KEY idx_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='审查规则表';

CREATE TABLE IF NOT EXISTS review_task (
    id                BIGINT       NOT NULL AUTO_INCREMENT,
    user_id           BIGINT       NOT NULL,
    project_id        BIGINT       NOT NULL,
    task_name         VARCHAR(100) DEFAULT NULL,
    review_type       VARCHAR(50)  NOT NULL DEFAULT 'standard' COMMENT 'quick/standard/full',
    status            VARCHAR(20)  NOT NULL DEFAULT 'pending'  COMMENT 'pending/running/success/failed/cancelled',
    total_files       INT          NOT NULL DEFAULT 0,
    processed_files   INT          NOT NULL DEFAULT 0,
    total_issues      INT          NOT NULL DEFAULT 0,
    severe_issues     INT          NOT NULL DEFAULT 0,
    high_issues       INT          NOT NULL DEFAULT 0,
    medium_issues     INT          NOT NULL DEFAULT 0,
    low_issues        INT          NOT NULL DEFAULT 0,
    score             INT          NOT NULL DEFAULT 0   COMMENT '综合评分0-100',
    summary           TEXT         DEFAULT NULL         COMMENT 'AI总体评价',
    rules_snapshot    JSON         DEFAULT NULL         COMMENT '本次启用的规则快照',
    model_name        VARCHAR(50)  DEFAULT NULL,
    start_time        DATETIME     DEFAULT NULL,
    end_time          DATETIME     DEFAULT NULL,
    duration_ms       INT          NOT NULL DEFAULT 0   COMMENT '耗时毫秒',
    error_message     VARCHAR(500) DEFAULT NULL,
    create_time       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_user_create (user_id, create_time),
    KEY idx_project_status (project_id, status),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='审查任务表';

CREATE TABLE IF NOT EXISTS review_task_file (
    id           BIGINT   NOT NULL AUTO_INCREMENT,
    task_id      BIGINT   NOT NULL,
    file_id      BIGINT   NOT NULL,
    create_time  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_task_file (task_id, file_id),
    KEY idx_task (task_id),
    KEY idx_file (file_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='审查任务文件关联表';

CREATE TABLE IF NOT EXISTS review_issue (
    id           BIGINT       NOT NULL AUTO_INCREMENT,
    task_id      BIGINT       NOT NULL,
    file_id      BIGINT       DEFAULT NULL,
    file_name    VARCHAR(255) DEFAULT NULL              COMMENT '冗余便于报告导出',
    line_number  INT          DEFAULT NULL              COMMENT '问题所在行',
    end_line     INT          DEFAULT NULL              COMMENT '结束行',
    issue_type   VARCHAR(50)  NOT NULL                  COMMENT '问题类型枚举',
    severity     VARCHAR(20)  NOT NULL                  COMMENT '严重/高/中/低',
    title        VARCHAR(200) DEFAULT NULL,
    description  TEXT         NOT NULL,
    suggestion   TEXT         DEFAULT NULL,
    fixed_code   LONGTEXT     DEFAULT NULL,
    status       VARCHAR(20)  NOT NULL DEFAULT 'unfixed' COMMENT 'unfixed/fixed/ignored/pending_review',
    handled_by   BIGINT       DEFAULT NULL              COMMENT '状态变更人',
    handled_at   DATETIME     DEFAULT NULL,
    create_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_task_severity (task_id, severity),
    KEY idx_task_status (task_id, status),
    KEY idx_file (file_id),
    KEY idx_create_time (create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='审查问题表';

CREATE TABLE IF NOT EXISTS ai_call_log (
    id             BIGINT       NOT NULL AUTO_INCREMENT,
    task_id        BIGINT       DEFAULT NULL,
    user_id        BIGINT       DEFAULT NULL,
    file_id        BIGINT       DEFAULT NULL,
    chunk_index    INT          DEFAULT NULL            COMMENT '分片序号',
    model_name     VARCHAR(50)  NOT NULL,
    prompt_tokens  INT          DEFAULT NULL,
    completion_tokens INT       DEFAULT NULL,
    total_tokens   INT          DEFAULT NULL,
    duration_ms    INT          DEFAULT NULL,
    prompt         LONGTEXT     DEFAULT NULL            COMMENT 'user prompt',
    response       LONGTEXT     DEFAULT NULL            COMMENT '原始返回',
    status         VARCHAR(20)  NOT NULL DEFAULT 'success' COMMENT 'success/failed/retry',
    error_message  VARCHAR(500) DEFAULT NULL,
    create_time    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_task (task_id),
    KEY idx_user_create (user_id, create_time),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI调用日志';

CREATE TABLE IF NOT EXISTS review_report (
    id            BIGINT     NOT NULL AUTO_INCREMENT,
    task_id       BIGINT     NOT NULL,
    user_id       BIGINT     NOT NULL,
    content_json  JSON       NOT NULL                   COMMENT '报告结构化数据',
    summary       TEXT       DEFAULT NULL,
    score         INT        NOT NULL DEFAULT 0,
    create_time   DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_task (task_id),
    KEY idx_user_create (user_id, create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='审查报告快照';

CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGINT       NOT NULL AUTO_INCREMENT,
    actor_id     BIGINT       DEFAULT NULL              COMMENT '操作者用户ID; 系统操作可为 NULL',
    actor_name   VARCHAR(80)  DEFAULT NULL              COMMENT '操作者用户名快照',
    action       VARCHAR(40)  NOT NULL                  COMMENT 'login/user/rule/ai/project/agent 等',
    target_type  VARCHAR(40)  DEFAULT NULL              COMMENT 'user/project/rule/agent/...',
    target_id    VARCHAR(80)  DEFAULT NULL              COMMENT '对象ID或键',
    detail       TEXT         DEFAULT NULL              COMMENT '操作说明',
    status       VARCHAR(20)  NOT NULL DEFAULT 'success' COMMENT 'success/failed',
    ip           VARCHAR(64)  DEFAULT NULL              COMMENT '请求来源 IP',
    create_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_action_time (action, create_time),
    KEY idx_actor_time (actor_id, create_time),
    KEY idx_create_time (create_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作审计日志';

-- ===== Agent 自进化 (v3.0) =====

CREATE TABLE IF NOT EXISTS review_experience (
    id                   BIGINT       NOT NULL AUTO_INCREMENT,
    fingerprint          VARCHAR(64)  NOT NULL                 COMMENT '问题指纹:issue_type+归一化模式哈希',
    language             VARCHAR(30)  DEFAULT '*'              COMMENT '适用语言,*=通用',
    issue_type           VARCHAR(50)  NOT NULL                 COMMENT '问题类型(中文枚举)',
    title                VARCHAR(200) DEFAULT NULL             COMMENT '代表性问题标题',
    code_pattern         LONGTEXT     DEFAULT NULL             COMMENT '脱敏后代表性代码片段',
    canonical_suggestion TEXT         DEFAULT NULL             COMMENT '优质修复建议(取自被采纳案例)',
    accepted_count       INT          NOT NULL DEFAULT 0       COMMENT '被采纳(fixed)次数',
    rejected_count       INT          NOT NULL DEFAULT 0       COMMENT '被忽略(ignored)次数',
    weight               DOUBLE       NOT NULL DEFAULT 0       COMMENT '时间衰减后权重',
    last_seen            DATETIME     DEFAULT NULL             COMMENT '最近出现时间(UTC)',
    project_id           BIGINT       DEFAULT NULL             COMMENT '作用域项目,NULL=全局',
    user_id              BIGINT       DEFAULT NULL             COMMENT '作用域用户,NULL=全局',
    create_time          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_fingerprint (fingerprint),
    KEY idx_lang_weight (language, weight)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='经验记忆库(自进化L1)';

CREATE TABLE IF NOT EXISTS evolution_proposal (
    id               BIGINT       NOT NULL AUTO_INCREMENT,
    proposal_type    VARCHAR(30)  NOT NULL                    COMMENT 'new_rule/disable_rule/adjust_severity/narrow_language/new_fewshot',
    target_rule_id   BIGINT       DEFAULT NULL                COMMENT '针对已有规则的提案指向',
    title            VARCHAR(200) NOT NULL                    COMMENT '提案摘要',
    payload          TEXT         NOT NULL                    COMMENT '提案内容 JSON',
    evidence         TEXT         DEFAULT NULL                COMMENT '支撑证据 JSON',
    status           VARCHAR(20)  NOT NULL DEFAULT 'pending'  COMMENT 'pending/eval_passed/eval_failed/approved/rejected/promoted/rolled_back',
    eval_score       TEXT         DEFAULT NULL                COMMENT '评估闸门跑分 JSON',
    applied_rule_id  BIGINT       DEFAULT NULL                COMMENT 'promote 后改动的规则 id',
    applied_snapshot TEXT         DEFAULT NULL                COMMENT '改动前状态 JSON,供回滚',
    created_by       VARCHAR(50)  NOT NULL DEFAULT 'evolution_agent',
    reviewed_by      BIGINT       DEFAULT NULL                COMMENT '审批人(admin)',
    reviewed_at      DATETIME     DEFAULT NULL                COMMENT '审批时间(UTC)',
    note             VARCHAR(500) DEFAULT NULL                COMMENT '驳回原因/回滚说明',
    create_time      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_status (status),
    KEY idx_type (proposal_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='进化提案(自进化L2/L3)';

CREATE TABLE IF NOT EXISTS eval_case (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    name            VARCHAR(100) NOT NULL                     COMMENT '用例名',
    language        VARCHAR(30)  DEFAULT '*'                  COMMENT '语言',
    code            LONGTEXT     NOT NULL                     COMMENT '代码片段',
    expected_issues TEXT         NOT NULL                     COMMENT '期望命中 JSON: [{issue_type, keyword?}]',
    tags            VARCHAR(200) DEFAULT NULL                 COMMENT '分类标签',
    enabled         TINYINT      NOT NULL DEFAULT 1           COMMENT '1=纳入闸门',
    source          VARCHAR(30)  NOT NULL DEFAULT 'seed'      COMMENT 'seed/from_feedback',
    create_time     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_time     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='黄金回归集(自进化评估闸门)';
