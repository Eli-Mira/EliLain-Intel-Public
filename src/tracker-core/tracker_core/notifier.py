"""Notifier module for sending reports via various channels."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BaseNotifier(ABC):
    """Base class for notification channels."""
    
    name: str = "base"
    
    @abstractmethod
    async def send(self, message: str) -> bool:
        """Send notification. Returns True if successful."""
        ...


class FileNotifier(BaseNotifier):
    """Save report to file."""
    
    name = "file"
    
    def __init__(self, output_dir: Path | str = "./reports"):
        super().__init__()
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    async def send(self, message: str) -> bool:
        """Write report to timestamped file."""
        from datetime import datetime
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = self.output_dir / f"report_{timestamp}.md"
        
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(message)
            logger.info(f"Report saved to {filename}")
            return True
        except OSError as e:
            logger.error(f"Failed to save report: {e}")
            return False


class TelegramNotifier(BaseNotifier):
    """Send report via Telegram bot."""
    
    name = "telegram"
    
    def __init__(self, bot_token: str, chat_id: str):
        super().__init__()
        self.bot_token = bot_token
        self.chat_id = chat_id
    
    async def send(self, message: str) -> bool:
        """Send message via Telegram Bot API."""
        try:
            import httpx
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json={
                    "chat_id": self.chat_id,
                    "text": message[:4096],  # Telegram limit
                    "parse_mode": "Markdown"
                })
                if response.status_code == 200:
                    logger.info("Telegram notification sent")
                    return True
                else:
                    # Never log the response body: it can echo the request URL,
                    # and the bot token is part of that URL's path.
                    logger.error("Telegram API error: HTTP %s", response.status_code)
                    return False
        except Exception as e:
            # httpx embeds the full request URL - including the bot token - in its
            # exception message. Log the exception CLASS only, never the message.
            logger.error("Failed to send Telegram notification: %s", type(e).__name__)
            return False


class EmailNotifier(BaseNotifier):
    """Send report via email."""
    
    name = "email"
    
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        from_addr: str,
        to_addrs: list[str]
    ):
        super().__init__()
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.from_addr = from_addr
        self.to_addrs = to_addrs
    
    async def send(self, message: str) -> bool:
        """Send email with report."""
        try:
            import aiosmtplib
            from email.message import EmailMessage
            
            msg = EmailMessage()
            msg["Subject"] = "Tracker Report"
            msg["From"] = self.from_addr
            msg["To"] = ", ".join(self.to_addrs)
            msg.set_content(message)
            
            async with aiosmtplib.SMTP(hostname=self.host, port=self.port) as smtp:
                await smtp.login(self.user, self.password)
                await smtp.send_message(msg)
            
            logger.info(f"Email sent to {self.to_addrs}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False


class NullNotifier(BaseNotifier):
    """No-op notifier for testing."""
    
    name = "none"
    
    async def send(self, message: str) -> bool:
        """Do nothing."""
        logger.debug("Null notifier: message discarded")
        return True


class DiscordNotifier(BaseNotifier):
    """Send report via Discord bot/webhook."""

    name = "discord"

    def __init__(self, bot_token: str, channel_id: str):
        super().__init__()
        self.bot_token = bot_token
        self.channel_id = channel_id

    async def send(self, message: str) -> bool:
        """Send message via Discord Bot API."""
        try:
            import httpx
            url = f"https://discord.com/api/v10/channels/{self.channel_id}/messages"
            headers = {
                "Authorization": f"Bot {self.bot_token}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, json={
                    "content": message[:2000],  # Discord limit
                })
                if response.status_code in (200, 201):
                    logger.info("Discord notification sent")
                    return True
                else:
                    # The error body can echo the Authorization header value.
                    logger.error("Discord API error: HTTP %s", response.status_code)
                    return False
        except Exception as e:
            logger.error("Failed to send Discord notification: %s", type(e).__name__)
            return False


class MultiNotifier(BaseNotifier):
    """Send notifications through multiple channels simultaneously."""
    
    name = "multi"
    
    def __init__(self, notifiers: list[BaseNotifier]):
        super().__init__()
        self.notifiers = notifiers
    
    async def send(self, message: str) -> bool:
        """Send to every selected channel; report success only if all succeeded.

        A partial fan-out is a delivery failure, not a success: reporting True
        for "at least one channel worked" silently hides a broken destination.
        """
        import asyncio

        tasks = [n.send(message) for n in self.notifiers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_ok = True
        for notifier, result in zip(self.notifiers, results):
            if isinstance(result, BaseException):
                # Exception MESSAGE only: it can embed request URLs or headers.
                logger.error("%s notifier error: %s", notifier.name, type(result).__name__)
                all_ok = False
            elif result:
                continue
            else:
                logger.warning("%s notifier returned False", notifier.name)
                all_ok = False

        return all_ok


def get_notifier(config: dict[str, Any]) -> BaseNotifier:
    """Create notifier from config."""
    channel = config.get("channel", "file")
    
    # Support comma-separated list: "telegram,discord,email"
    if "," in channel:
        channels = [c.strip() for c in channel.split(",") if c.strip()]
        notifiers = []
        for ch in channels:
            sub_config = {**config, "channel": ch}
            notifiers.append(get_notifier(sub_config))
        return MultiNotifier(notifiers)
    
    if channel == "telegram":
        return TelegramNotifier(
            bot_token=config["telegram_bot_token"],
            chat_id=config["telegram_chat_id"]
        )
    elif channel == "discord":
        return DiscordNotifier(
            bot_token=config["discord_bot_token"],
            channel_id=config["discord_channel_id"]
        )
    elif channel == "email":
        port = config.get("email_port", 587) or 587
        return EmailNotifier(
            host=config.get("email_host", "smtp.gmail.com") or "smtp.gmail.com",
            port=int(port) if port else 587,
            user=config["email_user"] or "",
            password=config["email_password"] or "",
            from_addr=config.get("email_from") or config["email_user"] or "",
            to_addrs=config.get("email_to") or []
        )
    elif channel == "none":
        return NullNotifier()
    else:
        return FileNotifier()
