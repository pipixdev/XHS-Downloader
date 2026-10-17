#!/bin/zsh

PROJECT="${0:A:h:h}"
[[ -f "$PROJECT/automation/local.env" ]] && source "$PROJECT/automation/local.env"
TERMINAL_TTY="$(tty)"
cd "$PROJECT" || exit 1

echo "正在运行小红书自动任务……"
echo
"$PROJECT/.venv/bin/python" "$PROJECT/automation/xhs_daily.py"
exit_code=$?

echo
if [[ $exit_code -eq 0 ]]; then
    echo "任务已完成。"
else
    echo "任务失败，退出码：$exit_code"
    echo "按回车关闭窗口……"
    read -r
fi

# Terminal can be configured to keep a window open after its shell exits.
# Close the exact tab/window that launched this command after the shell ends.
(
    osascript - "$TERMINAL_TTY" <<'APPLESCRIPT'
on run argv
    set targetTTY to item 1 of argv
    delay 0.5
    tell application "Terminal"
        repeat with terminalWindow in windows
            repeat with terminalTab in tabs of terminalWindow
                if tty of terminalTab is targetTTY then
                    if (count of tabs of terminalWindow) is 1 then
                        close terminalWindow
                    else
                        close terminalTab
                    end if
                    return
                end if
            end repeat
        end repeat
    end tell
end run
APPLESCRIPT
) >/dev/null 2>&1 &!

exit $exit_code
