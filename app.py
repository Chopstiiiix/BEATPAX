import os
import time
import uuid
import secrets
import mimetypes
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
from functools import wraps
from werkzeug.utils import secure_filename
from PIL import Image
from models import db, User, UserProfile, SoundPack, Beat, Wallet, Transaction, UserBeatLibrary, UserLikedTrack, CuratedPack, CuratedPackTrack, StemProject, StemFile
import blob_storage

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "your-secret-key-here-change-in-production")

# Configure database URI
# Supports both PostgreSQL (production) and SQLite (local development)
db_url = os.environ.get('DATABASE_URL', '')

if db_url.startswith('postgres://'):
    # Fix for SQLAlchemy - it requires postgresql:// not postgres://
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
elif db_url.startswith('postgresql://'):
    # Already in correct format for PostgreSQL
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
elif db_url.startswith('file:'):
    # Convert Prisma format to Flask-SQLAlchemy format (SQLite)
    db_path = db_url.replace('file:', '')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
else:
    # Default: Use SQLite database in project directory (local development)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///beatpax.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Connection pooling for PostgreSQL (serverless optimization)
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgresql://'):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_size': 3,
        'pool_recycle': 60,  # Recycle connections every 60 seconds
        'pool_pre_ping': True,  # Check connection health before use
        'max_overflow': 5,
        'pool_timeout': 30,
        'connect_args': {
            'connect_timeout': 10,
            'keepalives': 1,
            'keepalives_idle': 30,
            'keepalives_interval': 10,
            'keepalives_count': 5
        }
    }
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size for beat uploads
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')

CORS(app)

# Error handler for file too large
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': 'File too large. Maximum size is 50MB'}), 413

# Initialize database
db.init_app(app)
migrate = Migrate(app, db)

def db_commit_with_retry(max_retries=3):
    """Commit database changes with retry logic for connection errors."""
    for attempt in range(max_retries):
        try:
            db.session.commit()
            return True
        except Exception as e:
            error_str = str(e).lower()
            if 'ssl' in error_str or 'connection' in error_str or 'closed' in error_str:
                print(f"DB commit attempt {attempt + 1} failed: {e}")
                db.session.rollback()
                if attempt < max_retries - 1:
                    # Close and recreate the connection
                    db.session.remove()
                    time.sleep(0.5)  # Brief delay before retry
                    continue
            raise
    return False

def generate_unique_filename(original_filename):
    """Generate a unique filename while preserving extension"""
    name, ext = os.path.splitext(secure_filename(original_filename))
    unique_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return f"{name}_{timestamp}_{unique_id}{ext}"

# =============================================================================
# Auth
# =============================================================================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('authenticated'):
            # Return JSON error for API endpoints
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('authenticated'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Admin access required'}), 403
            return redirect(url_for('beatpax'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/register', methods=['GET', 'POST'])
def register():
    if session.get('authenticated'):
        return redirect(url_for('beatpax'))

    if request.method == 'POST':
        first_name = request.form.get('first_name', '').strip()
        surname = request.form.get('surname', '').strip()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone_number = request.form.get('phone_number', '').strip()
        age = request.form.get('age', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        # Validation
        if not all([first_name, surname, email, phone_number, age, password, confirm_password]):
            return render_template('register.html', error='All fields are required')

        if password != confirm_password:
            return render_template('register.html', error='Passwords do not match')

        if len(password) < 8:
            return render_template('register.html', error='Password must be at least 8 characters long')

        import re
        if username:
            if len(username) < 3 or len(username) > 30:
                return render_template('register.html', error='Username must be 3-30 characters')
            if not re.match(r'^[a-zA-Z0-9_]+$', username):
                return render_template('register.html', error='Username can only contain letters, numbers, and underscores')
            existing_username = User.query.filter(db.func.lower(User.username) == username.lower()).first()
            if existing_username:
                return render_template('register.html', error='Username is already taken')

        try:
            age_int = int(age)
            if age_int < 13 or age_int > 120:
                return render_template('register.html', error='Please enter a valid age (13-120)')
        except ValueError:
            return render_template('register.html', error='Please enter a valid age')

        # Check if email already exists
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            return render_template('register.html', error='Email already registered. Please login instead.')

        # Create new user
        try:
            new_user = User(
                first_name=first_name,
                surname=surname,
                username=username or None,
                email=email,
                phone_number=phone_number,
                age=age_int
            )
            new_user.set_password(password)

            db.session.add(new_user)
            db.session.flush()  # Get the user ID

            # Create wallet with signup bonus
            wallet = Wallet(user_id=new_user.id, balance=50)
            db.session.add(wallet)

            # Record the signup bonus transaction
            bonus_transaction = Transaction(
                user_id=new_user.id,
                transaction_type='bonus',
                amount=50,
                balance_after=50,
                reference_type='signup_bonus',
                description='Welcome bonus tokens'
            )
            db.session.add(bonus_transaction)

            db.session.commit()

            # Log user in
            session['authenticated'] = True
            session['user_id'] = new_user.id
            session['user_name'] = f"{new_user.first_name} {new_user.surname}"
            session['user_handle'] = f"@{new_user.username}" if new_user.username else None
            session['session_id'] = str(uuid.uuid4())

            return redirect(url_for('beatpax'))
        except Exception as e:
            db.session.rollback()
            print(f"Registration error: {e}")
            return render_template('register.html', error='An error occurred during registration. Please try again.')

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('authenticated'):
        return redirect(url_for('beatpax'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not email or not password:
            return render_template('login.html', error='Email and password are required')

        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            # Update last login
            user.last_login = datetime.utcnow()
            db.session.commit()

            # Log user in
            session['authenticated'] = True
            session['user_id'] = user.id
            session['user_name'] = f"{user.first_name} {user.surname}"
            session['user_handle'] = f"@{user.username}" if user.username else None
            session['session_id'] = str(uuid.uuid4())
            session['is_admin'] = user.is_admin or False

            return redirect(url_for('beatpax'))
        else:
            return render_template('login.html', error='Invalid email or password')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# =============================================================================
# Beatpax Helpers
# =============================================================================

ALLOWED_AUDIO_EXTENSIONS = {'mp3', 'wav', 'flac', 'm4a'}
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
MAX_AUDIO_SIZE = 50 * 1024 * 1024  # 50MB
MAX_IMAGE_SIZE = 5 * 1024 * 1024   # 5MB

def allowed_audio_file(filename):
    """Check if audio file extension is allowed"""
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_AUDIO_EXTENSIONS

def allowed_image_file(filename):
    """Check if image file extension is allowed"""
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_IMAGE_EXTENSIONS

def get_or_create_wallet(user_id):
    """Get user's wallet or create one if it doesn't exist"""
    wallet = Wallet.query.filter_by(user_id=user_id).first()
    if not wallet:
        wallet = Wallet(user_id=user_id, balance=50)
        db.session.add(wallet)
        # Record the signup bonus
        bonus = Transaction(
            user_id=user_id,
            transaction_type='bonus',
            amount=50,
            balance_after=50,
            reference_type='signup_bonus',
            description='Welcome bonus tokens'
        )
        db.session.add(bonus)
        db.session.commit()
    return wallet

def generate_share_code():
    """Generate a unique 8-character share code"""
    while True:
        code = secrets.token_urlsafe(6)[:8]
        if not CuratedPack.query.filter_by(share_code=code).first() and \
           not StemProject.query.filter_by(share_code=code).first():
            return code

# =============================================================================
# Page Routes
# =============================================================================

@app.route('/')
@login_required
def beatpax():
    """Main Beatpax catalog page"""
    user_id = session.get('user_id')
    wallet = get_or_create_wallet(user_id)
    return render_template('beatpax.html', wallet_balance=wallet.balance)


@app.route('/library')
@login_required
def beatpax_library():
    """User's downloaded beats library"""
    user_id = session.get('user_id')
    wallet = get_or_create_wallet(user_id)
    library = UserBeatLibrary.query.filter_by(user_id=user_id).order_by(
        UserBeatLibrary.purchased_at.desc()
    ).all()
    return render_template('beatpax.html',
                          wallet_balance=wallet.balance,
                          page='library',
                          library=library)


@app.route('/admin')
@admin_required
def admin_panel():
    """Admin dashboard page"""
    return render_template('admin.html')


@app.route('/wallet')
@login_required
def beatpax_wallet():
    """Token balance and purchase page"""
    user_id = session.get('user_id')
    wallet = get_or_create_wallet(user_id)
    transactions = Transaction.query.filter_by(user_id=user_id).order_by(
        Transaction.created_at.desc()
    ).limit(20).all()
    return render_template('beatpax.html',
                          wallet_balance=wallet.balance,
                          page='wallet',
                          wallet=wallet,
                          transactions=transactions)


# =============================================================================
# API Endpoints
# =============================================================================

@app.route('/api/explore')
@login_required
def beatpax_explore():
    """Get catalog data for explore page - returns sound packs"""
    user_id = session.get('user_id')

    try:
        from datetime import timedelta

        # Featured sound pack
        hero_pack = SoundPack.query.filter_by(is_featured=True, is_active=True).first()
        if not hero_pack:
            # Fall back to most downloaded pack
            hero_pack = SoundPack.query.filter_by(is_active=True).order_by(
                SoundPack.download_count.desc()
            ).first()

        # New releases - latest sound packs
        new_releases = SoundPack.query.filter_by(is_active=True).order_by(
            SoundPack.created_at.desc()
        ).limit(12).all()

        # Trending - most downloaded packs
        trending = SoundPack.query.filter_by(is_active=True).order_by(
            SoundPack.download_count.desc()
        ).limit(6).all()

        # Fresh - recent packs (last week)
        fresh_cutoff = datetime.utcnow() - timedelta(days=7)
        fresh = SoundPack.query.filter(
            SoundPack.is_active == True,
            SoundPack.created_at >= fresh_cutoff
        ).order_by(SoundPack.created_at.desc()).limit(6).all()

        # Top creators (by pack count and downloads)
        top_creators = db.session.query(
            User.id, User.first_name, User.surname,
            db.func.count(SoundPack.id).label('pack_count'),
            db.func.sum(SoundPack.download_count).label('total_downloads')
        ).join(SoundPack).filter(SoundPack.is_active == True).group_by(
            User.id
        ).order_by(db.desc('total_downloads')).limit(6).all()

        # User's library beat IDs (to show owned status)
        owned_beat_ids = [lib.beat_id for lib in UserBeatLibrary.query.filter_by(
            user_id=user_id
        ).all()]

        # Get owned pack IDs (if user owns any track from a pack, they own the pack)
        owned_pack_ids = list(set([
            beat.sound_pack_id for beat in Beat.query.join(UserBeatLibrary).filter(
                UserBeatLibrary.user_id == user_id,
                Beat.sound_pack_id != None
            ).all()
        ]))

        return jsonify({
            'hero': hero_pack.to_dict(include_tracks=True) if hero_pack else None,
            'new_releases': [p.to_dict(include_tracks=True) for p in new_releases],
            'trending': [p.to_dict(include_tracks=True) for p in trending],
            'fresh': [p.to_dict(include_tracks=True) for p in fresh],
            'top_creators': [{
                'id': c.id,
                'name': f"{c.first_name} {c.surname}",
                'pack_count': c.pack_count,
                'total_downloads': c.total_downloads or 0
            } for c in top_creators],
            'owned_beat_ids': owned_beat_ids,
            'owned_pack_ids': owned_pack_ids
        })
    except Exception as e:
        print(f"Error in beatpax explore: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to load catalog'}), 500


@app.route('/api/beats')
@login_required
def beatpax_beats():
    """Get sound packs with optional filtering"""
    genre = request.args.get('genre')
    search = request.args.get('search')
    sort = request.args.get('sort', 'newest')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    try:
        query = SoundPack.query.filter_by(is_active=True)

        if genre and genre != 'all':
            query = query.filter_by(genre=genre)

        if search:
            search_term = f"%{search}%"
            query = query.filter(
                db.or_(
                    SoundPack.name.ilike(search_term),
                    SoundPack.tags.ilike(search_term)
                )
            )

        if sort == 'newest':
            query = query.order_by(SoundPack.created_at.desc())
        elif sort == 'popular':
            query = query.order_by(SoundPack.download_count.desc())
        elif sort == 'trending':
            query = query.order_by(SoundPack.play_count.desc())
        elif sort == 'price_low':
            query = query.order_by(SoundPack.token_cost.asc())
        elif sort == 'price_high':
            query = query.order_by(SoundPack.token_cost.desc())

        paginated = query.paginate(page=page, per_page=per_page, error_out=False)

        return jsonify({
            'packs': [p.to_dict(include_tracks=True) for p in paginated.items],
            'total': paginated.total,
            'pages': paginated.pages,
            'current_page': page
        })
    except Exception as e:
        print(f"Error fetching packs: {e}")
        return jsonify({'error': 'Failed to fetch packs'}), 500


@app.route('/api/upload-config', methods=['GET'])
@login_required
def beatpax_upload_config():
    """Get upload configuration for client-side uploads"""
    is_vercel = os.environ.get('VERCEL') == 'true' or os.environ.get('VERCEL') == '1'
    blob_configured = blob_storage.is_blob_configured()
    blob_token = os.environ.get('BLOB_READ_WRITE_TOKEN', '') if blob_configured else ''

    # Log for debugging
    print(f"Upload config: is_vercel={is_vercel}, blob_configured={blob_configured}, token_present={bool(blob_token)}")

    # Determine max file size
    if is_vercel and blob_configured:
        max_size = 50 * 1024 * 1024  # 50MB with client upload
    elif is_vercel:
        max_size = 4 * 1024 * 1024   # 4MB without blob (server limit)
    else:
        max_size = 50 * 1024 * 1024  # 50MB local

    return jsonify({
        'is_production': is_vercel,
        'blob_configured': blob_configured,
        'blob_token': blob_token if is_vercel else '',
        'max_file_size': max_size,
        'server_upload_available': not is_vercel or not blob_configured,
        'message': 'Blob storage not configured. Large file uploads disabled.' if (is_vercel and not blob_configured) else None
    })


@app.route('/api/create-beat', methods=['POST'])
@login_required
def beatpax_create_beat():
    """Create a beat record from already-uploaded files (for client-side uploads)"""
    user_id = session.get('user_id')

    try:
        data = request.get_json()

        title = (data.get('title') or '').strip()
        genre = (data.get('genre') or '').strip()
        audio_url = (data.get('audio_url') or '').strip()
        cover_url = (data.get('cover_url') or '').strip() or None
        bpm = data.get('bpm')
        key = (data.get('key') or '').strip()
        tags = (data.get('tags') or '').strip()
        token_cost = data.get('token_cost', 5)

        # Validate required fields
        if not title:
            return jsonify({'error': 'Title is required'}), 400
        if not genre:
            return jsonify({'error': 'Genre is required'}), 400
        if not audio_url:
            return jsonify({'error': 'Audio URL is required'}), 400

        # Validate URL is from Vercel Blob (allow test URLs in development)
        is_valid_url = (
            audio_url.startswith('https://') and
            ('blob.vercel-storage.com' in audio_url or 'vercel-storage.com' in audio_url)
        )
        if not is_valid_url:
            return jsonify({'error': 'Invalid audio URL. Must be a Vercel Blob URL.'}), 400

        # Validate token cost
        token_cost = max(3, min(20, int(token_cost)))

        # Create beat record
        beat = Beat(
            title=title,
            creator_id=user_id,
            audio_url=audio_url,
            cover_url=cover_url,
            genre=genre,
            bpm=int(bpm) if bpm else None,
            key=key,
            tags=tags,
            token_cost=token_cost
        )

        db.session.add(beat)
        db.session.commit()

        return jsonify({
            'success': True,
            'beat': beat.to_dict(),
            'message': 'Beat created successfully!'
        })

    except Exception as e:
        db.session.rollback()
        print(f"Error creating beat: {e}")
        return jsonify({'error': 'Failed to create beat'}), 500


@app.route('/api/create-soundpack', methods=['POST'])
@login_required
def beatpax_create_soundpack():
    """Create a sound pack with multiple tracks (for client-side uploads)"""
    user_id = session.get('user_id')

    try:
        data = request.get_json()

        # Sound pack info (shared)
        pack_name = (data.get('pack_name') or '').strip()
        genre = (data.get('genre') or '').strip()
        cover_url = (data.get('cover_url') or '').strip() or None
        description = (data.get('description') or '').strip()
        tags = (data.get('tags') or '').strip()
        tracks_data = data.get('tracks', [])

        # Validate required fields
        if not pack_name:
            return jsonify({'error': 'Pack name is required'}), 400
        if not genre:
            return jsonify({'error': 'Genre is required'}), 400
        if not tracks_data or len(tracks_data) == 0:
            return jsonify({'error': 'At least one track is required'}), 400

        # Validate all tracks have audio URLs
        for i, track in enumerate(tracks_data):
            if not track.get('audio_url'):
                return jsonify({'error': f'Track {i+1} is missing audio URL'}), 400
            if not track.get('title'):
                return jsonify({'error': f'Track {i+1} is missing title'}), 400

        # Token cost = 1 token per track (calculated automatically)
        track_count = len(tracks_data)
        token_cost = track_count  # 1 token per track

        # Create sound pack
        sound_pack = SoundPack(
            name=pack_name,
            creator_id=user_id,
            cover_url=cover_url,
            genre=genre,
            description=description,
            tags=tags,
            token_cost=token_cost,
            track_count=track_count
        )
        db.session.add(sound_pack)
        db.session.flush()  # Get the pack ID

        # Create tracks
        created_tracks = []
        for i, track_data in enumerate(tracks_data):
            track = Beat(
                title=track_data.get('title', f'Track {i+1}'),
                creator_id=user_id,
                sound_pack_id=sound_pack.id,
                audio_url=track_data.get('audio_url'),
                cover_url=cover_url,  # Use pack cover
                genre=genre,
                bpm=track_data.get('bpm'),
                key=track_data.get('key', ''),
                tags=tags,
                token_cost=0,  # Individual tracks in pack are free (pack has cost)
                track_number=i + 1
            )
            db.session.add(track)
            created_tracks.append(track)

        db.session.commit()

        return jsonify({
            'success': True,
            'sound_pack': sound_pack.to_dict(include_tracks=True),
            'message': f'Sound pack "{pack_name}" created with {len(created_tracks)} tracks!'
        })

    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        print(f"Error creating sound pack: {e}")
        return jsonify({'error': f'Failed to create sound pack: {str(e)}'}), 500


@app.route('/api/soundpacks', methods=['GET'])
@login_required
def get_soundpacks():
    """Get all sound packs"""
    try:
        genre = request.args.get('genre')
        query = SoundPack.query.filter_by(is_active=True)

        if genre and genre != 'all':
            query = query.filter_by(genre=genre)

        packs = query.order_by(SoundPack.created_at.desc()).limit(20).all()
        return jsonify({
            'sound_packs': [pack.to_dict(include_tracks=True) for pack in packs]
        })
    except Exception as e:
        print(f"Error fetching sound packs: {e}")
        return jsonify({'error': 'Failed to fetch sound packs'}), 500


@app.route('/api/upload-audio', methods=['POST'])
@login_required
def beatpax_upload_audio():
    """Upload just an audio file and return the URL"""
    try:
        print(f"[UPLOAD-AUDIO] Starting audio upload, blob configured: {blob_storage.is_blob_configured()}")

        audio_file = request.files.get('audio')
        if not audio_file or not audio_file.filename:
            print("[UPLOAD-AUDIO] No audio file in request")
            return jsonify({'error': 'Audio file is required'}), 400

        print(f"[UPLOAD-AUDIO] File: {audio_file.filename}, Content-Type: {audio_file.content_type}")

        if not allowed_audio_file(audio_file.filename):
            return jsonify({'error': 'Invalid audio format. Allowed: MP3, WAV, FLAC, M4A'}), 400

        # Check file size
        audio_file.seek(0, 2)
        audio_size = audio_file.tell()
        audio_file.seek(0)
        print(f"[UPLOAD-AUDIO] File size: {audio_size} bytes")

        if audio_size > MAX_AUDIO_SIZE:
            return jsonify({'error': f'Audio file too large. Maximum {MAX_AUDIO_SIZE // (1024*1024)}MB'}), 400

        # Generate unique filename
        audio_filename = generate_unique_filename(audio_file.filename)
        audio_mime = audio_file.content_type or mimetypes.guess_type(audio_file.filename)[0] or 'audio/mpeg'

        # Upload to Blob storage or local
        if blob_storage.is_blob_configured():
            audio_path = blob_storage.generate_blob_path('packs/audio', audio_filename)
            print(f"[UPLOAD-AUDIO] Uploading to blob: {audio_path}")
            audio_url, _ = blob_storage.upload_file(audio_file, audio_path, audio_mime)
            print(f"[UPLOAD-AUDIO] Upload successful: {audio_url}")
        else:
            audio_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'packs', 'audio')
            os.makedirs(audio_dir, exist_ok=True)
            audio_local_path = os.path.join(audio_dir, audio_filename)
            audio_file.save(audio_local_path)
            audio_url = f'/uploads/packs/audio/{audio_filename}'
            print(f"[UPLOAD-AUDIO] Saved locally: {audio_url}")

        return jsonify({
            'success': True,
            'url': audio_url,
            'audio_url': audio_url  # Keep for backwards compatibility
        })

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"[UPLOAD-AUDIO] Error: {e}\n{error_details}")
        return jsonify({'error': f'Failed to upload audio: {str(e)[:100]}'}), 500


@app.route('/api/upload-image', methods=['POST'])
@login_required
def beatpax_upload_image():
    """Upload just an image file (for covers) and return the URL"""
    try:
        print(f"[UPLOAD-IMAGE] Starting image upload, blob configured: {blob_storage.is_blob_configured()}")

        image_file = request.files.get('image') or request.files.get('cover')
        if not image_file or not image_file.filename:
            print("[UPLOAD-IMAGE] No image file in request")
            return jsonify({'error': 'Image file is required'}), 400

        print(f"[UPLOAD-IMAGE] File: {image_file.filename}, Content-Type: {image_file.content_type}")

        if not allowed_image_file(image_file.filename):
            return jsonify({'error': 'Invalid image format. Allowed: JPG, PNG, GIF, WebP'}), 400

        # Check file size (max 10MB for images)
        image_file.seek(0, 2)
        image_size = image_file.tell()
        image_file.seek(0)
        print(f"[UPLOAD-IMAGE] File size: {image_size} bytes")

        if image_size > 10 * 1024 * 1024:
            return jsonify({'error': 'Image too large. Maximum 10MB'}), 400

        # Generate unique filename
        image_filename = generate_unique_filename(image_file.filename)
        image_mime = image_file.content_type or mimetypes.guess_type(image_file.filename)[0] or 'image/jpeg'

        # Upload to Blob storage or local
        if blob_storage.is_blob_configured():
            image_path = blob_storage.generate_blob_path('covers', image_filename)
            print(f"[UPLOAD-IMAGE] Uploading to blob: {image_path}")
            image_url, _ = blob_storage.upload_file(image_file, image_path, image_mime)
            print(f"[UPLOAD-IMAGE] Upload successful: {image_url}")
        else:
            image_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'covers')
            os.makedirs(image_dir, exist_ok=True)
            image_local_path = os.path.join(image_dir, image_filename)
            image_file.save(image_local_path)
            image_url = f'/uploads/covers/{image_filename}'
            print(f"[UPLOAD-IMAGE] Saved locally: {image_url}")

        return jsonify({
            'success': True,
            'url': image_url
        })

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"[UPLOAD-IMAGE] Error: {e}\n{error_details}")
        return jsonify({'error': f'Failed to upload image: {str(e)[:100]}'}), 500


@app.route('/api/upload', methods=['POST'])
@login_required
def beatpax_upload():
    """Upload a new beat"""
    user_id = session.get('user_id')

    try:
        # Get form data
        title = request.form.get('title', '').strip()
        genre = request.form.get('genre', '').strip()
        bpm = request.form.get('bpm', type=int)
        key = request.form.get('key', '').strip()
        tags = request.form.get('tags', '').strip()
        token_cost = request.form.get('token_cost', 5, type=int)

        # Validate required fields
        if not title:
            return jsonify({'error': 'Title is required'}), 400
        if not genre:
            return jsonify({'error': 'Genre is required'}), 400

        # Get audio file
        audio_file = request.files.get('audio')
        if not audio_file or not audio_file.filename:
            return jsonify({'error': 'Audio file is required'}), 400

        if not allowed_audio_file(audio_file.filename):
            return jsonify({'error': 'Invalid audio format. Allowed: MP3, WAV, FLAC, M4A'}), 400

        # Check file size
        audio_file.seek(0, 2)
        audio_size = audio_file.tell()
        audio_file.seek(0)

        if audio_size > MAX_AUDIO_SIZE:
            return jsonify({'error': 'Audio file too large. Maximum 50MB'}), 400

        # Generate unique filename
        audio_filename = generate_unique_filename(audio_file.filename)

        # Get MIME type
        audio_mime = audio_file.content_type or mimetypes.guess_type(audio_file.filename)[0] or 'audio/mpeg'

        # Upload audio to Blob storage
        if blob_storage.is_blob_configured():
            audio_path = blob_storage.generate_blob_path('beats/audio', audio_filename)
            audio_url, _ = blob_storage.upload_file(audio_file, audio_path, audio_mime)
        else:
            # Fallback to local storage
            audio_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'beats', 'audio')
            os.makedirs(audio_dir, exist_ok=True)
            audio_local_path = os.path.join(audio_dir, audio_filename)
            audio_file.save(audio_local_path)
            audio_url = f'/uploads/beats/audio/{audio_filename}'

        # Handle cover image (optional)
        cover_url = None
        cover_file = request.files.get('cover')
        if cover_file and cover_file.filename:
            if allowed_image_file(cover_file.filename):
                cover_file.seek(0, 2)
                cover_size = cover_file.tell()
                cover_file.seek(0)

                if cover_size <= MAX_IMAGE_SIZE:
                    cover_filename = generate_unique_filename(cover_file.filename)
                    cover_mime = cover_file.content_type or mimetypes.guess_type(cover_file.filename)[0] or 'image/jpeg'

                    if blob_storage.is_blob_configured():
                        cover_path = blob_storage.generate_blob_path('beats/covers', cover_filename)
                        cover_url, _ = blob_storage.upload_file(cover_file, cover_path, cover_mime)
                    else:
                        cover_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'beats', 'covers')
                        os.makedirs(cover_dir, exist_ok=True)
                        cover_local_path = os.path.join(cover_dir, cover_filename)
                        cover_file.save(cover_local_path)
                        cover_url = f'/uploads/beats/covers/{cover_filename}'

        # Validate token cost
        token_cost = max(3, min(20, token_cost))  # Between 3 and 20

        # Create beat record
        beat = Beat(
            title=title,
            creator_id=user_id,
            audio_url=audio_url,
            cover_url=cover_url,
            genre=genre,
            bpm=bpm,
            key=key,
            tags=tags,
            token_cost=token_cost
        )

        db.session.add(beat)
        db.session.commit()

        return jsonify({
            'success': True,
            'beat': beat.to_dict(),
            'message': 'Beat uploaded successfully!'
        })

    except Exception as e:
        db.session.rollback()
        import traceback
        error_details = str(e)
        traceback.print_exc()

        # Check for specific error types
        if 'Blob' in error_details or 'BLOB' in error_details:
            return jsonify({'error': 'Storage service error. Please try again or use a smaller file.'}), 500
        elif 'size' in error_details.lower() or 'large' in error_details.lower():
            return jsonify({'error': 'File too large. Please use a file under 4MB for web uploads.'}), 413
        elif 'timeout' in error_details.lower():
            return jsonify({'error': 'Upload timed out. Please try again with a smaller file.'}), 504

        print(f"Error uploading beat: {error_details}")
        return jsonify({'error': f'Upload failed: {error_details[:100]}'}), 500


@app.route('/api/beats/<int:beat_id>/play', methods=['POST'])
@login_required
def beatpax_play(beat_id):
    """Record a play (free action)"""
    try:
        beat = Beat.query.get(beat_id)
        if not beat or not beat.is_active:
            return jsonify({'error': 'Beat not found'}), 404

        beat.play_count += 1
        db.session.commit()

        return jsonify({
            'success': True,
            'play_count': beat.play_count
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error recording play: {e}")
        return jsonify({'error': 'Failed to record play'}), 500


@app.route('/api/beats/<int:beat_id>/download', methods=['POST'])
@login_required
def beatpax_download(beat_id):
    """Download a beat (spend tokens)"""
    user_id = session.get('user_id')

    try:
        beat = Beat.query.get(beat_id)
        if not beat or not beat.is_active:
            return jsonify({'error': 'Beat not found'}), 404

        # Check if already owned
        existing = UserBeatLibrary.query.filter_by(
            user_id=user_id, beat_id=beat_id
        ).first()

        if existing:
            # Already owned - just increment download count
            existing.download_count += 1
            existing.downloaded_at = datetime.utcnow()
            db.session.commit()
            return jsonify({
                'success': True,
                'already_owned': True,
                'audio_url': beat.audio_url,
                'message': 'You already own this beat!'
            })

        # Fixed cost: 1 token per beat
        token_cost = 1

        # Check wallet balance
        wallet = get_or_create_wallet(user_id)
        if wallet.balance < token_cost:
            return jsonify({
                'error': 'Insufficient tokens',
                'required': token_cost,
                'balance': wallet.balance
            }), 400

        # Deduct tokens from buyer
        wallet.balance -= token_cost
        wallet.total_spent += token_cost

        # Record buyer's transaction
        buyer_transaction = Transaction(
            user_id=user_id,
            transaction_type='spend',
            amount=-token_cost,
            balance_after=wallet.balance,
            reference_type='beat_download',
            reference_id=beat_id,
            description=f'Downloaded: {beat.title}'
        )
        db.session.add(buyer_transaction)

        # Credit creator (80% of token cost)
        creator_earnings = max(1, int(token_cost * 0.8))  # Minimum 1 token for creator
        creator_wallet = get_or_create_wallet(beat.creator_id)
        creator_wallet.balance += creator_earnings
        creator_wallet.total_earned += creator_earnings

        # Record creator's transaction
        creator_transaction = Transaction(
            user_id=beat.creator_id,
            transaction_type='earn',
            amount=creator_earnings,
            balance_after=creator_wallet.balance,
            reference_type='beat_sale',
            reference_id=beat_id,
            description=f'Sale: {beat.title}'
        )
        db.session.add(creator_transaction)

        # Add to user's library
        library_entry = UserBeatLibrary(
            user_id=user_id,
            beat_id=beat_id,
            tokens_spent=token_cost,
            downloaded_at=datetime.utcnow(),
            download_count=1
        )
        db.session.add(library_entry)

        # Increment beat download count
        beat.download_count += 1

        db.session.commit()

        return jsonify({
            'success': True,
            'audio_url': beat.audio_url,
            'tokens_spent': token_cost,
            'new_balance': wallet.balance,
            'message': f'Downloaded! {token_cost} token spent.'
        })

    except Exception as e:
        db.session.rollback()
        print(f"Error downloading beat: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to download beat'}), 500


# Token API Endpoints
@app.route('/api/tokens/balance')
@login_required
def get_token_balance():
    """Get user's token balance"""
    user_id = session.get('user_id')
    wallet = get_or_create_wallet(user_id)
    return jsonify({
        'balance': wallet.balance,
        'total_spent': wallet.total_spent,
        'total_earned': wallet.total_earned
    })


@app.route('/api/tokens/purchase', methods=['POST'])
@login_required
def purchase_tokens():
    """Purchase tokens (stub - would integrate payment)"""
    user_id = session.get('user_id')
    data = request.get_json()
    package = data.get('package')

    # Token packages (stub pricing)
    packages = {
        '100': {'tokens': 100, 'price': 4.99},
        '250': {'tokens': 250, 'price': 9.99},
        '500': {'tokens': 500, 'price': 17.99},
        '1000': {'tokens': 1000, 'price': 29.99}
    }

    if package not in packages:
        return jsonify({'error': 'Invalid package'}), 400

    pkg = packages[package]

    try:
        wallet = get_or_create_wallet(user_id)
        wallet.balance += pkg['tokens']

        transaction = Transaction(
            user_id=user_id,
            transaction_type='purchase',
            amount=pkg['tokens'],
            balance_after=wallet.balance,
            reference_type='token_purchase',
            description=f"Purchased {pkg['tokens']} tokens"
        )
        db.session.add(transaction)
        db.session.commit()

        return jsonify({
            'success': True,
            'tokens_added': pkg['tokens'],
            'new_balance': wallet.balance,
            'message': f"Added {pkg['tokens']} tokens!"
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error purchasing tokens: {e}")
        return jsonify({'error': 'Failed to purchase tokens'}), 500


@app.route('/api/tokens/transactions')
@login_required
def get_transactions():
    """Get user's transaction history"""
    user_id = session.get('user_id')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    try:
        paginated = Transaction.query.filter_by(user_id=user_id).order_by(
            Transaction.created_at.desc()
        ).paginate(page=page, per_page=per_page, error_out=False)

        return jsonify({
            'transactions': [t.to_dict() for t in paginated.items],
            'total': paginated.total,
            'pages': paginated.pages,
            'current_page': page
        })
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return jsonify({'error': 'Failed to fetch transactions'}), 500


@app.route('/api/library')
@login_required
def get_user_library():
    """Get user's beat library"""
    user_id = session.get('user_id')

    try:
        library = UserBeatLibrary.query.filter_by(user_id=user_id).order_by(
            UserBeatLibrary.purchased_at.desc()
        ).all()

        return jsonify({
            'library': [entry.to_dict() for entry in library],
            'count': len(library)
        })
    except Exception as e:
        print(f"Error fetching library: {e}")
        return jsonify({'error': 'Failed to fetch library'}), 500


@app.route('/api/my-beats')
@login_required
def get_my_beats():
    """Get beats uploaded by the current user"""
    user_id = session.get('user_id')

    try:
        beats = Beat.query.filter_by(creator_id=user_id).order_by(
            Beat.created_at.desc()
        ).all()

        return jsonify({
            'beats': [b.to_dict() for b in beats],
            'count': len(beats)
        })
    except Exception as e:
        print(f"Error fetching my beats: {e}")
        return jsonify({'error': 'Failed to fetch beats'}), 500


@app.route('/api/my-uploads')
@login_required
def get_my_uploads():
    """Get all sound packs uploaded by the current user"""
    user_id = session.get('user_id')

    try:
        # Get sound packs
        packs = SoundPack.query.filter_by(creator_id=user_id, is_active=True).order_by(
            SoundPack.created_at.desc()
        ).all()

        # Get standalone beats (not part of a pack)
        standalone_beats = Beat.query.filter_by(
            creator_id=user_id,
            sound_pack_id=None,
            is_active=True
        ).order_by(Beat.created_at.desc()).all()

        return jsonify({
            'sound_packs': [pack.to_dict(include_tracks=True) for pack in packs],
            'standalone_beats': [b.to_dict() for b in standalone_beats],
            'total_packs': len(packs),
            'total_standalone': len(standalone_beats)
        })
    except Exception as e:
        print(f"Error fetching my uploads: {e}")
        return jsonify({'error': 'Failed to fetch uploads'}), 500


@app.route('/api/soundpacks/<int:pack_id>', methods=['PUT'])
@login_required
def update_soundpack(pack_id):
    """Update a sound pack"""
    user_id = session.get('user_id')

    try:
        pack = SoundPack.query.get(pack_id)
        if not pack:
            return jsonify({'error': 'Sound pack not found'}), 404

        if pack.creator_id != user_id:
            return jsonify({'error': 'You can only edit your own uploads'}), 403

        data = request.get_json()

        # Update fields
        if 'name' in data:
            pack.name = data['name'].strip()
        if 'genre' in data:
            pack.genre = data['genre'].strip()
        if 'description' in data:
            pack.description = data['description'].strip()
        if 'tags' in data:
            pack.tags = data['tags'].strip()
        if 'cover_url' in data:
            pack.cover_url = data['cover_url']
        # Note: token_cost is automatically calculated as 1 per track

        db.session.commit()

        return jsonify({
            'success': True,
            'sound_pack': pack.to_dict(include_tracks=True),
            'message': 'Sound pack updated successfully!'
        })

    except Exception as e:
        db.session.rollback()
        print(f"Error updating sound pack: {e}")
        return jsonify({'error': 'Failed to update sound pack'}), 500


@app.route('/api/soundpacks/<int:pack_id>', methods=['DELETE'])
@login_required
def delete_soundpack(pack_id):
    """Delete a sound pack and all its tracks"""
    user_id = session.get('user_id')

    try:
        pack = SoundPack.query.get(pack_id)
        if not pack:
            return jsonify({'error': 'Sound pack not found'}), 404

        if pack.creator_id != user_id:
            return jsonify({'error': 'You can only delete your own uploads'}), 403

        # Soft delete - mark as inactive
        pack.is_active = False
        for track in pack.tracks:
            track.is_active = False

        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Sound pack deleted successfully!'
        })

    except Exception as e:
        db.session.rollback()
        print(f"Error deleting sound pack: {e}")
        return jsonify({'error': 'Failed to delete sound pack'}), 500


@app.route('/api/tracks/<int:track_id>', methods=['PUT'])
@login_required
def update_track(track_id):
    """Update an individual track"""
    user_id = session.get('user_id')

    try:
        track = Beat.query.get(track_id)
        if not track:
            return jsonify({'error': 'Track not found'}), 404

        if track.creator_id != user_id:
            return jsonify({'error': 'You can only edit your own uploads'}), 403

        data = request.get_json()

        # Update fields
        if 'title' in data:
            track.title = data['title'].strip()
        if 'bpm' in data:
            track.bpm = int(data['bpm']) if data['bpm'] else None
        if 'key' in data:
            track.key = data['key'].strip()

        db.session.commit()

        return jsonify({
            'success': True,
            'track': track.to_dict(),
            'message': 'Track updated successfully!'
        })

    except Exception as e:
        db.session.rollback()
        print(f"Error updating track: {e}")
        return jsonify({'error': 'Failed to update track'}), 500


@app.route('/api/tracks/<int:track_id>', methods=['DELETE'])
@login_required
def delete_track(track_id):
    """Delete an individual track"""
    user_id = session.get('user_id')

    try:
        track = Beat.query.get(track_id)
        if not track:
            return jsonify({'error': 'Track not found'}), 404

        if track.creator_id != user_id:
            return jsonify({'error': 'You can only delete your own uploads'}), 403

        # Soft delete
        track.is_active = False

        # Update pack track count if part of a pack
        if track.sound_pack_id:
            pack = SoundPack.query.get(track.sound_pack_id)
            if pack:
                pack.track_count = Beat.query.filter_by(
                    sound_pack_id=pack.id, is_active=True
                ).count()

        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Track deleted successfully!'
        })

    except Exception as e:
        db.session.rollback()
        print(f"Error deleting track: {e}")
        return jsonify({'error': 'Failed to delete track'}), 500


# =============================================================================
# Public Share Routes
# =============================================================================

@app.route('/pack/<int:pack_id>')
def beatpax_share_pack(pack_id):
    """Public page to view a shared sound pack - no login required"""
    try:
        pack = SoundPack.query.get(pack_id)
        if not pack or not pack.is_active:
            return render_template('beatpax_share.html', pack=None, error='Sound pack not found')

        # Get pack data with tracks
        pack_data = pack.to_dict(include_tracks=True)

        # Check if user is logged in for download capability
        is_logged_in = session.get('authenticated', False)
        user_id = session.get('user_id')
        wallet_balance = 0

        if is_logged_in and user_id:
            wallet = Wallet.query.filter_by(user_id=user_id).first()
            wallet_balance = wallet.balance if wallet else 0

        return render_template('beatpax_share.html',
                               pack=pack_data,
                               is_logged_in=is_logged_in,
                               wallet_balance=wallet_balance,
                               error=None)
    except Exception as e:
        print(f"Error loading shared pack: {e}")
        return render_template('beatpax_share.html', pack=None, error='Failed to load sound pack')


@app.route('/api/pack/<int:pack_id>/public')
def beatpax_public_pack(pack_id):
    """Public API to get sound pack data - no login required"""
    try:
        pack = SoundPack.query.get(pack_id)
        if not pack or not pack.is_active:
            return jsonify({'error': 'Sound pack not found'}), 404

        return jsonify({
            'pack': pack.to_dict(include_tracks=True)
        })
    except Exception as e:
        print(f"Error fetching public pack: {e}")
        return jsonify({'error': 'Failed to fetch sound pack'}), 500


# =============================================================================
# Liked Tracks API Endpoints
# =============================================================================

@app.route('/api/liked')
@login_required
def get_liked_tracks():
    """Get user's liked tracks with full beat data"""
    user_id = session.get('user_id')

    try:
        liked = UserLikedTrack.query.filter_by(user_id=user_id).order_by(
            UserLikedTrack.liked_at.desc()
        ).all()

        return jsonify({
            'tracks': [entry.to_dict() for entry in liked],
            'count': len(liked)
        })
    except Exception as e:
        print(f"Error fetching liked tracks: {e}")
        return jsonify({'error': 'Failed to fetch liked tracks'}), 500


@app.route('/api/liked/ids')
@login_required
def get_liked_track_ids():
    """Get just the IDs of liked tracks for UI state"""
    user_id = session.get('user_id')

    try:
        liked = UserLikedTrack.query.filter_by(user_id=user_id).all()
        return jsonify({
            'liked_ids': [entry.beat_id for entry in liked]
        })
    except Exception as e:
        print(f"Error fetching liked track IDs: {e}")
        return jsonify({'error': 'Failed to fetch liked track IDs'}), 500


@app.route('/api/beats/<int:beat_id>/like', methods=['POST'])
@login_required
def toggle_track_like(beat_id):
    """Toggle like on a track"""
    user_id = session.get('user_id')

    try:
        beat = Beat.query.get(beat_id)
        if not beat or not beat.is_active:
            return jsonify({'error': 'Track not found'}), 404

        # Check if already liked
        existing = UserLikedTrack.query.filter_by(
            user_id=user_id, beat_id=beat_id
        ).first()

        if existing:
            # Unlike
            db.session.delete(existing)
            db.session.commit()
            return jsonify({
                'liked': False,
                'message': 'Removed from liked'
            })
        else:
            # Like
            new_like = UserLikedTrack(user_id=user_id, beat_id=beat_id)
            db.session.add(new_like)
            db.session.commit()
            return jsonify({
                'liked': True,
                'message': 'Added to liked!'
            })
    except Exception as e:
        db.session.rollback()
        print(f"Error toggling like: {e}")
        return jsonify({'error': 'Failed to toggle like'}), 500


# =============================================================================
# Curated Packs API Endpoints
# =============================================================================

@app.route('/api/curated', methods=['GET'])
@login_required
def get_curated_packs():
    """Get user's curated packs"""
    user_id = session.get('user_id')

    try:
        packs = CuratedPack.query.filter_by(
            user_id=user_id, is_active=True
        ).order_by(CuratedPack.created_at.desc()).all()

        return jsonify({
            'packs': [pack.to_dict(include_tracks=True) for pack in packs],
            'count': len(packs)
        })
    except Exception as e:
        print(f"Error fetching curated packs: {e}")
        return jsonify({'error': 'Failed to fetch curated packs'}), 500


@app.route('/api/curated', methods=['POST'])
@login_required
def create_curated_pack():
    """Create a new curated pack"""
    user_id = session.get('user_id')

    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'Pack name is required'}), 400

        track_ids = data.get('track_ids', [])
        if not track_ids:
            return jsonify({'error': 'At least one track is required'}), 400

        # Verify all tracks exist
        for track_id in track_ids:
            beat = Beat.query.get(track_id)
            if not beat or not beat.is_active:
                return jsonify({'error': f'Track {track_id} not found'}), 404

        # Create the pack
        pack = CuratedPack(
            user_id=user_id,
            name=name,
            description=data.get('description', '').strip() or None,
            cover_url=data.get('cover_url') or None,
            recipient_name=data.get('recipient_name', '').strip() or None,
            share_code=generate_share_code(),
            is_free=data.get('is_free', False)
        )
        db.session.add(pack)
        db.session.flush()  # Get the pack ID

        # Add tracks
        for order, track_id in enumerate(track_ids):
            track_entry = CuratedPackTrack(
                curated_pack_id=pack.id,
                beat_id=track_id,
                track_order=order
            )
            db.session.add(track_entry)

        db.session.commit()

        return jsonify({
            'success': True,
            'pack': pack.to_dict(include_tracks=True),
            'message': 'Curated pack created!'
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error creating curated pack: {e}")
        return jsonify({'error': 'Failed to create curated pack'}), 500


@app.route('/api/curated/<int:pack_id>', methods=['PUT'])
@login_required
def update_curated_pack(pack_id):
    """Update a curated pack"""
    user_id = session.get('user_id')

    try:
        pack = CuratedPack.query.get(pack_id)
        if not pack or not pack.is_active:
            return jsonify({'error': 'Curated pack not found'}), 404

        if pack.user_id != user_id:
            return jsonify({'error': 'You can only edit your own packs'}), 403

        data = request.get_json()

        if 'name' in data:
            pack.name = data['name'].strip()
        if 'description' in data:
            pack.description = data['description'].strip() or None
        if 'recipient_name' in data:
            pack.recipient_name = data['recipient_name'].strip() or None
        if 'is_free' in data:
            pack.is_free = data['is_free']

        # Update tracks if provided
        if 'track_ids' in data:
            track_ids = data['track_ids']
            if not track_ids:
                return jsonify({'error': 'At least one track is required'}), 400

            # Verify all tracks exist
            for track_id in track_ids:
                beat = Beat.query.get(track_id)
                if not beat or not beat.is_active:
                    return jsonify({'error': f'Track {track_id} not found'}), 404

            # Remove existing tracks
            CuratedPackTrack.query.filter_by(curated_pack_id=pack.id).delete()

            # Add new tracks
            for order, track_id in enumerate(track_ids):
                track_entry = CuratedPackTrack(
                    curated_pack_id=pack.id,
                    beat_id=track_id,
                    track_order=order
                )
                db.session.add(track_entry)

        db.session.commit()

        return jsonify({
            'success': True,
            'pack': pack.to_dict(include_tracks=True),
            'message': 'Curated pack updated!'
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error updating curated pack: {e}")
        return jsonify({'error': 'Failed to update curated pack'}), 500


@app.route('/api/curated/<int:pack_id>', methods=['DELETE'])
@login_required
def delete_curated_pack(pack_id):
    """Delete a curated pack"""
    user_id = session.get('user_id')

    try:
        pack = CuratedPack.query.get(pack_id)
        if not pack:
            return jsonify({'error': 'Curated pack not found'}), 404

        if pack.user_id != user_id:
            return jsonify({'error': 'You can only delete your own packs'}), 403

        # Soft delete
        pack.is_active = False
        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Curated pack deleted!'
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting curated pack: {e}")
        return jsonify({'error': 'Failed to delete curated pack'}), 500


@app.route('/curated/<share_code>')
def view_curated_pack(share_code):
    """Public page to view a shared curated pack"""
    try:
        pack = CuratedPack.query.filter_by(share_code=share_code, is_active=True).first()
        if not pack:
            return render_template('beatpax_curated.html', pack=None, error='Pack not found')

        # Increment view count
        pack.view_count += 1
        db.session.commit()

        # Check if user is logged in
        is_logged_in = session.get('authenticated', False)
        user_id = session.get('user_id')
        wallet_balance = 0

        if is_logged_in and user_id:
            wallet = Wallet.query.filter_by(user_id=user_id).first()
            wallet_balance = wallet.balance if wallet else 0

        pack_data = pack.to_dict(include_tracks=True)

        return render_template('beatpax_curated.html',
                               pack=pack_data,
                               is_logged_in=is_logged_in,
                               wallet_balance=wallet_balance,
                               error=None)
    except Exception as e:
        db.session.rollback()
        print(f"Error viewing curated pack: {e}")
        return render_template('beatpax_curated.html', pack=None, error='Failed to load pack')


@app.route('/api/curated/<share_code>/download', methods=['POST'])
def download_curated_pack(share_code):
    """Download tracks from a curated pack"""
    try:
        pack = CuratedPack.query.filter_by(share_code=share_code, is_active=True).first()
        if not pack:
            return jsonify({'error': 'Pack not found'}), 404

        # Get track IDs from request (optional - can download specific tracks)
        data = request.get_json() or {}
        requested_track_ids = data.get('track_ids')

        # Get all tracks in the pack
        pack_tracks = CuratedPackTrack.query.filter_by(curated_pack_id=pack.id).all()
        track_ids = [t.beat_id for t in pack_tracks]

        # If specific tracks requested, filter to those
        if requested_track_ids:
            track_ids = [tid for tid in track_ids if tid in requested_track_ids]

        if not track_ids:
            return jsonify({'error': 'No tracks to download'}), 400

        # If pack is free, allow download without login
        if pack.is_free:
            pack.download_count += 1
            db.session.commit()

            # Return track download URLs
            tracks = Beat.query.filter(Beat.id.in_(track_ids), Beat.is_active == True).all()
            return jsonify({
                'success': True,
                'is_free': True,
                'tracks': [{'id': t.id, 'title': t.title, 'audio_url': t.audio_url} for t in tracks],
                'message': 'Enjoy your free tracks!'
            })

        # For paid packs, require login
        if not session.get('authenticated'):
            return jsonify({'error': 'Login required to download', 'require_login': True}), 401

        user_id = session.get('user_id')

        # Check wallet balance
        wallet = Wallet.query.filter_by(user_id=user_id).first()
        if not wallet:
            wallet = Wallet(user_id=user_id, balance=50)
            db.session.add(wallet)

        # Calculate cost (1 token per track, skip already owned)
        tracks_to_download = []
        for track_id in track_ids:
            existing = UserBeatLibrary.query.filter_by(
                user_id=user_id, beat_id=track_id
            ).first()
            if not existing:
                tracks_to_download.append(track_id)

        if not tracks_to_download:
            return jsonify({
                'success': True,
                'message': 'You already own all these tracks!',
                'tracks_added': 0
            })

        total_cost = len(tracks_to_download)  # 1 token per track

        if wallet.balance < total_cost:
            return jsonify({
                'error': f'Insufficient tokens. Need {total_cost}, have {wallet.balance}',
                'balance': wallet.balance,
                'cost': total_cost
            }), 400

        # Process purchase
        wallet.balance -= total_cost
        wallet.total_spent += total_cost

        for track_id in tracks_to_download:
            library_entry = UserBeatLibrary(
                user_id=user_id,
                beat_id=track_id,
                tokens_spent=1
            )
            db.session.add(library_entry)

            # Record transaction
            transaction = Transaction(
                user_id=user_id,
                transaction_type='spend',
                amount=-1,
                balance_after=wallet.balance,
                reference_type='curated_pack_download',
                reference_id=track_id,
                description=f'Downloaded from curated pack: {pack.name}'
            )
            db.session.add(transaction)

        pack.download_count += 1
        db.session.commit()

        return jsonify({
            'success': True,
            'tracks_added': len(tracks_to_download),
            'tokens_spent': total_cost,
            'new_balance': wallet.balance,
            'message': f'Added {len(tracks_to_download)} tracks to your library!'
        })

    except Exception as e:
        db.session.rollback()
        print(f"Error downloading curated pack: {e}")
        return jsonify({'error': 'Failed to download pack'}), 500


# =============================================================================
# Profile API Endpoints
# =============================================================================

@app.route('/api/profile', methods=['GET'])
@login_required
def get_profile():
    """Get current user's editable profile fields"""
    user_id = session.get('user_id')
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404
        from datetime import timedelta
        username_cooldown_days = 30
        username_locked = False
        username_days_left = 0
        if user.username_changed_at:
            delta = datetime.utcnow() - user.username_changed_at
            if delta < timedelta(days=username_cooldown_days):
                username_locked = True
                username_days_left = username_cooldown_days - delta.days

        return jsonify({
            'first_name': user.first_name,
            'surname': user.surname,
            'username': user.username,
            'email': user.email,
            'phone_number': user.phone_number,
            'age': user.age,
            'username_locked': username_locked,
            'username_days_left': username_days_left
        })
    except Exception as e:
        print(f"Error fetching profile: {e}")
        return jsonify({'error': 'Failed to fetch profile'}), 500


@app.route('/api/profile', methods=['PUT'])
@login_required
def update_profile():
    """Update current user's profile fields"""
    user_id = session.get('user_id')
    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        data = request.get_json()

        if 'first_name' in data:
            val = data['first_name'].strip()
            if not val:
                return jsonify({'error': 'First name cannot be empty'}), 400
            user.first_name = val

        if 'surname' in data:
            val = data['surname'].strip()
            if not val:
                return jsonify({'error': 'Surname cannot be empty'}), 400
            user.surname = val

        if 'username' in data:
            import re
            from datetime import timedelta
            val = data['username'].strip() if data['username'] else ''
            old_username = user.username or ''
            # Only enforce cooldown if the username is actually changing
            if val.lower() != old_username.lower():
                if user.username_changed_at:
                    delta = datetime.utcnow() - user.username_changed_at
                    if delta < timedelta(days=30):
                        days_left = 30 - delta.days
                        return jsonify({'error': f'Username can only be changed once every 30 days. {days_left} days remaining.'}), 400
                if val:
                    if len(val) < 3 or len(val) > 30:
                        return jsonify({'error': 'Username must be 3-30 characters'}), 400
                    if not re.match(r'^[a-zA-Z0-9_]+$', val):
                        return jsonify({'error': 'Username can only contain letters, numbers, and underscores'}), 400
                    existing = User.query.filter(db.func.lower(User.username) == val.lower(), User.id != user_id).first()
                    if existing:
                        return jsonify({'error': 'Username is already taken'}), 400
                    user.username = val
                else:
                    user.username = None
                user.username_changed_at = datetime.utcnow()

        if 'phone_number' in data:
            user.phone_number = data['phone_number'].strip()

        if 'age' in data:
            try:
                age_int = int(data['age'])
                if age_int < 13 or age_int > 120:
                    return jsonify({'error': 'Please enter a valid age (13-120)'}), 400
                user.age = age_int
            except (ValueError, TypeError):
                return jsonify({'error': 'Please enter a valid age'}), 400

        db.session.commit()

        session['user_name'] = f"{user.first_name} {user.surname}"
        session['user_handle'] = f"@{user.username}" if user.username else None

        return jsonify({
            'first_name': user.first_name,
            'surname': user.surname,
            'username': user.username,
            'email': user.email,
            'phone_number': user.phone_number,
            'age': user.age
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error updating profile: {e}")
        return jsonify({'error': 'Failed to update profile'}), 500


# =============================================================================
# Stems API Endpoints
# =============================================================================

@app.route('/api/stems', methods=['GET'])
@login_required
def get_stem_projects():
    """Get user's stem projects"""
    user_id = session.get('user_id')

    try:
        projects = StemProject.query.filter_by(
            user_id=user_id, is_active=True
        ).order_by(StemProject.created_at.desc()).all()

        return jsonify({
            'projects': [p.to_dict(include_files=True) for p in projects],
            'count': len(projects)
        })
    except Exception as e:
        print(f"Error fetching stem projects: {e}")
        return jsonify({'error': 'Failed to fetch stem projects'}), 500


@app.route('/api/stems', methods=['POST'])
@login_required
def create_stem_project():
    """Create a new stem project"""
    user_id = session.get('user_id')

    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'Project name is required'}), 400

        files_data = data.get('files', [])
        if not files_data:
            return jsonify({'error': 'At least one stem file is required'}), 400

        project = StemProject(
            user_id=user_id,
            name=name,
            description=data.get('description', '').strip() or None,
            share_code=generate_share_code()
        )
        db.session.add(project)
        db.session.flush()

        for order, file_info in enumerate(files_data):
            stem_file = StemFile(
                stem_project_id=project.id,
                file_name=file_info.get('file_name', ''),
                audio_url=file_info.get('audio_url', ''),
                file_size=file_info.get('file_size', 0),
                stem_type=file_info.get('stem_type', 'other'),
                track_order=order
            )
            db.session.add(stem_file)

        db.session.commit()

        return jsonify({
            'success': True,
            'project': project.to_dict(include_files=True),
            'message': 'Stem project created!'
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error creating stem project: {e}")
        return jsonify({'error': 'Failed to create stem project'}), 500


@app.route('/api/stems/<int:project_id>', methods=['DELETE'])
@login_required
def delete_stem_project(project_id):
    """Delete a stem project"""
    user_id = session.get('user_id')

    try:
        project = StemProject.query.get(project_id)
        if not project:
            return jsonify({'error': 'Stem project not found'}), 404

        if project.user_id != user_id:
            return jsonify({'error': 'You can only delete your own projects'}), 403

        project.is_active = False
        db.session.commit()

        return jsonify({
            'success': True,
            'message': 'Stem project deleted!'
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting stem project: {e}")
        return jsonify({'error': 'Failed to delete stem project'}), 500


@app.route('/api/stems/<int:project_id>/send-email', methods=['POST'])
@login_required
def stem_send_email(project_id):
    """Generate mailto URL for sharing a stem project"""
    user_id = session.get('user_id')

    try:
        project = StemProject.query.get(project_id)
        if not project or not project.is_active:
            return jsonify({'error': 'Stem project not found'}), 404

        if project.user_id != user_id:
            return jsonify({'error': 'You can only share your own projects'}), 403

        data = request.get_json()
        recipient_email = data.get('email', '').strip()
        if not recipient_email:
            return jsonify({'error': 'Recipient email is required'}), 400

        share_url = f"{request.host_url.rstrip('/')}/stems/{project.share_code}"
        subject = f"Check out my stems: {project.name}"
        body = f"Hey! I wanted to share my stem project \"{project.name}\" with you.\n\nListen here: {share_url}\n\nSent via Beatpax"

        import urllib.parse
        mailto_url = f"mailto:{recipient_email}?subject={urllib.parse.quote(subject)}&body={urllib.parse.quote(body)}"

        return jsonify({
            'success': True,
            'mailto_url': mailto_url
        })
    except Exception as e:
        print(f"Error generating email for stem project: {e}")
        return jsonify({'error': 'Failed to generate email'}), 500


@app.route('/stems/<share_code>')
def view_stem_project(share_code):
    """Public page to view a shared stem project — redirects to home for MVP"""
    try:
        project = StemProject.query.filter_by(share_code=share_code, is_active=True).first()
        if not project:
            return redirect('/')

        project.view_count += 1
        db.session.commit()

        return redirect('/')
    except Exception as e:
        db.session.rollback()
        print(f"Error viewing stem project: {e}")
        return redirect('/')


# =============================================================================
# Admin API Endpoints
# =============================================================================

@app.route('/api/admin/stats')
@admin_required
def admin_stats():
    """Dashboard analytics"""
    try:
        from datetime import timedelta
        week_ago = datetime.utcnow() - timedelta(days=7)

        total_users = User.query.count()
        new_users_7d = User.query.filter(User.created_at >= week_ago).count()
        total_beats = Beat.query.count()
        total_packs = SoundPack.query.count()
        total_curated = CuratedPack.query.count()
        total_stems = StemProject.query.count()
        total_downloads = db.session.query(db.func.coalesce(db.func.sum(Beat.download_count), 0)).scalar()
        total_plays = db.session.query(db.func.coalesce(db.func.sum(Beat.play_count), 0)).scalar()
        total_transactions = Transaction.query.count()

        recent_signups = User.query.order_by(User.created_at.desc()).limit(5).all()

        return jsonify({
            'total_users': total_users,
            'new_users_7d': new_users_7d,
            'total_beats': total_beats,
            'total_packs': total_packs,
            'total_curated': total_curated,
            'total_stems': total_stems,
            'total_downloads': total_downloads,
            'total_plays': total_plays,
            'total_transactions': total_transactions,
            'recent_signups': [{
                'id': u.id,
                'name': f"{u.first_name} {u.surname}",
                'email': u.email,
                'created_at': u.created_at.isoformat() if u.created_at else None
            } for u in recent_signups]
        })
    except Exception as e:
        print(f"Error fetching admin stats: {e}")
        return jsonify({'error': 'Failed to fetch stats'}), 500


@app.route('/api/admin/users')
@admin_required
def admin_users():
    """Paginated user list with search"""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        search = request.args.get('search', '').strip()

        query = User.query

        if search:
            search_term = f"%{search}%"
            query = query.filter(
                db.or_(
                    User.first_name.ilike(search_term),
                    User.surname.ilike(search_term),
                    User.email.ilike(search_term)
                )
            )

        query = query.order_by(User.created_at.desc())
        paginated = query.paginate(page=page, per_page=per_page, error_out=False)

        users = []
        for u in paginated.items:
            beat_count = Beat.query.filter_by(creator_id=u.id).count()
            pack_count = SoundPack.query.filter_by(creator_id=u.id).count()
            wallet = Wallet.query.filter_by(user_id=u.id).first()
            users.append({
                'id': u.id,
                'name': f"{u.first_name} {u.surname}",
                'email': u.email,
                'age': u.age,
                'is_admin': u.is_admin,
                'created_at': u.created_at.isoformat() if u.created_at else None,
                'last_login': u.last_login.isoformat() if u.last_login else None,
                'beat_count': beat_count,
                'pack_count': pack_count,
                'wallet_balance': wallet.balance if wallet else 0
            })

        return jsonify({
            'users': users,
            'total': paginated.total,
            'pages': paginated.pages,
            'current_page': page
        })
    except Exception as e:
        print(f"Error fetching admin users: {e}")
        return jsonify({'error': 'Failed to fetch users'}), 500


@app.route('/api/admin/users/<int:user_id>/toggle-admin', methods=['POST'])
@admin_required
def admin_toggle_admin(user_id):
    """Toggle admin status for a user"""
    try:
        if user_id == session.get('user_id'):
            return jsonify({'error': 'Cannot change your own admin status'}), 400

        user = User.query.get(user_id)
        if not user:
            return jsonify({'error': 'User not found'}), 404

        user.is_admin = not user.is_admin
        db.session.commit()

        return jsonify({
            'success': True,
            'is_admin': user.is_admin,
            'message': f"{'Promoted' if user.is_admin else 'Demoted'} {user.first_name} {user.surname}"
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error toggling admin: {e}")
        return jsonify({'error': 'Failed to toggle admin status'}), 500


@app.route('/api/admin/content')
@admin_required
def admin_content():
    """Content list with type filter, search, pagination"""
    try:
        content_type = request.args.get('type', 'beats')
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        search = request.args.get('search', '').strip()

        allowed_types = ['beats', 'packs', 'curated', 'stems']
        if content_type not in allowed_types:
            return jsonify({'error': f'Invalid type. Allowed: {", ".join(allowed_types)}'}), 400

        # Get counts for all types
        counts = {
            'beats': Beat.query.count(),
            'packs': SoundPack.query.count(),
            'curated': CuratedPack.query.count(),
            'stems': StemProject.query.count()
        }

        items = []

        if content_type == 'beats':
            query = Beat.query
            if search:
                query = query.filter(Beat.title.ilike(f"%{search}%"))
            paginated = query.order_by(Beat.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
            for b in paginated.items:
                items.append({
                    'id': b.id, 'name': b.title, 'creator_name': f"{b.creator.first_name} {b.creator.surname}" if b.creator else 'Unknown',
                    'genre': b.genre, 'bpm': b.bpm, 'play_count': b.play_count, 'download_count': b.download_count,
                    'is_active': b.is_active, 'is_featured': b.is_featured,
                    'created_at': b.created_at.isoformat() if b.created_at else None
                })
        elif content_type == 'packs':
            query = SoundPack.query
            if search:
                query = query.filter(SoundPack.name.ilike(f"%{search}%"))
            paginated = query.order_by(SoundPack.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
            for p in paginated.items:
                items.append({
                    'id': p.id, 'name': p.name, 'creator_name': f"{p.creator.first_name} {p.creator.surname}" if p.creator else 'Unknown',
                    'genre': p.genre, 'track_count': p.track_count, 'download_count': p.download_count,
                    'is_active': p.is_active, 'is_featured': p.is_featured,
                    'created_at': p.created_at.isoformat() if p.created_at else None
                })
        elif content_type == 'curated':
            query = CuratedPack.query
            if search:
                query = query.filter(CuratedPack.name.ilike(f"%{search}%"))
            paginated = query.order_by(CuratedPack.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
            for c in paginated.items:
                items.append({
                    'id': c.id, 'name': c.name, 'creator_name': f"{c.user.first_name} {c.user.surname}" if c.user else 'Unknown',
                    'share_code': c.share_code, 'view_count': c.view_count, 'download_count': c.download_count,
                    'track_count': len(c.tracks), 'is_active': c.is_active,
                    'created_at': c.created_at.isoformat() if c.created_at else None
                })
        elif content_type == 'stems':
            query = StemProject.query
            if search:
                query = query.filter(StemProject.name.ilike(f"%{search}%"))
            paginated = query.order_by(StemProject.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
            for s in paginated.items:
                items.append({
                    'id': s.id, 'name': s.name, 'creator_name': f"{s.user.first_name} {s.user.surname}" if s.user else 'Unknown',
                    'file_count': len(s.files), 'view_count': s.view_count, 'download_count': s.download_count,
                    'is_active': s.is_active,
                    'created_at': s.created_at.isoformat() if s.created_at else None
                })

        return jsonify({
            'items': items,
            'total': paginated.total,
            'pages': paginated.pages,
            'current_page': page,
            'counts': counts
        })
    except Exception as e:
        print(f"Error fetching admin content: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to fetch content'}), 500


@app.route('/api/admin/content/<content_type>/<int:content_id>', methods=['DELETE'])
@admin_required
def admin_delete_content(content_type, content_id):
    """Soft-delete content"""
    try:
        type_map = {
            'beat': Beat,
            'pack': SoundPack,
            'curated': CuratedPack,
            'stem': StemProject
        }

        if content_type not in type_map:
            return jsonify({'error': 'Invalid content type'}), 400

        model = type_map[content_type]
        item = model.query.get(content_id)
        if not item:
            return jsonify({'error': 'Content not found'}), 404

        item.is_active = False

        # For packs, also soft-delete child tracks
        if content_type == 'pack':
            for track in item.tracks:
                track.is_active = False

        db.session.commit()

        return jsonify({
            'success': True,
            'message': f'{content_type.title()} deactivated'
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting content: {e}")
        return jsonify({'error': 'Failed to delete content'}), 500


@app.route('/api/admin/content/<content_type>/<int:content_id>/toggle-featured', methods=['POST'])
@admin_required
def admin_toggle_featured(content_type, content_id):
    """Toggle featured flag on beats and packs"""
    try:
        if content_type not in ('beat', 'pack'):
            return jsonify({'error': 'Featured only available for beats and packs'}), 400

        model = Beat if content_type == 'beat' else SoundPack
        item = model.query.get(content_id)
        if not item:
            return jsonify({'error': 'Content not found'}), 404

        item.is_featured = not item.is_featured
        db.session.commit()

        return jsonify({
            'success': True,
            'is_featured': item.is_featured,
            'message': f"{'Featured' if item.is_featured else 'Unfeatured'}"
        })
    except Exception as e:
        db.session.rollback()
        print(f"Error toggling featured: {e}")
        return jsonify({'error': 'Failed to toggle featured'}), 500


# =============================================================================
# CLI Commands
# =============================================================================

import click

@app.cli.command('make-admin')
@click.argument('email')
def make_admin(email):
    """Promote a user to admin by email."""
    user = User.query.filter_by(email=email.lower().strip()).first()
    if not user:
        click.echo(f'User not found: {email}')
        return
    user.is_admin = True
    db.session.commit()
    click.echo(f'Promoted {user.first_name} {user.surname} ({user.email}) to admin.')


# Create database tables on app startup
with app.app_context():
    db.create_all()

    # Migrate: add missing columns to existing tables
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    existing_columns = {col['name'] for col in inspector.get_columns('users')}
    with db.engine.connect() as conn:
        if 'username' not in existing_columns:
            conn.execute(text('ALTER TABLE users ADD COLUMN username VARCHAR(50) UNIQUE'))
            conn.commit()
        if 'username_changed_at' not in existing_columns:
            conn.execute(text('ALTER TABLE users ADD COLUMN username_changed_at TIMESTAMP'))
            conn.commit()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
