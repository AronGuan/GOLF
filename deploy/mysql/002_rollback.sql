-- ============================================================================
-- 002_auth.sql 的回滚脚本
--
-- 只删除 002 建立的三张表与迁移记录，001_init.sql 建立的
-- tasks / task_phases / task_metrics / task_risks 不受影响。
--
-- ⚠️ 危险: 会删除 users / user_tokens / operation_logs 及其全部数据。
--    执行前务必确认连接的是正确的实例与库。
--
--   mysql -h <host> -u geo golf < 002_rollback.sql
-- ============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

DROP TABLE IF EXISTS operation_logs;
DROP TABLE IF EXISTS user_tokens;
DROP TABLE IF EXISTS users;

DELETE FROM schema_migrations WHERE version = '002_auth';

SET FOREIGN_KEY_CHECKS = 1;

-- 回滚后确认:
--   SHOW TABLES;
--     -- 应只剩 tasks / task_phases / task_metrics / task_risks / schema_migrations
--   SELECT version FROM schema_migrations;
--     -- 应只剩 001_init
