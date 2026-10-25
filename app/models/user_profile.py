"""UserProfile model — business profile fields, 1:1 with public.users via user_id FK.

Phase 1b (schema-mode install). The auth source (public.users) is created in
Phase 0 / migrations/001. The User class in app/models/user.py composes
auth (public.users) + profile (this table) + payment settings
(accounts.user_payment_settings) via SQLAlchemy relationships.

Columns:
    user_id          BIGINT PK + FK to public.users(id) (or accounts.users when
                     USERS_TABLE defaults). The FK target is the fully-
                     qualified <USERS_TABLE>.id from app.config.
    country          user's locale (2-letter ISO: 'AU', 'US') — default 'AU'
    business_name    the legal entity name (e.g. "Acme Pty Ltd")
    abn              Australian Business Number (or local equivalent)
    address_line1/2  structured address
    city, state, postcode, address_country
                     address's country name (e.g. 'Australia', 'United States')
    contact_email    customer-facing contact (e.g. accounts@acme.com)
    contact_number   phone number
    logo_path        path to uploaded logo (relative to upload_folder)

Pattern modelled on the the sibling mail project 1b refactor (3ae27a7) but with the
1:3 split (mail has 1 users table; accounts has 3: public.users +
user_profiles + user_payment_settings).
"""
from app.models import db, get_utc_now
from config import accounts_config


class UserProfile(db.Model):
    __tablename__ = 'user_profiles'
    # The schema follows ACCOUNTS_SCHEMA (default 'public'), so the same
    # code installs into a dedicated schema when one is configured.
    # The FK target follows USERS_TABLE -- see accounts_config.users_fk.
    __table_args__ = {'schema': accounts_config.accounts_schema}

    user_id = db.Column(
        db.BigInteger,
        db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'),
        primary_key=True,
    )
    country = db.Column(db.String(50), nullable=False, default='AU')
    business_name = db.Column(db.String(255), nullable=True)
    abn = db.Column(db.String(20), nullable=True)
    address_line1 = db.Column(db.String(255), nullable=True)
    address_line2 = db.Column(db.String(255), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(50), nullable=True)
    postcode = db.Column(db.String(20), nullable=True)
    address_country = db.Column(db.String(100), nullable=False, default='Australia')
    contact_email = db.Column(db.String(255), nullable=True)
    contact_number = db.Column(db.String(50), nullable=True)
    logo_path = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now
    )

    def to_dict(self):
        """Plain dict representation (no Fernet decryption needed — this
        table has no encrypted columns)."""
        return {
            'user_id': self.user_id,
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
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
