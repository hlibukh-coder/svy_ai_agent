"""
Email channel — inbound via IMAP polling, outbound via SMTP. Uses the stdlib
imaplib/smtplib wrapped in asyncio.to_thread (no extra dependency).

Credentials (accounts table): {
  "imap_host","imap_port","smtp_host","smtp_port","user","password",
  "use_ssl": true, "from_name": "СВЮ.КЛУБ", "poll_seconds": 60
}
conv_id = "email:{account_id}:{from_address}". The reply is a single threaded email.
"""
import asyncio
import email
import email.header
import email.utils
import imaplib
import logging
import os
import smtplib
from email.message import EmailMessage

from src import accounts as account_manager
from src.channels.base import ChannelAdapter, InboundMessage, OutboundResult

logger = logging.getLogger(__name__)

_EMAIL_MAX = 25 * 1024 * 1024  # ~25MB typical attachment cap


class EmailAdapter(ChannelAdapter):
    channel = "email"

    def __init__(self, account_id, label, credentials, on_inbound):
        super().__init__(account_id, label, credentials, on_inbound)
        c = self.credentials
        self.imap_host = c.get("imap_host", "")
        self.imap_port = int(c.get("imap_port", 993) or 993)
        self.smtp_host = c.get("smtp_host", "")
        self.smtp_port = int(c.get("smtp_port", 465) or 465)
        self.user = c.get("user", "")
        self.password = c.get("password", "")
        self.use_ssl = bool(c.get("use_ssl", True))
        self.from_name = c.get("from_name", "") or "СВЮ.КЛУБ"
        self.poll_seconds = int(c.get("poll_seconds", 60) or 60)
        self._task: asyncio.Task | None = None
        self._stop = False
        # peer(email) -> {"subject","message_id"} for threading replies
        self._threads: dict[str, dict] = {}

    def peer_for_phone(self, phone: str) -> str:
        return phone  # email peers are addresses, not phones

    def max_file_bytes(self) -> int:
        return _EMAIL_MAX

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if not (self.imap_host and self.user and self.password):
            await account_manager.update_status(self.account_id, "error", "missing email credentials")
            return
        try:
            await asyncio.to_thread(self._imap_login_test)
            await account_manager.update_status(self.account_id, "authorized")
        except Exception as e:
            await account_manager.update_status(self.account_id, "error", str(e))
            logger.error(f"[EMAIL:{self.account_id}] login failed: {e}")
            return
        self._stop = False
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"[EMAIL:{self.account_id}] polling {self.imap_host} every {self.poll_seconds}s")

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def healthcheck(self) -> dict:
        try:
            await asyncio.to_thread(self._imap_login_test)
            return {"status": "authorized", "email": self.user}
        except Exception as e:
            return {"status": "disconnected", "error": str(e)}

    # ── inbound (IMAP poll) ──────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        while not self._stop:
            try:
                items = await asyncio.to_thread(self._fetch_unseen)
                seen_uids = []
                for uid, parsed in items:
                    if self.is_duplicate(parsed["message_id"]):
                        seen_uids.append(uid)
                        continue
                    self._threads[parsed["from"]] = {
                        "subject": parsed["subject"], "message_id": parsed["message_id"]}
                    msg = InboundMessage(
                        channel="email", account_id=self.account_id, peer=parsed["from"],
                        text=parsed["body"], sender_email=parsed["from"],
                        sender_name=parsed["from_name"], external_id=parsed["message_id"],
                        thread_ref=parsed["message_id"], subject=parsed["subject"],
                    )
                    await self._on_inbound(msg)
                    seen_uids.append(uid)
                if seen_uids:
                    await asyncio.to_thread(self._mark_seen, seen_uids)
            except Exception as e:
                logger.error(f"[EMAIL:{self.account_id}] poll error: {e}")
            await asyncio.sleep(self.poll_seconds)

    def _imap_connect(self) -> imaplib.IMAP4:
        conn = (imaplib.IMAP4_SSL(self.imap_host, self.imap_port) if self.use_ssl
                else imaplib.IMAP4(self.imap_host, self.imap_port))
        conn.login(self.user, self.password)
        return conn

    def _imap_login_test(self) -> None:
        conn = self._imap_connect()
        try:
            conn.select("INBOX")
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _fetch_unseen(self) -> list[tuple[bytes, dict]]:
        conn = self._imap_connect()
        out: list[tuple[bytes, dict]] = []
        try:
            conn.select("INBOX")
            typ, data = conn.search(None, "UNSEEN")
            if typ != "OK":
                return out
            for uid in data[0].split():
                # BODY.PEEK does not set the \Seen flag (we mark it ourselves after dispatch)
                typ, msg_data = conn.fetch(uid, "(BODY.PEEK[])")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                out.append((uid, self._parse(raw)))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out

    def _mark_seen(self, uids: list[bytes]) -> None:
        try:
            conn = self._imap_connect()
            conn.select("INBOX")
            for uid in uids:
                try:
                    conn.store(uid, "+FLAGS", "\\Seen")
                except Exception:
                    pass
            conn.logout()
        except Exception as e:
            logger.error(f"[EMAIL:{self.account_id}] mark_seen error: {e}")

    @staticmethod
    def _parse(raw: bytes) -> dict:
        m = email.message_from_bytes(raw)
        from_name, from_addr = email.utils.parseaddr(m.get("From", ""))
        subject = str(email.header.make_header(email.header.decode_header(m.get("Subject", ""))))
        message_id = (m.get("Message-ID", "") or "").strip()
        body = ""
        if m.is_multipart():
            for part in m.walk():
                if part.get_content_type() == "text/plain" and "attachment" not in str(
                        part.get("Content-Disposition", "")):
                    try:
                        body = part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", "replace")
                        break
                    except Exception:
                        continue
        else:
            try:
                body = m.get_payload(decode=True).decode(
                    m.get_content_charset() or "utf-8", "replace")
            except Exception:
                body = m.get_payload() or ""
        return {"from": from_addr, "from_name": from_name, "subject": subject,
                "message_id": message_id, "body": (body or "").strip()}

    # ── outbound (SMTP) ──────────────────────────────────────────────────────
    def _smtp_send(self, to_addr: str, subject: str, body: str,
                   in_reply_to: str = "", attachment: dict | None = None) -> None:
        em = EmailMessage()
        em["From"] = email.utils.formataddr((self.from_name, self.user))
        em["To"] = to_addr
        em["Subject"] = subject
        if in_reply_to:
            em["In-Reply-To"] = in_reply_to
            em["References"] = in_reply_to
        em.set_content(body or "")
        if attachment:
            data = attachment["data"]
            maintype, _, subtype = (attachment.get("mimetype") or "application/octet-stream").partition("/")
            em.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                              filename=attachment.get("filename", "file"))
        if self.use_ssl:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port) as s:
                s.login(self.user, self.password)
                s.send_message(em)
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as s:
                s.starttls()
                s.login(self.user, self.password)
                s.send_message(em)

    def _reply_subject(self, peer: str) -> tuple[str, str]:
        t = self._threads.get(peer) or {}
        subj = t.get("subject", "")
        in_reply_to = t.get("message_id", "")
        if subj and not subj.lower().startswith("re:"):
            subj = "Re: " + subj
        return subj or "Повідомлення від СВЮ.КЛУБ", in_reply_to

    async def send_text(self, peer: str, text: str) -> OutboundResult:
        subject, in_reply_to = self._reply_subject(peer)
        try:
            await asyncio.to_thread(self._smtp_send, peer, subject, text, in_reply_to)
            return OutboundResult(ok=True)
        except Exception as e:
            return OutboundResult(ok=False, error=str(e))

    async def send_reply(self, peer: str, reply: str) -> None:
        # Email reply is a SINGLE message, not several short ones.
        res = await self.send_text(peer, reply)
        if not res.ok:
            logger.error(f"[EMAIL:{self.account_id}] reply failed: {res.error}")

    async def send_file(self, peer, file, caption="", filename="", mimetype="") -> OutboundResult:
        try:
            if isinstance(file, (bytes, bytearray)):
                data = bytes(file)
            else:
                with open(file, "rb") as fh:
                    data = fh.read()
                filename = filename or os.path.basename(str(file))
            subject, in_reply_to = self._reply_subject(peer)
            attachment = {"data": data, "filename": filename or "file",
                          "mimetype": mimetype or "application/octet-stream"}
            await asyncio.to_thread(self._smtp_send, peer, subject, caption or "", in_reply_to, attachment)
            return OutboundResult(ok=True)
        except Exception as e:
            return OutboundResult(ok=False, error=str(e))
