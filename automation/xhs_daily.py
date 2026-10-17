#!/usr/bin/env python3
"""Use the regular macOS Edge session to download liked RedNote notes."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "bin" / "python"
MAIN = ROOT / "main.py"
SETTINGS = ROOT / "Volume" / "settings.json"
PROFILE_ID = os.environ.get("XHS_PROFILE_ID", "").strip()
DEFAULT_URL = f"https://www.rednote.com/user/profile/{PROFILE_ID}?tab=liked"
TIMEOUT = 180.0
LOGIN_PROBE_TIMEOUT = 15.0
BATCH_SIZE = 30
SCROLL_WAIT = 3.0
ACTION_DOWNLOAD = "1"
ACTION_COOKIE_ONLY = "2"

OPEN_TAB_SCRIPT = r'''
on run argv
    set targetUrl to item 1 of argv
    tell application "Microsoft Edge"
        activate
        if (count of windows) is 0 then make new window
        tell front window
            set newTab to make new tab at end of tabs with properties {URL:targetUrl}
            set active tab index to count of tabs
            return id of newTab as text
        end tell
    end tell
end run
'''

EXECUTE_SCRIPT = r'''
on run argv
    set wantedId to item 1 of argv
    set jsCode to item 2 of argv
    tell application "Microsoft Edge"
        repeat with browserWindow in windows
            repeat with browserTab in tabs of browserWindow
                if (id of browserTab as text) is wantedId then
                    set jsResult to execute browserTab javascript jsCode
                    if jsResult is missing value then return ""
                    return jsResult as text
                end if
            end repeat
        end repeat
    end tell
    error "找不到自动任务打开的 Edge 标签页"
end run
'''

NAVIGATE_SCRIPT = r'''
on run argv
    set wantedId to item 1 of argv
    set targetUrl to item 2 of argv
    tell application "Microsoft Edge"
        repeat with browserWindow in windows
            repeat with browserTab in tabs of browserWindow
                if (id of browserTab as text) is wantedId then
                    set URL of browserTab to targetUrl
                    activate
                    return
                end if
            end repeat
        end repeat
    end tell
    error "找不到自动任务打开的 Edge 标签页"
end run
'''

CLOSE_TAB_SCRIPT = r'''
on run argv
    set wantedId to item 1 of argv
    tell application "Microsoft Edge"
        repeat with browserWindow in windows
            repeat with browserTab in tabs of browserWindow
                if (id of browserTab as text) is wantedId then
                    close browserTab
                    return
                end if
            end repeat
        end repeat
    end tell
end run
'''

PAGE_DATA_SCRIPT = r'''
JSON.stringify((() => {
    let state = window.__INITIAL_STATE__;
    if (!state?.user?.notes) {
        const stateScript = [...document.scripts]
            .find(script => script.textContent.startsWith("window.__INITIAL_STATE__="));
        if (stateScript) {
            const source = stateScript.textContent;
            const expression = source.slice(source.indexOf("=") + 1)
                .replace(/;\s*$/, "");
            state = Function("return (" + expression + ")")();
        }
    }
    const notes = state?.user?.notes;
    const raw = notes?._rawValue?.[2] ?? notes?.[2];
    const initialItems = Array.isArray(raw) ? raw
        .filter(item => item?.id && item?.xsecToken)
        .map(item => ({id: item.id, token: item.xsecToken})) : [];
    const userInfo = state?.user?.userInfo?._rawValue ?? state?.user?.userInfo;
    const itemMap = new Map(initialItems.map(item => [item.id, item]));
    document.querySelectorAll("section.note-item a.cover[href*='xsec_token']")
        .forEach(anchor => {
            const link = new URL(anchor.href, location.origin);
            const id = link.pathname.split("/").filter(Boolean).at(-1);
            const token = link.searchParams.get("xsec_token");
            if (id && token) itemMap.set(id, {id, token});
        });
    return {
        url: location.href,
        cookie: document.cookie,
        loggedIn: Boolean(state?.user?.loggedIn),
        // Logged-out visitors get an auto-issued guest session that still
        // reports loggedIn, so the guest flag is the real login signal.
        guest: userInfo?.guest === true,
        stateReady: Array.isArray(raw),
        items: [...itemMap.values()],
    };
})())
'''


class LoginRequiredError(RuntimeError):
    """The regular Edge session needs an interactive RedNote login."""


class EdgeJavaScriptDisabledError(RuntimeError):
    """Edge has disabled JavaScript execution through Apple Events."""


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler()],
    )


def choose_action() -> str:
    print("请选择要执行的操作：")
    print("1. 更新 Cookie 并下载点赞作品（可自定义滚动次数）")
    print("2. 仅更新 Cookie")
    while True:
        action = input("请输入 1 或 2：").strip()
        if action in {ACTION_DOWNLOAD, ACTION_COOKIE_ONLY}:
            return action
        print("输入无效，请输入 1 或 2。")


def choose_scroll_count() -> int:
    while True:
        value = input("请输入滚动页面次数（直接回车默认 0）：").strip()
        if not value:
            return 0
        try:
            count = int(value)
        except ValueError:
            count = -1
        if count >= 0:
            return count
        print("输入无效，请输入大于或等于 0 的整数。")


def run_osascript(script: str, *arguments: str) -> str:
    result = subprocess.run(
        ["osascript", "-", *arguments],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    message = result.stderr.strip() or "Edge 原生自动化执行失败"
    if "Executing JavaScript through AppleScript is turned off" in message:
        raise EdgeJavaScriptDisabledError(message)
    raise RuntimeError(message)


def open_edge_tab(url: str) -> str:
    return run_osascript(OPEN_TAB_SCRIPT, url)


def navigate_edge_tab(tab_id: str, url: str) -> None:
    run_osascript(NAVIGATE_SCRIPT, tab_id, url)


def close_edge_tab(tab_id: str) -> None:
    run_osascript(CLOSE_TAB_SCRIPT, tab_id)


def execute_edge_javascript(tab_id: str, expression: str) -> str:
    return run_osascript(EXECUTE_SCRIPT, tab_id, expression)


def has_login_state(page_data: dict) -> bool:
    cookie_header = page_data.get("cookie", "")
    url = page_data.get("url", "")
    if "rednote.com" in url:
        return (
            bool(page_data.get("loggedIn"))
            and not page_data.get("guest")
            and "a1=" in cookie_header
        )
    return all(marker in cookie_header for marker in ("web_session=", "a1="))


def wait_for_edge_data(
    tab_id: str,
    include_links: bool,
    timeout: float,
) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        try:
            output = execute_edge_javascript(tab_id, PAGE_DATA_SCRIPT)
            latest = json.loads(output) if output else {}
        except json.JSONDecodeError:
            latest = {}
        except RuntimeError as error:
            if "JavaScript execution failed" not in str(error):
                raise
            latest = {}

        if include_links and latest.get("items"):
            return latest
        if not include_links and has_login_state(latest):
            return latest
        time.sleep(1)

    if not has_login_state(latest):
        raise LoginRequiredError("日常 Edge 中没有检测到有效的 RedNote 登录态")
    if include_links and latest.get("stateReady"):
        raise RuntimeError("点赞列表为空，或当前账号没有可读取的首屏点赞作品")
    raise RuntimeError("未检测到点赞数据：RedNote 页面结构可能已经变化")


def collect_scrolled_items(tab_id: str, page_data: dict, scroll_count: int) -> dict:
    collected = {item["id"]: item for item in page_data.get("items", [])}
    latest = page_data
    for index in range(scroll_count):
        execute_edge_javascript(
            tab_id,
            'window.scrollBy({top:Math.max(500, innerHeight * 0.8), '
            'behavior:"smooth"}); "ok"',
        )
        time.sleep(SCROLL_WAIT)
        output = execute_edge_javascript(tab_id, PAGE_DATA_SCRIPT)
        latest = json.loads(output) if output else latest
        for item in latest.get("items", []):
            collected[item["id"]] = item
        logging.info(
            "已完成第 %s/%s 次滚动，当前累计 %s 条作品",
            index + 1,
            scroll_count,
            len(collected),
        )
    latest["items"] = list(collected.values())
    return latest


def build_note_links(page_data: dict) -> list[str]:
    """Build legacy-domain links consumed reliably by the existing core parser."""
    links: list[str] = []
    seen: set[str] = set()
    for item in page_data.get("items", []):
        link = (
            f"https://www.xiaohongshu.com/discovery/item/{item['id']}"
            f"?source=webshare&xhsshare=pc_web&xsec_token={item['token']}"
            "&xsec_source=pc_share"
        )
        if link not in seen:
            seen.add(link)
            links.append(link)
    return links


def write_settings_cookie(page_data: dict) -> bool:
    if not has_login_state(page_data):
        return False
    cookie_header = page_data.get("cookie", "")
    data = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
    data["cookie"] = cookie_header
    SETTINGS.write_text(
        json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
    )
    return True


def download_links(links: list[str], batch_size: int) -> None:
    if not PYTHON.exists():
        raise RuntimeError(f"找不到项目虚拟环境解释器：{PYTHON}")
    for offset in range(0, len(links), batch_size):
        batch = links[offset : offset + batch_size]
        logging.info("开始下载第 %s-%s 条作品", offset + 1, offset + len(batch))
        result = subprocess.run(
            [
                str(PYTHON),
                str(MAIN),
                "--url",
                " ".join(batch),
                "--author_archive",
                "true",
                "--download_record",
                "true",
            ],
            cwd=ROOT,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"下载器退出码 {result.returncode}")


def enable_javascript_prompt() -> None:
    print(
        "请在 Edge 菜单中开启：View（视图）→ Developer（开发人员）→ "
        "Allow JavaScript from Apple Events（允许来自 Apple 事件的 JavaScript）。",
        flush=True,
    )
    input("开启后按回车继续：")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="同步 RedNote Cookie 并下载点赞作品；不传参数时交互式询问。"
    )
    parser.add_argument(
        "--action",
        choices=["download", "cookie"],
        help="download：更新 Cookie 并下载点赞作品；cookie：仅更新 Cookie",
    )
    parser.add_argument(
        "--scrolls",
        type=int,
        help="下载时滚动页面次数（默认 0，仅 download 生效）",
    )
    args = parser.parse_args()
    if args.scrolls is not None and args.scrolls < 0:
        parser.error("--scrolls 必须大于或等于 0")
    return args


def main() -> int:
    configure_logging()
    args = parse_args()
    if not PROFILE_ID:
        logging.error("请通过环境变量 XHS_PROFILE_ID 指定自己的 RedNote 用户 ID")
        return 2
    if args.action:
        action = ACTION_DOWNLOAD if args.action == "download" else ACTION_COOKIE_ONLY
    else:
        action = choose_action()
    include_links = action == ACTION_DOWNLOAD
    if not include_links:
        scroll_count = 0
    elif args.scrolls is not None:
        scroll_count = args.scrolls
    elif args.action:
        scroll_count = 0
    else:
        scroll_count = choose_scroll_count()
    tab_id = ""
    try:
        logging.info("正在通过 macOS 原生自动化打开日常 Edge")
        tab_id = open_edge_tab(DEFAULT_URL)
        while True:
            try:
                page_data = wait_for_edge_data(
                    tab_id, include_links, LOGIN_PROBE_TIMEOUT
                )
                break
            except EdgeJavaScriptDisabledError:
                enable_javascript_prompt()
            except LoginRequiredError:
                print(
                    "请在自动任务打开的日常 Edge 标签页中登录 RedNote；"
                    "登录完成并看到点赞页面后回到这里。",
                    flush=True,
                )
                input("确认已登录后按回车继续：")
                navigate_edge_tab(tab_id, DEFAULT_URL)
                page_data = wait_for_edge_data(tab_id, include_links, TIMEOUT)
                break

        if include_links:
            page_data = collect_scrolled_items(tab_id, page_data, scroll_count)

        cookie_header = page_data.get("cookie", "")
        cookie_count = len([part for part in cookie_header.split("; ") if part])
        if not write_settings_cookie(page_data):
            raise RuntimeError(
                f"从日常 Edge 读取到 {cookie_count} 个 RedNote Cookie，"
                "但未确认有效登录态"
            )
        logging.info("已从日常 Edge 同步 %s 个 RedNote Cookie", cookie_count)
        if action == ACTION_COOKIE_ONLY:
            logging.info("Cookie 更新完成")
            return 0

        links = build_note_links(page_data)
        if not links:
            raise RuntimeError("未读取到点赞作品链接")
        logging.info("共提取 %s 条点赞作品链接", len(links))
        close_edge_tab(tab_id)
        tab_id = ""
        logging.info("作品抓取完成，已关闭自动任务打开的 Edge 标签页")
        download_links(links, BATCH_SIZE)
        logging.info("点赞下载任务完成")
        return 0
    except Exception:
        task_name = "点赞下载任务" if include_links else "Cookie 更新任务"
        logging.exception("%s失败", task_name)
        return 1
    finally:
        if tab_id:
            try:
                close_edge_tab(tab_id)
            except Exception as error:
                logging.warning("未能关闭自动任务标签页：%s", error)


if __name__ == "__main__":
    sys.exit(main())
