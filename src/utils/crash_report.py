"""Send an email report when the live bot crashes or shuts down."""
from __future__ import annotations

import os
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo


def send_crash_report(reason: str, traceback_str: str | None = None) -> None:
    """Email a crash/shutdown report to ALERT_EMAIL.

    Silently skips if GMAIL_APP_PASSWORD is not set in the environment.
    Never raises — all errors go to stderr so they don't mask the original crash.
    """
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not app_password:
        return

    to_addr = os.environ.get("ALERT_EMAIL", "johnwjustin196@gmail.com").strip()
    from_addr = "johnwjustin196@gmail.com"

    try:
        et = ZoneInfo("America/New_York")
        now_str = datetime.now(et).strftime("%Y-%m-%d %H:%M:%S ET")
        is_crash = "CRASH" in reason.upper() or traceback_str is not None
        subject = f"[Trading Bot] {'CRASH' if is_crash else 'Shutdown'} — {now_str}"

        log_tail = _read_log_tail(60)

        lines = [
            f"Time:   {now_str}",
            f"Reason: {reason}",
        ]
        if traceback_str:
            lines += ["", "--- Traceback ---", traceback_str.rstrip()]
        if log_tail:
            lines += ["", "--- Last 60 log lines ---", log_tail]

        body = "\n".join(lines)
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_addr

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.login(from_addr, app_password)
            server.sendmail(from_addr, [to_addr], msg.as_string())

    except Exception as exc:
        print(f"[crash_report] failed to send email: {exc}", file=sys.stderr)


def _read_log_tail(n: int) -> str:
    log_file = os.environ.get("BOT_LOG_FILE", "logs/bot.log")
    try:
        path = Path(log_file)
        if not path.is_absolute():
            # resolve relative to project root (two levels up from this file)
            path = Path(__file__).resolve().parent.parent.parent / log_file
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""
