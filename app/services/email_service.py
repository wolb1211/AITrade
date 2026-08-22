from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import Settings


class EmailService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.mail_host and self.settings.mail_user and self.settings.mail_password)

    def send_verification_code(self, *, to: str, code: str, purpose: str, minutes: int) -> None:
        if not self.configured:
            raise RuntimeError("mail_not_configured")
        purpose_names = {
            "register": "注册验证",
            "login": "登录验证",
            "reset_password": "重置密码",
            "change_email_old": "确认更换邮箱",
            "change_email_new": "验证新邮箱",
        }
        purpose_name = purpose_names.get(purpose, "身份验证")
        message = EmailMessage()
        message["Subject"] = f"GainLab AI Trader {purpose_name}验证码"
        message["From"] = f"GainLab AI Trader <{self.settings.mail_from or self.settings.mail_user}>"
        message["To"] = to
        message.set_content(f"您的验证码是 {code}，{minutes} 分钟内有效。请勿将验证码告知他人。")
        message.add_alternative(
            f"""
            <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;padding:32px;border:1px solid #e5e7eb;border-radius:12px">
              <h2 style="margin:0 0 20px;color:#0f172a">GainLab AI Trader</h2>
              <p style="color:#475569">您正在进行：{purpose_name}</p>
              <div style="margin:28px 0;padding:18px;text-align:center;background:#f0fdf4;border-radius:10px;font-size:30px;font-weight:700;letter-spacing:8px;color:#047857">{code}</div>
              <p style="color:#64748b">验证码将在 {minutes} 分钟后失效，且只能使用一次。如非本人操作，请忽略本邮件。</p>
            </div>
            """,
            subtype="html",
        )
        if self.settings.mail_secure:
            with smtplib.SMTP_SSL(self.settings.mail_host, self.settings.mail_port, timeout=20) as smtp:
                smtp.login(self.settings.mail_user, self.settings.mail_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(self.settings.mail_host, self.settings.mail_port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(self.settings.mail_user, self.settings.mail_password)
                smtp.send_message(message)
