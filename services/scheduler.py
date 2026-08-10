"""
services/scheduler.py — APScheduler Proactive Daily Digest Service.

Manages scheduled daily digests sent via SMTP Email and/or Telegram Bot API.
Runs daily at a configurable time (default 16:00) for active learners.

Also runs a self-healing hourly vector-store reconcile job (see
run_vector_reconcile_job / services.product_service.reconcile_vector_store):
if a product's Chroma/Mesh dual-write failed at create/update time (Mesh
down, rate-limited, etc.), that product is retried automatically on the
next cycle instead of staying permanently missing from semantic search.
"""
import os
import smtplib
import logging
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from database.db import SessionLocal
from database.models import User, Event, Recommendation, Product
from services.tracking_prefs import is_agent_tracking_enabled
from services.agent import generate_and_save_recommendation
from services.product_service import reconcile_vector_store

logger = logging.getLogger("smartreco.scheduler")

scheduler: Optional[BackgroundScheduler] = None


def get_users_active_today(db: Session) -> List[User]:
    """
    Returns users who have agent_tracking_enabled=True and at least 1 event created today (UTC).
    """
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    
    users = db.query(User).all()
    active_users: List[User] = []

    for user in users:
        if not is_agent_tracking_enabled(db, user):
            continue

        has_events_today = (
            db.query(Event)
            .filter(Event.user_id == user.id, Event.created_at >= today_start)
            .first()
            is not None
        )

        if has_events_today:
            active_users.append(user)

    return active_users


def send_email_digest(to_email: str, subject: str, body_text: str, body_html: str) -> bool:
    """Sends email digest via SMTP if SMTP credentials are configured in .env."""
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    from_email = os.getenv("DIGEST_FROM_EMAIL", smtp_user or "digest@smartreco.ai")

    if not smtp_host or not smtp_user or not smtp_pass:
        logger.debug("SMTP not fully configured; skipping email digest to %s", to_email)
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_email

        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_email, [to_email], msg.as_string())

        logger.info("DIGEST SENT [email] recipient=%s subject=%r", to_email, subject)
        return True
    except Exception as exc:
        logger.error("DIGEST FAILED [email] recipient=%s: %s", to_email, exc, exc_info=True)
        return False


def send_telegram_digest(chat_id: str, message: str) -> bool:
    """Sends Telegram digest via Telegram Bot HTTP API if TELEGRAM_BOT_TOKEN is set."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.debug("TELEGRAM_BOT_TOKEN not configured; skipping Telegram digest for chat %s", chat_id)
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            logger.info("DIGEST SENT [telegram] recipient=%s", chat_id)
            return True
        else:
            logger.error(
                "DIGEST FAILED [telegram] recipient=%s status=%d: %s",
                chat_id, res.status_code, res.text,
            )
            return False
    except Exception as exc:
        logger.error("DIGEST FAILED [telegram] recipient=%s: %s", chat_id, exc, exc_info=True)
        return False


def format_digest(db: Session, user: User, rec: Recommendation) -> Tuple[str, str, str, str]:
    """
    Formats the recommendation into:
      (subject, plain_text_digest, html_digest, telegram_markdown)
    """
    import json
    payload = {}
    if rec and rec.narrative:
        try:
            payload = json.loads(rec.narrative)
        except Exception:
            pass

    main_block = payload.get("main") or {}
    narrative = main_block.get("narrative") or "Here are your recommended courses for today."
    product_ids = main_block.get("product_ids") or []

    products = db.query(Product).filter(Product.id.in_(product_ids)).all() if product_ids else []

    subject = f"SmartReco Daily Digest — Picked for {getattr(user, 'name', None) or 'you'}"

    # Plain text format
    text_lines = [
        f"Hello {getattr(user, 'name', None) or 'Learner'},",
        "",
        narrative,
        "",
        "Recommended Courses for You Today:",
    ]
    for p in products:
        text_lines.append(f"- {p.title} (Rating: {p.rating or 'N/A'}, Level: {p.level or 'All'})")
    text_lines.extend(["", "Happy Learning!", "SmartReco AI Agent"])
    plain_text = "\n".join(text_lines)

    # HTML format — branded card layout matching the SmartReco app palette
    # (indigo/violet gradient, rounded cards) instead of a bare unstyled block.
    level_badge_colors = {
        "beginner": ("#EAFBF1", "#16A34A"),
        "intermediate": ("#FEF6E7", "#B45309"),
        "advanced": ("#FBE9EA", "#DC2626"),
    }

    def _course_row(p) -> str:
        level = (p.level or "All").strip()
        bg, fg = level_badge_colors.get(level.lower(), ("#EEEBFB", "#4F46E5"))
        rating = p.rating or "N/A"
        return f"""
        <tr>
          <td style="padding:14px 16px;border:1px solid #E4E1F5;border-radius:12px;display:block;margin-bottom:10px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
                  <div style="font-size:15px;font-weight:600;color:#1E1B3C;margin-bottom:6px;">{p.title}</div>
                  <span style="display:inline-block;background:{bg};color:{fg};font-size:11px;font-weight:600;padding:3px 10px;border-radius:999px;margin-right:6px;">{level}</span>
                  <span style="display:inline-block;color:#6B6890;font-size:12px;font-weight:600;">★ {rating}</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    html_rows = "".join(_course_row(p) for p in products) if products else """
        <tr><td style="padding:14px 16px;color:#6B6890;font-size:13px;">No new recommendations today — check back tomorrow!</td></tr>"""

    display_name = getattr(user, "name", None) or "Learner"

    html_digest = f"""<!DOCTYPE html>
<html>
  <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
  <body style="margin:0;padding:0;background:#F3F1FC;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F1FC;padding:32px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#FFFFFF;border-radius:18px;overflow:hidden;box-shadow:0 8px 24px rgba(63,44,140,0.10);">
            <!-- Header -->
            <tr>
              <td style="background:linear-gradient(135deg,#4F46E5,#7C3AED);padding:28px 32px;">
                <div style="font-size:20px;font-weight:700;color:#FFFFFF;">SmartReco</div>
                <div style="font-size:13px;color:#EEEBFB;margin-top:2px;">Your Daily Learning Digest</div>
              </td>
            </tr>
            <!-- Greeting -->
            <tr>
              <td style="padding:28px 32px 8px;">
                <p style="margin:0 0 4px;font-size:15px;color:#1E1B3C;">Hi <strong>{display_name}</strong>,</p>
                <p style="margin:0;font-size:14px;color:#6B6890;line-height:1.6;">{narrative}</p>
              </td>
            </tr>
            <!-- Courses -->
            <tr>
              <td style="padding:16px 32px 8px;">
                <div style="font-size:13px;font-weight:700;color:#1E1B3C;text-transform:uppercase;letter-spacing:0.03em;margin-bottom:12px;">Recommended for you</div>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  {html_rows}
                </table>
              </td>
            </tr>
            <!-- CTA -->
            <tr>
              <td style="padding:8px 32px 32px;">
                <a href="#" style="display:inline-block;background:linear-gradient(135deg,#4F46E5,#7C3AED);color:#FFFFFF;text-decoration:none;font-size:14px;font-weight:600;padding:12px 24px;border-radius:10px;">Open SmartReco →</a>
              </td>
            </tr>
            <!-- Footer -->
            <tr>
              <td style="padding:20px 32px;background:#F3F1FC;text-align:center;">
                <p style="margin:0;font-size:12px;color:#A6A3C4;">Sent automatically by the SmartReco Proactive Agent. Happy learning! 🎓</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""

    # Telegram Markdown format
    tg_lines = [
        f"🎓 *SmartReco Daily Digest for {getattr(user, 'name', None) or 'Learner'}*",
        "",
        narrative,
        "",
        "*Recommended Courses:*",
    ]

    for p in products:
        tg_lines.append(f"• *{p.title}* ({p.rating or 'N/A'} ⭐)")
    tg_text = "\n".join(tg_lines)

    return subject, plain_text, html_digest, tg_text


def run_daily_digest_job() -> Dict[str, Any]:
    """
    Executes the proactive daily digest batch job across active users.
    Returns status summary.
    """
    logger.info("Starting scheduled daily digest job execution")
    db = SessionLocal()
    summary = {"processed_users": 0, "emails_sent": 0, "telegrams_sent": 0, "errors": []}

    try:
        active_users = get_users_active_today(db)
        logger.info("Daily digest job identified %d active users for today", len(active_users))

        for user in active_users:
            summary["processed_users"] += 1
            try:
                # Reuse latest recommendation or run LangGraph flow
                rec = (
                    db.query(Recommendation)
                    .filter(Recommendation.user_id == user.id, Recommendation.is_latest == True)  # noqa: E712
                    .first()
                )

                if not rec:
                    rec = generate_and_save_recommendation(db, user, force=True)

                if not rec:
                    logger.info("No recommendation available for user %s; skipping digest delivery", user.id)
                    continue

                subject, plain_text, html_digest, tg_text = format_digest(db, user, rec)

                # Email dispatch
                if user.email and send_email_digest(user.email, subject, plain_text, html_digest):
                    summary["emails_sent"] += 1

                # Telegram dispatch (uses TELEGRAM_CHAT_ID or user telegram id if present)
                chat_id = os.getenv("TELEGRAM_CHAT_ID")
                if chat_id and send_telegram_digest(chat_id, tg_text):
                    summary["telegrams_sent"] += 1

            except Exception as user_exc:
                logger.error("Error processing digest for user %s: %s", user.id, user_exc, exc_info=True)
                summary["errors"].append({"user_id": user.id, "error": str(user_exc)})

    finally:
        db.close()

    logger.info("Completed daily digest job: %s", summary)
    return summary


def run_vector_reconcile_job() -> Dict[str, Any]:
    """
    Self-healing background job: retries any product whose Chroma/Mesh dual-write
    previously failed (status="failed" in ChromaSyncLog). Runs hourly. See
    services.product_service.reconcile_vector_store() for the actual repair logic —
    this wrapper just owns the DB session lifecycle, matching run_daily_digest_job().
    """
    logger.info("Starting scheduled vector-store reconcile job")
    db = SessionLocal()
    try:
        summary = reconcile_vector_store(db)
    except Exception as exc:
        logger.error("Vector reconcile job raised unexpectedly: %s", exc, exc_info=True)
        summary = {"attempted": 0, "repaired": 0, "still_failed": 0, "product_not_found": 0, "error": str(exc)}
    finally:
        db.close()

    logger.info("Completed scheduled vector-store reconcile job: %s", summary)
    return summary


def start_scheduler():
    """Initializes and starts the APScheduler BackgroundScheduler."""
    global scheduler
    if scheduler and scheduler.running:
        logger.info("Scheduler already running.")
        return

    hour = int(os.getenv("DIGEST_SCHEDULE_HOUR", "16"))
    minute = int(os.getenv("DIGEST_SCHEDULE_MINUTE", "0"))

    reconcile_interval_hours = int(os.getenv("VECTOR_RECONCILE_INTERVAL_HOURS", "1"))

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_daily_digest_job,
        trigger="cron",
        hour=hour,
        minute=minute,
        id="daily_digest_job",
        replace_existing=True,
    )
    scheduler.add_job(
        run_vector_reconcile_job,
        trigger="interval",
        hours=reconcile_interval_hours,
        id="vector_reconcile_job",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),  # also do a first pass shortly after boot
    )
    scheduler.start()
    logger.info(
        "APScheduler BackgroundScheduler started: daily digest job at %02d:%02d UTC, "
        "vector reconcile job every %d hour(s)",
        hour, minute, reconcile_interval_hours,
    )


def shutdown_scheduler():
    """Gracefully shuts down APScheduler."""
    global scheduler
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler BackgroundScheduler shut down")
