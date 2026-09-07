-- ============================================================================
-- 高尔夫挥杆分析 —— MySQL 初始化 DDL
--
-- 目标库: golf (MySQL 8.0.46, utf8mb4 / utf8mb4_0900_ai_ci)
-- 数据源: backend/app/schemas.py (AnalysisResult / PhaseResult / StageMetric /
--         RiskItem / GlobalMetrics / VideoMeta / TaskState)
--
-- 设计约定:
--   1. 全小写下划线命名 —— 实例 lower_case_table_names=0 (区分大小写)
--   2. 不加物理外键 —— MVP 阶段用逻辑关联 (task_id VARCHAR)，
--      避免级联约束把「清理过期任务」变成高风险操作
--   3. 所有子表按 task_id 建索引 —— 主查询模式是「按任务取全部结果」
--   4. result_json 保留完整响应快照 —— 结构演进时旧数据仍可完整回溯
--
-- 回滚: 见同目录 001_rollback.sql
-- ============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ---------------------------------------------------------------------------
-- 1. 任务主表
--    1 行 = 1 次分析任务。VideoMeta 与 GlobalMetrics 的固定字段扁平化至此，
--    避免为「1:1 关系」多建两张表带来的无谓 JOIN。
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS tasks;
CREATE TABLE tasks (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

    -- 业务主键 (uuid4().hex[:12])
    task_id             VARCHAR(32)     NOT NULL                COMMENT '对外任务ID',

    -- 状态机 (TaskStatus / TaskState)
    status              VARCHAR(16)     NOT NULL DEFAULT 'pending' COMMENT 'pending|processing|success|failed',
    progress            TINYINT UNSIGNED NOT NULL DEFAULT 0      COMMENT '0~100',
    step                TINYINT UNSIGNED NOT NULL DEFAULT 1      COMMENT '1~4 进度步骤(小程序进度条)',
    step_text           VARCHAR(64)     NOT NULL DEFAULT ''      COMMENT 'PDD 字符串 step',
    message             VARCHAR(128)    NOT NULL DEFAULT '排队中',
    error_code          VARCHAR(32)     NULL                     COMMENT 'NO_PERSON|NO_SWING|BAD_ORIENTATION|...',
    error_message       VARCHAR(255)    NULL,

    -- VideoMeta (扁平化)
    camera_view         VARCHAR(16)     NOT NULL DEFAULT 'face_on' COMMENT 'face_on|down_the_line',
    fps                 FLOAT           NOT NULL DEFAULT 0,
    duration            FLOAT           NOT NULL DEFAULT 0       COMMENT '秒',
    width               INT UNSIGNED    NOT NULL DEFAULT 0,
    height              INT UNSIGNED    NOT NULL DEFAULT 0,
    frame_count         INT UNSIGNED    NOT NULL DEFAULT 0,
    total_frames        INT UNSIGNED    NOT NULL DEFAULT 0       COMMENT 'PDD 字段名, = frame_count',
    sample_step         INT UNSIGNED    NOT NULL DEFAULT 1,
    low_fps             TINYINT(1)      NOT NULL DEFAULT 0,
    orientation         SMALLINT        NOT NULL DEFAULT 0       COMMENT 'EXIF 角度; 非 0 已在 probe 阶段拒绝',

    -- GlobalMetrics 三固定字段 (metrics 列表进 task_metrics, scope=global)
    tempo_ratio         FLOAT           NULL                     COMMENT '上杆/下杆时间比',
    swing_duration      FLOAT           NULL                     COMMENT '全挥杆时长(秒)',
    max_head_drift_pct  FLOAT           NULL                     COMMENT '头部最大漂移百分比',

    -- 结果其余部分
    warnings            JSON            NULL                     COMMENT 'List[str]',
    disclaimer          VARCHAR(512)    NOT NULL DEFAULT '',
    result_json         JSON            NULL                     COMMENT '完整 AnalysisResult 快照(兜底回溯用)',

    -- 存储位置 (视频原片 / 阶段图目录)
    video_path          VARCHAR(512)    NULL,
    out_dir             VARCHAR(512)    NULL,

    -- 预留: 微信用户标识。MVP 不做登录, 恒 NULL
    openid              VARCHAR(64)     NULL,

    created_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    finished_at         DATETIME(3)     NULL                     COMMENT '进入 success/failed 的时间',

    PRIMARY KEY (id),
    UNIQUE KEY uk_task_id (task_id),
    KEY idx_status_created (status, created_at),
    KEY idx_created (created_at),
    KEY idx_openid_created (openid, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='挥杆分析任务主表';


-- ---------------------------------------------------------------------------
-- 2. 阶段结果表
--    1 任务固定 8 行 (address..finish)。主键直接用 (task_id, phase_key):
--    天然唯一 + 按任务取全部阶段即聚簇扫描, 无需回表。
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS task_phases;
CREATE TABLE task_phases (
    task_id             VARCHAR(32)     NOT NULL,
    phase_index         TINYINT UNSIGNED NOT NULL                COMMENT '1~8 (PHASE_ORDER)',
    phase_key           VARCHAR(20)     NOT NULL                 COMMENT 'address|takeaway|backswing|top|downswing|impact|follow_through|finish',
    name_cn             VARCHAR(32)     NOT NULL DEFAULT '',
    name_en             VARCHAR(32)     NOT NULL DEFAULT '',

    frame_index         INT             NOT NULL                 COMMENT '原视频帧号',
    timestamp_sec       FLOAT           NOT NULL DEFAULT 0       COMMENT '秒 = frame_index / fps',
    estimated           TINYINT(1)      NOT NULL DEFAULT 0       COMMENT '是否估算帧(非模型/规则直接命中)',
    image_url           VARCHAR(512)    NOT NULL DEFAULT '',

    -- 定位来源 (DTL per-event 混合新增, 当前 pipeline 尚未回填, 先留 NULL)
    source              VARCHAR(16)     NULL                     COMMENT 'swingnet|rule|m3; NULL=未记录(旧数据)',
    confidence          FLOAT           NULL                     COMMENT '该阶段 SwingNet 置信度; NULL=未记录',

    created_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    PRIMARY KEY (task_id, phase_key),
    KEY idx_task_index (task_id, phase_index),
    KEY idx_frame (frame_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='任务阶段结果(每任务 8 行)';


-- ---------------------------------------------------------------------------
-- 3. 指标表
--    scope=phase 挂某阶段 (phase_key 非空); scope=global 挂全程 (phase_key NULL)。
--    StageMetric 的 11 个字段 1:1 落地, 便于后续做「同指标跨任务分布」分析。
--
--    注意: UNIQUE KEY 含可为 NULL 的 phase_key —— MySQL 唯一索引允许多个 NULL,
--    因此 global 指标 (phase_key IS NULL) 需靠 (task_id, scope, metric_key) 区分,
--    scope 不同则 phase_key 恒 NULL 也不会误撞。
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS task_metrics;
CREATE TABLE task_metrics (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    task_id             VARCHAR(32)     NOT NULL,
    scope               VARCHAR(16)     NOT NULL DEFAULT 'phase' COMMENT 'phase|global',
    phase_key           VARCHAR(20)     NULL                     COMMENT 'scope=phase 时非空; global 为 NULL',
    phase_index         TINYINT UNSIGNED NULL                    COMMENT '冗余排序位, global 为 NULL',

    metric_key          VARCHAR(64)     NOT NULL,
    name                VARCHAR(64)     NOT NULL DEFAULT '',
    value               DOUBLE          NOT NULL DEFAULT 0,
    unit                VARCHAR(16)     NOT NULL DEFAULT '',
    ref_min             DOUBLE          NOT NULL DEFAULT 0,
    ref_max             DOUBLE          NOT NULL DEFAULT 0,

    status              VARCHAR(16)     NOT NULL DEFAULT 'normal' COMMENT 'low|normal|high|critical_low|critical_high',
    estimated           TINYINT(1)      NOT NULL DEFAULT 0,
    source              VARCHAR(16)     NOT NULL DEFAULT 'measured' COMMENT 'measured(L0)|proxy(L1)|reference(L2)',
    confidence          FLOAT           NOT NULL DEFAULT 1.0,
    description         VARCHAR(512)    NOT NULL DEFAULT ''      COMMENT '术语解释; 空串=前端不渲染',

    created_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    PRIMARY KEY (id),
    UNIQUE KEY uk_task_scope_phase_metric (task_id, scope, phase_key, metric_key),
    KEY idx_task_phase (task_id, phase_index),
    KEY idx_metric_status (metric_key, status),
    KEY idx_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='阶段指标与全程指标(每任务约 190 行)';


-- ---------------------------------------------------------------------------
-- 4. 风险项表
--    RiskItem 1:1 落地。suggestions 是 List[str] -> JSON。
--    trigger_description 由后端渲染完毕, 直接存成品文案。
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS task_risks;
CREATE TABLE task_risks (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    task_id             VARCHAR(32)     NOT NULL,
    rule_id             VARCHAR(32)     NOT NULL                 COMMENT 'RISK-001 ~ RISK-017',
    risk_name           VARCHAR(64)     NOT NULL DEFAULT '',
    risk_level          VARCHAR(16)     NOT NULL DEFAULT 'low'   COMMENT 'high|medium|low',
    trigger_phase       VARCHAR(20)     NOT NULL DEFAULT ''      COMMENT '触发阶段 key',

    metric_key          VARCHAR(64)     NOT NULL DEFAULT '',
    metric_name         VARCHAR(64)     NOT NULL DEFAULT '',
    value               DOUBLE          NOT NULL DEFAULT 0,
    unit                VARCHAR(16)     NOT NULL DEFAULT '',
    ref_min             DOUBLE          NOT NULL DEFAULT 0,
    ref_max             DOUBLE          NOT NULL DEFAULT 0,

    trigger_description TEXT                                     COMMENT '后端渲染完毕的成品文案',
    suggestions         JSON            NULL                     COMMENT 'List[str]',
    manual_excerpt      TEXT            NULL                     COMMENT '手册原文摘录',
    manual_page         VARCHAR(32)     NULL                     COMMENT '手册页码, 可能出现 "P6/P11"',

    created_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

    PRIMARY KEY (id),
    UNIQUE KEY uk_task_phase_rule (task_id, trigger_phase, rule_id),
    KEY idx_task (task_id),
    KEY idx_rule (rule_id),
    KEY idx_level (risk_level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='损伤风险项(PDD §5.1, RISK-001~017)';


-- ---------------------------------------------------------------------------
-- 5. 迁移版本表
--    手动执行的轻量级版本记录, 便于多人/多机环境确认库结构进度。
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS schema_migrations;
CREATE TABLE schema_migrations (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    version             VARCHAR(32)     NOT NULL                COMMENT '如 001_init',
    description         VARCHAR(255)    NOT NULL DEFAULT '',
    applied_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uk_version (version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='DDL 迁移版本记录';

INSERT INTO schema_migrations (version, description)
VALUES ('001_init', '初始化: tasks / task_phases / task_metrics / task_risks');

SET FOREIGN_KEY_CHECKS = 1;

-- ============================================================================
-- 验收查询 (建表后手动执行)
--   SHOW TABLES;
--   SELECT table_name, table_rows, table_comment
--     FROM information_schema.tables WHERE table_schema='golf';
-- ============================================================================
