-- 公司管理底层数据库约束模板
-- 必须开启外键和严格模式
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS skill_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill TEXT NOT NULL,              -- skill 名称: browser-profile-ops, image-generation, printer等
    business TEXT NOT NULL,           -- 归属业务线: nestcar, chindahotpot, main, krothong
    platform TEXT NOT NULL,           -- 平台: line-official-manager, google-cloud, printer-epson
    account_label TEXT NOT NULL,      -- 账号名/标识: chindabot, nestcar_main
    profile_key TEXT,                 -- 浏览器环境专用: profile路径或UUID
    credentials_json TEXT,            -- 凭证信息 (可加密)
    notes TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    -- 【硬约束】同一业务、同一平台、同一账号标签绝不允许重复
    UNIQUE(skill, business, platform, account_label)
);

CREATE TABLE IF NOT EXISTS skill_routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill TEXT NOT NULL,
    business TEXT NOT NULL,
    platform TEXT NOT NULL,
    action_type TEXT NOT NULL,        -- 动作: send, upload, check, download
    target_pattern TEXT DEFAULT '*',  -- 匹配目标群体
    priority INTEGER DEFAULT 10,
    is_active INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    -- 【硬约束】同一动作的路由规则必须唯一，避免多路径发包
    UNIQUE(skill, business, platform, action_type)
);

CREATE TABLE IF NOT EXISTS business_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business TEXT NOT NULL,
    config_key TEXT NOT NULL,
    config_value TEXT NOT NULL DEFAULT '',
    config_json TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    -- 【硬约束】业务级全局配置必须单例
    UNIQUE(business, config_key)
);

CREATE TABLE IF NOT EXISTS skill_authorization (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    auth_level TEXT DEFAULT 'read-write',
    approved_by TEXT DEFAULT 'main',
    updated_at TEXT DEFAULT (datetime('now')),
    -- 【硬约束】授权关系不可重复插入
    UNIQUE(business, skill_name, platform)
);
