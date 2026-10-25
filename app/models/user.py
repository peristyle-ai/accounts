"""User model — composed from public.users (auth) + UserProfile + UserPaymentSettings.

Phase 1b (schema-mode install). The auth source (public.users) is created
in Phase 0 / migrations/001. The User class here is the public API used
everywhere in the app; it transparently composes:

  - public.users          — auth fields (id, email, password_hash, display_name,
                            role, is_active, created_at, updated_at, password_changed_at)
                            Schema + table come from app.config (USERS_TABLE).
                            Default for the peristyle-ai install: 'public.users'.

  - accounts.user_profiles  — business profile (country, business_name, abn,
                              address, contact_*, logo_path). Always in the
                              accounts schema.

  - accounts.user_payment_settings  — bank + payment + Fernet-encrypted
                                       api_key. Always in the accounts schema.

The dependent models (Expense, Invoice, Customer, etc.) all have
ForeignKey('users.id') — they FK to the public.users table. The
@property accessors on this class delegate reads/writes to the right
underlying table so EVERY existing call site (templates, routes,
scripts) continues to work without change.

This is the same pattern as the the sibling mail project 1b refactor (3ae27a7)
but with a 1:3 split (mail has 1 users table; accounts has 3).

Schema table references:
  - The User class references `users` (unqualified) which resolves to
    the table at the USERS_TABLE config value. For the peristyle-ai
    install that's `public.users` (set in app.config from USERS_TABLE
    env var). For the legacy standalone install that's `accounts.users`
    (created by init_db.py when USERS_TABLE is unset).
  - The UserProfile and UserPaymentSettings classes are always in
    the `accounts` schema (created by init_db.py in Phase 1a).
"""
import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from config import accounts_config
from app.models import db, get_utc_now
from app.shared.pii import mask_bsb, mask_account


class User(UserMixin, db.Model):
    """Composed user. Auth fields from the external USERS_TABLE (e.g.
    public.users); business profile + bank/payment from accounts.user_profiles
    + accounts.user_payment_settings (loaded via SQLAlchemy relationships).

    The __table_args__ schema is set at class definition time based on
    the current app.config (USERS_TABLE → parse out the schema). For the
    peristyle-ai install, that's 'public'. For the legacy standalone
    install, that's 'accounts'.
    """
    __tablename__ = 'users'
    # Schema is derived from USERS_TABLE. E.g. 'public.users' → schema='public',
    # 'accounts.users' → schema='accounts'. For peristyle-ai default it's 'public'.
    __table_args__ = ({'schema': accounts_config.users_schema},)

    # --- Auth columns (in public.users for peristyle-ai install) ---
    id = db.Column(db.BigInteger, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(255), nullable=False, default='')
    role = db.Column(
        db.String(50), nullable=False, default='member',
        # NOTE: the role check constraint lives in the DB; SQLAlchemy doesn't
        # enforce it. Mirror the same values as the harness spec: admin/member/viewer.
    )
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now
    )
    password_changed_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=get_utc_now
    )

    # --- Relationships to the new tables (in accounts schema) ---
    profile = db.relationship(
        'UserProfile', uselist=False, backref='user',
        # lazy='joined' would force a JOIN on every User query; the
        # default 'select' is fine — most code paths only need auth fields.
    )
    payment_settings = db.relationship(
        'UserPaymentSettings', uselist=False, backref='user',
    )

    # --- Relationships to the existing dependent tables (still FK to public.users.id) ---
    expenses = db.relationship('Expense', backref='user', lazy='dynamic')
    invoices = db.relationship('Invoice', backref='user', lazy='dynamic')
    activity_logs = db.relationship('ActivityLog', backref='user', lazy='dynamic')

    # --- Auth methods (operate on the auth columns in this table) ---

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def generate_api_key(self):
        """Generate a new API key, encrypt it with Fernet, store in payment_settings.
        Returns the PLAINTEXT api_key (caller must give it to the user once;
        only the ciphertext is stored in the DB).

        Rotating a key is safe: `@api_key_required` resolves keys through
        `UserPaymentSettings.find_by_api_key`, which decrypts the stored
        ciphertext before comparing. See app/shared/decorators.py.
        """
        plaintext = secrets.token_hex(32)
        if not self.payment_settings:
            # Lazy-create the payment settings row so the api_key has somewhere to live.
            # NOTE: the import is local to avoid a circular import
            # (user_payment_settings -> app.models.user).
            from app.models.user_payment_settings import UserPaymentSettings
            self.payment_settings = UserPaymentSettings(user_id=self.id, api_key=None)
        # Use the encrypting setter so there is exactly one code path
        # that writes this column.
        self.payment_settings.api_key_plain = plaintext
        return plaintext

    def get_api_key(self):
        """Get the plaintext API key.

        Decrypts from `payment_settings.api_key`; tolerates legacy rows
        that still hold the key in plaintext (pre-encryption).
        """
        if not self.payment_settings:
            return None
        return self.payment_settings.api_key_plain

    def set_api_key(self, plaintext):
        """Set the API key (encrypts and stores in payment_settings.api_key).

        Safe to call on a row that already holds a legacy plaintext key:
        the setter encrypts, and `find_by_api_key` decrypts before
        comparing, so the new key authenticates immediately.
        """
        if not self.payment_settings:
            from app.models.user_payment_settings import UserPaymentSettings
            self.payment_settings = UserPaymentSettings(user_id=self.id)
        self.payment_settings.api_key_plain = plaintext

    # --- @property accessors that delegate to UserProfile (business fields) ---

    @property
    def country(self):
        return self.profile.country if self.profile else 'AU'

    @country.setter
    def country(self, value):
        self._ensure_profile()
        self.profile.country = value

    @property
    def business_name(self):
        return self.profile.business_name if self.profile else None

    @business_name.setter
    def business_name(self, value):
        self._ensure_profile()
        self.profile.business_name = value

    @property
    def abn(self):
        return self.profile.abn if self.profile else None

    @abn.setter
    def abn(self, value):
        self._ensure_profile()
        self.profile.abn = value

    @property
    def address_line1(self):
        return self.profile.address_line1 if self.profile else None

    @address_line1.setter
    def address_line1(self, value):
        self._ensure_profile()
        self.profile.address_line1 = value

    @property
    def address_line2(self):
        return self.profile.address_line2 if self.profile else None

    @address_line2.setter
    def address_line2(self, value):
        self._ensure_profile()
        self.profile.address_line2 = value

    @property
    def city(self):
        return self.profile.city if self.profile else None

    @city.setter
    def city(self, value):
        self._ensure_profile()
        self.profile.city = value

    @property
    def state(self):
        return self.profile.state if self.profile else None

    @state.setter
    def state(self, value):
        self._ensure_profile()
        self.profile.state = value

    @property
    def postcode(self):
        return self.profile.postcode if self.profile else None

    @postcode.setter
    def postcode(self, value):
        self._ensure_profile()
        self.profile.postcode = value

    @property
    def address_country(self):
        return self.profile.address_country if self.profile else 'Australia'

    @address_country.setter
    def address_country(self, value):
        self._ensure_profile()
        self.profile.address_country = value

    @property
    def contact_email(self):
        return self.profile.contact_email if self.profile else None

    @contact_email.setter
    def contact_email(self, value):
        self._ensure_profile()
        self.profile.contact_email = value

    @property
    def contact_number(self):
        return self.profile.contact_number if self.profile else None

    @contact_number.setter
    def contact_number(self, value):
        self._ensure_profile()
        self.profile.contact_number = value

    @property
    def logo_path(self):
        return self.profile.logo_path if self.profile else None

    @logo_path.setter
    def logo_path(self, value):
        self._ensure_profile()
        self.profile.logo_path = value

    # --- @property accessors that delegate to UserPaymentSettings ---

    @property
    def api_key(self):
        # NOTE: this is a "set?" boolean for API responses (use get_api_key
        # for the plaintext). The plaintext itself lives in
        # payment_settings.api_key and is Fernet-encrypted at rest.
        if not self.payment_settings:
            return None
        return self.payment_settings.api_key  # ciphertext

    @api_key.setter
    def api_key(self, value):
        # value is ciphertext (Fernet). Use api_key_plain to set plaintext.
        if not self.payment_settings:
            self.payment_settings = UserPaymentSettings(user_id=self.id)
        self.payment_settings.api_key = value

    @property
    def bank_name(self):
        return self.payment_settings.bank_name if self.payment_settings else None

    @bank_name.setter
    def bank_name(self, value):
        self._ensure_payment_settings()
        self.payment_settings.bank_name = value

    @property
    def account_name(self):
        return self.payment_settings.account_name if self.payment_settings else None

    @account_name.setter
    def account_name(self, value):
        self._ensure_payment_settings()
        self.payment_settings.account_name = value

    @property
    def account_number(self):
        return self.payment_settings.account_number if self.payment_settings else None

    @account_number.setter
    def account_number(self, value):
        # value is Fernet ciphertext; use account_number_plain for plaintext
        self._ensure_payment_settings()
        self.payment_settings.account_number = value

    @property
    def account_number_plain(self):
        return self.payment_settings.account_number_plain if self.payment_settings else None

    @account_number_plain.setter
    def account_number_plain(self, value):
        self._ensure_payment_settings()
        self.payment_settings.account_number_plain = value

    @property
    def bsb(self):
        return self.payment_settings.bsb if self.payment_settings else None

    @bsb.setter
    def bsb(self, value):
        self._ensure_payment_settings()
        self.payment_settings.bsb = value

    @property
    def bsb_plain(self):
        return self.payment_settings.bsb_plain if self.payment_settings else None

    @bsb_plain.setter
    def bsb_plain(self, value):
        self._ensure_payment_settings()
        self.payment_settings.bsb_plain = value

    @property
    def de_user_id(self):
        return self.payment_settings.de_user_id if self.payment_settings else None

    @de_user_id.setter
    def de_user_id(self, value):
        self._ensure_payment_settings()
        self.payment_settings.de_user_id = value

    @property
    def de_user_id_plain(self):
        return self.payment_settings.de_user_id_plain if self.payment_settings else None

    @de_user_id_plain.setter
    def de_user_id_plain(self, value):
        self._ensure_payment_settings()
        self.payment_settings.de_user_id_plain = value

    @property
    def de_user_id_masked(self):
        return self.payment_settings.de_user_id_masked if self.payment_settings else None

    @property
    def payment_terms(self):
        return self.payment_settings.payment_terms if self.payment_settings else 14

    @payment_terms.setter
    def payment_terms(self, value):
        self._ensure_payment_settings()
        self.payment_settings.payment_terms = value

    # --- Internal helpers ---

    def _ensure_profile(self):
        """Lazy-create the UserProfile row if it doesn't exist (so setters work)."""
        if not self.profile:
            # Import here to avoid circular import at module load time
            from app.models.user_profile import UserProfile
            self.profile = UserProfile(user_id=self.id)

    def _ensure_payment_settings(self):
        """Lazy-create the UserPaymentSettings row if it doesn't exist."""
        if not self.payment_settings:
            from app.models.user_payment_settings import UserPaymentSettings
            self.payment_settings = UserPaymentSettings(user_id=self.id)

    # --- Serialisation ---

    def to_dict(self):
        """API response shape. Masked bank details, plaintext profile fields."""
        return {
            'id': self.id,
            'email': self.email,
            'display_name': self.display_name,
            'role': self.role,
            'is_active': self.is_active,
            'country': self.country,
            'business_name': self.business_name,
            'abn': self.abn,
            'address_line1': self.address_line1,
            'address_line2': self.address_line2,
            'city': self.city,
            'state': self.state,
            'postcode': self.postcode,
            'address_country': self.address_country,
            'contact_email': self.contact_email,
            'contact_number': self.contact_number,
            'logo_path': self.logo_path,
            'bank_name': self.bank_name,
            'account_name': self.account_name,
            'account_number': mask_account(self.account_number_plain),
            'bsb': mask_bsb(self.bsb_plain),
            'de_user_id': self.de_user_id_masked,
            'payment_terms': self.payment_terms,
            'api_key_set': bool(self.payment_settings and self.payment_settings.api_key),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
