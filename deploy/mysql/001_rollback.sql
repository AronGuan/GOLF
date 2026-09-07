-- ============================================================================
-- 001_init.sql 的回滚脚本
--
-- 危险: 会删除 golf 库下全部 5 张业务表及所有数据。
-- 执行前务必确认连接的是正确的实例与库。
--
--   mysql -h <host> -u geo golf < 001_rollback.sql
-- ============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

DROP TABLE IF EXISTS schema_migrations;
DROP TABLE IF EXISTS task_risks;
DROP TABLE IF EXISTS task_metrics;
DROP TABLE IF EXISTS task_phases;
DROP TABLE IF EXISTS tasks;

SET FOREIGN_KEY_CHECKS = 1;

-- 回滚后确认:
--   SHOW TABLES;   -- 应返回 Empty set
