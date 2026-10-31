#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH Cookie 自动清理器（Windows 目录版）。

用途
----
DSH 桌面版每次启动可能使用随机本地端口，旧版 http://127.0.0.1:<port> /
http://localhost:<port> 下的 dsh-auth-* Cookie 会不断堆积。Cookie 总量过大时，
请求头可能超过 16KB，触发 "Web authentication failed / 431"。

本工具在确认 DSH 已完全退出后：
  1. 先只读预检 dsh-auth-* 条数；未达到阈值（默认 50 条）直接退出；
  2. 达到阈值时，把 Cookies 数据库复制到临时工作区；
  3. 只保留 host 为 127.0.0.1 / localhost 且 name LIKE 'dsh-auth-%' 的最近 N 条；
  4. 清理前把原库备份到程序目录的 backups 文件夹，按组保留最近 30 个；
  5. 清理后原子写回；全程写程序目录 log 文件夹中的日志。

安全边界
--------
* 检测到 DSH 进程时只提示并退出，绝不终止进程。
* 只处理目标 Network 目录中的 Cookies 和 Cookies-journal。
* 写回前再次检测 DSH；若 DSH 已启动则放弃写回。
* 备份目录只清理 backups 下严格匹配 Cookies.bak.* 的普通文件。
* 同一时间只允许一个实例运行，避免 SQLite 锁死。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

try:
    import winreg
except ImportError:  # 非 Windows 开发环境
    winreg = None  # type: ignore[assignment]

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # 极少数精简 Python 不带 Tk；打包版会正常包含。
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

try:
    import psutil
except ImportError:  # 单元测试可只测试数据库逻辑；真正运行时要求 psutil
    psutil = None  # type: ignore[assignment]

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # 托盘需要 pystray + Pillow；打包版会包含
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]


APP_TITLE = "DSH Cookie 自动清理器"
# 更新比较基准；按新方案固定为 1.0.0，发布新版本时同步修改这里。
CURRENT_VERSION = "1.0.0"
VERSION = CURRENT_VERSION
CONFIG_FILENAME = "config.json"
SETUP_TITLE = "DSH Cookie 清理工具 - 首次设置"
LOG_DIR_NAME = "log"
BACKUP_DIR_NAME = "backups"
LOG_FILENAME = "clean_cookie.log"

DEFAULT_KEEP = 10
DEFAULT_THRESHOLD = 50
WAIT_SECONDS = 2
DEFAULT_BACKUP_RETENTION = 30
DEFAULT_LOG_MAX_MB = 2
DEFAULT_LOG_BACKUP_COUNT = 3
DEFAULT_CHECK_INTERVAL_HOURS = 2

# 检测更新的 GitHub 仓库。发布前把这里改成你自己的仓库即可。
GITHUB_REPO = "dsh-cookie-cleaner/DSHCookieCleaner"
UPDATE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE_NAME = "DSHCookieCleaner"
SINGLE_INSTANCE_MUTEX = r"Local\DSHCookieCleaner.SingleInstance"

# 兼容旧代码/旧测试中的常量名。
BACKUP_RETENTION = DEFAULT_BACKUP_RETENTION

TARGET_TABLE = "cookies"
TARGET_HOSTS_SQL = "host_key IN ('127.0.0.1', 'localhost')"
TARGET_NAME_SQL = "name LIKE 'dsh-auth-%'"
TARGET_PREDICATE = f"{TARGET_HOSTS_SQL} AND {TARGET_NAME_SQL}"

# 仅在 APPDATA 主路径不存在时使用的最后回退。正常用户永远优先使用 APPDATA。
FALLBACK_NETWORK_DIR = Path(
    r"C:\Users\cgcyr\AppData\Roaming\@deepseek-ai\dsh-desktop\Network"
)

# 只匹配备份主库名，不带 -journal 后缀。组名形如：
# Cookies.bak.20261031_153000 或 Cookies.bak.20261031_153000_1
_BACKUP_MAIN_RE = re.compile(r"^(Cookies\.bak\.(\d{8}_\d{6})(?:_(\d+))?)$")
_BACKUP_JOURNAL_SUFFIX = "-journal"

COUNT_SQL = f"SELECT COUNT(*) FROM {TARGET_TABLE} WHERE {TARGET_PREDICATE}"

DELETE_SQL = f"""
DELETE FROM {TARGET_TABLE}
WHERE {TARGET_PREDICATE}
  AND rowid NOT IN (
      SELECT rowid FROM {TARGET_TABLE}
      WHERE {TARGET_PREDICATE}
      ORDER BY creation_utc DESC, rowid DESC
      LIMIT ?
  )
"""


# ---------------------------------------------------------------------------
# 异常与数据结构
# ---------------------------------------------------------------------------


class CookieCleanerError(Exception):
    """可直接展示给用户的中文错误。"""


class DshRunningError(CookieCleanerError):
    """检测到 DSH 正在运行，已安全停止。"""


class ConfigError(CookieCleanerError):
    """config.json 读取、校验或保存失败。"""


@dataclass(frozen=True)
class AppPaths:
    """程序自身目录及其 log / backups 子目录。"""

    base_dir: Path
    log_dir: Path
    log_path: Path
    backup_dir: Path


@dataclass(frozen=True)
class AppConfig:
    """用户通过首次设置窗口保存的配置。"""

    backup_dir: Path
    log_dir: Path
    backup_retention: int = DEFAULT_BACKUP_RETENTION
    log_max_mb: int = DEFAULT_LOG_MAX_MB
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT
    threshold: int = DEFAULT_THRESHOLD
    keep: int = DEFAULT_KEEP
    autostart: bool = False
    check_interval_hours: int = DEFAULT_CHECK_INTERVAL_HOURS


@dataclass(frozen=True)
class ResolvedPaths:
    network_dir: Path
    cookies_path: Path
    journal_path: Path
    source: str
    used_fallback: bool = False
    warning: str | None = None


@dataclass(frozen=True)
class DshProcess:
    pid: int
    name: str
    exe: str
    cmdline: str = ""


@dataclass(frozen=True)
class CookieStats:
    count: int
    keep: int
    threshold: int = DEFAULT_THRESHOLD

    @property
    def threshold_reached(self) -> bool:
        return self.count >= self.threshold

    @property
    def should_clean(self) -> bool:
        return self.threshold_reached and self.count > self.keep

    @property
    def would_delete(self) -> int:
        """不考虑阈值时的原始删除数量，供底层数据库测试使用。"""

        return max(0, self.count - self.keep)

    @property
    def planned_delete(self) -> int:
        """实际计划删除数量；未达到阈值时为 0。"""

        if not self.threshold_reached:
            return 0
        return self.would_delete

    @property
    def planned_after(self) -> int:
        return self.count - self.planned_delete


@dataclass(frozen=True)
class BackupGroup:
    """backups 目录中的一组备份：主库 + 可选 journal。"""

    base_name: str
    main: Path
    journal: Path | None
    backup_time: datetime
    sequence: int


@dataclass
class CleanupResult:
    cookies_path: Path
    before: int
    after: int
    deleted: int
    planned_delete: int
    keep: int
    threshold: int
    dry_run: bool
    skipped: bool = False
    skip_reason: str | None = None
    backup_path: Path | None = None
    backup_journal_path: Path | None = None
    backup_dir: Path | None = None
    backup_count: int = 0
    backups_deleted: int = 0
    backup_retention: int = DEFAULT_BACKUP_RETENTION
    journal_removed: bool = False


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


class _ChineseLevelFormatter(logging.Formatter):
    """让日志级别保持中文：信息 / 警告 / 错误。"""

    _LEVELS = {
        logging.DEBUG: "调试",
        logging.INFO: "信息",
        logging.WARNING: "警告",
        logging.ERROR: "错误",
        logging.CRITICAL: "严重",
    }

    def format(self, record: logging.LogRecord) -> str:
        original = record.levelname
        record.levelname = self._LEVELS.get(record.levelno, original)
        try:
            return super().format(record)
        finally:
            record.levelname = original


class Logger:
    """基于标准 logging 的文件日志。

    文件写入由 RotatingFileHandler 完成，按配置的大小上限和历史数量轮换。
    echo=True 时同时输出到控制台；--noconsole 打包版会自动关闭控制台输出。
    """

    def __init__(
        self,
        path: Path,
        echo: bool = True,
        max_bytes: int = DEFAULT_LOG_MAX_MB * 1024 * 1024,
        backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
    ) -> None:
        self.requested_path = Path(path)
        self.path = Path(path)
        self.echo = echo
        if max_bytes < 1:
            raise ValueError("单个日志大小上限必须大于 0 字节。")
        if backup_count < 0:
            raise ValueError("历史日志保留数量不能为负数。")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(
            f"dsh_cookie_cleaner.{id(self)}.{time.time_ns()}"
        )
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.handlers.clear()

        formatter = _ChineseLevelFormatter(
            "[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        self._file_handler = logging.handlers.RotatingFileHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
        self._file_handler.setFormatter(formatter)
        self._file_handler.setLevel(logging.INFO)
        self._logger.addHandler(self._file_handler)

        self._console_handler: logging.StreamHandler | None = None
        if echo and sys.stdout is not None:
            self._console_handler = logging.StreamHandler(sys.stdout)
            self._console_handler.setFormatter(formatter)
            self._console_handler.setLevel(logging.INFO)
            self._logger.addHandler(self._console_handler)

    def info(self, message: object) -> None:
        self._logger.info(str(message))

    def warning(self, message: object) -> None:
        self._logger.warning(str(message))

    def error(self, message: object) -> None:
        self._logger.error(str(message))

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            try:
                handler.flush()
                handler.close()
            except (OSError, ValueError):
                pass
            finally:
                try:
                    self._logger.removeHandler(handler)
                except (KeyError, ValueError):
                    pass
        self._logger.handlers.clear()


class _NullLogger:
    def info(self, message: object) -> None:
        pass

    def warning(self, message: object) -> None:
        pass

    def error(self, message: object) -> None:
        pass


def app_dir() -> Path:
    """exe 所在目录；源码运行时为 main.py 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def app_paths(base_dir: Path | str | None = None) -> AppPaths:
    """解析并创建程序目录下的 log / backups 文件夹。

    所有路径都相对于程序自身，绝不写死某个绝对目录，因此 exe 可以随意移动。
    """

    base = _as_absolute(Path(base_dir)) if base_dir is not None else app_dir()
    log_dir = base / LOG_DIR_NAME
    backup_dir = base / BACKUP_DIR_NAME
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CookieCleanerError(
            "无法在程序所在目录创建 log / backups 文件夹，"
            "请把程序放到有写权限的普通目录后再运行。\n"
            f"程序目录：{base}\n原因：{exc}"
        ) from exc

    return AppPaths(
        base_dir=base,
        log_dir=log_dir,
        log_path=log_dir / LOG_FILENAME,
        backup_dir=backup_dir,
    )


# ---------------------------------------------------------------------------
# config.json：首次设置窗口与配置读取
# ---------------------------------------------------------------------------


def config_path(base_dir: Path | str | None = None) -> Path:
    """config.json 固定在程序所在目录。"""

    base = _as_absolute(Path(base_dir)) if base_dir is not None else app_dir()
    return base / CONFIG_FILENAME


def default_config(base_dir: Path | str | None = None) -> AppConfig:
    """首次运行时的默认配置，不创建任何目录。"""

    base = _as_absolute(Path(base_dir)) if base_dir is not None else app_dir()
    return AppConfig(
        backup_dir=base / BACKUP_DIR_NAME,
        log_dir=base / LOG_DIR_NAME,
        backup_retention=DEFAULT_BACKUP_RETENTION,
        log_max_mb=DEFAULT_LOG_MAX_MB,
        log_backup_count=DEFAULT_LOG_BACKUP_COUNT,
        threshold=DEFAULT_THRESHOLD,
        keep=DEFAULT_KEEP,
        autostart=False,
        check_interval_hours=DEFAULT_CHECK_INTERVAL_HOURS,
    )


def _normalise_config_path(raw: object, default: Path, label: str) -> Path:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if not isinstance(raw, str):
        raise ConfigError(f"{label}必须是文本路径。")
    expanded = Path(os.path.expanduser(raw.strip()))
    if not expanded.is_absolute():
        expanded = default.parent / expanded
    return _as_absolute(expanded)


def _coerce_positive_int(raw: object, default: int, label: str) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):
        raise ConfigError(f"{label}必须是正整数。")
    if isinstance(raw, str):
        text = raw.strip()
        if text == "":
            return default
        if not text.isdigit():
            raise ConfigError(f"{label}只能输入数字！")
        raw = int(text)
    if not isinstance(raw, int) or raw < 1:
        raise ConfigError(f"{label}必须是大于 0 的整数。")
    return raw


def _coerce_bool(raw: object, default: bool, label: str) -> bool:
    """解析 true/false/1/0，兼容旧配置缺省字段。"""

    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str):
        text = raw.strip().casefold()
        if text in {"1", "true", "yes", "on", "是"}:
            return True
        if text in {"0", "false", "no", "off", "否"}:
            return False
    raise ConfigError(f"{label}必须是“是”或“否”。")


def load_config(
    path: Path | str | None = None,
    base_dir: Path | str | None = None,
) -> AppConfig:
    """读取并校验 config.json。缺失或损坏时抛出中文 ConfigError。"""

    cfg_path = Path(path) if path is not None else config_path(base_dir)
    try:
        text = cfg_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到配置文件：{cfg_path}") from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {cfg_path}：{exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件不是有效的 JSON：{cfg_path}\n"
            f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigError(f"配置文件根节点必须是对象：{cfg_path}")

    defaults = default_config(base_dir or cfg_path.parent)
    backup_dir = _normalise_config_path(
        data.get("backup_dir"), defaults.backup_dir, "备份存放位置"
    )
    log_dir = _normalise_config_path(
        data.get("log_dir"), defaults.log_dir, "日志存放位置"
    )
    return AppConfig(
        backup_dir=backup_dir,
        log_dir=log_dir,
        backup_retention=_coerce_positive_int(
            data.get("backup_retention"), defaults.backup_retention, "备份保留上限"
        ),
        log_max_mb=_coerce_positive_int(
            data.get("log_max_mb"), defaults.log_max_mb, "单个日志大小上限"
        ),
        log_backup_count=_coerce_positive_int(
            data.get("log_backup_count"),
            defaults.log_backup_count,
            "历史日志保留数量",
        ),
        threshold=_coerce_positive_int(
            data.get("threshold"), defaults.threshold, "触发清理阈值"
        ),
        keep=_coerce_positive_int(
            data.get("keep"), defaults.keep, "清理后保留最新 Cookie"
        ),
        autostart=_coerce_bool(
            data.get("autostart"), defaults.autostart, "开机自动清理"
        ),
        check_interval_hours=_coerce_positive_int(
            data.get("check_interval_hours"),
            defaults.check_interval_hours,
            "后台检查间隔（小时）",
        ),
    )


def save_config(config: AppConfig, path: Path | str | None = None) -> Path:
    """原子写入 config.json，避免半截配置文件。"""

    cfg_path = Path(path) if path is not None else config_path()
    payload = {
        "backup_dir": str(config.backup_dir),
        "log_dir": str(config.log_dir),
        "backup_retention": config.backup_retention,
        "log_max_mb": config.log_max_mb,
        "log_backup_count": config.log_backup_count,
        "threshold": config.threshold,
        "keep": config.keep,
        "autostart": config.autostart,
        "check_interval_hours": config.check_interval_hours,
    }
    tmp_path: Path | None = None
    try:
        os.makedirs(cfg_path.parent, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{CONFIG_FILENAME}.", suffix=".tmp", dir=str(cfg_path.parent)
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, cfg_path)
        tmp_path = None
        return cfg_path
    except OSError as exc:
        raise ConfigError(f"无法保存配置文件 {cfg_path}：{exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def ensure_config_dirs(config: AppConfig) -> None:
    """按配置创建备份/日志目录。"""

    for label, directory in (
        ("备份", config.backup_dir),
        ("日志", config.log_dir),
    ):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ConfigError(
                f"无法创建{label}目录：{directory}\n原因：{exc}"
            ) from exc
        if not directory.is_dir():
            raise ConfigError(f"{label}路径不是文件夹：{directory}")


def _unique_migration_target(directory: Path, name: str) -> Path:
    """目标目录已有同名文件时，追加 _1、_2，避免覆盖用户数据。"""

    candidate = directory / name
    if not candidate.exists():
        return candidate
    source = Path(name)
    for index in range(1, 10_000):
        candidate = directory / f"{source.stem}_{index}{source.suffix}"
        if not candidate.exists():
            return candidate
    raise ConfigError(f"目标目录中同名文件过多，无法迁移：{name}")


def migrate_directory(old_dir: Path, new_dir: Path, label: str) -> int:
    """把旧目录中的内容用 shutil.move 迁移到新目录，支持跨盘符。

    只迁移内容，不删除旧目录本身；同名文件自动改名，绝不覆盖。
    """

    old_abs = _as_absolute(old_dir)
    new_abs = _as_absolute(new_dir)
    if old_abs == new_abs or not old_abs.exists():
        return 0
    if not old_abs.is_dir():
        raise ConfigError(f"旧{label}路径不是文件夹，无法迁移：{old_abs}")
    try:
        if new_abs.resolve().is_relative_to(old_abs.resolve()):
            raise ConfigError(
                f"新{label}目录不能位于旧{label}目录内部：{new_abs}"
            )
    except OSError as exc:
        raise ConfigError(f"检查{label}迁移路径失败：{exc}") from exc

    try:
        os.makedirs(new_abs, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"无法创建新{label}目录 {new_abs}：{exc}") from exc

    moved = 0
    try:
        children = sorted(old_abs.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise ConfigError(f"无法读取旧{label}目录 {old_abs}：{exc}") from exc

    for source in children:
        target = _unique_migration_target(new_abs, source.name)
        try:
            shutil.move(str(source), str(target))
        except OSError as exc:
            raise ConfigError(
                f"迁移{label}文件失败：{source} -> {target}\n原因：{exc}"
            ) from exc
        moved += 1
    return moved


def migrate_config_paths(
    old: AppConfig, new: AppConfig, logger: object | None = None
) -> tuple[int, int]:
    """配置路径变化时迁移旧日志、旧备份；返回 (日志文件数, 备份文件数)。"""

    log = logger if logger is not None else _NullLogger()
    log_moved = 0
    backup_moved = 0
    if _as_absolute(old.log_dir) != _as_absolute(new.log_dir):
        log.info(f"迁移日志目录：{old.log_dir} -> {new.log_dir}")
        log_moved = migrate_directory(old.log_dir, new.log_dir, "日志")
    if _as_absolute(old.backup_dir) != _as_absolute(new.backup_dir):
        log.info(f"迁移备份目录：{old.backup_dir} -> {new.backup_dir}")
        backup_moved = migrate_directory(old.backup_dir, new.backup_dir, "备份")
    return log_moved, backup_moved


def parse_positive_int_text(raw: object, default: int, label: str) -> int:
    """GUI 数字输入校验：空白用默认值，非数字统一提示。"""

    text = str(raw).strip()
    if text == "":
        return default
    if not text.isdigit():
        raise ConfigError("只能输入数字！")
    value = int(text)
    if value < 1:
        raise ConfigError(f"{label}必须大于 0！")
    return value


def build_config_from_form(
    values: dict[str, object],
    base_dir: Path | str | None = None,
) -> AppConfig:
    """从设置窗口的字符串值构造配置；不创建目录、不写文件。"""

    defaults = default_config(base_dir)
    backup_dir = _normalise_config_path(
        values.get("backup_dir"), defaults.backup_dir, "备份存放位置"
    )
    log_dir = _normalise_config_path(
        values.get("log_dir"), defaults.log_dir, "日志存放位置"
    )
    return AppConfig(
        backup_dir=backup_dir,
        log_dir=log_dir,
        backup_retention=parse_positive_int_text(
            values.get("backup_retention", ""),
            defaults.backup_retention,
            "备份保留上限",
        ),
        log_max_mb=parse_positive_int_text(
            values.get("log_max_mb", ""),
            defaults.log_max_mb,
            "单个日志大小上限",
        ),
        log_backup_count=parse_positive_int_text(
            values.get("log_backup_count", ""),
            defaults.log_backup_count,
            "历史日志保留数量",
        ),
        threshold=parse_positive_int_text(
            values.get("threshold", ""),
            defaults.threshold,
            "触发清理阈值",
        ),
        keep=parse_positive_int_text(
            values.get("keep", ""),
            defaults.keep,
            "清理后保留最新 Cookie",
        ),
        autostart=_coerce_bool(
            values.get("autostart"), defaults.autostart, "开机自动清理"
        ),
        check_interval_hours=parse_positive_int_text(
            values.get("check_interval_hours", ""),
            defaults.check_interval_hours,
            "后台检查间隔（小时）",
        ),
    )


def autostart_command() -> str:
    """开机自启命令行：打包版直接调用 exe，源码运行时调用当前解释器。"""

    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        return f'"{executable}" --silent'
    return f'"{executable}" "{Path(__file__).resolve()}" --silent'


def apply_autostart(enabled: bool, command: str | None = None) -> None:
    """写入或删除 HKCU Run 项；失败时抛出中文 ConfigError。"""

    if winreg is None:
        raise ConfigError("当前系统不支持写入开机自启注册表项。")

    cmd = command if command is not None else autostart_command()
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    AUTOSTART_VALUE_NAME,
                    0,
                    winreg.REG_SZ,
                    cmd,
                )
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_VALUE_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        action = "写入" if enabled else "删除"
        raise ConfigError(f"无法{action}开机自启注册表项：{exc}") from exc


def _sync_autostart(config: AppConfig, logger: object) -> None:
    """打包版每次启动时校正注册表，兼容移动文件夹后的自启路径。"""

    if not getattr(sys, "frozen", False):
        return
    try:
        apply_autostart(config.autostart)
    except ConfigError as exc:
        try:
            logger.warning(str(exc))  # type: ignore[attr-defined]
        except AttributeError:
            pass


def _safe_print(text: object) -> None:
    if sys.stdout is None:
        return
    try:
        print(text)
    except (OSError, UnicodeError, AttributeError):
        pass


def _show_message(kind: str, title: str, text: str) -> None:
    """在 GUI 打包版中显示提示；没有 Tk 时退回控制台。"""

    if tk is None or messagebox is None:
        _safe_print(f"{title}\n\n{text}")
        return

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        shower = getattr(messagebox, kind)
        shower(title, text, parent=root)
    except (tk.TclError, AttributeError, RuntimeError):
        _safe_print(f"{title}\n\n{text}")
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass


def show_error(title: str, text: str) -> None:
    _show_message("showerror", title, text)


def show_info(title: str, text: str) -> None:
    _show_message("showinfo", title, text)


def show_native_info(title: str, text: str) -> None:
    """线程安全的信息框，供托盘后台线程使用，不创建 Tk 根窗口。"""

    if sys.platform != "win32":
        show_info(title, text)
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            str(text),
            str(title),
            0x00000040 | 0x00040000,  # MB_ICONINFORMATION | MB_TOPMOST
        )
    except BaseException:
        _safe_print(f"{title}\n\n{text}")


class _SetupDialog:
    """首次运行/--setup 的图形设置窗口。"""

    _NUMERIC_FIELDS = (
        ("backup_retention", "备份保留上限（份）：", 30),
        ("log_max_mb", "单个日志大小上限（MB）：", 2),
        ("log_backup_count", "历史日志保留数量（个）：", 3),
        ("threshold", "触发清理阈值（条）：", 50),
        ("keep", "清理后保留最新 Cookie（条）：", 10),
        ("check_interval_hours", "后台检查间隔（小时）：", 2),
    )

    def __init__(self, initial: AppConfig, cfg_path: Path) -> None:
        if tk is None or ttk is None:
            raise ConfigError(
                "当前运行环境缺少 tkinter，无法打开图形设置窗口。"
                "请使用带 Tk 支持的 Python，或直接使用已打包的 EXE。"
            )
        self.initial = initial
        self.cfg_path = cfg_path
        self.result: AppConfig | None = None

        self.root = tk.Tk()
        self.root.title(SETUP_TITLE)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)

        self.vars: dict[str, tk.StringVar] = {
            "backup_dir": tk.StringVar(value=str(initial.backup_dir)),
            "log_dir": tk.StringVar(value=str(initial.log_dir)),
            "backup_retention": tk.StringVar(value=str(initial.backup_retention)),
            "log_max_mb": tk.StringVar(value=str(initial.log_max_mb)),
            "log_backup_count": tk.StringVar(value=str(initial.log_backup_count)),
            "threshold": tk.StringVar(value=str(initial.threshold)),
            "keep": tk.StringVar(value=str(initial.keep)),
            "check_interval_hours": tk.StringVar(
                value=str(initial.check_interval_hours)
            ),
        }
        self.autostart_var = tk.BooleanVar(value=initial.autostart)

        body = ttk.Frame(self.root, padding=18)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)

        ttk.Label(
            body,
            text="首次使用，请确认下面的设置",
            font=("Microsoft YaHei UI", 13, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            body,
            text="设置会保存在程序目录的 config.json；以后运行不再弹出本窗口。",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 14))

        row = 2
        self._add_path_row(
            body,
            row,
            "backup_dir",
            "备份存放位置：",
            "选择备份存放文件夹",
        )
        row += 1
        self._add_path_row(
            body,
            row,
            "log_dir",
            "日志存放位置：",
            "选择日志存放文件夹",
        )
        row += 1
        ttk.Separator(body, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=12
        )
        row += 1

        self._numeric_entries: dict[str, ttk.Entry] = {}
        for key, label, _default in self._NUMERIC_FIELDS:
            ttk.Label(body, text=label).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=4
            )
            entry = ttk.Entry(body, textvariable=self.vars[key], width=12)
            entry.grid(row=row, column=2, sticky="e", pady=4)
            self._numeric_entries[key] = entry
            row += 1

        ttk.Separator(body, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(10, 8)
        )
        row += 1
        ttk.Checkbutton(
            body,
            text="开机时自动在后台清理（静默运行）",
            variable=self.autostart_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 6))
        row += 1

        ttk.Label(
            body,
            text=f"配置文件：{self.cfg_path}",
            foreground="#777777",
            wraplength=600,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(14, 10))
        row += 1

        buttons = ttk.Frame(body)
        buttons.grid(row=row, column=0, columnspan=3, sticky="e")
        ttk.Button(buttons, text="取消", command=self._cancel).grid(
            row=0, column=0, padx=(0, 8)
        )
        ttk.Button(buttons, text="保存并继续", command=self._save).grid(
            row=0, column=1
        )

        self.root.bind("<Return>", lambda _event: self._save())
        self.root.bind("<Escape>", lambda _event: self._cancel())
        self.root.update_idletasks()
        self._center()
        self.root.after(80, self.root.focus_force)

    def _center(self) -> None:
        width = max(650, self.root.winfo_reqwidth())
        height = self.root.winfo_reqheight()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _add_path_row(
        self,
        parent: ttk.Frame,
        row: int,
        key: str,
        label: str,
        dialog_title: str,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", pady=4
        )
        ttk.Entry(parent, textvariable=self.vars[key], width=56).grid(
            row=row, column=1, sticky="ew", padx=(8, 8), pady=4
        )
        ttk.Button(
            parent,
            text="浏览...",
            command=lambda: self._browse(key, dialog_title),
        ).grid(row=row, column=2, sticky="e", pady=4)

    def _browse(self, key: str, dialog_title: str) -> None:
        if filedialog is None:
            return
        current = self.vars[key].get().strip()
        initialdir = current if os.path.isdir(current) else str(self.cfg_path.parent)
        chosen = filedialog.askdirectory(
            parent=self.root,
            title=dialog_title,
            initialdir=initialdir,
            mustexist=False,
        )
        if chosen:
            self.vars[key].set(chosen)

    def _save(self) -> None:
        values = {key: var.get() for key, var in self.vars.items()}
        values["autostart"] = "1" if self.autostart_var.get() else "0"
        try:
            config = build_config_from_form(values, base_dir=self.cfg_path.parent)
        except ConfigError as exc:
            messagebox.showerror("输入错误", str(exc), parent=self.root)
            return

        try:
            migrate_config_paths(self.initial, config)
        except ConfigError as exc:
            messagebox.showerror("迁移旧文件失败", str(exc), parent=self.root)
            return

        try:
            ensure_config_dirs(config)
        except ConfigError as exc:
            messagebox.showerror("无法创建目录", str(exc), parent=self.root)
            return

        try:
            save_config(config, self.cfg_path)
        except ConfigError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return

        # 源码运行时写注册表会指向 python.exe，不适合作为开机启动项；
        # 只有打包后的 EXE 才真正登记/取消登记。
        if getattr(sys, "frozen", False):
            try:
                apply_autostart(config.autostart)
            except ConfigError as exc:
                messagebox.showerror(
                    "开机自启设置失败",
                    f"{exc}\n\n配置已保存，但开机自启未能更新。"
                    "请检查系统安全软件是否拦截注册表写入。",
                    parent=self.root,
                )
                return

        # 空白数字输入已回填为默认值，保存后同步显示（窗口随后关闭）。
        self.vars["backup_retention"].set(str(config.backup_retention))
        self.vars["log_max_mb"].set(str(config.log_max_mb))
        self.vars["log_backup_count"].set(str(config.log_backup_count))
        self.vars["threshold"].set(str(config.threshold))
        self.vars["keep"].set(str(config.keep))
        self.vars["check_interval_hours"].set(str(config.check_interval_hours))
        self.result = config
        self.root.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.root.destroy()

    def show(self) -> AppConfig | None:
        self.root.mainloop()
        return self.result


def open_setup_dialog(
    initial: AppConfig | None = None,
    cfg_path: Path | None = None,
) -> AppConfig | None:
    """打开首次设置窗口；用户取消时返回 None。"""

    if tk is None:
        raise ConfigError(
            "当前运行环境缺少 tkinter，无法打开图形设置窗口。"
            "请使用带 Tk 支持的 Python，或直接使用已打包的 EXE。"
        )
    config = initial if initial is not None else default_config()
    path = cfg_path if cfg_path is not None else config_path()
    try:
        dialog = _SetupDialog(config, path)
    except tk.TclError as exc:
        raise ConfigError(f"无法打开图形设置窗口：{exc}") from exc
    return dialog.show()


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def _as_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def resolve_paths(
    override: str | os.PathLike[str] | None = None,
    appdata: str | None = None,
) -> ResolvedPaths:
    """优先从 APPDATA 解析，只有主路径不存在时才用硬编码回退。"""

    if override:
        network_dir = _as_absolute(Path(override))
        return ResolvedPaths(
            network_dir=network_dir,
            cookies_path=network_dir / "Cookies",
            journal_path=network_dir / "Cookies-journal",
            source="--cookies-dir 参数",
        )

    raw_appdata = appdata if appdata is not None else os.environ.get("APPDATA")
    primary: Path | None = None
    if raw_appdata and str(raw_appdata).strip():
        primary = _as_absolute(
            Path(raw_appdata) / "@deepseek-ai" / "dsh-desktop" / "Network"
        )
        try:
            if primary.is_dir():
                return ResolvedPaths(
                    network_dir=primary,
                    cookies_path=primary / "Cookies",
                    journal_path=primary / "Cookies-journal",
                    source="APPDATA 环境变量",
                )
        except OSError as exc:
            raise CookieCleanerError(
                f"无法检查 APPDATA 下的 DSH 路径 {primary}：{exc}"
            ) from exc

    fallback = _as_absolute(FALLBACK_NETWORK_DIR)
    try:
        fallback_exists = fallback.is_dir()
    except OSError:
        fallback_exists = False

    if fallback_exists:
        warning = f"使用了硬编码回退路径，可能不是当前用户：{fallback}"
        return ResolvedPaths(
            network_dir=fallback,
            cookies_path=fallback / "Cookies",
            journal_path=fallback / "Cookies-journal",
            source="硬编码回退路径",
            used_fallback=True,
            warning=warning,
        )

    if primary is not None:
        raise CookieCleanerError(
            "未找到 DSH 的 Network 目录。\n"
            f"APPDATA 解析路径：{primary}\n"
            f"硬编码回退路径：{fallback}\n"
            "请确认 DSH 桌面版已安装并至少成功运行过一次。"
        )
    raise CookieCleanerError(
        "无法解析 DSH 的 Network 目录：环境变量 APPDATA 为空，"
        f"且硬编码回退路径不存在：{fallback}"
    )


# ---------------------------------------------------------------------------
# DSH 进程检测
# ---------------------------------------------------------------------------


def _normalise_text(value: object) -> str:
    return str(value or "").casefold().replace("/", "\\")


def is_dsh_process(
    name: str,
    exe: str,
    cmdline: Sequence[str] | None = None,
) -> bool:
    """精确识别 DSH，避免把普通 Electron 程序误判为 DSH。"""

    lname = _normalise_text(name)
    lexe = _normalise_text(exe)
    joined_cmdline = " ".join(str(x) for x in (cmdline or ())).replace("/", "\\").casefold()

    if "dsh-desktop" in lname:
        return True
    if "deepseek harness" in lname:
        return True
    if "deepseek-harness" in lname or "deepseek_harness" in lname:
        return True
    if "dsh-desktop" in lexe:
        return True

    # Electron 开发/包装场景：exe 本身叫 electron.exe，但命令行指向 dsh-desktop。
    exe_basename = Path(lexe).name
    if exe_basename in {"electron.exe", "electron"} and "dsh-desktop" in joined_cmdline:
        return True

    return False


def find_dsh_processes() -> list[DshProcess]:
    if psutil is None:
        raise CookieCleanerError(
            "缺少 psutil 依赖，无法安全检测 DSH 是否正在运行。"
            "请先执行：python -m pip install -r requirements.txt"
        )

    found: dict[int, DshProcess] = {}
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            pid = int(proc.info.get("pid") or 0)
            if pid == os.getpid():
                continue
            name = str(proc.info.get("name") or "")
            exe = str(proc.info.get("exe") or "")
            raw_cmdline = proc.info.get("cmdline") or []
            cmdline = [str(x) for x in raw_cmdline]
            if is_dsh_process(name, exe, cmdline):
                found[pid] = DshProcess(pid=pid, name=name, exe=exe, cmdline=" ".join(cmdline))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except (TypeError, ValueError):
            continue

    return sorted(found.values(), key=lambda item: item.pid)


def _describe_processes(processes: Iterable[DshProcess]) -> str:
    lines = []
    for proc in processes:
        exe = proc.exe or "<无法读取路径>"
        lines.append(f"  - PID {proc.pid} | {proc.name or '<未知进程>'} | {exe}")
    return "\n".join(lines)


def _guard_not_running(
    scanner: Callable[[], Sequence[DshProcess]],
    logger: object,
    phase: str,
) -> None:
    logger.info(f"检测 DSH 进程（{phase}）...")
    try:
        processes = list(scanner())
    except CookieCleanerError:
        raise
    except Exception as exc:
        raise CookieCleanerError(
            f"无法检测 DSH 进程：{exc}。为避免数据库损坏，已停止。"
        ) from exc

    if not processes:
        logger.info("未检测到 DSH 进程。")
        return

    detail = _describe_processes(processes)
    logger.error(f"检测到 {len(processes)} 个 DSH 进程：\n{detail}")
    if phase == "写回前":
        tail = "已放弃写回，原 Cookies 未被替换。"
    else:
        tail = "已停止，未读取或修改 Cookies。"
    raise DshRunningError(
        f"检测到 DSH 正在运行：\n{detail}\n\n"
        f"{tail}\n"
        "请先完全退出 DSH（含托盘）后再运行。"
    )


# ---------------------------------------------------------------------------
# SQLite 处理
# ---------------------------------------------------------------------------


def _database_error(action: str, db_path: Path, exc: BaseException) -> CookieCleanerError:
    text = str(exc)
    low = text.casefold()
    if "locked" in low or "busy" in low:
        detail = "数据库被占用，请确认 DSH 已完全退出（含托盘）后重试。"
    elif "readonly" in low or "permission" in low or "access is denied" in low:
        detail = "文件只读或权限不足，请检查当前用户对 Cookies 文件的权限。"
    elif "not a database" in low or "malformed" in low or "disk image" in low:
        detail = "Cookies 不是有效的 SQLite 数据库，或数据库已经损坏。"
    elif "no such table" in low:
        detail = "数据库中没有 cookies 表，文件可能不是 DSH/Chromium 的 Cookies 数据库。"
    elif "no such column" in low:
        detail = "cookies 表缺少本工具需要的列。"
    else:
        detail = f"SQLite 返回：{text}"
    return CookieCleanerError(f"{action}失败：{detail}\n文件：{db_path}")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _check_schema(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name = ? COLLATE NOCASE",
        (TARGET_TABLE,),
    ).fetchone()
    if row is None:
        raise CookieCleanerError(
            "未找到 cookies 表。该文件可能不是 DSH/Chromium 的 Cookies 数据库。"
        )

    columns = {
        str(item[1]).casefold()
        for item in conn.execute(f"PRAGMA table_info({TARGET_TABLE})").fetchall()
    }
    required = {"host_key", "name", "creation_utc"}
    missing = required - columns
    if missing:
        raise CookieCleanerError(
            "cookies 表缺少必需列："
            + ", ".join(sorted(missing))
            + "。请确认这是 Chromium 格式的 Cookies 数据库。"
        )

    try:
        conn.execute(f"SELECT rowid FROM {TARGET_TABLE} LIMIT 0").fetchall()
    except sqlite3.Error as exc:
        raise CookieCleanerError(
            "cookies 表不支持 rowid，无法按本工具的安全 SQL 清理。"
        ) from exc


def _quick_check(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA quick_check").fetchall()
    if not rows or str(rows[0][0]).casefold() != "ok":
        detail = "; ".join(str(row[0]) for row in rows[:5]) or "未知错误"
        raise CookieCleanerError(f"Cookies 数据库完整性检查失败：{detail}")


def _count_target_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute(COUNT_SQL).fetchone()[0])


def _validate_limits(
    keep: int,
    threshold: int,
    backup_retention: int | None = None,
) -> None:
    if keep < 1:
        raise CookieCleanerError("清理后保留条数必须大于等于 1。")
    if threshold < 1:
        raise CookieCleanerError("清理阈值必须大于等于 1。")
    if backup_retention is not None and backup_retention < 1:
        raise CookieCleanerError("备份保留上限必须大于等于 1。")


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """以只读 URI 打开数据库，确保预检阶段不可能写入。"""

    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def preview_database(
    db_path: Path,
    keep: int,
    threshold: int = DEFAULT_THRESHOLD,
) -> CookieStats:
    """对原始 Cookies 做只读预检，用于阈值判断。

    该函数不会创建临时副本、不会备份、不会写入原库。
    """

    _validate_limits(keep, threshold)
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect_readonly(db_path)
        _quick_check(conn)
        _check_schema(conn)
        return CookieStats(
            count=_count_target_rows(conn),
            keep=keep,
            threshold=threshold,
        )
    except CookieCleanerError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _database_error("只读预检 Cookies 数据库", db_path, exc) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def analyze_database(
    db_path: Path,
    keep: int,
    threshold: int = DEFAULT_THRESHOLD,
) -> CookieStats:
    """只读分析临时数据库副本，不做任何删除。"""

    _validate_limits(keep, threshold)
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(db_path)
        _quick_check(conn)
        _check_schema(conn)
        return CookieStats(
            count=_count_target_rows(conn),
            keep=keep,
            threshold=threshold,
        )
    except CookieCleanerError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _database_error("分析 Cookies 数据库", db_path, exc) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _checkpoint_if_wal(conn: sqlite3.Connection) -> None:
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        mode = str(row[0]).casefold() if row else ""
        if mode == "wal":
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result and int(result[0]) != 0:
                raise CookieCleanerError(
                    "临时数据库 WAL 检查点未完成，已中止写回以避免丢失数据。"
                )
    except sqlite3.Error as exc:
        raise CookieCleanerError(f"处理临时数据库 WAL 失败：{exc}") from exc


def clean_database(db_path: Path, keep: int) -> tuple[int, int, int]:
    """清理临时数据库，返回 (清理前, 清理后, 删除条数)。"""

    if keep < 1:
        raise CookieCleanerError("--keep 必须大于等于 1。")

    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(db_path)
        _quick_check(conn)
        _check_schema(conn)
        before = _count_target_rows(conn)
        if before <= keep:
            return before, before, 0

        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _count_target_rows(conn)
            if current != before:
                raise CookieCleanerError(
                    "临时数据库在清理过程中发生变化，已中止本次操作。"
                )
            cursor = conn.execute(DELETE_SQL, (keep,))
            deleted = cursor.rowcount
            after = _count_target_rows(conn)
            if deleted < 0:
                deleted = before - after
            if before - deleted != after:
                raise CookieCleanerError(
                    "删除条数校验失败："
                    f"清理前 {before}，删除 {deleted}，清理后 {after}。"
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

        _checkpoint_if_wal(conn)
        return before, after, deleted
    except CookieCleanerError:
        raise
    except (sqlite3.Error, OSError) as exc:
        raise _database_error("清理 Cookies 数据库", db_path, exc) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# ---------------------------------------------------------------------------
# 临时副本、备份与写回
# ---------------------------------------------------------------------------


def _validate_source(paths: ResolvedPaths, logger: object) -> None:
    try:
        if not paths.network_dir.exists():
            raise CookieCleanerError(f"DSH Network 目录不存在：{paths.network_dir}")
        if not paths.network_dir.is_dir():
            raise CookieCleanerError(f"DSH Network 路径不是目录：{paths.network_dir}")
        if not paths.cookies_path.is_file():
            raise CookieCleanerError(f"未找到 Cookies 文件：{paths.cookies_path}")
        if paths.cookies_path.stat().st_size <= 0:
            raise CookieCleanerError(f"Cookies 文件为空，拒绝处理：{paths.cookies_path}")
        if paths.journal_path.exists() and not paths.journal_path.is_file():
            raise CookieCleanerError(
                f"Cookies-journal 不是普通文件，拒绝处理：{paths.journal_path}"
            )
    except CookieCleanerError:
        raise
    except OSError as exc:
        raise CookieCleanerError(f"检查 Cookies 路径失败：{exc}") from exc

    # WAL 文件和主库必须成套处理。本工具不触碰 WAL，因此检测到未合并 WAL 时安全退出。
    wal_path = paths.network_dir / "Cookies-wal"
    try:
        if wal_path.is_file() and wal_path.stat().st_size > 0:
            raise CookieCleanerError(
                "检测到未合并的 Cookies-wal 文件。"
                "为避免丢失最新 Cookie，请先启动 DSH 并正常完全退出一次，"
                "待 WAL 合并后重新运行本工具。"
            )
    except OSError as exc:
        raise CookieCleanerError(f"检查 Cookies-wal 失败：{exc}") from exc

    logger.info(f"Cookies 文件：{paths.cookies_path}")
    logger.info(f"Cookies 大小：{paths.cookies_path.stat().st_size} 字节")
    if paths.journal_path.exists():
        logger.info(f"检测到 Cookies-journal：{paths.journal_path}")


def _prepare_working_copy(paths: ResolvedPaths) -> tuple[Path, Path]:
    """在 Network 同盘目录建立临时副本，确保后续 os.replace 是原子替换。"""

    try:
        workdir = Path(
            tempfile.mkdtemp(prefix=".dsh-cookie-clean-", dir=str(paths.network_dir))
        )
    except OSError as exc:
        raise CookieCleanerError(
            "无法在 DSH Network 目录创建临时工作目录（可能权限不足或被安全软件拦截）："
            f"{exc}"
        ) from exc

    work_db = workdir / "Cookies"
    try:
        shutil.copy2(paths.cookies_path, work_db)
        os.chmod(work_db, stat.S_IREAD | stat.S_IWRITE)
        if paths.journal_path.is_file():
            work_journal = workdir / "Cookies-journal"
            shutil.copy2(paths.journal_path, work_journal)
            os.chmod(work_journal, stat.S_IREAD | stat.S_IWRITE)
    except OSError as exc:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass
        raise CookieCleanerError(
            f"复制 Cookies 到临时目录失败（文件可能被占用或权限不足）：{exc}"
        ) from exc

    return workdir, work_db


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _new_backup_path(backup_dir: Path) -> Path:
    base = f"Cookies.bak.{_timestamp()}"
    candidate = backup_dir / base
    index = 1
    while candidate.exists() or candidate.with_name(candidate.name + "-journal").exists():
        candidate = backup_dir / f"{base}_{index}"
        index += 1
    return candidate


def create_backup(
    paths: ResolvedPaths,
    backup_dir: Path,
    logger: object,
) -> tuple[Path, Path | None]:
    """把原始 Cookies（以及可能存在的 journal）复制到 backups 目录。"""

    backup_dir = _as_absolute(Path(backup_dir))
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        if not backup_dir.is_dir():
            raise OSError("备份路径不是目录")
    except OSError as exc:
        raise CookieCleanerError(
            "无法创建/访问备份目录，已取消清理，原文件未被修改。\n"
            f"备份目录：{backup_dir}\n原因：{exc}"
        ) from exc

    backup = _new_backup_path(backup_dir)
    backup_journal: Path | None = None
    try:
        shutil.copy2(paths.cookies_path, backup)
        os.chmod(backup, stat.S_IREAD | stat.S_IWRITE)
        if backup.stat().st_size != paths.cookies_path.stat().st_size:
            raise OSError("备份文件大小与原文件不一致")
        if paths.journal_path.is_file():
            backup_journal = backup.with_name(backup.name + _BACKUP_JOURNAL_SUFFIX)
            shutil.copy2(paths.journal_path, backup_journal)
            os.chmod(backup_journal, stat.S_IREAD | stat.S_IWRITE)
    except OSError as exc:
        # 不留下半截备份，避免它被当成可恢复的完整备份。
        for partial in (backup, backup_journal):
            if partial is not None:
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
        raise CookieCleanerError(
            "备份 Cookies 失败，已取消清理，原文件未被修改："
            f"{exc}\n目标备份：{backup}"
        ) from exc

    logger.info(f"已备份 Cookies 到 backups 目录：{backup}")
    if backup_journal is not None:
        logger.info(f"已同时备份 Cookies-journal：{backup_journal}")
    return backup, backup_journal


def scan_backup_groups(backup_dir: Path) -> list[BackupGroup]:
    """扫描 backups 目录，按“主库 + 可选 journal”分组。

    只认严格匹配 Cookies.bak.YYYYMMDD_HHMMSS[_n] 的普通文件；
    不跟随符号链接，也不读取备份内容。返回按时间从新到旧排列的组。
    """

    backup_dir = _as_absolute(Path(backup_dir))
    if not backup_dir.is_dir():
        return []

    parts: dict[str, dict[str, Path]] = {}
    try:
        entries = list(backup_dir.iterdir())
    except OSError as exc:
        raise CookieCleanerError(f"无法读取备份目录 {backup_dir}：{exc}") from exc

    for item in entries:
        try:
            if item.is_symlink() or not item.is_file():
                continue
        except OSError:
            continue

        name = item.name
        is_journal = name.endswith(_BACKUP_JOURNAL_SUFFIX)
        base_name = name[: -len(_BACKUP_JOURNAL_SUFFIX)] if is_journal else name
        match = _BACKUP_MAIN_RE.fullmatch(base_name)
        if match is None:
            continue

        bucket = parts.setdefault(base_name, {})
        bucket["journal" if is_journal else "main"] = item

    groups: list[BackupGroup] = []
    for base_name, bucket in parts.items():
        main = bucket.get("main")
        if main is None:
            # 只有孤立 journal 时不算完整备份组，绝不擅自删除它。
            continue
        match = _BACKUP_MAIN_RE.fullmatch(base_name)
        if match is None:
            continue
        try:
            backup_time = datetime.strptime(match.group(2), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        groups.append(
            BackupGroup(
                base_name=base_name,
                main=main,
                journal=bucket.get("journal"),
                backup_time=backup_time,
                sequence=int(match.group(3) or 0),
            )
        )

    groups.sort(
        key=lambda group: (group.backup_time, group.sequence, group.base_name),
        reverse=True,
    )
    return groups


def _safe_unlink_backup(path: Path, backup_dir: Path, logger: object) -> bool:
    """只删除 backups 目录下、严格匹配备份名的普通文件。"""

    try:
        if path.parent != backup_dir:
            logger.warning(f"拒绝删除 backups 目录之外的文件：{path}")
            return False
        if path.is_symlink():
            logger.warning(f"拒绝删除符号链接：{path}")
            return False
        if not path.is_file():
            return False
        path.unlink()
        return True
    except OSError as exc:
        logger.warning(f"删除旧备份失败：{path}（{exc}）")
        return False


def prune_backups(
    backup_dir: Path,
    retention: int = BACKUP_RETENTION,
    logger: object | None = None,
) -> tuple[int, int]:
    """只保留最近 retention 组备份，返回 (删除文件数, 剩余组数)。"""

    if retention < 1:
        raise CookieCleanerError("备份保留组数必须大于等于 1。")

    log = logger if logger is not None else _NullLogger()
    backup_dir = _as_absolute(Path(backup_dir))
    try:
        groups = scan_backup_groups(backup_dir)
    except CookieCleanerError as exc:
        log.warning(str(exc))
        return 0, 0

    if len(groups) <= retention:
        log.info(
            f"清理旧备份：当前 {len(groups)} 组，未超过 {retention} 组，无需删除。"
        )
        return 0, len(groups)

    doomed = groups[retention:]
    deleted_files = 0
    for group in doomed:
        for path in (group.main, group.journal):
            if path is not None and _safe_unlink_backup(path, backup_dir, log):
                deleted_files += 1

    remaining = len(groups) - len(doomed)
    log.info(
        f"清理旧备份：已删除 {deleted_files} 个历史备份文件"
        f"（{len(doomed)} 组），当前保留 {remaining} 组。"
    )
    return deleted_files, remaining


def _write_back(
    work_db: Path,
    paths: ResolvedPaths,
    logger: object,
) -> bool:
    """把清理后的临时库原子替换回 Cookies；随后删除 journal。"""

    try:
        os.replace(work_db, paths.cookies_path)
    except OSError as exc:
        raise CookieCleanerError(
            "Cookies 写回失败，原文件未被替换。请确认 DSH 已完全退出、"
            f"文件未被安全软件占用，且当前用户有写权限。\n原因：{exc}"
        ) from exc

    logger.info(f"已原子写回 Cookies：{paths.cookies_path}")

    journal_removed = False
    if paths.journal_path.exists():
        try:
            paths.journal_path.unlink()
            journal_removed = True
            logger.info(f"已删除 Cookies-journal：{paths.journal_path}")
        except OSError as exc:
            raise CookieCleanerError(
                "Cookies 已成功写回并保留了备份，但无法删除 Cookies-journal。"
                "请不要启动 DSH，先手动删除该文件后再启动。\n"
                f"文件：{paths.journal_path}\n原因：{exc}"
            ) from exc

    return journal_removed


# ---------------------------------------------------------------------------
# 主清理流程
# ---------------------------------------------------------------------------


def cleanup(
    paths: ResolvedPaths,
    keep: int = DEFAULT_KEEP,
    dry_run: bool = False,
    logger: object | None = None,
    scanner: Callable[[], Sequence[DshProcess]] | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
    wait_seconds: float = WAIT_SECONDS,
    threshold: int = DEFAULT_THRESHOLD,
    backup_dir: Path | None = None,
    backup_retention: int = DEFAULT_BACKUP_RETENTION,
) -> CleanupResult:
    """执行一次安全清理。

    顺序很重要：先只读检查阈值，只有达到阈值才会复制到临时工作区。
    因而低于阈值时，原始 Cookies 连“复制”都不会发生，更不会备份或写回。
    """

    log = logger if logger is not None else _NullLogger()
    scan = scanner if scanner is not None else find_dsh_processes

    _validate_limits(keep, threshold, backup_retention)
    if backup_dir is None:
        backup_dir = app_paths().backup_dir
    else:
        backup_dir = _as_absolute(Path(backup_dir))

    # 1) 启动前检测；绝不先碰数据库。
    _guard_not_running(scan, log, "启动前")

    # 2) 等待 2 秒释放可能残留的文件锁，然后再次检测。
    log.info(f"等待 {wait_seconds:g} 秒，等待文件锁释放...")
    sleep_func(wait_seconds)
    _guard_not_running(scan, log, "复制前")

    _validate_source(paths, log)
    log.info("只读预检原始 Cookies（不复制、不备份、不写回）...")
    stats = preview_database(paths.cookies_path, keep, threshold)
    log.info(
        f"清理前匹配 dsh-auth-*：{stats.count} 条；"
        f"清理阈值：{threshold} 条；保留上限：{keep} 条"
    )

    if not stats.threshold_reached:
        reason = (
            f"当前 {stats.count} 条，未达到清理阈值 {threshold} 条，"
            "跳过清理并退出。"
        )
        log.info(reason)
        log.info("未复制、未备份、未修改原始 Cookies。")
        return CleanupResult(
            cookies_path=paths.cookies_path,
            before=stats.count,
            after=stats.count,
            deleted=0,
            planned_delete=0,
            keep=keep,
            threshold=threshold,
            backup_retention=backup_retention,
            dry_run=dry_run,
            skipped=True,
            skip_reason=reason,
            backup_dir=backup_dir,
        )

    if stats.count <= keep:
        reason = (
            f"当前 {stats.count} 条，达到清理阈值 {threshold} 条，"
            f"但未超过保留上限 {keep} 条，无需清理。"
        )
        log.info(reason)
        log.info("未复制、未备份、未修改原始 Cookies。")
        return CleanupResult(
            cookies_path=paths.cookies_path,
            before=stats.count,
            after=stats.count,
            deleted=0,
            planned_delete=0,
            keep=keep,
            threshold=threshold,
            backup_retention=backup_retention,
            dry_run=dry_run,
            skipped=True,
            skip_reason=reason,
            backup_dir=backup_dir,
        )

    planned_delete = stats.would_delete
    log.info(f"计划删除：{planned_delete} 条；计划保留：{stats.planned_after} 条")

    if dry_run:
        log.info("演练模式（--dry-run）：不复制、不备份、不删除、不写回。")
        return CleanupResult(
            cookies_path=paths.cookies_path,
            before=stats.count,
            after=stats.count,
            deleted=0,
            planned_delete=planned_delete,
            keep=keep,
            threshold=threshold,
            backup_retention=backup_retention,
            dry_run=True,
            backup_dir=backup_dir,
        )

    workdir: Path | None = None
    try:
        log.info("复制 Cookies 到同目录临时工作区...")
        workdir, work_db = _prepare_working_copy(paths)

        log.info("分析临时数据库（真实 Cookies 尚未修改）...")
        work_stats = analyze_database(work_db, keep, threshold)
        if work_stats.count != stats.count:
            raise CookieCleanerError(
                "预检后 Cookies 记录数量发生变化，可能被其他程序修改；"
                "已放弃清理，原 Cookies 未被替换。"
                f"（预检 {stats.count} 条 / 现在 {work_stats.count} 条）"
            )
        if work_stats.count <= keep:
            reason = (
                f"当前 {work_stats.count} 条，未超过保留上限 {keep} 条，"
                "无需清理。"
            )
            log.info(reason)
            return CleanupResult(
                cookies_path=paths.cookies_path,
                before=work_stats.count,
                after=work_stats.count,
                deleted=0,
                planned_delete=0,
                keep=keep,
                threshold=threshold,
                backup_retention=backup_retention,
                dry_run=False,
                skipped=True,
                skip_reason=reason,
                backup_dir=backup_dir,
            )

        log.info("清理前备份原始 Cookies 到 backups 目录...")
        _guard_not_running(scan, log, "备份前")
        backup_path, backup_journal = create_backup(paths, backup_dir, log)
        backups_deleted, backup_count = prune_backups(
            backup_dir, backup_retention, log
        )

        log.info("在临时数据库中删除旧记录...")
        before, after, deleted = clean_database(work_db, keep)
        if deleted != planned_delete or after != stats.planned_after:
            raise CookieCleanerError(
                "清理结果与计划不一致，已放弃写回，原 Cookies 未被替换。"
                f"（计划删除 {planned_delete} / 实际 {deleted}）"
            )
        log.info(f"临时数据库清理完成：{before} -> {after} 条，删除 {deleted} 条。")

        # 写回前最后一次确认 DSH 未运行。
        _guard_not_running(scan, log, "写回前")

        log.info("写回 Cookies（原子替换）...")
        journal_removed = _write_back(work_db, paths, log)

        log.info(
            f"清理成功：清理前 {before} 条，删除 {deleted} 条，"
            f"清理后 {after} 条；备份：{backup_path}"
        )
        return CleanupResult(
            cookies_path=paths.cookies_path,
            before=before,
            after=after,
            deleted=deleted,
            planned_delete=planned_delete,
            keep=keep,
            threshold=threshold,
            backup_retention=backup_retention,
            dry_run=False,
            backup_path=backup_path,
            backup_journal_path=backup_journal,
            backup_dir=backup_dir,
            backup_count=backup_count,
            backups_deleted=backups_deleted,
            journal_removed=journal_removed,
        )
    finally:
        if workdir is not None:
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 单实例互斥锁
# ---------------------------------------------------------------------------


_MUTEX_HANDLE: int | None = None


def acquire_single_instance(name: str = SINGLE_INSTANCE_MUTEX) -> bool:
    """创建命名互斥锁；已有实例时返回 False，不等待、不杀进程。"""

    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    if _MUTEX_HANDLE:
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        raise CookieCleanerError(
            f"无法创建单实例互斥锁（Windows 错误码 {error}），"
            "请重启电脑后再试。"
        )
    if error == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False

    _MUTEX_HANDLE = int(handle)
    return True


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是一个整数") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("必须大于等于 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="DSHCookieCleaner",
        description=f"{APP_TITLE} v{VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  DSHCookieCleaner.exe\n"
            "  DSHCookieCleaner.exe --setup\n"
            "  DSHCookieCleaner.exe --dry-run\n"
            "  DSHCookieCleaner.exe --silent\n"
            "  DSHCookieCleaner.exe --cookies-dir \"D:\\Temp\\Network\" --dry-run\n"
            "\n"
            "首次运行会弹出设置窗口；配置保存在程序目录的 config.json。\n"
            "程序目录、log、backups 均相对于 exe 自身，移动 exe 后会自动跟随。\n"
        ),
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="重新打开首次设置窗口（会覆盖现有 config.json）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只执行一次清理后退出（默认常驻系统托盘）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计将删除多少条，不备份、不删除、不写回（一次运行）",
    )
    parser.add_argument(
        "--cookies-dir",
        metavar="DIR",
        help="覆盖 DSH 的 Network 目录（目录内应有 Cookies 文件）",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="静默运行：不弹结果窗口（开机自启使用）",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="结束后不等待回车（便于自动化/测试）",
    )
    parser.add_argument(
        "--no-popup",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def _configure_console() -> None:
    # --noconsole 打包时 sys.stdout/stderr 可能为 None；给日志和控制台输出一个
    # 安全的空设备，避免 logging.StreamHandler 初始化时报错。
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _pause() -> None:
    try:
        input("\n按回车键退出...")
    except (EOFError, KeyboardInterrupt):
        pass


def format_result(
    result: CleanupResult,
    config: AppConfig,
    log_path: Path | None = None,
) -> str:
    """生成给 GUI 弹窗或控制台使用的中文结果摘要。"""

    lines: list[str] = ["", "=" * 68]
    if result.skipped:
        lines.append("【跳过清理】")
        lines.append(f"Cookies：{result.cookies_path}")
        if result.skip_reason:
            lines.append(result.skip_reason)
        lines.append("未复制、未备份、未修改原始 Cookies。")
    elif result.dry_run:
        lines.append("【演练结果】未修改任何 Cookie")
        lines.append(f"Cookies：{result.cookies_path}")
        lines.append(
            f"当前匹配 dsh-auth-*：{result.before} 条"
            f"（阈值 {result.threshold}）"
        )
        lines.append(f"预计删除：{result.planned_delete} 条")
        lines.append(
            f"预计保留：{max(0, result.before - result.planned_delete)} 条"
            f"（保留上限 {result.keep}）"
        )
        lines.append("未复制临时库，未创建备份，未写入数据库。")
    else:
        lines.append("【清理完成】")
        lines.append(f"Cookies：{result.cookies_path}")
        lines.append(
            f"清理前匹配 dsh-auth-*：{result.before} 条"
            f"（阈值 {result.threshold}）"
        )
        lines.append(f"删除：{result.deleted} 条")
        lines.append(
            f"清理后匹配 dsh-auth-*：{result.after} 条"
            f"（保留上限 {result.keep}）"
        )
        if result.backup_path is not None:
            lines.append(f"备份：{result.backup_path}")
        if result.backup_journal_path is not None:
            lines.append(f"Journal 备份：{result.backup_journal_path}")
        if result.journal_removed:
            lines.append("Cookies-journal 已清理。")
        lines.append(
            f"旧备份轮换：删除 {result.backups_deleted} 个文件，"
            f"当前保留 {result.backup_count} 组"
            f"（上限 {result.backup_retention} 组）。"
        )

    actual_log_path = log_path or (config.log_dir / LOG_FILENAME)
    lines.append(f"程序目录：{app_dir()}")
    lines.append(f"配置文件：{config_path()}")
    lines.append(f"备份目录：{config.backup_dir}")
    lines.append(f"日志文件：{actual_log_path}")
    lines.append("=" * 68)
    return "\n".join(lines)


def result_popup_text(result: CleanupResult) -> str:
    """给普通用户看的简短结果，和日志里的详细结果分开。"""

    if result.skipped:
        if result.skip_reason and "未达到清理阈值" in result.skip_reason:
            return (
                f"检查完毕：当前 {result.before} 条，"
                f"未达到阈值 {result.threshold} 条，无需清理。"
            )
        return (
            f"检查完毕：当前 {result.before} 条，"
            f"未超过保留上限 {result.keep} 条，无需清理。"
        )
    if result.dry_run:
        return (
            f"演练完成：当前 {result.before} 条，"
            f"达到阈值 {result.threshold} 条，预计删除 "
            f"{result.planned_delete} 条。演练模式未修改任何数据。"
        )
    return f"清理成功！已删除 {result.deleted} 条旧记录，并保存备份。"


def _print_result(
    result: CleanupResult,
    config: AppConfig,
    log_path: Path | None = None,
) -> None:
    _safe_print(format_result(result, config, log_path))


def _show_result(
    title: str,
    text: str,
    normal_popup: bool,
    error: bool = False,
) -> None:
    if normal_popup and not error:
        show_info(title, text)
    elif normal_popup and error:
        show_error(title, text)
    else:
        _safe_print(f"{title}\n\n{text}")


def _show_fatal(title: str, text: str, normal_popup: bool) -> None:
    _show_result(title, f"错误：{text}", normal_popup, error=True)


# ---------------------------------------------------------------------------
# 托盘常驻、全屏检测与更新检查
# ---------------------------------------------------------------------------


def _bundle_path(relative: str) -> Path:
    """定位打包后的资源；源码运行时用程序目录。"""

    root = Path(getattr(sys, "_MEIPASS", app_dir()))
    return root / relative


def _load_tray_image():
    """加载托盘图标；没有自定义图标时画一个深蓝底色的占位图。"""

    if Image is None:
        return None
    candidates = (
        _bundle_path("assets/app.ico"),
        app_dir() / "assets" / "app.ico",
        app_dir() / "app.ico",
        _bundle_path("app.ico"),
    )
    for path in candidates:
        try:
            if path.is_file():
                image = Image.open(path)
                image.load()
                return image.convert("RGBA").resize((64, 64))
        except (OSError, ValueError):
            continue

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=14, fill="#1E3A8A")
    # 没有字体依赖，画一个简化的白色 DSH 占位块。
    draw.rectangle((14, 24, 22, 42), fill="#FFFFFF")
    draw.rectangle((25, 24, 34, 42), fill="#FFFFFF")
    draw.rectangle((37, 24, 48, 42), fill="#FFFFFF")
    return image


def _tray_notify(icon: object, message: str, title: str = APP_TITLE) -> None:
    """托盘气泡失败时静默忽略，绝不因通知失败影响主流程。"""

    notify = getattr(icon, "notify", None)
    if notify is None:
        return
    try:
        notify(message, title)
    except BaseException:
        pass


def notify_already_running() -> bool:
    """第二个实例在右下角弹一个临时气泡；失败时返回 False。"""

    if pystray is None:
        return False
    image = _load_tray_image()
    if image is None:
        return False
    icon = None
    try:
        icon = pystray.Icon(
            "DSHCookieCleaner-Notify",
            image,
            APP_TITLE,
            menu=pystray.Menu(),
        )
        icon.run_detached()
        _tray_notify(icon, "程序已在后台运行")
        threading.Event().wait(1.8)
        return True
    except BaseException:
        return False
    finally:
        if icon is not None:
            try:
                icon.stop()
            except BaseException:
                pass


def is_fullscreen_foreground() -> bool:
    """粗略判断前台窗口是否全屏（游戏/视频），失败时一律返回 False。"""

    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        hwnd = user32.GetForegroundWindow()
        if not hwnd or not user32.IsWindowVisible(hwnd):
            return False

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(
            hwnd, ctypes.byref(pid)
        )
        if pid.value == os.getpid():
            return False

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return False

        screen_w = int(user32.GetSystemMetrics(0))
        screen_h = int(user32.GetSystemMetrics(1))
        tolerance = 8
        return (
            width >= screen_w - tolerance
            and height >= screen_h - tolerance
            and int(rect.left) <= tolerance
            and int(rect.top) <= tolerance
        )
    except (OSError, ValueError, AttributeError):
        return False


def _version_key(value: str) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", value or "")]
    return tuple(parts) if parts else (0,)


def check_for_update(
    url: str = UPDATE_API_URL,
    current_version: str = CURRENT_VERSION,
    timeout: float = 5.0,
) -> tuple[bool, str, str]:
    """请求 GitHub Release API，返回 (是否有新版, 最新标签, 发布页 URL)。

    网络/超时/API 错误统一抛出 CookieCleanerError，方便托盘层提示。
    """

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"DSHCookieCleaner/{current_version}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise CookieCleanerError("网络连接失败，请稍后再试") from exc

    if not isinstance(payload, dict):
        raise CookieCleanerError("网络连接失败，请稍后再试")
    tag = str(payload.get("tag_name") or "").strip()
    page = str(payload.get("html_url") or "").strip()
    if not tag:
        raise CookieCleanerError("网络连接失败，请稍后再试")
    if not page:
        page = f"https://github.com/{GITHUB_REPO}/releases"
    return _version_key(tag) > _version_key(current_version), tag, page


class TrayApp:
    """pystray 常驻托盘应用；所有定时工作都在 daemon 线程中。"""

    def __init__(self, config: AppConfig, args: argparse.Namespace) -> None:
        if pystray is None or Image is None:
            raise ConfigError(
                "当前环境缺少 pystray 或 Pillow，无法启动系统托盘。"
                "请运行：python -m pip install -r requirements.txt"
            )
        self.config = config
        self.args = args
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._check_lock = threading.Lock()
        self._settings_lock = threading.Lock()
        self._logger: Logger | None = None
        self._icon: object | None = None
        self._worker: threading.Thread | None = None

    def _make_logger(self, config: AppConfig | None = None) -> Logger:
        cfg = config or self.config
        return Logger(
            cfg.log_dir / LOG_FILENAME,
            echo=False,
            max_bytes=cfg.log_max_mb * 1024 * 1024,
            backup_count=cfg.log_backup_count,
        )

    def _notify(self, message: str) -> None:
        _tray_notify(self._icon, message)

    def _on_check_now(self, _icon: object, _item: object) -> None:
        threading.Thread(target=self._manual_check, daemon=True).start()

    def _manual_check(self) -> None:
        status, message = self._run_check(manual=True)
        self._notify(message)
        if self._logger is not None and status == "ok":
            self._logger.info(f"手动检查结果：{message}")

    def _on_settings(self, _icon: object, _item: object) -> None:
        if not self._settings_lock.acquire(blocking=False):
            self._notify("设置窗口已打开")
            return
        threading.Thread(target=self._settings_worker, daemon=True).start()

    def _settings_worker(self) -> None:
        old = self.config
        try:
            with self._check_lock:
                if self._logger is not None:
                    self._logger.info("打开设置窗口，暂停后台检查。")
                    self._logger.close()
                    self._logger = None
                new_config: AppConfig | None = None
                try:
                    new_config = open_setup_dialog(old, config_path())
                except BaseException as exc:
                    self._logger = self._make_logger(old)
                    self._logger.error(f"打开设置窗口失败：{exc}")
                    self._notify("打开设置失败，请查看日志。")
                    return

                if new_config is None:
                    self._logger = self._make_logger(old)
                    self._logger.info("用户取消设置。")
                    return

                self.config = new_config
                ensure_config_dirs(new_config)
                self._logger = self._make_logger(new_config)
                self._logger.info(
                    f"设置已保存：check_interval_hours="
                    f"{new_config.check_interval_hours}"
                )
                _sync_autostart(new_config, self._logger)
                self._wake.set()
                self._notify("设置已保存，后台检查将使用新配置。")
        except BaseException as exc:
            try:
                if self._logger is None:
                    self._logger = self._make_logger(old)
                self._logger.error(f"设置流程异常：{exc}")
            except BaseException:
                pass
            self._notify("设置保存失败，请查看日志。")
        finally:
            self._settings_lock.release()

    def _on_check_update(self, _icon: object, _item: object) -> None:
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self) -> None:
        try:
            has_new, tag, page = check_for_update()
        except CookieCleanerError:
            self._notify("网络连接失败，请稍后再试")
            self._show_update_message("检测更新", "网络连接失败，请稍后再试")
            return
        except BaseException as exc:
            if self._logger is not None:
                self._logger.error(f"检查更新异常：{exc}")
            self._notify("网络连接失败，请稍后再试")
            return

        if has_new:
            try:
                webbrowser.open(page)
            except BaseException:
                pass
            message = f"发现新版本 {tag}，已打开下载页面。"
            self._notify(message)
            self._show_update_message("发现新版本", message)
        else:
            message = f"当前已是最新版本 {CURRENT_VERSION}。"
            self._notify(message)
            self._show_update_message("检测更新", message)

    def _show_update_message(self, title: str, message: str) -> None:
        # 用系统原生消息框，避免在托盘后台线程创建 Tk 窗口。
        try:
            show_native_info(title, message)
        except BaseException:
            pass

    def _on_quit(self, _icon: object, _item: object) -> None:
        self._request_stop()

    def _request_stop(self) -> None:
        try:
            if self._logger is not None:
                self._logger.info("用户请求彻底退出程序。")
        except BaseException:
            pass
        self._stop.set()
        self._wake.set()
        try:
            if self._icon is not None:
                self._icon.stop()
        except BaseException:
            pass

    def _sleep_or_stop(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise CookieCleanerError("程序正在退出，已中止本次清理。")

    def _run_check(self, manual: bool = False) -> tuple[str, str]:
        if not self._check_lock.acquire(blocking=False):
            return "busy", "正在检查中，请稍候。"
        try:
            if self._stop.is_set():
                return "stopped", "程序正在退出。"
            log = self._logger
            if log is None:
                return "error", "日志尚未初始化。"
            log.info("后台检查开始。")

            try:
                processes = find_dsh_processes()
            except BaseException as exc:
                log.error(f"检测 DSH 进程失败：{exc}")
                return "error", "检测 DSH 进程失败，请查看日志。"

            if processes:
                message = "请先从右下角托盘彻底退出 DSH 后再试。"
                log.info(f"DSH 正在运行（{len(processes)} 个进程），"
                         "跳过本次检查。")
                return "dsh", message

            if is_fullscreen_foreground():
                log.info("检测到全屏前台窗口（可能是游戏），推迟本次检查。")
                return "fullscreen", "当前处于全屏状态，已推迟本次检查。"

            try:
                paths = resolve_paths(self.args.cookies_dir)
                stats = preview_database(
                    paths.cookies_path,
                    self.config.keep,
                    self.config.threshold,
                )
            except CookieCleanerError as exc:
                log.error(str(exc))
                return "error", f"检查失败：{exc}"
            except BaseException as exc:
                log.error(f"检查 Cookies 失败：{exc}")
                return "error", "检查 Cookies 失败，请查看日志。"

            if not stats.threshold_reached:
                message = (
                    f"检查完毕：当前 {stats.count} 条，"
                    f"未达到阈值 {self.config.threshold} 条，无需清理。"
                )
                log.info(message)
                return "skip", message
            if stats.count <= self.config.keep:
                message = (
                    f"检查完毕：当前 {stats.count} 条，"
                    f"未超过保留上限 {self.config.keep} 条，无需清理。"
                )
                log.info(message)
                return "skip", message

            log.info(f"达到清理阈值，开始清理（保留 {self.config.keep} 条）。")
            try:
                result = cleanup(
                    paths,
                    keep=self.config.keep,
                    logger=log,
                    scanner=find_dsh_processes,
                    sleep_func=self._sleep_or_stop,
                    threshold=self.config.threshold,
                    backup_dir=self.config.backup_dir,
                    backup_retention=self.config.backup_retention,
                )
            except DshRunningError:
                message = "请先从右下角托盘彻底退出 DSH 后再试。"
                log.warning(message)
                return "dsh", message
            except CookieCleanerError as exc:
                log.error(f"清理失败：{exc}")
                return "error", f"清理失败：{exc}"
            except BaseException as exc:
                log.error(f"后台清理异常：{exc}")
                return "error", "清理失败，请查看日志。"

            message = result_popup_text(result)
            log.info(f"后台清理结果：{message}")
            return "ok", message
        finally:
            self._check_lock.release()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            interval = max(60, int(self.config.check_interval_hours) * 3600)
            if self._wake.wait(interval):
                self._wake.clear()
                continue
            if self._stop.is_set():
                break
            try:
                self._run_check(manual=False)
            except BaseException as exc:
                if self._logger is not None:
                    self._logger.error(f"后台定时检查异常：{exc}")

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem("立即检查", self._on_check_now),
            pystray.MenuItem("设置", self._on_settings, default=True),
            pystray.MenuItem("检测更新", self._on_check_update),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("⚠️ 彻底退出程序", self._on_quit),
        )

    def run(self) -> int:
        self._logger = self._make_logger()
        try:
            self._logger.info("=" * 68)
            self._logger.info(f"{APP_TITLE} v{VERSION} 托盘模式启动")
            self._logger.info(f"程序目录：{app_dir()}")
            self._logger.info(f"配置文件：{config_path()}")
            self._logger.info(f"备份目录：{self.config.backup_dir}")
            self._logger.info(
                f"后台检查间隔：{self.config.check_interval_hours} 小时；"
                f"阈值 {self.config.threshold} 条；保留 {self.config.keep} 条"
            )
            _sync_autostart(self.config, self._logger)
            image = _load_tray_image()
            if image is None:
                raise ConfigError("无法加载托盘图标。")
            self._icon = pystray.Icon(
                "DSHCookieCleaner",
                image,
                APP_TITLE,
                menu=self._build_menu(),
            )
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="DSHCookieCleanerWorker",
                daemon=True,
            )
            self._worker.start()
        except BaseException:
            self._stop.set()
            self._wake.set()
            if self._logger is not None:
                self._logger.close()
                self._logger = None
            raise

        try:
            self._icon.run()
        except BaseException as exc:
            if self._logger is not None:
                self._logger.error(f"托盘主循环异常：{exc}")
        finally:
            self._stop.set()
            self._wake.set()
            try:
                self._icon.stop()
            except BaseException:
                pass
            if self._worker is not None:
                self._worker.join(timeout=3)
            if self._logger is not None:
                self._logger.info("托盘程序退出。")
                self._logger.close()
                self._logger = None
        return 0


def run_tray(config: AppConfig, args: argparse.Namespace) -> int:
    """启动托盘；最外层兜底，关机/强制结束时不弹异常框。"""

    app: TrayApp | None = None
    try:
        app = TrayApp(config, args)
        return app.run()
    except (ConfigError, OSError) as exc:
        _safe_print(f"托盘启动失败：{exc}")
        return 3
    except BaseException:
        # 强制关机、任务栏结束进程等场景：不弹窗，安静退出。
        return 0
    finally:
        if app is not None:
            app._request_stop()


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    normal_popup = not args.no_popup and not args.silent
    once = bool(args.once or args.dry_run)

    try:
        if not acquire_single_instance():
            if not notify_already_running() and normal_popup:
                show_info("程序已在后台运行", "程序已在后台运行。")
            _safe_print("程序已在后台运行，本次启动已安静退出。")
            return 0
    except CookieCleanerError as exc:
        _show_fatal("无法启动", str(exc), normal_popup)
        return 3

    cfg_path = config_path()
    config: AppConfig | None = None
    need_setup = args.setup or not cfg_path.exists()

    if need_setup and args.silent and not args.setup:
        _safe_print("配置文件不存在，静默模式退出。")
        return 0

    if need_setup:
        initial: AppConfig | None = None
        if cfg_path.exists():
            try:
                initial = load_config(cfg_path)
            except ConfigError:
                initial = None
        try:
            config = open_setup_dialog(initial, cfg_path)
        except ConfigError as exc:
            _show_fatal("无法打开设置窗口", str(exc), normal_popup)
            return 3
        if config is None:
            _safe_print("已取消首次设置，程序退出。")
            return 0
    else:
        try:
            config = load_config(cfg_path)
        except ConfigError as exc:
            if args.silent:
                _show_fatal("配置读取失败", str(exc), False)
                return 3
            _show_result(
                "配置读取失败",
                f"{exc}\n\n将重新打开设置窗口，请检查后保存。",
                normal_popup,
                error=True,
            )
            try:
                config = open_setup_dialog(None, cfg_path)
            except ConfigError as setup_exc:
                _show_fatal("无法打开设置窗口", str(setup_exc), normal_popup)
                return 3
            if config is None:
                _safe_print("已取消设置，程序退出。")
                return 0

    try:
        ensure_config_dirs(config)
    except ConfigError as exc:
        _show_fatal("无法创建目录", str(exc), normal_popup)
        return 3

    # 默认进入系统托盘；--once / --dry-run 保留一次性命令行行为。
    if not once:
        return run_tray(config, args)

    try:
        log = Logger(
            config.log_dir / LOG_FILENAME,
            echo=not args.no_popup and not args.silent,
            max_bytes=config.log_max_mb * 1024 * 1024,
            backup_count=config.log_backup_count,
        )
    except (ConfigError, OSError, ValueError) as exc:
        _show_fatal("无法初始化日志", str(exc), normal_popup)
        return 3

    exit_code = 1
    try:
        log.info("=" * 68)
        log.info(f"{APP_TITLE} v{VERSION} 单次运行")
        log.info(f"程序目录：{app_dir()}")
        log.info(f"配置文件：{cfg_path}")
        log.info(f"日志文件：{log.path}")
        log.info(f"备份目录：{config.backup_dir}")
        log.info(
            f"配置：threshold={config.threshold}, keep={config.keep}, "
            f"backup_retention={config.backup_retention}, "
            f"check_interval_hours={config.check_interval_hours}, "
            f"log_max_mb={config.log_max_mb}, "
            f"log_backup_count={config.log_backup_count}, "
            f"autostart={config.autostart}"
        )
        log.info(
            f"参数：dry_run={args.dry_run}, silent={args.silent}, "
            f"cookies_dir={args.cookies_dir!r}"
        )
        _sync_autostart(config, log)

        paths = resolve_paths(args.cookies_dir)
        log.info(f"路径来源：{paths.source}")
        log.info(f"Network 目录：{paths.network_dir}")
        log.info(f"Cookies：{paths.cookies_path}")
        log.info(f"Journal：{paths.journal_path}")
        if paths.warning:
            log.warning(paths.warning)

        result = cleanup(
            paths,
            keep=config.keep,
            dry_run=args.dry_run,
            logger=log,
            scanner=find_dsh_processes,
            threshold=config.threshold,
            backup_dir=config.backup_dir,
            backup_retention=config.backup_retention,
        )
        text = result_popup_text(result)
        log.info(f"结果提示：{text}")
        _show_result("清理结果", text, normal_popup)
        exit_code = 0
    except DshRunningError as exc:
        log.error(str(exc))
        _show_result(
            "请先退出 DSH",
            f"请先从右下角托盘彻底退出 DSH 后再试。\n\n{exc}\n\n日志：{log.path}",
            normal_popup,
            error=True,
        )
        exit_code = 2
    except CookieCleanerError as exc:
        log.error(str(exc))
        _show_result(
            "清理失败",
            f"错误：{exc}\n\n日志：{log.path}",
            normal_popup,
            error=True,
        )
        exit_code = 3
    except KeyboardInterrupt:
        log.warning("用户中断，已停止。")
        _safe_print("已中断。")
        exit_code = 130
    except Exception as exc:  # 最后兜底，确保错误写入日志而不是静默退出
        log.error(f"未预期错误：{type(exc).__name__}: {exc}")
        _show_result(
            "程序错误",
            f"未预期错误：{type(exc).__name__}: {exc}\n\n日志：{log.path}",
            normal_popup,
            error=True,
        )
        exit_code = 1
    finally:
        log.info(f"程序退出，退出码 {exit_code}。")
        log.info("=" * 68)
        log.close()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
