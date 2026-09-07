-- ============================================================================
-- 高尔夫挥杆分析 —— 微信登录与操作记录 DDL
--
-- 目标库: golf (MySQL 8.0.46, utf8mb4 / utf8mb4_0900_ai_ci)
-- 方案文档: docs/plans/2026-09-05-wechat-login-and-audit.md
-- 前置: 001_init.sql 已执行 (tasks / task_phases / task_metrics /
--       task_risks / schema_migrations 已存在)
--
-- 设计约定 (沿用 001_init.sql):
--   1. 全小写下划线命名 —— 实例 lower_case_table_names=0 (区分大小写)
--   2. 不加物理外键 —— 逻辑关联 (openid / task_id)，应用层保证删除顺序
--   3. DATETIME(3) 统一时间精度
--
-- 安全约定:
--   - user_tokens.token_hash 存 SHA-256，绝不存明文 token
--   - user_tokens.session_key 可解密微信敏感数据，绝不下发客户端
--   - users.nickname / avatar_url 以空串表示"用户未设置"，
--     默认值由 GET /auth/me 兜底返回 (不入库，见方案 §3.6)
--
-- 回滚: 见同目录 002_rollback.sql
-- ============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ---------------------------------------------------------------------------
-- 1. 用户表
--    1 行 = 1 个微信用户 (以 openid 唯一标识)。
--    nickname / avatar_url 留空 = 用户从未主动设置过，此时接口层返回默认值：
--      昵称 -> "球手 " + LPAD(id, 4, '0')   例: 球手 0007
--      头像 -> 首字色块，配色由 sha256(openid)[:4] % 360 得出 (确定性)
--    判断是否"用户自己设置过"用 nickname_updated_at IS NULL，无需额外标记列。
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS users;
CREATE TABLE users (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    openid              VARCHAR(64)     NOT NULL                COMMENT '小程序内唯一标识',
    unionid             VARCHAR(64)     NULL                    COMMENT '开放平台唯一标识(需绑定开放平台才有)',

    -- 用户资料。空串 = 未设置，由接口兜底 (见文件头注释)
    nickname            VARCHAR(128)    NOT NULL DEFAULT ''     COMMENT '用户设置的昵称; 空串=未设置',
    avatar_url          VARCHAR(512)    NOT NULL DEFAULT ''     COMMENT '头像URL; 空串=未设置',
    gender              TINYINT         NOT NULL DEFAULT 0      COMMENT '0未知 1男 2女',
    country             VARCHAR(64)     NOT NULL DEFAULT '',
    province            VARCHAR(64)     NOT NULL DEFAULT '',
    city                VARCHAR(64)     NOT NULL DEFAULT '',
    language            VARCHAR(16)     NOT NULL DEFAULT '',

    -- 状态与统计
    status              TINYINT         NOT NULL DEFAULT 1      COMMENT '1正常 0禁用',
    login_count         INT UNSIGNED    NOT NULL DEFAULT 0,
    last_login_at       DATETIME(3)     NULL,
    last_login_ip       VARCHAR(45)     NULL,

    -- 资料修改时间。同时承担两个用途:
    --   1) 判断是否用户自己设置过 (IS NULL = 从未设置)
    --   2) 频率限制 (头像 10 次/天, 昵称 5 次/天, 见方案 §3.6)
    nickname_updated_at DATETIME(3)     NULL,
    avatar_updated_at   DATETIME(3)     NULL,

    created_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uk_openid (openid),
    KEY idx_unionid (unionid),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='微信用户';


-- ---------------------------------------------------------------------------
-- 2. 登录态表
--    1 行 = 1 个有效 token。自建 token (非 session_key、非 JWT)：
--      - 不用 session_key: 它可解密微信敏感数据，下发即泄露
--      - 不用 JWT: 无法主动失效，而 MVP 需要「禁用用户立即生效」
--    库里只存 SHA-256 哈希，库被拖走也无法冒用。
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS user_tokens;
CREATE TABLE user_tokens (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    token_hash          CHAR(64)        NOT NULL                COMMENT 'SHA-256(token), 不存明文',
    openid              VARCHAR(64)     NOT NULL,
    session_key         VARCHAR(128)    NULL                    COMMENT '微信会话密钥, 绝不下发客户端',
    expires_at          DATETIME(3)     NOT NULL,
    last_seen_at        DATETIME(3)     NULL,
    revoked             TINYINT(1)      NOT NULL DEFAULT 0      COMMENT '1=已撤销(登出/禁用)',
    created_ip          VARCHAR(45)     NULL,
    user_agent          VARCHAR(512)    NULL,
    created_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    UNIQUE KEY uk_token_hash (token_hash),
    KEY idx_openid (openid),
    KEY idx_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='登录态(token 仅存哈希)';


-- ---------------------------------------------------------------------------
-- 3. 操作记录表
--    1 行 = 1 次用户动作。openid 为 NULL 表示匿名操作 (登录失败仍会记录)。
--
--    记录的 action:
--      login            POST /auth/login                        {is_new_user}
--      upload           POST /task/create                       {camera_view, file_size, file_ext}
--      view_result      GET  /task/result/{id}                  {camera_view, frame_count}
--      view_frame       GET  /task/{id}/frame/{idx}             {frame_index}
--      adjust_frame     GET  /task/{id}/phase_metrics/{p}/{i}   {phase, frame_index}
--      update_avatar    POST /user/avatar                       {size}
--      update_nickname  POST /user/profile                      {length}
--
--    ⚠️ 刻意不记录 GET /task/status/{id} 轮询 (1.5s 一次, 量大且无分析价值)。
--
--    ⚠️ 清理策略: 本表只增不删，建议随任务 TTL 一起归档 —— 按 created_at
--       删除 90 天前数据 (M2 接入后可做成定时任务，本期先留注释)。
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS operation_logs;
CREATE TABLE operation_logs (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    openid              VARCHAR(64)     NULL                    COMMENT 'NULL=匿名操作',
    task_id             VARCHAR(32)     NULL                    COMMENT '关联任务(无则 NULL)',
    action              VARCHAR(32)     NOT NULL                COMMENT 'login|upload|view_result|...',
    action_name         VARCHAR(64)     NOT NULL DEFAULT ''     COMMENT '中文名(运营看板用)',
    detail              JSON            NULL                    COMMENT '结构化上下文',
    result              VARCHAR(16)     NOT NULL DEFAULT 'success' COMMENT 'success|fail',
    fail_reason         VARCHAR(255)    NULL,
    ip                  VARCHAR(45)     NULL                    COMMENT 'IPv6 兼容长度',
    user_agent          VARCHAR(512)    NULL,
    duration_ms         INT             NULL                    COMMENT '耗时(分析类操作)',
    created_at          DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (id),
    KEY idx_openid_created (openid, created_at),
    KEY idx_task (task_id),
    KEY idx_action (action),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='用户操作记录(匿名操作也记)';


-- ---------------------------------------------------------------------------
-- 4. 记录迁移版本 (001 建立的表保持不变)
-- ---------------------------------------------------------------------------
INSERT INTO schema_migrations (version, description)
VALUES ('002_auth', '微信登录与操作记录: users / user_tokens / operation_logs');

SET FOREIGN_KEY_CHECKS = 1;

-- ============================================================================
-- 验收查询 (建表后手动执行)
--   SHOW TABLES;
--   SELECT table_name, table_comment FROM information_schema.tables
--     WHERE table_schema='golf' ORDER BY table_name;
--   SELECT version, description, applied_at FROM schema_migrations;
-- ============================================================================
