#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSHCookieCleaner 的单元测试。

所有测试只使用临时目录和临时 SQLite 数据库，绝不读取或修改真实 %APPDATA%
下的 Cookies。运行方式：

    python test_clean.py -v
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import types
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import main as cleaner

COOKIE_SCHEMA = """
CREATE TABLE cookies (
    creation_utc INTEGER NOT NULL,
    host_key TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '/',
    expires_utc INTEGER NOT NULL DEFAULT 0,
    is_secure INTEGER NOT NULL DEFAULT 0,
    is_httponly INTEGER NOT NULL DEFAULT 0,
    last_access_utc INTEGER NOT NULL DEFAULT 0,
    has_expires INTEGER NOT NULL DEFAULT 0,
    is_persistent INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 1,
    encrypted_value BLOB DEFAULT X'',
    samesite INTEGER NOT NULL DEFAULT -1,
    source_scheme INTEGER NOT NULL DEFAULT 0,
    source_port INTEGER NOT NULL DEFAULT -1,
    last_update_utc INTEGER NOT NULL DEFAULT 0,
    source_type INTEGER NOT NULL DEFAULT 0,
    has_cross_site_ancestor INTEGER NOT NULL DEFAULT 0
)
"""


def create_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(COOKIE_SCHEMA)
    conn.commit()
    return conn


def insert_cookie(
    conn: sqlite3.Connection,
    host_key: str,
    name: str,
    creation_utc: int,
    value: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO cookies (creation_utc, host_key, name, value) VALUES (?, ?, ?, ?)",
        (creation_utc, host_key, name, value if value is not None else name),
    )


def seed_realistic_database(path: Path, target_count: int = 30) -> dict[str, list[tuple]]:
    """建立目标 Cookie + 其他 host/name 的临时库，返回非目标快照。"""

    conn = create_database(path)
    base = 1_700_000_000_000_000
    for index in range(target_count):
        insert_cookie(
            conn,
            "127.0.0.1" if index % 2 == 0 else "localhost",
            f"dsh-auth-{index:02d}",
            base + index,
            f"token-{index:02d}",
        )

    # 同 host、但名字不匹配：必须保留。
    for index in range(10):
        insert_cookie(
            conn,
            "127.0.0.1" if index % 2 == 0 else "localhost",
            f"normal-session-{index:02d}",
            base + 100 + index,
            f"normal-{index}",
        )

    # 其他 host 的普通 Cookie：必须保留。
    for index in range(10):
        insert_cookie(
            conn,
            "example.com",
            f"site-cookie-{index:02d}",
            base + 200 + index,
            f"site-{index}",
        )

    # 其他 host 上名字同样是 dsh-auth-*：host 条件不匹配，必须保留。
    for index in range(5):
        insert_cookie(
            conn,
            "other.example.com",
            f"dsh-auth-other-{index:02d}",
            base + 300 + index,
            f"other-{index}",
        )

    conn.commit()
    rows = conn.execute(
        "SELECT rowid, host_key, name, creation_utc, value FROM cookies "
        "WHERE NOT (host_key IN ('127.0.0.1', 'localhost') "
        "AND name LIKE 'dsh-auth-%') "
        "ORDER BY rowid"
    ).fetchall()
    conn.close()
    return {"others": rows}


def target_rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT rowid, host_key, name, creation_utc FROM cookies "
            "WHERE host_key IN ('127.0.0.1', 'localhost') "
            "AND name LIKE 'dsh-auth-%' "
            "ORDER BY creation_utc DESC"
        ).fetchall()
    finally:
        conn.close()


def non_target_rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT rowid, host_key, name, creation_utc, value FROM cookies "
            "WHERE NOT (host_key IN ('127.0.0.1', 'localhost') "
            "AND name LIKE 'dsh-auth-%') "
            "ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


def backup_names(backup_dir: Path) -> list[str]:
    if not backup_dir.exists():
        return []
    return sorted(
        path.name for path in backup_dir.iterdir() if path.name.startswith("Cookies.bak.")
    )


def make_backup_groups(backup_dir: Path, count: int, with_journal: bool = False) -> list[Path]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 1, 1, 0, 0, 0)
    mains = []
    for index in range(count):
        stamp = (start + timedelta(minutes=index)).strftime("%Y%m%d_%H%M%S")
        main = backup_dir / f"Cookies.bak.{stamp}"
        main.write_bytes(b"backup")
        mains.append(main)
        if with_journal:
            main.with_name(main.name + "-journal").write_bytes(b"journal")
    return mains


def resolved_for(network_dir: Path) -> cleaner.ResolvedPaths:
    return cleaner.ResolvedPaths(
        network_dir=network_dir,
        cookies_path=network_dir / "Cookies",
        journal_path=network_dir / "Cookies-journal",
        source="测试临时目录",
    )


class DatabaseCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-cookie-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "Cookies"

    def test_keeps_latest_ten_and_preserves_other_cookies(self) -> None:
        snapshot = seed_realistic_database(self.db, 60)
        self.assertEqual(len(target_rows(self.db)), 60)
        stats = cleaner.analyze_database(self.db, 10, 50)
        self.assertEqual(stats.count, 60)
        self.assertTrue(stats.threshold_reached)
        self.assertEqual(stats.planned_delete, 50)

        before, after, deleted = cleaner.clean_database(self.db, 10)
        self.assertEqual((before, after, deleted), (60, 10, 50))

        kept = target_rows(self.db)
        self.assertEqual(len(kept), 10)
        self.assertEqual(
            [row[3] for row in kept],
            [1_700_000_000_000_000 + index for index in range(59, 49, -1)],
        )
        self.assertEqual(non_target_rows(self.db), snapshot["others"])

    def test_keep_larger_than_target_count_changes_nothing(self) -> None:
        conn = create_database(self.db)
        for index in range(3):
            insert_cookie(conn, "localhost", f"dsh-auth-{index}", 100 + index)
        conn.commit()
        conn.close()
        before_rows = target_rows(self.db)

        self.assertEqual(cleaner.clean_database(self.db, 10), (3, 3, 0))
        self.assertEqual(target_rows(self.db), before_rows)

    def test_preview_is_read_only_and_plans_by_threshold(self) -> None:
        seed_realistic_database(self.db, 60)
        before = target_rows(self.db)
        stats = cleaner.preview_database(self.db, 10, 50)
        self.assertEqual(stats.count, 60)
        self.assertEqual(stats.planned_delete, 50)
        self.assertEqual(target_rows(self.db), before)

        stats49 = cleaner.preview_database(self.db, 10, 61)
        self.assertFalse(stats49.threshold_reached)
        self.assertEqual(stats49.planned_delete, 0)

    def test_missing_cookies_table_has_clear_error(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.commit()
        conn.close()
        with self.assertRaises(cleaner.CookieCleanerError) as ctx:
            cleaner.analyze_database(self.db, 10)
        self.assertIn("cookies 表", str(ctx.exception))

    def test_missing_required_column_has_clear_error(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT)")
        conn.commit()
        conn.close()
        with self.assertRaises(cleaner.CookieCleanerError) as ctx:
            cleaner.analyze_database(self.db, 10)
        self.assertIn("creation_utc", str(ctx.exception))

    def test_invalid_sqlite_file_has_clear_error(self) -> None:
        self.db.write_bytes(b"this is not a sqlite database")
        with self.assertRaises(cleaner.CookieCleanerError) as ctx:
            cleaner.analyze_database(self.db, 10)
        message = str(ctx.exception)
        self.assertTrue("数据库" in message or "SQLite" in message)


class ProcessDetectionTests(unittest.TestCase):
    def test_matches_known_dsh_process_forms(self) -> None:
        self.assertTrue(
            cleaner.is_dsh_process(
                "DeepSeek Harness.exe", r"D:\Deepseek Harness\DeepSeek Harness.exe"
            )
        )
        self.assertTrue(
            cleaner.is_dsh_process(
                "dsh-desktop.exe", r"C:\Apps\dsh-desktop\dsh-desktop.exe"
            )
        )
        self.assertTrue(
            cleaner.is_dsh_process(
                "electron.exe",
                r"C:\Users\me\.dsh\electron\electron.exe",
                [r"C:\Apps\dsh-desktop\main.js"],
            )
        )
        self.assertTrue(
            cleaner.is_dsh_process(
                "whatever.exe", r"C:\Users\me\AppData\Local\dsh-desktop\app.exe"
            )
        )

    def test_does_not_match_other_electron_apps(self) -> None:
        self.assertFalse(
            cleaner.is_dsh_process(
                "electron.exe",
                r"C:\Users\me\.dsh\electron\electron.exe",
                [r"C:\Users\me\projects\notes-app\main.js"],
            )
        )
        self.assertFalse(
            cleaner.is_dsh_process("Code.exe", r"C:\Program Files\VSCode\Code.exe")
        )
        self.assertFalse(
            cleaner.is_dsh_process(
                "DSHCookieCleaner.exe",
                r"E:\tools\DSHCookieCleaner\DSHCookieCleaner.exe",
            )
        )
        self.assertFalse(
            cleaner.is_dsh_process("Teams.exe", r"C:\Program Files\Teams\Teams.exe")
        )


class PathResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-path-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _make_network(self, appdata: Path, cookie_bytes: bytes = b"db") -> Path:
        network = appdata / "@deepseek-ai" / "dsh-desktop" / "Network"
        network.mkdir(parents=True)
        (network / "Cookies").write_bytes(cookie_bytes)
        return network

    def test_uses_appdata_instead_of_hardcoded_fallback(self) -> None:
        appdata = self.root / "Roaming"
        network = self._make_network(appdata)
        decoy_fallback = self.root / "Fallback" / "Network"
        decoy_fallback.mkdir(parents=True)
        (decoy_fallback / "Cookies").write_bytes(b"fallback")

        with mock.patch.object(cleaner, "FALLBACK_NETWORK_DIR", decoy_fallback):
            resolved = cleaner.resolve_paths(appdata=str(appdata))

        self.assertEqual(resolved.cookies_path, network / "Cookies")
        self.assertFalse(resolved.used_fallback)
        self.assertEqual(resolved.source, "APPDATA 环境变量")

    def test_fallback_warns_when_primary_missing(self) -> None:
        appdata = self.root / "MissingRoaming"
        fallback_network = self.root / "Fallback" / "Network"
        fallback_network.mkdir(parents=True)
        (fallback_network / "Cookies").write_bytes(b"db")

        with mock.patch.object(cleaner, "FALLBACK_NETWORK_DIR", fallback_network):
            resolved = cleaner.resolve_paths(appdata=str(appdata))

        self.assertTrue(resolved.used_fallback)
        self.assertEqual(resolved.cookies_path, fallback_network / "Cookies")
        self.assertIn("使用了硬编码回退路径，可能不是当前用户", resolved.warning or "")

    def test_cookies_dir_override_has_priority(self) -> None:
        override = self.root / "ManualNetwork"
        override.mkdir()
        (override / "Cookies").write_bytes(b"db")
        resolved = cleaner.resolve_paths(override=str(override), appdata="ignored")
        self.assertEqual(resolved.cookies_path, override / "Cookies")
        self.assertEqual(resolved.source, "--cookies-dir 参数")

    def test_app_paths_are_relative_to_program_directory(self) -> None:
        base = self.root / "PortableApp"
        paths = cleaner.app_paths(base)
        self.assertEqual(paths.base_dir, base)
        self.assertEqual(paths.log_dir, base / "log")
        self.assertEqual(paths.log_path, base / "log" / "clean_cookie.log")
        self.assertEqual(paths.backup_dir, base / "backups")
        self.assertTrue(paths.log_dir.is_dir())
        self.assertTrue(paths.backup_dir.is_dir())


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-config-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_default_config_uses_program_directory(self) -> None:
        config = cleaner.default_config(self.root)
        self.assertEqual(config.backup_dir, self.root / "backups")
        self.assertEqual(config.log_dir, self.root / "log")
        self.assertEqual(config.backup_retention, 30)
        self.assertEqual(config.log_max_mb, 2)
        self.assertEqual(config.log_backup_count, 3)
        self.assertEqual(config.threshold, 50)
        self.assertEqual(config.keep, 10)
        self.assertEqual(config.check_interval_hours, 2)
        self.assertFalse(config.autostart)
        self.assertFalse((self.root / "backups").exists())

    def test_config_round_trip_preserves_values(self) -> None:
        original = cleaner.AppConfig(
            backup_dir=self.root / "my-backups",
            log_dir=self.root / "my-logs",
            backup_retention=7,
            log_max_mb=4,
            log_backup_count=2,
            threshold=60,
            keep=12,
            autostart=True,
            check_interval_hours=3,
        )
        path = cleaner.save_config(original, self.root / "config.json")
        loaded = cleaner.load_config(path)

        self.assertEqual(loaded, original)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["backup_dir"], str(original.backup_dir))
        self.assertEqual(data["log_max_mb"], 4)
        self.assertEqual(data["threshold"], 60)
        self.assertEqual(data["check_interval_hours"], 3)
        self.assertIs(data["autostart"], True)

    def test_load_config_accepts_utf8_bom(self) -> None:
        config = cleaner.default_config(self.root)
        path = self.root / "config.json"
        data = {
            "backup_dir": str(config.backup_dir),
            "log_dir": str(config.log_dir),
            "backup_retention": config.backup_retention,
            "log_max_mb": config.log_max_mb,
            "log_backup_count": config.log_backup_count,
            "threshold": config.threshold,
            "keep": config.keep,
        }
        path.write_bytes(
            b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False).encode("utf-8")
        )
        self.assertEqual(cleaner.load_config(path), config)

    def test_missing_config_raises_chinese_error(self) -> None:
        with self.assertRaises(cleaner.ConfigError) as ctx:
            cleaner.load_config(self.root / "missing.json")
        self.assertIn("找不到配置文件", str(ctx.exception))

    def test_invalid_json_raises_chinese_error(self) -> None:
        path = self.root / "config.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(cleaner.ConfigError) as ctx:
            cleaner.load_config(path)
        self.assertIn("不是有效的 JSON", str(ctx.exception))

    def test_invalid_numbers_are_rejected(self) -> None:
        values = {
            "backup_dir": str(self.root / "backups"),
            "log_dir": str(self.root / "log"),
            "backup_retention": "abc",
            "log_max_mb": "2",
            "log_backup_count": "3",
            "threshold": "50",
            "keep": "10",
        }
        with self.assertRaises(cleaner.ConfigError) as ctx:
            cleaner.build_config_from_form(values)
        self.assertEqual(str(ctx.exception), "只能输入数字！")

        with self.assertRaises(cleaner.ConfigError) as ctx_zero:
            cleaner.parse_positive_int_text("0", 30, "备份保留上限")
        self.assertIn("必须大于 0", str(ctx_zero.exception))

    def test_blank_form_numbers_use_defaults(self) -> None:
        values = {
            "backup_dir": "",
            "log_dir": "",
            "backup_retention": "",
            "log_max_mb": " ",
            "log_backup_count": "",
            "threshold": "",
            "keep": "",
        }
        config = cleaner.build_config_from_form(values, base_dir=self.root)
        defaults = cleaner.default_config(self.root)
        self.assertEqual(config, defaults)

    def test_autostart_form_value_and_command(self) -> None:
        values = {
            "backup_dir": str(self.root / "backups"),
            "log_dir": str(self.root / "log"),
            "backup_retention": "30",
            "log_max_mb": "2",
            "log_backup_count": "3",
            "threshold": "50",
            "keep": "10",
            "autostart": "1",
        }
        config = cleaner.build_config_from_form(values, base_dir=self.root)
        self.assertTrue(config.autostart)
        command = cleaner.autostart_command()
        self.assertIn("--silent", command)
        self.assertIn(str(Path(cleaner.sys.executable).resolve()), command)

    def test_ensure_config_dirs_creates_both_directories(self) -> None:
        config = cleaner.default_config(self.root)
        cleaner.ensure_config_dirs(config)
        self.assertTrue(config.backup_dir.is_dir())
        self.assertTrue(config.log_dir.is_dir())

    def test_logger_rotates_by_size_and_backup_count(self) -> None:
        path = self.root / "log" / "clean_cookie.log"
        logger = cleaner.Logger(path, echo=False, max_bytes=220, backup_count=2)
        try:
            for index in range(20):
                logger.info(f"rotation-test-{index:02d}-" + "x" * 40)
        finally:
            logger.close()

        self.assertTrue(path.exists())
        self.assertTrue(path.with_name(path.name + ".1").exists())
        # backupCount=2 表示最多保留 .1 和 .2，不应无限增长。
        self.assertFalse(path.with_name(path.name + ".3").exists())


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-migrate-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_migrate_directory_moves_files_and_keeps_old_dir(self) -> None:
        old = self.root / "old"
        new = self.root / "new"
        old.mkdir()
        (old / "a.log").write_text("a", encoding="utf-8")
        (old / "b.log").write_text("b", encoding="utf-8")

        moved = cleaner.migrate_directory(old, new, "日志")

        self.assertEqual(moved, 2)
        self.assertEqual((new / "a.log").read_text(encoding="utf-8"), "a")
        self.assertEqual((new / "b.log").read_text(encoding="utf-8"), "b")
        self.assertTrue(old.is_dir())
        self.assertEqual(list(old.iterdir()), [])

    def test_migration_never_overwrites_same_name(self) -> None:
        old = self.root / "old"
        new = self.root / "new"
        old.mkdir()
        new.mkdir()
        (old / "clean_cookie.log").write_text("old", encoding="utf-8")
        (new / "clean_cookie.log").write_text("new", encoding="utf-8")

        cleaner.migrate_directory(old, new, "日志")

        self.assertEqual((new / "clean_cookie.log").read_text(encoding="utf-8"), "new")
        self.assertEqual(
            (new / "clean_cookie_1.log").read_text(encoding="utf-8"), "old"
        )

    def test_migrate_config_paths_moves_logs_and_backups(self) -> None:
        old = cleaner.AppConfig(
            backup_dir=self.root / "old-backups",
            log_dir=self.root / "old-log",
        )
        new = cleaner.AppConfig(
            backup_dir=self.root / "new-backups",
            log_dir=self.root / "new-log",
        )
        old.backup_dir.mkdir()
        old.log_dir.mkdir()
        (old.backup_dir / "Cookies.bak.20260101_000000").write_bytes(b"db")
        (old.log_dir / "clean_cookie.log").write_text("log", encoding="utf-8")

        moved = cleaner.migrate_config_paths(old, new)

        self.assertEqual(moved, (1, 1))
        self.assertTrue((new.log_dir / "clean_cookie.log").is_file())
        self.assertTrue((new.backup_dir / "Cookies.bak.20260101_000000").is_file())

    def test_new_directory_inside_old_directory_is_rejected(self) -> None:
        old = self.root / "old"
        old.mkdir()
        with self.assertRaises(cleaner.ConfigError):
            cleaner.migrate_directory(old, old / "nested", "日志")


class UpdateCheckTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return self._payload

    def test_version_key_compares_numeric_parts(self) -> None:
        self.assertGreater(
            cleaner._version_key("v1.10.0"), cleaner._version_key("1.9.9")
        )
        self.assertEqual(cleaner._version_key("v1.0"), (1, 0))

    def test_newer_release_is_detected(self) -> None:
        payload = {
            "tag_name": "v1.1.0",
            "html_url": "https://example.test/releases/v1.1.0",
        }
        response = self.FakeResponse(payload)
        with mock.patch.object(
            cleaner.urllib.request, "urlopen", return_value=response
        ):
            has_new, tag, page = cleaner.check_for_update(
                current_version="1.0.0"
            )

        self.assertTrue(has_new)
        self.assertEqual(tag, "v1.1.0")
        self.assertEqual(page, "https://example.test/releases/v1.1.0")

    def test_current_release_is_not_newer(self) -> None:
        response = self.FakeResponse({"tag_name": "v1.0.0"})
        with mock.patch.object(
            cleaner.urllib.request, "urlopen", return_value=response
        ):
            has_new, tag, page = cleaner.check_for_update(
                current_version="1.0.0"
            )

        self.assertFalse(has_new)
        self.assertEqual(tag, "v1.0.0")
        self.assertEqual(
            page, f"https://github.com/{cleaner.GITHUB_REPO}/releases"
        )

    def test_network_error_has_exact_chinese_message(self) -> None:
        with mock.patch.object(
            cleaner.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("offline"),
        ):
            with self.assertRaises(cleaner.CookieCleanerError) as ctx:
                cleaner.check_for_update()

        self.assertEqual(str(ctx.exception), "网络连接失败，请稍后再试")

    def test_missing_tag_is_reported_as_network_error(self) -> None:
        response = self.FakeResponse({"html_url": "https://example.test"})
        with mock.patch.object(
            cleaner.urllib.request, "urlopen", return_value=response
        ):
            with self.assertRaises(cleaner.CookieCleanerError):
                cleaner.check_for_update()


@unittest.skipUnless(cleaner.pystray is not None, "pystray 未安装")
class TrayMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-tray-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_menu_contains_required_actions(self) -> None:
        app = cleaner.TrayApp(
            cleaner.default_config(self.root),
            types.SimpleNamespace(cookies_dir=None),
        )
        menu = app._build_menu()
        labels = [str(item.text) for item in menu.items if str(item.text)]
        self.assertIn("立即检查", labels)
        self.assertIn("设置", labels)
        self.assertIn("检测更新", labels)
        self.assertIn("⚠️ 彻底退出程序", labels)

    def test_fullscreen_probe_never_raises(self) -> None:
        self.assertIsInstance(cleaner.is_fullscreen_foreground(), bool)


class CommandLineTests(unittest.TestCase):
    def test_defaults_are_gui_driven(self) -> None:
        args = cleaner.build_parser().parse_args([])
        self.assertFalse(args.setup)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.silent)
        self.assertFalse(hasattr(args, "threshold"))
        self.assertFalse(hasattr(args, "keep"))

    def test_setup_and_dry_run_flags(self) -> None:
        args = cleaner.build_parser().parse_args(["--setup", "--dry-run"])
        self.assertTrue(args.setup)
        self.assertTrue(args.dry_run)

    def test_silent_flag(self) -> None:
        args = cleaner.build_parser().parse_args(["--silent"])
        self.assertTrue(args.silent)


class ThresholdCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-threshold-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.network = self.root / "Network"
        self.network.mkdir()
        self.db = self.network / "Cookies"
        self.backups = self.root / "backups"
        self.log = cleaner.Logger(self.root / "log" / "clean_cookie.log", echo=False)
        self.addCleanup(self.log.close)

    @staticmethod
    def no_sleep(_seconds: float) -> None:
        pass

    def _run(self, target_count: int, **kwargs):
        seed_realistic_database(self.db, target_count)
        return cleaner.cleanup(
            resolved_for(self.network),
            logger=self.log,
            scanner=lambda: [],
            sleep_func=self.no_sleep,
            backup_dir=self.backups,
            **kwargs,
        )

    def test_49_cookies_do_not_trigger_cleanup_or_copy(self) -> None:
        seed_realistic_database(self.db, 49)
        original = self.db.read_bytes()
        with mock.patch.object(
            cleaner,
            "_prepare_working_copy",
            side_effect=AssertionError("49 条时不得复制原始 Cookies"),
        ):
            result = cleaner.cleanup(
                resolved_for(self.network),
                logger=self.log,
                scanner=lambda: [],
                sleep_func=self.no_sleep,
                backup_dir=self.backups,
            )

        self.assertTrue(result.skipped)
        self.assertEqual(result.before, 49)
        self.assertEqual(result.planned_delete, 0)
        self.assertIsNone(result.backup_path)
        self.assertEqual(self.db.read_bytes(), original)
        self.assertFalse(self.backups.exists())
        self.assertEqual(list(self.network.glob(".dsh-cookie-clean-*")), [])

        log_text = (self.root / "log" / "clean_cookie.log").read_text(encoding="utf-8")
        self.assertIn(
            "当前 49 条，未达到清理阈值 50 条，跳过清理并退出。", log_text
        )

    def test_50_cookies_trigger_cleanup_and_keep_10(self) -> None:
        result = self._run(50)
        self.assertFalse(result.skipped)
        self.assertEqual((result.before, result.after, result.deleted), (50, 10, 40))
        self.assertEqual(len(target_rows(self.db)), 10)
        self.assertIsNotNone(result.backup_path)
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.parent, self.backups)
        self.assertEqual(len(backup_names(self.backups)), 1)

    def test_60_cookies_trigger_cleanup_and_keep_latest_10(self) -> None:
        result = self._run(60)
        self.assertFalse(result.skipped)
        self.assertEqual((result.before, result.after, result.deleted), (60, 10, 50))
        kept = target_rows(self.db)
        self.assertEqual(len(kept), 10)
        self.assertEqual(
            [row[3] for row in kept],
            [1_700_000_000_000_000 + index for index in range(59, 49, -1)],
        )

    def test_threshold_60_skips_59_and_cleans_60(self) -> None:
        result59 = self._run(59, threshold=60)
        self.assertTrue(result59.skipped)
        self.assertEqual(len(target_rows(self.db)), 59)

        self.db.unlink()
        self.backups = self.root / "backups-60"
        result60 = self._run(60, threshold=60)
        self.assertFalse(result60.skipped)
        self.assertEqual(result60.deleted, 50)
        self.assertEqual(len(target_rows(self.db)), 10)

    def test_dry_run_does_not_copy_or_backup(self) -> None:
        seed_realistic_database(self.db, 60)
        original = self.db.read_bytes()
        with mock.patch.object(
            cleaner,
            "_prepare_working_copy",
            side_effect=AssertionError("dry-run 不得复制原始 Cookies"),
        ):
            result = cleaner.cleanup(
                resolved_for(self.network),
                logger=self.log,
                scanner=lambda: [],
                sleep_func=self.no_sleep,
                dry_run=True,
                backup_dir=self.backups,
            )

        self.assertTrue(result.dry_run)
        self.assertFalse(result.skipped)
        self.assertEqual(result.planned_delete, 50)
        self.assertEqual(result.deleted, 0)
        self.assertIsNone(result.backup_path)
        self.assertEqual(self.db.read_bytes(), original)
        self.assertFalse(self.backups.exists())

    def test_full_cleanup_backs_up_to_backups_dir_and_logs(self) -> None:
        seed_realistic_database(self.db, 60)
        snapshot = non_target_rows(self.db)
        original = self.db.read_bytes()
        result = cleaner.cleanup(
            resolved_for(self.network),
            logger=self.log,
            scanner=lambda: [],
            sleep_func=self.no_sleep,
            backup_dir=self.backups,
        )

        self.assertEqual((result.before, result.after, result.deleted), (60, 10, 50))
        self.assertFalse(result.dry_run)
        self.assertIsNotNone(result.backup_path)
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.parent, self.backups)
        self.assertEqual(result.backup_path.read_bytes(), original)
        self.assertEqual(non_target_rows(self.db), snapshot)
        self.assertFalse((self.network / "Cookies-journal").exists())

        log_text = (self.root / "log" / "clean_cookie.log").read_text(encoding="utf-8")
        self.assertIn("已备份 Cookies 到 backups 目录", log_text)
        self.assertIn("清理旧备份：当前 1 组，未超过 30 组", log_text)
        self.assertIn("清理前匹配 dsh-auth-*：60 条", log_text)

    def test_dsh_running_aborts_before_any_database_access(self) -> None:
        seed_realistic_database(self.db, 60)
        original = self.db.read_bytes()

        def fail_sleep(_seconds: float) -> None:
            self.fail("检测到 DSH 后不应等待或继续复制数据库")

        process = cleaner.DshProcess(
            1234, "DeepSeek Harness.exe", r"D:\Deepseek Harness\DeepSeek Harness.exe"
        )
        with self.assertRaises(cleaner.DshRunningError) as ctx:
            cleaner.cleanup(
                resolved_for(self.network),
                logger=self.log,
                scanner=lambda: [process],
                sleep_func=fail_sleep,
                backup_dir=self.backups,
            )

        self.assertIn("请先完全退出 DSH（含托盘）", str(ctx.exception))
        self.assertEqual(self.db.read_bytes(), original)
        self.assertEqual(backup_names(self.backups), [])
        self.assertEqual(list(self.network.glob(".dsh-cookie-clean-*")), [])

    def test_dsh_started_before_writeback_aborts_and_keeps_original(self) -> None:
        seed_realistic_database(self.db, 60)
        original = self.db.read_bytes()
        calls = {"count": 0}

        def scanner():
            calls["count"] += 1
            # 1 启动前，2 复制前，3 备份前，4 写回前。
            if calls["count"] >= 4:
                return [
                    cleaner.DshProcess(
                        4321,
                        "DeepSeek Harness.exe",
                        r"D:\Deepseek Harness\DeepSeek Harness.exe",
                    )
                ]
            return []

        with self.assertRaises(cleaner.DshRunningError) as ctx:
            cleaner.cleanup(
                resolved_for(self.network),
                logger=self.log,
                scanner=scanner,
                sleep_func=self.no_sleep,
                backup_dir=self.backups,
            )

        self.assertIn("已放弃写回", str(ctx.exception))
        self.assertEqual(self.db.read_bytes(), original)
        self.assertEqual(len(target_rows(self.db)), 60)
        # 已经完成的备份保留下来，方便用户核对。
        self.assertEqual(len(backup_names(self.backups)), 1)
        self.assertEqual(list(self.network.glob(".dsh-cookie-clean-*")), [])

    def test_zero_byte_journal_is_backed_up_and_removed(self) -> None:
        seed_realistic_database(self.db, 60)
        journal = self.network / "Cookies-journal"
        journal.write_bytes(b"")

        result = cleaner.cleanup(
            resolved_for(self.network),
            logger=self.log,
            scanner=lambda: [],
            sleep_func=self.no_sleep,
            backup_dir=self.backups,
        )

        self.assertIsNotNone(result.backup_journal_path)
        assert result.backup_journal_path is not None
        self.assertTrue(result.backup_journal_path.exists())
        self.assertTrue(result.journal_removed)
        self.assertFalse(journal.exists())

    def test_existing_35_groups_are_rotated_to_30_during_cleanup(self) -> None:
        old_mains = make_backup_groups(self.backups, 35)
        result = self._run(60)
        self.assertEqual(result.backups_deleted, 6)
        self.assertEqual(result.backup_count, 30)
        self.assertEqual(len(cleaner.scan_backup_groups(self.backups)), 30)
        for old in old_mains[:6]:
            self.assertFalse(old.exists(), f"最旧备份未删除：{old}")

    def test_configured_backup_retention_is_used(self) -> None:
        old_mains = make_backup_groups(self.backups, 5)
        result = self._run(60, backup_retention=3)
        self.assertEqual(result.backup_retention, 3)
        self.assertEqual(result.backups_deleted, 3)
        self.assertEqual(result.backup_count, 3)
        self.assertEqual(len(cleaner.scan_backup_groups(self.backups)), 3)
        for old in old_mains[:3]:
            self.assertFalse(old.exists(), f"最旧备份未删除：{old}")


class BackupRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="dsh-backup-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backups = self.root / "backups"

    def test_35_groups_keep_latest_30(self) -> None:
        mains = make_backup_groups(self.backups, 35)
        deleted, remaining = cleaner.prune_backups(self.backups, 30)
        self.assertEqual(deleted, 5)
        self.assertEqual(remaining, 30)
        self.assertEqual(len(cleaner.scan_backup_groups(self.backups)), 30)
        for old in mains[:5]:
            self.assertFalse(old.exists())
        self.assertTrue(mains[-1].exists())

    def test_group_journal_is_deleted_with_its_database(self) -> None:
        mains = make_backup_groups(self.backups, 31, with_journal=True)
        deleted, remaining = cleaner.prune_backups(self.backups, 30)
        self.assertEqual(deleted, 2)
        self.assertEqual(remaining, 30)
        self.assertFalse(mains[0].exists())
        self.assertFalse(mains[0].with_name(mains[0].name + "-journal").exists())

    def test_orphan_journal_and_unrelated_file_are_never_deleted(self) -> None:
        orphan = self.backups / "Cookies.bak.20200101_000000-journal"
        self.backups.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")
        keep = self.backups / "keep-me.txt"
        keep.write_bytes(b"keep")

        deleted, remaining = cleaner.prune_backups(self.backups, 30)
        self.assertEqual(deleted, 0)
        self.assertEqual(remaining, 0)
        self.assertTrue(orphan.exists())
        self.assertTrue(keep.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
