# app.py
import base64
import hashlib
import os
import re
import secrets
import string
import requests
import urllib.parse

from dotenv import load_dotenv
from flask import Flask, request, jsonify, session, redirect, url_for, send_from_directory
from flask_cors import CORS
from datetime import timedelta

# For Spotify
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# For Telethon
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# Our separate logic
import spotify as spint
import tele

load_dotenv()

# Flask app setup
app = Flask(__name__,  static_folder='static', static_url_path='')
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config["SESSION_PERMANENT"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True  # Set True in production with HTTPS


CORS(app, resources={
    r"/*": {
        "origins": [re.compile(r"^https://localhost:\d+$"), "https://your-production-site.com"]
    }
})

# Telegram credentials
TELEGRAM_API_ID = os.getenv('TELEGRAM_API_ID')
TELEGRAM_API_ID = int(TELEGRAM_API_ID) if TELEGRAM_API_ID is not None else None
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH')

SPOTIFY_USERNAME = os.getenv('USERNAME', '')
SPOTIPY_CLIENT_ID = os.getenv('SPOTIPY_CLIENT_ID')
SPOTIPY_CLIENT_SECRET = os.getenv('SPOTIPY_CLIENT_SECRET')
SPOTIPY_REDIRECT_URI = os.getenv('SPOTIPY_REDIRECT_URI')

# We'll keep a single Telethon client in memory or a session file on disk:
TELETHON_SESSION_NAME = 'web-telethon-session'  # or any name
telethon_client = None

# Read more about Authorization Code Flow with Spotify:
# https://spotipy.readthedocs.io/en/2.11.2/#authorization-code-flow
scope = 'playlist-modify-public'
spotify_oauth = SpotifyOAuth(scope=scope)


@app.route('/', methods=['GET'])
def redirect_to_index():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    return send_from_directory(root, 'index.html')

#################################################
# TELEGRAM AUTH
#################################################

@app.route('/telegram/login', methods=['POST'])
def telegram_login():
    """
    This route might accept a phone number (and possibly a code, password)
    for the sake of demonstration. Real usage might require multiple steps.
    """
    global telethon_client
    data = request.get_json(force=True)
    phone_number = data.get('phone')

    if not phone_number:
        return jsonify({"error": "Phone number required"}), 400

    # Initialize client. If 'web-telethon-session.session' exists, it reuses it
    telethon_client = TelegramClient(TELETHON_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)

    try:
        telethon_client.start(phone=phone_number)
        # If 2FA is enabled or code needed, Telethon typically prompts in console, which is not
        # truly "web friendly." You might handle it by hooking Telethon callbacks or returning
        # partial states to the front-end. This is more advanced.
        # For a simplified approach, if you already have a saved session on disk, this 'start' might succeed immediately.
    except SessionPasswordNeededError:
        # In real usage, you'd handle requesting the user's 2FA password
        return jsonify({"error": "2FA password needed, but not implemented here"}), 401
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500

    # If we got here, presumably we are "logged in"
    return jsonify({"success": True})

@app.route('/telegram/logged_in', methods=['GET'])
def telegram_logged_in():
    """
    Simple check if we *think* the Telegram client is alive and authenticated.
    """
    global telethon_client
    if telethon_client and telethon_client.is_user_authorized():
        return jsonify({"logged_in": True})
    return jsonify({"logged_in": False})

@app.route('/telegram/songs', methods=['POST'])
def telegram_songs():
    """
    Expects JSON: { "chat": "some_channel_or_group_id" }
    Uses the Telethon client to read messages and return a list of parsed songs.
    """
    global telethon_client
    if not (telethon_client and telethon_client.is_user_authorized()):
        return jsonify({"error": "Telegram not logged in"}), 401

    data = request.get_json(force=True)
    chat = data.get('chat', None)
    if not chat:
        return jsonify({"error": "No chat specified"}), 400

    # Use tele.py logic
    songs = tele.get_songs_from_telegram(telethon_client, chat)
    return jsonify({"songs": songs})

#################################################
# SPOTIFY AUTH
#################################################

@app.route('/spotify/login', methods=['GET'])
def spotify_login():
    session.permanent = True
    session["next_url"] = request.headers.get("Referer", "/")

    code_verifier = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(64))
    hashed = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(hashed).decode("utf-8").rstrip("=")
    session["code_verifier"] = code_verifier
    params = {
        "client_id": SPOTIPY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIPY_REDIRECT_URI,
        "scope": scope,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge
    }
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)
    return redirect(url)

@app.route('/spotify/callback', methods=['GET'])
def spotify_callback():
    """
    Spotify redirects here after the user logs in and authorizes.
    We'll fetch the token and store it in the session.
    """
    code = request.args.get("code")
    if not code:
        return "No code returned."
    code_verifier = session.get("code_verifier")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SPOTIPY_REDIRECT_URI,
        "client_id": SPOTIPY_CLIENT_ID,
        "code_verifier": code_verifier
    }
    r = requests.post("https://accounts.spotify.com/api/token", data=data)
    token_info = r.json()

    session["access_token"] = token_info.get("access_token")
    next_url = session.pop("next_url")

    return redirect(next_url)

@app.route('/spotify/me', methods=['GET'])
def spotify_me():
    access_token = session.get("access_token")
    if not access_token:
        return jsonify({"error": "Not logged in"}), 401
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get("https://api.spotify.com/v1/me", headers=headers)
    return jsonify(r.json())

#################################################
# ADD SONGS TO SPOTIFY
#################################################

@app.route('/spotify/add_songs', methods=['POST'])
def spotify_add_songs():
    """
    Expects JSON: {
      "playlistName": "MyPlaylist",
      "songs": ["Song A", "Song B", ...]
    }
    Creates or retrieves the playlist, searches songs, adds them.
    """
    sp_client = get_spotify_client_from_session()
    if not sp_client:
        return jsonify({"error": "Not logged in to Spotify"}), 401

    data = request.get_json(force=True)
    playlist_name = data.get("playlistName", "New Playlist")
    songs = data.get("songs", [])

    if not songs:
        return jsonify({"error": "No songs provided"}), 400

    result = spint.process_songs(sp_client, SPOTIFY_USERNAME, playlist_name, songs)
    return jsonify(result)


#################################################
# HELPER: GET SPOTIFY CLIENT FROM SESSION
#################################################

def get_spotify_client_from_session():
    """
    Uses session['spotify_token_info'] to build a valid Spotify client.
    Also refreshes token if needed.
    """
    if 'spotify_token_info' not in session:
        return None

    token_info = session['spotify_token_info']
    # Check if token is expired
    if spotify_oauth.is_token_expired(token_info):
        token_info = spotify_oauth.refresh_access_token(token_info['refresh_token'])
        session['spotify_token_info'] = token_info

    # Return a spotipy client
    sp = spotipy.Spotify(auth=token_info['access_token'])
    return sp


#################################################
# MAIN
#################################################
if __name__ == '__main__':
    app.run(port=8000, debug=True)
