from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
import os

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(100), nullable=False)
    surname = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    def set_password(self, password):
        """Hash and set password"""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Verify password"""
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'first_name': self.first_name,
            'surname': self.surname,
            'email': self.email,
            'phone_number': self.phone_number,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class UserProfile(db.Model):
    __tablename__ = 'user_profiles'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    avatar_url = db.Column(db.String(500))
    role = db.Column(db.String(50))
    bio = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref='profile')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'display_name': self.display_name,
            'avatar_url': self.avatar_url,
            'role': self.role,
            'bio': self.bio,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }


class SoundPack(db.Model):
    """Sound pack containing multiple tracks"""
    __tablename__ = 'sound_packs'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    creator_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    cover_url = db.Column(db.String(500))
    genre = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text)
    tags = db.Column(db.String(500))  # Comma-separated tags
    token_cost = db.Column(db.Integer, nullable=False, default=10)
    play_count = db.Column(db.Integer, default=0)
    download_count = db.Column(db.Integer, default=0)
    track_count = db.Column(db.Integer, default=0)
    is_featured = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    creator = db.relationship('User', backref='sound_packs')
    tracks = db.relationship('Beat', backref='sound_pack', lazy=True)

    def to_dict(self, include_tracks=False):
        # Token cost = 1 token per track
        calculated_token_cost = self.track_count if self.track_count else len([t for t in self.tracks if t.is_active])
        data = {
            'id': self.id,
            'name': self.name,
            'creator_id': self.creator_id,
            'creator_name': f"{self.creator.first_name} {self.creator.surname}" if self.creator else None,
            'cover_url': self.cover_url,
            'genre': self.genre,
            'description': self.description,
            'tags': self.tags.split(',') if self.tags else [],
            'token_cost': calculated_token_cost,  # 1 token per track
            'token_per_track': 1,  # Fixed price per individual track
            'play_count': self.play_count,
            'download_count': self.download_count,
            'track_count': self.track_count,
            'is_featured': self.is_featured,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
        if include_tracks:
            data['tracks'] = [track.to_dict() for track in self.tracks if track.is_active]
        return data


class Beat(db.Model):
    """Individual track/beat - can be standalone or part of a sound pack"""
    __tablename__ = 'beats'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    creator_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    sound_pack_id = db.Column(db.Integer, db.ForeignKey('sound_packs.id'), nullable=True)  # Optional pack reference
    audio_url = db.Column(db.String(500), nullable=False)
    cover_url = db.Column(db.String(500))
    genre = db.Column(db.String(50), nullable=False)
    bpm = db.Column(db.Integer)
    key = db.Column(db.String(10))
    tags = db.Column(db.String(500))  # Comma-separated tags
    token_cost = db.Column(db.Integer, nullable=False, default=5)
    play_count = db.Column(db.Integer, default=0)
    download_count = db.Column(db.Integer, default=0)
    track_number = db.Column(db.Integer)  # Order within sound pack
    is_featured = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    creator = db.relationship('User', backref='beats')

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'creator_id': self.creator_id,
            'creator_name': f"{self.creator.first_name} {self.creator.surname}" if self.creator else None,
            'sound_pack_id': self.sound_pack_id,
            'audio_url': self.audio_url,
            'cover_url': self.cover_url or (self.sound_pack.cover_url if self.sound_pack else None),
            'genre': self.genre,
            'bpm': self.bpm,
            'key': self.key,
            'tags': self.tags.split(',') if self.tags else [],
            'token_cost': 1,  # Fixed: 1 token per track
            'play_count': self.play_count,
            'download_count': self.download_count,
            'track_number': self.track_number,
            'is_featured': self.is_featured,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Wallet(db.Model):
    """User token wallet for Beatpax"""
    __tablename__ = 'wallets'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    balance = db.Column(db.Integer, nullable=False, default=50)  # New user bonus
    total_spent = db.Column(db.Integer, nullable=False, default=0)
    total_earned = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    user = db.relationship('User', backref='wallet')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'balance': self.balance,
            'total_spent': self.total_spent,
            'total_earned': self.total_earned,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Transaction(db.Model):
    """Token transaction history for Beatpax"""
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    transaction_type = db.Column(db.String(20), nullable=False)  # 'purchase', 'spend', 'earn', 'bonus'
    amount = db.Column(db.Integer, nullable=False)  # Positive for credits, negative for debits
    balance_after = db.Column(db.Integer, nullable=False)
    reference_type = db.Column(db.String(50))  # 'beat_download', 'beat_sale', 'token_purchase', 'signup_bonus'
    reference_id = db.Column(db.Integer)  # ID of related beat or package
    description = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationship
    user = db.relationship('User', backref='transactions')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'transaction_type': self.transaction_type,
            'amount': self.amount,
            'balance_after': self.balance_after,
            'reference_type': self.reference_type,
            'reference_id': self.reference_id,
            'description': self.description,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class UserBeatLibrary(db.Model):
    """User's purchased/downloaded beats"""
    __tablename__ = 'user_beat_library'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    beat_id = db.Column(db.Integer, db.ForeignKey('beats.id'), nullable=False)
    tokens_spent = db.Column(db.Integer, nullable=False)
    purchased_at = db.Column(db.DateTime, default=datetime.utcnow)
    downloaded_at = db.Column(db.DateTime)
    download_count = db.Column(db.Integer, default=0)

    # Relationships
    user = db.relationship('User', backref='beat_library')
    beat = db.relationship('Beat', backref='purchases')

    # Unique constraint to prevent duplicate purchases
    __table_args__ = (db.UniqueConstraint('user_id', 'beat_id', name='unique_user_beat'),)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'beat_id': self.beat_id,
            'beat': self.beat.to_dict() if self.beat else None,
            'tokens_spent': self.tokens_spent,
            'purchased_at': self.purchased_at.isoformat() if self.purchased_at else None,
            'downloaded_at': self.downloaded_at.isoformat() if self.downloaded_at else None,
            'download_count': self.download_count
        }


class UserLikedTrack(db.Model):
    """User's liked tracks for Beatpax"""
    __tablename__ = 'user_liked_tracks'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    beat_id = db.Column(db.Integer, db.ForeignKey('beats.id'), nullable=False)
    liked_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref='liked_tracks')
    beat = db.relationship('Beat', backref='likes')

    # Unique constraint to prevent duplicate likes
    __table_args__ = (db.UniqueConstraint('user_id', 'beat_id', name='unique_user_liked_beat'),)

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'beat_id': self.beat_id,
            'beat': self.beat.to_dict() if self.beat else None,
            'liked_at': self.liked_at.isoformat() if self.liked_at else None
        }


class CuratedPack(db.Model):
    """User-curated sound pack for sharing"""
    __tablename__ = 'curated_packs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    cover_url = db.Column(db.String(500))  # Album art
    recipient_name = db.Column(db.String(100))  # "For Sarah"
    share_code = db.Column(db.String(8), unique=True, nullable=False)
    is_free = db.Column(db.Boolean, default=False)  # If true, no tokens required
    view_count = db.Column(db.Integer, default=0)
    download_count = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship('User', backref='curated_packs')
    tracks = db.relationship('CuratedPackTrack', backref='curated_pack', lazy=True, cascade='all, delete-orphan')

    def to_dict(self, include_tracks=False):
        data = {
            'id': self.id,
            'user_id': self.user_id,
            'creator_name': f"{self.user.first_name} {self.user.surname}" if self.user else None,
            'name': self.name,
            'description': self.description,
            'cover_url': self.cover_url,
            'recipient_name': self.recipient_name,
            'share_code': self.share_code,
            'share_url': f"/curated/{self.share_code}",
            'is_free': self.is_free,
            'view_count': self.view_count,
            'download_count': self.download_count,
            'track_count': len(self.tracks),
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
        if include_tracks:
            data['tracks'] = [t.to_dict() for t in sorted(self.tracks, key=lambda x: x.track_order)]
        return data


class CuratedPackTrack(db.Model):
    """Track in a curated pack"""
    __tablename__ = 'curated_pack_tracks'

    id = db.Column(db.Integer, primary_key=True)
    curated_pack_id = db.Column(db.Integer, db.ForeignKey('curated_packs.id'), nullable=False)
    beat_id = db.Column(db.Integer, db.ForeignKey('beats.id'), nullable=False)
    track_order = db.Column(db.Integer, default=0)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    beat = db.relationship('Beat')

    # Unique constraint to prevent duplicate tracks in same pack
    __table_args__ = (db.UniqueConstraint('curated_pack_id', 'beat_id', name='unique_curated_pack_beat'),)

    def to_dict(self):
        return {
            'id': self.id,
            'curated_pack_id': self.curated_pack_id,
            'beat_id': self.beat_id,
            'beat': self.beat.to_dict() if self.beat else None,
            'track_order': self.track_order,
            'added_at': self.added_at.isoformat() if self.added_at else None
        }
