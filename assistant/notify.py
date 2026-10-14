"""Windows toast notifications, sent through PowerShell so there is no extra
dependency. Failures are logged and swallowed: a missed pop-up must never break
a sync (the same information is in the brief and the Notion task)."""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)

# The title and message travel in environment variables, never in the script text,
# so email-derived text can't be interpreted as PowerShell.
_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($env:ASSISTANT_TOAST_TITLE)) > $null
$nodes.Item(1).AppendChild($xml.CreateTextNode($env:ASSISTANT_TOAST_BODY)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def toast(title: str, message: str) -> bool:
    if os.name != "nt":
        return False
    env = dict(os.environ, ASSISTANT_TOAST_TITLE=title[:120], ASSISTANT_TOAST_BODY=message[:220])
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-Command", _SCRIPT],
            env=env, timeout=30, check=True, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("notification failed: %s", exc)
        return False
