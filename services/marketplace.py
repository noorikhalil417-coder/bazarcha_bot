"""Marketplace rules and data access."""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from database import Database

AD_TYPES = {"sale", "wanted", "rental", "service"}
PRICE_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
TRANSITIONS = {
    "draft": {"pending", "archived"}, "pending": {"approved", "rejected", "archived"},
    "approved": {"publishing", "archived"}, "publishing": {"published", "publication_failed"},
    "publication_failed": {"publishing", "archived"}, "published": {"archived", "expired", "sold"},
    "rejected": {"draft", "archived"}, "expired": {"draft", "archived"},
    "archived": {"draft"}, "sold": {"archived"},
}


class MarketplaceError(ValueError):
    pass


def parse_price_input(value: str) -> tuple[int | None, bool]:
    """Parse an explicit AFN price without inferring values from ambiguous text."""
    normalized = re.sub(r"\s+", " ", value.translate(PRICE_DIGITS).strip().casefold())
    negotiable = False
    if normalized in {"توافقی", "قابل مذاکره", "با جورآمد", "جورآمد"}:
        return None, True

    normalized, count = re.subn(r"(?:با\s*جورآمد|قابل مذاکره|توافقی|جورآمد)\s*$", "", normalized)
    negotiable = count > 0
    normalized = re.sub(r"^قیمت\s*", "", normalized).strip()
    normalized = re.sub(r"\s*(?:افغانی|افغانۍ|afn)\s*$", "", normalized).strip()

    multiplier = 1
    if re.search(r"\s*هزار$", normalized):
        normalized = re.sub(r"\s*هزار$", "", normalized).strip()
        multiplier = 1000

    amount_pattern = r"(?:[0-9]+|[0-9]{1,3}(?:[,،٬ ][0-9]{3})+)"
    if not re.fullmatch(amount_pattern, normalized):
        if negotiable and not normalized:
            return None, True
        raise MarketplaceError("قیمت را نفهمیدم؛ عدد، «۳ هزار»، «با جورآمد» یا «توافقی» بنویسید. قیمت را حدس نمی‌زنم.")
    amount = int(re.sub(r"[,،٬ ]", "", normalized)) * multiplier
    if amount <= 0 or amount > 10**12:
        raise MarketplaceError("قیمت باید عددی مثبت و حداکثر ۱٬۰۰۰٬۰۰۰٬۰۰۰٬۰۰۰ افغانی باشد.")
    return amount, negotiable


def format_price(ad: dict[str, Any] | sqlite3.Row) -> str:
    amount = ad["price_minor"]
    negotiable = bool(ad["price_negotiable"])
    if amount is None:
        return "توافقی"
    formatted = f"{amount:,}".replace(",", "٬").translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    return f"{formatted} افغانی" + ("؛ قابل جورآمد" if negotiable else "")


def category_emoji(category_name: str) -> str:
    label = category_name.casefold()
    for words, emoji in (
        (("موبایل", "تکنالوژی"), "📱"),
        (("موتر", "وسایط"), "🚗"),
        (("خانه", "جایداد"), "🏠"),
        (("لباس", "شخصی"), "👕"),
        (("لوازم خانه",), "🛋️"),
        (("خدمات",), "🛠️"),
        (("کار", "استخدام"), "💼"),
        (("حیوان", "زراعت"), "🌾"),
    ):
        if any(word in label for word in words):
            return emoji
    return "🛍️"


def format_public_ad(ad: dict[str, Any] | sqlite3.Row) -> str:
    kind = {"sale": "فروش", "wanted": "خرید", "rental": "کرایه", "service": "خدمات"}.get(ad["ad_type"], "آگهی")
    return (
        f"{category_emoji(ad['category_name'])} {ad['title']}\n"
        f"📂 {ad['category_name']} | {kind}\n"
        f"💰 قیمت: {format_price(ad)}\n\n"
        f"📝 توضیحات:\n{ad['description']}\n\n"
        f"📍 موقعیت: {ad['city']}\n"
        f"📞 راه تماس: {ad['contact']}\n"
        f"🔖 شناسهٔ آگهی: #{ad['ad_id']}\n"
        "🏷️ بازارچه"
    )


def normalize_text(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value).strip()


def validate_ad(data: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(data)
    for field in ("title", "description", "city", "contact"):
        cleaned[field] = normalize_text(str(cleaned.get(field, "")))
        if not cleaned[field]:
            raise MarketplaceError("همهٔ بخش‌های آگهی باید تکمیل شوند.")
    if len(cleaned["title"]) > 120 or len(cleaned["description"]) > 3000:
        raise MarketplaceError("عنوان یا تشریح آگهی بیش از اندازه دراز است.")
    if len(cleaned["city"]) > 80 or len(cleaned["contact"]) > 160:
        raise MarketplaceError("معلومات محل یا راه تماس بیش از اندازه دراز است.")
    if cleaned.get("ad_type") not in AD_TYPES:
        raise MarketplaceError("نوع آگهی معتبر نیست.")
    price = cleaned.get("price_minor")
    if price is not None and (not isinstance(price, int) or price < 0 or price > 10**14):
        raise MarketplaceError("قیمت باید یک عدد معتبر و مثبت باشد.")
    if price is None and not cleaned.get("price_negotiable"):
        raise MarketplaceError("قیمت را بنویسید یا گزینهٔ قابل مذاکره را انتخاب کنید.")
    return cleaned


class Marketplace:
    def __init__(self, database: Database, max_active_ads: int = 5, expiry_days: int = 30):
        self.db = database
        self.max_active_ads = max_active_ads
        self.expiry_days = expiry_days

    def ensure_user(self, user_id: int, username: str | None = None, display_name: str = "") -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO users(user_id, username, display_name) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, display_name=excluded.display_name, "
                "last_seen_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (user_id, username, display_name),
            )

    def categories(self, enabled_only: bool = True) -> list[sqlite3.Row]:
        query = "SELECT * FROM categories" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY sort_order, category_id"
        with self.db.connect() as conn:
            return conn.execute(query).fetchall()

    def rename_category(self, category_id: int, name: str, admin_id: int) -> None:
        name = normalize_text(name)
        if not name or len(name) > 80:
            raise MarketplaceError("نام دسته‌بندی باید میان ۱ تا ۸۰ نویسه باشد.")
        with self.db.transaction() as conn:
            try:
                cursor = conn.execute("UPDATE categories SET name=? WHERE category_id=?", (name, category_id))
            except sqlite3.IntegrityError as exc:
                raise MarketplaceError("این نام دسته‌بندی از پیش استفاده شده است.") from exc
            if cursor.rowcount != 1:
                raise MarketplaceError("دسته‌بندی پیدا نشد.")
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'category_rename',?)", (admin_id, f"{category_id}:{name}"))

    def reorder_category(self, category_id: int, position: int, admin_id: int) -> None:
        with self.db.transaction() as conn:
            ids = [row[0] for row in conn.execute("SELECT category_id FROM categories ORDER BY sort_order,category_id")]
            if category_id not in ids or position < 1 or position > len(ids):
                raise MarketplaceError("دسته‌بندی یا جایگاه معتبر نیست.")
            ids.remove(category_id)
            ids.insert(position - 1, category_id)
            conn.executemany("UPDATE categories SET sort_order=? WHERE category_id=?", enumerate(ids))
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'category_reorder',?)", (admin_id, f"{category_id}:{position}"))

    def create_search_session(self, user_id: int, keyword: str = "") -> str:
        session_id = uuid.uuid4().hex[:16]
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
            conn.execute("INSERT INTO search_sessions(session_id,user_id,keyword) VALUES(?,?,?)", (session_id, user_id, keyword[:80]))
        return session_id

    def get_search_session(self, session_id: str, user_id: int) -> sqlite3.Row | None:
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM search_sessions WHERE session_id=? AND user_id=?", (session_id, user_id)).fetchone()

    def update_search_session(self, session_id: str, user_id: int, field: str, value: Any) -> None:
        allowed = {"keyword", "category_id", "city", "ad_type", "min_price", "max_price", "pending_field"}
        if field not in allowed:
            raise MarketplaceError("این فیلتر معتبر نیست.")
        with self.db.transaction() as conn:
            cursor = conn.execute(f"UPDATE search_sessions SET {field}=? WHERE session_id=? AND user_id=?", (value, session_id, user_id))
            if cursor.rowcount != 1:
                raise MarketplaceError("نشست جست‌وجو پایان یافته است.")

    def pending_search_input(self, user_id: int) -> sqlite3.Row | None:
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM search_sessions WHERE user_id=? AND pending_field IS NOT NULL ORDER BY created_at DESC LIMIT 1", (user_id,)).fetchone()

    def search_count(self, *, keyword: str = "", category_id: int | None = None, city: str = "", ad_type: str | None = None,
                     min_price: int | None = None, max_price: int | None = None) -> int:
        clauses, params = self._search_clauses(keyword, category_id, city, ad_type, min_price, max_price)
        with self.db.connect() as conn:
            return int(conn.execute("SELECT count(*) FROM ads WHERE " + " AND ".join(clauses), params).fetchone()[0])

    def seller_rankings(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT users.user_id,users.username,users.display_name,SUM(reputation_events.points) AS score,"
                "COUNT(DISTINCT reputation_events.ad_id) AS approved_ads "
                "FROM reputation_events JOIN users USING(user_id) "
                "WHERE reputation_events.reason='ad_approved' "
                "AND reputation_events.created_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-7 days') "
                "GROUP BY users.user_id ORDER BY score DESC,approved_ads DESC,users.user_id LIMIT ?",
                (min(max(limit, 1), 50),),
            ).fetchall()

    def claim_scheduled_job(self, job_key: str, interval_hours: int, lease_minutes: int = 45) -> bool:
        if interval_hours < 0 or lease_minutes < 1:
            raise ValueError("Invalid scheduled job interval")
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO scheduled_state(job_key) VALUES(?)", (job_key,))
            cursor = conn.execute(
                "UPDATE scheduled_state SET lease_until=strftime('%Y-%m-%dT%H:%M:%fZ','now',?) "
                "WHERE job_key=? AND (lease_until IS NULL OR lease_until <= strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "AND (last_run_at IS NULL OR last_run_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now',?))",
                (f"+{lease_minutes} minutes", job_key, f"-{interval_hours} hours"),
            )
            return cursor.rowcount == 1

    def finish_scheduled_job(self, job_key: str, succeeded: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE scheduled_state SET last_run_at=CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE last_run_at END,lease_until=NULL WHERE job_key=?",
                (int(succeeded), job_key),
            )

    def cleanup_temporary_state(self, retention_days: int) -> int:
        if retention_days < 1:
            raise ValueError("Retention must be at least one day")
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM search_sessions WHERE created_at < strftime('%Y-%m-%dT%H:%M:%fZ','now',?)", (f"-{retention_days} days",))
            conn.execute("DELETE FROM processed_updates WHERE processed_at < strftime('%Y-%m-%dT%H:%M:%fZ','now',?)", (f"-{retention_days} days",))
            return cursor.rowcount

    def create_draft(self, owner_id: int, category_id: int, ad_type: str) -> int:
        if ad_type not in AD_TYPES:
            raise MarketplaceError("نوع آگهی معتبر نیست.")
        with self.db.transaction() as conn:
            category = conn.execute("SELECT 1 FROM categories WHERE category_id=? AND enabled=1", (category_id,)).fetchone()
            if not category:
                raise MarketplaceError("دسته‌بندی انتخاب‌شده فعال نیست.")
            count = conn.execute("SELECT count(*) FROM ads WHERE owner_id=? AND status IN ('pending','approved','publishing','published')", (owner_id,)).fetchone()[0]
            if count >= self.max_active_ads:
                raise MarketplaceError("شما به بیشترین شمار آگهی فعال رسیده‌اید.")
            cursor = conn.execute("INSERT INTO ads(owner_id,category_id,ad_type,title,description,city,contact) VALUES(?,?,?,'','','','')", (owner_id, category_id, ad_type))
            return int(cursor.lastrowid)

    def draft_for_user(self, owner_id: int) -> sqlite3.Row | None:
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM ads WHERE owner_id=? AND status='draft' ORDER BY ad_id DESC LIMIT 1", (owner_id,)).fetchone()

    def photos(self, ad_id: int) -> list[str]:
        with self.db.connect() as conn:
            return [row[0] for row in conn.execute("SELECT file_id FROM ad_photos WHERE ad_id=? ORDER BY sort_order", (ad_id,))]

    def update_draft_field(self, ad_id: int, owner_id: int, field: str, value: Any) -> None:
        if field not in {"title", "description", "price_minor", "currency", "price_negotiable", "city", "contact"}:
            raise MarketplaceError("این بخش قابل تغییر نیست.")
        with self.db.transaction() as conn:
            cursor = conn.execute(f"UPDATE ads SET {field}=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=? AND owner_id=? AND status='draft'", (value, ad_id, owner_id))
            if cursor.rowcount != 1:
                raise MarketplaceError("این آگهی در دسترس شما نیست.")

    def add_photo(self, ad_id: int, owner_id: int, file_id: str) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status FROM ads WHERE ad_id=? AND owner_id=?", (ad_id, owner_id)).fetchone()
            if not row or row["status"] != "draft":
                raise MarketplaceError("این آگهی در دسترس شما نیست.")
            count = conn.execute("SELECT count(*) FROM ad_photos WHERE ad_id=?", (ad_id,)).fetchone()[0]
            if count >= 10:
                raise MarketplaceError("حداکثر ۱۰ عکس پذیرفته می‌شود.")
            conn.execute("INSERT OR IGNORE INTO ad_photos(ad_id,file_id,sort_order) VALUES(?,?,?)", (ad_id, file_id, count))

    def submit(self, ad_id: int, owner_id: int, review_required: bool = True) -> str:
        """Queue a complete draft for administrator approval.

        ``review_required`` remains accepted for callers from older versions; all
        submissions now require approval regardless of that legacy argument.
        """
        _ = review_required
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM ads WHERE ad_id=? AND owner_id=? AND status='draft'", (ad_id, owner_id)).fetchone()
            if not row:
                raise MarketplaceError("این آگهی در دسترس شما نیست.")
            data = dict(row)
            validate_ad(data)
            duplicate = conn.execute("SELECT 1 FROM ads WHERE owner_id=? AND title=? AND description=? AND status IN ('pending','approved','publishing','published')", (owner_id, data["title"], data["description"])).fetchone()
            if duplicate:
                raise MarketplaceError("آگهی مشابه شما از پیش ثبت شده است.")
            status = "pending"
            conn.execute("UPDATE ads SET status=?, expires_at=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=?", (status, (datetime.now(UTC) + timedelta(days=self.expiry_days)).isoformat(), ad_id))
            return status

    def get_ad(self, ad_id: int) -> sqlite3.Row | None:
        with self.db.connect() as conn:
            return conn.execute("SELECT ads.*, categories.name AS category_name, users.username AS owner_username FROM ads JOIN categories USING(category_id) JOIN users ON users.user_id=ads.owner_id WHERE ad_id=?", (ad_id,)).fetchone()

    def owned_ad(self, ad_id: int, owner_id: int) -> sqlite3.Row | None:
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM ads WHERE ad_id=? AND owner_id=?", (ad_id, owner_id)).fetchone()

    def user_ads(self, owner_id: int, limit: int = 20) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute("SELECT ad_id,title,status,review_note,created_at FROM ads WHERE owner_id=? ORDER BY ad_id DESC LIMIT ?", (owner_id, min(max(limit, 1), 50))).fetchall()

    def transition(self, ad_id: int, new_status: str, admin_id: int | None = None, note: str = "") -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status, owner_id FROM ads WHERE ad_id=?", (ad_id,)).fetchone()
            if not row or new_status not in TRANSITIONS.get(row["status"], set()):
                raise MarketplaceError("وضعیت آگهی تغییر کرده یا این کار مجاز نیست.")
            conn.execute("UPDATE ads SET status=?, review_note=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=?", (new_status, note[:500], ad_id))
            if admin_id is not None:
                conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (admin_id,))
                conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)", (admin_id, f"ad_{new_status}", str(ad_id)))
                conn.execute("INSERT INTO moderation_events(user_id,admin_id,ad_id,action,reason) VALUES(?,?,?,?,?)", (row["owner_id"], admin_id, ad_id, new_status, note[:500]))
            if new_status == "approved":
                self._award_ad_reputation(conn, ad_id, row["owner_id"])

    @staticmethod
    def _award_ad_reputation(conn: sqlite3.Connection, ad_id: int, owner_id: int) -> None:
        event = conn.execute("SELECT 1 FROM reputation_events WHERE ad_id=? AND reason='ad_approved'", (ad_id,)).fetchone()
        if not event:
            conn.execute("INSERT INTO reputation_events(user_id,ad_id,points,reason) VALUES(?,?,1,'ad_approved')", (owner_id, ad_id))
            conn.execute("UPDATE users SET reputation=reputation+1 WHERE user_id=?", (owner_id,))

    def reopen(self, ad_id: int, owner_id: int) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute("UPDATE ads SET status='draft', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=? AND owner_id=? AND status IN ('rejected','expired','archived')", (ad_id, owner_id))
            if cursor.rowcount != 1:
                raise MarketplaceError("این آگهی را نمی‌توان دوباره ویرایش کرد.")

    def owner_action(self, ad_id: int, owner_id: int, action: str) -> None:
        allowed = {"sold": ("published",), "archived": ("published", "pending", "approved")}
        if action not in allowed:
            raise MarketplaceError("این کار مجاز نیست.")
        with self.db.transaction() as conn:
            placeholders = ",".join("?" for _ in allowed[action])
            cursor = conn.execute(f"UPDATE ads SET status=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=? AND owner_id=? AND status IN ({placeholders})", (action, ad_id, owner_id, *allowed[action]))
            if cursor.rowcount != 1:
                raise MarketplaceError("این آگهی در دسترس شما نیست یا وضعیت آن تغییر کرده است.")

    def claim_publication(self, ad_id: int, admin_id: int, chat_id: int, retry_failed: bool = False) -> bool:
        with self.db.transaction() as conn:
            statuses = "('approved','publication_failed')" if retry_failed else "('approved')"
            cursor = conn.execute(f"UPDATE ads SET status='publishing', publication_chat_id=?, publish_claimed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=? AND status IN {statuses} AND publication_message_id IS NULL", (chat_id, ad_id))
            if cursor.rowcount:
                conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)", (admin_id, "ad_publish_claim", str(ad_id)))
            return cursor.rowcount == 1

    def finish_publication(self, ad_id: int, message_id: int, chat_id: int | None = None) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute("UPDATE ads SET status='published', publication_message_id=coalesce(publication_message_id,?), published_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=? AND status='publishing' AND (publication_message_id IS NULL OR publication_message_id=?)", (message_id, ad_id, message_id))
            if cursor.rowcount != 1:
                raise MarketplaceError("ثبت نشر آگهی ممکن نشد؛ از نشر دوباره خودداری شد.")
            owner_id = conn.execute("SELECT owner_id FROM ads WHERE ad_id=?", (ad_id,)).fetchone()[0]
            self._award_ad_reputation(conn, ad_id, owner_id)
            if chat_id is not None:
                conn.execute("INSERT OR IGNORE INTO ad_publication_messages(ad_id,chat_id,message_id,sort_order) VALUES(?,?,?,0)", (ad_id, chat_id, message_id))

    def record_publication_message(self, ad_id: int, message_id: int, chat_id: int | None = None) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute("UPDATE ads SET publication_message_id=? WHERE ad_id=? AND status='publishing' AND publication_message_id IS NULL", (message_id, ad_id))
            if cursor.rowcount != 1:
                raise MarketplaceError("شناسهٔ نشر قبلاً ثبت شده یا وضعیت آگهی تغییر کرده است.")
            if chat_id is not None:
                conn.execute("INSERT INTO ad_publication_messages(ad_id,chat_id,message_id,sort_order) VALUES(?,?,?,0)", (ad_id, chat_id, message_id))

    def record_publication_messages(self, ad_id: int, chat_id: int, message_ids: list[int]) -> None:
        if not message_ids or len(message_ids) > 10 or any(message_id <= 0 for message_id in message_ids) or len(set(message_ids)) != len(message_ids):
            raise MarketplaceError("شناسه‌های پیام نشر معتبر نیستند.")
        with self.db.transaction() as conn:
            cursor = conn.execute("UPDATE ads SET publication_message_id=? WHERE ad_id=? AND status='publishing' AND publication_message_id IS NULL", (message_ids[0], ad_id))
            if cursor.rowcount != 1:
                raise MarketplaceError("شناسهٔ پیام آگهی از پیش ثبت شده یا وضعیت تغییر کرده است.")
            conn.executemany("INSERT INTO ad_publication_messages(ad_id,chat_id,message_id,sort_order) VALUES(?,?,?,?)", [(ad_id, chat_id, message_id, order) for order, message_id in enumerate(message_ids)])

    def add_publication_message(self, ad_id: int, chat_id: int, message_id: int) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT coalesce(max(sort_order),-1)+1 FROM ad_publication_messages WHERE ad_id=?", (ad_id,)).fetchone()
            conn.execute("INSERT OR IGNORE INTO ad_publication_messages(ad_id,chat_id,message_id,sort_order) VALUES(?,?,?,?)", (ad_id, chat_id, message_id, row[0]))

    def finish_album_publication(self, ad_id: int, chat_id: int, message_ids: list[int]) -> None:
        if not 1 <= len(message_ids) <= 11 or any(message_id <= 0 for message_id in message_ids) or len(set(message_ids)) != len(message_ids):
            raise MarketplaceError("شناسه‌های آلبوم تلگرام معتبر نیستند.")
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status,publication_message_id FROM ads WHERE ad_id=?", (ad_id,)).fetchone()
            if not row or row["status"] != "publishing" or (row["publication_message_id"] is not None and row["publication_message_id"] != message_ids[0]):
                raise MarketplaceError("آگهی از پیش نشر شده یا وضعیت آن تغییر کرده است.")
            conn.executemany("INSERT OR IGNORE INTO ad_publication_messages(ad_id,chat_id,message_id,sort_order) VALUES(?,?,?,?)", [(ad_id, chat_id, message_id, order) for order, message_id in enumerate(message_ids)])
            conn.execute("UPDATE ads SET status='published',publication_chat_id=?,publication_message_id=?,published_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=? AND status='publishing'", (chat_id, message_ids[0], ad_id))
            owner_id = conn.execute("SELECT owner_id FROM ads WHERE ad_id=?", (ad_id,)).fetchone()[0]
            self._award_ad_reputation(conn, ad_id, owner_id)

    def publication_messages(self, ad_id: int) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute("SELECT chat_id,message_id,sort_order FROM ad_publication_messages WHERE ad_id=? ORDER BY sort_order", (ad_id,)).fetchall()

    def fail_publication(self, ad_id: int, admin_id: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE ads SET status='publication_failed' WHERE ad_id=? AND status='publishing'", (ad_id,))
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)", (admin_id, "ad_publish_failed", str(ad_id)))

    def confirm_partial_publication(self, ad_id: int, admin_id: int, message_ids: list[int] | None = None) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT status,publication_chat_id,publication_message_id FROM ads WHERE ad_id=?", (ad_id,)).fetchone()
            if not row or row["status"] not in {"publishing", "publication_failed"}:
                raise MarketplaceError("آگهی در انتظار بررسی نشر نیست.")
            known_ids = message_ids or ([row["publication_message_id"]] if row["publication_message_id"] else [])
            if not known_ids or any(not isinstance(value, int) or value <= 0 for value in known_ids) or len(set(known_ids)) != len(known_ids):
                raise MarketplaceError("شناسهٔ پیام نشرشده را بررسی کرده و بنویسید.")
            if row["publication_message_id"] and known_ids[0] != row["publication_message_id"]:
                raise MarketplaceError("شناسه با پیام ثبت‌شده برابر نیست.")
            conn.executemany("INSERT OR IGNORE INTO ad_publication_messages(ad_id,chat_id,message_id,sort_order) VALUES(?,?,?,?)", [(ad_id, row["publication_chat_id"], message_id, order) for order, message_id in enumerate(known_ids)])
            cursor = conn.execute("UPDATE ads SET status='published',publication_message_id=coalesce(publication_message_id,?),published_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=? AND status IN ('publishing','publication_failed')", (known_ids[0], ad_id))
            if cursor.rowcount != 1:
                raise MarketplaceError("تأیید نشر ثبت نشد؛ از نشر دوباره خودداری شد.")
            conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (admin_id,))
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'ad_partial_publish_confirm',?)", (admin_id, str(ad_id)))
            owner_id = conn.execute("SELECT owner_id FROM ads WHERE ad_id=?", (ad_id,)).fetchone()[0]
            self._award_ad_reputation(conn, ad_id, owner_id)

    def search(self, keyword: str = "", category_id: int | None = None, city: str = "", ad_type: str | None = None,
               min_price: int | None = None, max_price: int | None = None, page: int = 0, page_size: int = 10) -> list[sqlite3.Row]:
        clauses, params = self._search_clauses(keyword, category_id, city, ad_type, min_price, max_price)
        page_size = min(max(page_size, 1), 50)
        with self.db.connect() as conn:
            return conn.execute("SELECT ads.*, categories.name AS category_name FROM ads JOIN categories USING(category_id) WHERE " + " AND ".join(clauses) + " ORDER BY CASE WHEN ads.featured_until > strftime('%Y-%m-%dT%H:%M:%fZ','now') THEN 0 ELSE 1 END, ads.ad_id DESC LIMIT ? OFFSET ?", (*params, page_size, max(0, page) * page_size)).fetchall()

    @staticmethod
    def _search_clauses(keyword: str, category_id: int | None, city: str, ad_type: str | None,
                        min_price: int | None, max_price: int | None) -> tuple[list[str], list[Any]]:
        clauses, params = ["status='published'"], []
        if keyword.strip():
            clauses.append("(title LIKE ? OR description LIKE ?)")
            term = f"%{keyword.strip()[:80]}%"
            params.extend((term, term))
        if category_id is not None:
            clauses.append("category_id=?"); params.append(category_id)
        if city.strip():
            clauses.append("city LIKE ?"); params.append(f"%{city.strip()[:80]}%")
        if ad_type:
            if ad_type not in AD_TYPES: raise MarketplaceError("نوع آگهی معتبر نیست.")
            clauses.append("ad_type=?"); params.append(ad_type)
        if min_price is not None:
            clauses.append("price_minor>=?"); params.append(min_price)
        if max_price is not None:
            clauses.append("price_minor<=?"); params.append(max_price)
        return clauses, params

    def add_report(self, ad_id: int, reporter_id: int, category: str, description: str = "", limit: int = 3) -> int:
        with self.db.transaction() as conn:
            recent = conn.execute("SELECT count(*) FROM reports WHERE reporter_id=? AND created_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 day')", (reporter_id,)).fetchone()[0]
            if recent >= limit: raise MarketplaceError("شمار گزارش روزانهٔ شما به حد رسیده است.")
            try:
                cursor = conn.execute("INSERT INTO reports(ad_id,reporter_id,category,description) SELECT ad_id,?,?,? FROM ads WHERE ad_id=? AND status='published'", (reporter_id, category[:80], description[:500], ad_id))
            except sqlite3.IntegrityError as exc:
                raise MarketplaceError("شما این آگهی را پیش از این گزارش کرده‌اید.") from exc
            if cursor.rowcount != 1: raise MarketplaceError("آگهی نشرشده پیدا نشد.")
            return int(cursor.lastrowid)

    def review_report(self, report_id: int, admin_id: int, dismiss: bool) -> None:
        status = "dismissed" if dismiss else "reviewed"
        with self.db.transaction() as conn:
            cursor = conn.execute("UPDATE reports SET status=? WHERE report_id=? AND status='open'", (status, report_id))
            if cursor.rowcount != 1:
                raise MarketplaceError("این گزارش پیش از این بررسی شده یا پیدا نشد.")
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)", (admin_id, f"report_{status}", str(report_id)))

    def set_trusted(self, user_id: int, admin_id: int) -> bool:
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
            conn.execute("UPDATE users SET trusted=1-trusted WHERE user_id=?", (user_id,))
            trusted = bool(conn.execute("SELECT trusted FROM users WHERE user_id=?", (user_id,)).fetchone()[0])
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)", (admin_id, "trusted_toggle", str(user_id)))
            return trusted

    def get_setting(self, key: str, default: str = "") -> str:
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str, admin_id: int) -> None:
        if key not in {"rules", "welcome_message", "welcome_enabled", "blocked_phrases", "links_allowed", "review_mode", "featured_enabled", "public_rankings", "join_approval"}:
            raise MarketplaceError("این تنظیم قابل تغییر نیست.")
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value[:3000]))
            conn.execute("INSERT INTO admin_actions(admin_id,action,target) VALUES(?,?,?)", (admin_id, f"setting_{key}", value[:80]))

    def pending_ads(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute("SELECT ads.*,categories.name category_name FROM ads JOIN categories USING(category_id) WHERE status='pending' ORDER BY ad_id LIMIT ?", (min(max(limit, 1), 50),)).fetchall()

    def flag_group_message(
        self, user_id: int, chat_id: int, message_id: int, username: str | None = None,
        action: str = "review_group_ad",
    ) -> tuple[int, bool]:
        if action != "review_group_ad" and not action.startswith("review_"):
            raise MarketplaceError("نوع بررسی پیام معتبر نیست.")
        reference = f"{chat_id}:{message_id}:{username or ''}"
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT event_id FROM moderation_events WHERE action=? AND reason=?",
                (action, reference),
            ).fetchone()
            if existing:
                return int(existing["event_id"]), False
            cursor = conn.execute(
                "INSERT INTO moderation_events(user_id,action,reason) VALUES(?,?,?)",
                (user_id, action, reference),
            )
            return int(cursor.lastrowid), True

    def pending_group_reviews(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT events.event_id,events.user_id,events.reason,events.created_at,events.action,"
                "users.display_name,users.username FROM moderation_events AS events "
                "LEFT JOIN users USING(user_id) WHERE substr(events.action,1,7)='review_' "
                "AND (events.reason GLOB '-[0-9]*:[0-9]*:*' OR events.reason GLOB '[0-9]*:[0-9]*:*') "
                "AND NOT EXISTS (SELECT 1 FROM admin_actions WHERE action='group_message_review' "
                "AND target=CAST(events.event_id AS TEXT)) ORDER BY events.event_id LIMIT ?",
                (min(max(limit, 1), 50),),
            ).fetchall()

    def mark_group_message_reviewed(self, event_id: int, admin_id: int) -> tuple[int, int, str | None]:
        with self.db.transaction() as conn:
            event = conn.execute(
                "SELECT reason FROM moderation_events WHERE event_id=? AND substr(action,1,7)='review_'",
                (event_id,),
            ).fetchone()
            if not event or conn.execute(
                "SELECT 1 FROM admin_actions WHERE action='group_message_review' AND target=?",
                (str(event_id),),
            ).fetchone():
                raise MarketplaceError("این پیام پیدا نشد یا پیش از این بررسی شده است.")
            try:
                parts = event["reason"].split(":", 2)
                chat_id, message_id = int(parts[0]), int(parts[1])
                username = parts[2] or None if len(parts) > 2 else None
            except (TypeError, ValueError) as exc:
                raise MarketplaceError("پیوند پیام برای بررسی معتبر نیست.") from exc
            conn.execute(
                "INSERT INTO admin_actions(admin_id,action,target) VALUES(?,'group_message_review',?)",
                (admin_id, str(event_id)),
            )
            return chat_id, message_id, username

    def open_reports(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute("SELECT reports.*,ads.title FROM reports JOIN ads USING(ad_id) WHERE reports.status='open' ORDER BY report_id LIMIT ?", (min(max(limit, 1), 50),)).fetchall()

    def due_expiry_notifications(self) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute("SELECT ad_id,owner_id,title,expires_at FROM ads WHERE status='published' AND expiry_notified_at IS NULL AND expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now') AND expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now','+2 days') LIMIT 100").fetchall()

    def mark_expiry_notified(self, ad_id: int) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("UPDATE ads SET expiry_notified_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE ad_id=? AND status='published' AND expiry_notified_at IS NULL", (ad_id,))
            return cursor.rowcount == 1

    def expire_ads(self) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute("UPDATE ads SET status='expired',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE status='published' AND expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now')")
            return cursor.rowcount