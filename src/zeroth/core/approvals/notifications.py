"""Legacy import path for :mod:`zeroth.governance.approvals.notifications`."""

from zeroth.governance.approvals.notifications import (
    ApprovalNotification,
    CompositeNotifier,
    EmailNotifier,
    Notifier,
    SlackNotifier,
    build_approval_notifier,
)

__all__ = [
    "ApprovalNotification",
    "CompositeNotifier",
    "EmailNotifier",
    "Notifier",
    "SlackNotifier",
    "build_approval_notifier",
]
