import os
import sys
import logging
import asyncio
import json
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from dataclasses import dataclass
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport

# Load environment variables
load_dotenv()

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Gmail-mcp-server")

# OAuth Configuration - Consistent order
SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/gmail.compose',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send'
]
SCOPES.sort()

# Paths
TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tokens')
CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')

# Ensure tokens directory exists
os.makedirs(TOKEN_PATH, exist_ok=True)

# Global variables with better management
gmail_services: Dict[str, Any] = {}
user_sessions: Dict[str, Dict] = {}
service_last_refresh: Dict[str, datetime] = {}  # Track last refresh per user

class GmailAuth:
    def __init__(self):
        self.client_config = self._load_client_config()
    
    def _load_client_config(self):
        """Load OAuth client configuration from credentials.json"""
        try:
            with open(CREDENTIALS_PATH, 'r') as f:
                creds = json.load(f)
                return creds['web'] if 'web' in creds else creds['installed']
        except Exception as e:
            logger.error(f"Error loading client config: {e}")
            raise
    
    def create_flow(self, redirect_uri: str):
        """Create OAuth flow"""
        flow = Flow.from_client_config(
            {'web': self.client_config},
            scopes=SCOPES
        )
        flow.redirect_uri = redirect_uri
        return flow
    
    def get_authorization_url(self, redirect_uri: str, state: str):
        """Get authorization URL for OAuth flow"""
        flow = self.create_flow(redirect_uri)
        auth_url, _ = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            state=state,
            prompt='consent'  # Force consent to get refresh token
        )
        return auth_url
    
    def exchange_code_for_tokens(self, code: str, redirect_uri: str):
        """Exchange authorization code for tokens"""
        flow = self.create_flow(redirect_uri)
        flow.fetch_token(code=code)
        return flow.credentials

gmail_auth = GmailAuth()

def create_message(sender: str, to: str, subject: str, body: str) -> Dict:
    """Create a MIME message for email sending."""
    logger.debug(f"Creating email message from {sender} to {to} with subject: {subject}")
    try:
        msg = MIMEMultipart()
        msg['to'] = to
        msg['from'] = sender
        msg['subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        logger.debug("Email message created successfully")
        return {'raw': raw_message}
    except Exception as e:
        logger.error(f"Error creating email message: {e}", exc_info=True)
        raise

def save_user_credentials(user_id: str, credentials: Credentials):
    """Save user credentials with enhanced error handling"""
    token_file = os.path.join(TOKEN_PATH, f'token_{user_id}.json')
    try:
        # Ensure refresh token exists
        if not credentials.refresh_token:
            logger.warning(f"No refresh token for user {user_id} - they may need to re-authenticate")
        
        token_data = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": credentials.scopes,
            "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
            "saved_at": datetime.now().isoformat()
        }
        
        with open(token_file, 'w') as f:
            json.dump(token_data, f, indent=2)
        logger.info(f"Saved credentials for user {user_id} (refresh_token: {bool(credentials.refresh_token)})")
    except Exception as e:
        logger.error(f"Error saving credentials for user {user_id}: {e}")

def load_user_credentials(user_id: str) -> Optional[Credentials]:
    """Load user credentials with robust refresh handling"""
    token_file = os.path.join(TOKEN_PATH, f'token_{user_id}.json')
    try:
        if not os.path.exists(token_file):
            logger.info(f"No token file found for user {user_id}")
            return None
            
        with open(token_file, 'r') as f:
            creds_data = json.load(f)
        
        # Create credentials from the saved data
        credentials = Credentials.from_authorized_user_info(creds_data, SCOPES)
        
        # Check if token needs refresh
        now = datetime.now()
        
        # Always refresh if expired or if it's been more than 45 minutes
        needs_refresh = (
            credentials.expired or
            (credentials.expiry and credentials.expiry <= now + timedelta(minutes=15)) or
            service_last_refresh.get(user_id, datetime.min) < now - timedelta(minutes=45)
        )
        
        if needs_refresh and credentials.refresh_token:
            try:
                logger.info(f"Refreshing token for user {user_id}")
                credentials.refresh(GoogleRequest())
                save_user_credentials(user_id, credentials)
                service_last_refresh[user_id] = now
                logger.info(f"Token refreshed successfully for user {user_id}")
            except Exception as refresh_error:
                logger.error(f"Failed to refresh token for user {user_id}: {refresh_error}")
                # If refresh fails, remove cached service and return None
                gmail_services.pop(user_id, None)
                return None
        elif not credentials.refresh_token:
            logger.warning(f"No refresh token available for user {user_id}")
        
        return credentials
        
    except Exception as e:
        logger.error(f"Error loading credentials for user {user_id}: {e}")
        return None

def get_gmail_service(user_id: str):
    """Get Gmail service with better caching and validation"""
    now = datetime.now()
    
    # Check if we need to refresh the cached service
    last_refresh = service_last_refresh.get(user_id, datetime.min)
    cache_expired = last_refresh < now - timedelta(minutes=30)
    
    if user_id in gmail_services and not cache_expired:
        try:
            service = gmail_services[user_id]
            # Quick validation - try to get profile
            profile = service.users().getProfile(userId='me').execute()
            logger.debug(f"Using cached Gmail service for user {user_id}")
            return service
        except Exception as e:
            logger.warning(f"Cached service invalid for user {user_id}: {e}")
            gmail_services.pop(user_id, None)
    
    # Load fresh credentials and create new service
    credentials = load_user_credentials(user_id)
    if credentials and credentials.valid:
        try:
            service = build('gmail', 'v1', credentials=credentials)
            gmail_services[user_id] = service
            service_last_refresh[user_id] = now
            logger.info(f"Created/refreshed Gmail service for user {user_id}")
            return service
        except Exception as e:
            logger.error(f"Error creating Gmail service for user {user_id}: {e}")
            return None
    
    logger.warning(f"No valid credentials found for user {user_id}")
    return None

def get_user_info(credentials: Credentials):
    """Get user profile information"""
    try:
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(GoogleRequest())
        
        import requests
        response = requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {credentials.token}'}
        )
        
        if response.status_code == 200:
            user_data = response.json()
            email = user_data.get('email', '')
            name = user_data.get('name', user_data.get('given_name', ''))
            
            if email:
                if not name:
                    name = email.split('@')[0]
                return {'email': email, 'name': name}
        
        logger.error(f"Failed to get user info: {response.status_code}")
        return {'email': '', 'name': ''}
        
    except Exception as e:
        logger.error(f"Error getting user info: {e}")
        return {'email': '', 'name': ''}

# Initialize MCP server
mcp = FastMCP(
    name="Gmail-Send-Robust",
    stateless_http=True,
    port=8101
)

# Initialize SSE transport
transport = SseServerTransport("/messages")

@mcp.tool()
def send_email(user_id: str, to: str, subject: str, body: str) -> Dict:
    """Send an email message for a specific user.
    
    Args:
        user_id: User identifier
        to: Recipient's email address
        subject: Email subject line
        body: Body text of the email
        
    Returns:
        Dict: Dictionary containing:
            - messageId: ID of the sent message
            - threadId: ID of the message thread
            - status: Status message
    """
    logger.info(f"Sending email for user {user_id} to: {to}")
    
    service = get_gmail_service(user_id)
    if not service:
        return {
            'messageId': None,
            'threadId': None,
            'status': 'Authentication required. Please visit http://localhost:8101 to authenticate.'
        }
    
    try:
        # Get user's email address
        profile = service.users().getProfile(userId='me').execute()
        sender_email = profile['emailAddress']
        
        msg = create_message(sender_email, to, subject, body)
        sent = service.users().messages().send(userId='me', body=msg).execute()
        
        logger.info(f"Email sent successfully for user {user_id}, message ID: {sent['id']}")
        return {
            'messageId': sent['id'],
            'threadId': sent['threadId'],
            'status': 'Email sent successfully'
        }
    except Exception as e:
        logger.error(f"Failed to send email for user {user_id}: {e}", exc_info=True)
        
        # If it's an auth error, clear the cached service
        if 'invalid_grant' in str(e) or 'unauthorized' in str(e).lower():
            gmail_services.pop(user_id, None)
            return {
                'messageId': None,
                'threadId': None,
                'status': 'Authentication expired. Please visit http://localhost:8101 to re-authenticate.'
            }
        
        return {
            'messageId': None,
            'threadId': None,
            'status': f'Failed to send email: {str(e)}'
        }

@mcp.tool()
def check_connection(user_id: str) -> Dict:
    """Check if user is authenticated and can send emails.
    
    Args:
        user_id: User identifier
        
    Returns:
        Dict: Connection status information
    """
    logger.info(f"Checking connection for user {user_id}")
    
    service = get_gmail_service(user_id)
    if not service:
        return {
            'connected': False,
            'email': None,
            'status': 'Not authenticated. Please visit http://localhost:8101 to authenticate.'
        }
    
    try:
        profile = service.users().getProfile(userId='me').execute()
        return {
            'connected': True,
            'email': profile['emailAddress'],
            'status': 'Connected and ready to send emails',
            'messagesTotal': profile.get('messagesTotal', 0),
            'historyId': profile.get('historyId', 'Unknown')
        }
    except Exception as e:
        logger.error(f"Connection check failed for user {user_id}: {e}")
        gmail_services.pop(user_id, None)
        return {
            'connected': False,
            'email': None,
            'status': f'Connection failed: {str(e)}. Please re-authenticate at http://localhost:8101'
        }

# Simplified Web Interface
app = FastAPI()

app.add_middleware(
    SessionMiddleware, 
    secret_key=os.getenv('SESSION_SECRET', 'gmail-mcp-robust-key'),
    max_age=86400 * 7,  # 7 days
    same_site='lax',
    https_only=False
)

# Simple HTML templates
SIMPLE_BASE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }}</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/tailwindcss/2.2.19/tailwind.min.css" rel="stylesheet">
</head>
<body class="bg-gray-100 min-h-screen">
    <div class="container mx-auto px-4 py-8">
        {{ content }}
    </div>
</body>
</html>
"""

HOME_TEMPLATE = """
<div class="max-w-md mx-auto bg-white rounded-lg shadow-lg p-8">
    {% if not user_info %}
    <div class="text-center">
        <h1 class="text-3xl font-bold text-gray-800 mb-4">Gmail MCP Server</h1>
        <p class="text-gray-600 mb-8">Authenticate to enable email sending via MCP</p>
        
        <a href="/login" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 px-6 rounded-lg inline-block transition duration-200">
            🔐 Connect with Google
        </a>
    </div>
    {% else %}
    <div class="text-center">
        <h1 class="text-3xl font-bold text-gray-800 mb-4">✅ Connected</h1>
        <p class="text-gray-600 mb-4">{{ user_info.email }}</p>
        
        <div class="space-y-4">
            <div class="bg-green-50 border border-green-200 rounded-lg p-4">
                <p class="text-green-800 font-semibold">Status: Ready to send emails</p>
                <p class="text-green-600 text-sm">User ID: {{ user_info.email.replace('@', '_').replace('.', '_') }}</p>
            </div>
            
            <button onclick="checkConnection()" class="w-full bg-green-600 hover:bg-green-700 text-white font-bold py-2 px-4 rounded transition duration-200" id="checkBtn">
                🔍 Test Connection
            </button>
            
            <div id="connectionResult" class="hidden p-4 rounded-lg"></div>
            
            <a href="/logout" class="w-full bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-4 rounded inline-block transition duration-200">
                🚪 Logout
            </a>
        </div>
    </div>
    {% endif %}
</div>

<script>
async function checkConnection() {
    const btn = document.getElementById('checkBtn');
    const result = document.getElementById('connectionResult');
    
    btn.textContent = '⏳ Checking...';
    btn.disabled = true;
    
    try {
        const response = await fetch('/check-connection');
        const data = await response.json();
        
        result.className = data.connected 
            ? 'p-4 rounded-lg bg-green-50 border border-green-200'
            : 'p-4 rounded-lg bg-red-50 border border-red-200';
        
        result.innerHTML = `
            <p class="${data.connected ? 'text-green-800' : 'text-red-800'} font-semibold">
                ${data.connected ? '✅ Connected' : '❌ Not Connected'}
            </p>
            <p class="${data.connected ? 'text-green-600' : 'text-red-600'} text-sm">${data.status}</p>
        `;
        result.classList.remove('hidden');
        
    } catch (error) {
        result.className = 'p-4 rounded-lg bg-red-50 border border-red-200';
        result.innerHTML = `<p class="text-red-800">❌ Error: ${error.message}</p>`;
        result.classList.remove('hidden');
    }
    
    btn.textContent = '🔍 Test Connection';
    btn.disabled = false;
}
</script>
"""

def render_template(template: str, **kwargs) -> str:
    """Simple template rendering"""
    result = template
    for key, value in kwargs.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                placeholder = f"{{{{ {key}.{sub_key} }}}}"
                if placeholder in result:
                    result = result.replace(placeholder, str(sub_value))
        else:
            placeholder = f"{{{{ {key} }}}}"
            if placeholder in result:
                result = result.replace(placeholder, str(value))
    
    # Simple conditional rendering
    import re
    
    # Handle {% if not user_info %}
    if_not_pattern = r'{%\s*if\s+not\s+(\w+)\s*%}(.*?){%\s*else\s*%}(.*?){%\s*endif\s*%}'
    def replace_if_not(match):
        var_name = match.group(1)
        if_content = match.group(2)
        else_content = match.group(3)
        return else_content if kwargs.get(var_name) else if_content
    
    result = re.sub(if_not_pattern, replace_if_not, result, flags=re.DOTALL)
    
    # Handle {% if user_info %}
    if_pattern = r'{%\s*if\s+(\w+)\s*%}(.*?){%\s*endif\s*%}'
    def replace_if(match):
        var_name = match.group(1)
        content = match.group(2)
        return content if kwargs.get(var_name) else ''
    
    result = re.sub(if_pattern, replace_if, result, flags=re.DOTALL)
    
    return result

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page"""
    user_info = request.session.get('user_info')
    content = render_template(HOME_TEMPLATE, user_info=user_info)
    html = render_template(SIMPLE_BASE_TEMPLATE, title="Gmail MCP Server", content=content)
    return HTMLResponse(content=html)

@app.get("/login")
async def login(request: Request):
    """Initiate OAuth login"""
    request.session.clear()
    
    state = secrets.token_urlsafe(32)
    request.session['oauth_state'] = state
    
    redirect_uri = str(request.url_for('oauth_callback'))
    auth_url = gmail_auth.get_authorization_url(redirect_uri, state)
    
    return RedirectResponse(url=auth_url)

@app.get("/oauth2callback")
async def oauth_callback(request: Request, code: str = None, state: str = None, error: str = None):
    """Handle OAuth callback"""
    logger.info(f"OAuth callback - State: {state}, Code present: {bool(code)}, Error: {error}")
    
    if error:
        logger.error(f"OAuth error: {error}")
        return RedirectResponse(url="/?error=" + error)
    
    if not code:
        return RedirectResponse(url="/?error=no_code")
    
    stored_state = request.session.get('oauth_state')
    if not stored_state or state != stored_state:
        return RedirectResponse(url="/?error=state_mismatch")
    
    try:
        redirect_uri = str(request.url_for('oauth_callback'))
        credentials = gmail_auth.exchange_code_for_tokens(code, redirect_uri)
        
        # Ensure we have a refresh token
        if not credentials.refresh_token:
            logger.warning("No refresh token received - user may need to revoke access and re-authenticate")
        
        user_info = get_user_info(credentials)
        
        if not user_info.get('email'):
            return RedirectResponse(url="/?error=no_email")
        
        user_id = user_info['email'].replace('@', '_').replace('.', '_')
        
        # Save credentials
        save_user_credentials(user_id, credentials)
        
        # Store session data
        request.session.clear()
        request.session['user_info'] = user_info
        request.session['user_id'] = user_id
        
        # Clear cached service to force refresh
        gmail_services.pop(user_id, None)
        service_last_refresh[user_id] = datetime.now()
        
        logger.info(f"User {user_info['email']} authenticated successfully")
        return RedirectResponse(url="/")
        
    except Exception as e:
        logger.error(f"OAuth callback error: {e}", exc_info=True)
        request.session.clear()
        return RedirectResponse(url="/?error=auth_failed")

@app.get("/check-connection")
async def check_connection_web(request: Request):
    """Check connection status via web"""
    user_id = request.session.get('user_id')
    if not user_id:
        return JSONResponse({"connected": False, "status": "Not logged in"})
    
    result = check_connection(user_id)
    return JSONResponse(result)

@app.get("/logout")
async def logout(request: Request):
    """Logout user"""
    user_id = request.session.get('user_id')
    if user_id:
        user_sessions.pop(user_id, None)
        gmail_services.pop(user_id, None)
        service_last_refresh.pop(user_id, None)
    
    request.session.clear()
    return RedirectResponse(url="/")

@app.get("/health")
def health_check():
    """Health check endpoint"""
    token_files = len([f for f in os.listdir(TOKEN_PATH) if f.startswith('token_')]) if os.path.exists(TOKEN_PATH) else 0
    
    return {
        "status": "healthy",
        "server": "Gmail MCP Send-Only Server",
        "authenticated_users": token_files,
        "active_services": len(gmail_services),
        "cached_refreshes": len(service_last_refresh)
    }

# Handle SSE connections
async def handle_sse(request: Request):
    """Handle SSE connections for MCP"""
    async with transport.connect_sse(request.scope, request.receive, request._send) as (in_stream, out_stream):
        await mcp._mcp_server.run(in_stream, out_stream, mcp._mcp_server.create_initialization_options())

# Starlette app for SSE
from starlette.applications import Starlette
from starlette.routing import Route, Mount

sse_app = Starlette(
    routes=[
        Route("/sse", handle_sse, methods=["GET"]),
        Mount("/messages/", app=transport.handle_post_message)
    ]
)

# Mount SSE app
app.mount("/", sse_app)

if __name__ == "__main__":
    import uvicorn
    
    async def run_server():
        logger.info("Starting Robust Gmail MCP Server...")
        logger.info("Web interface: http://localhost:8101")
        logger.info("MCP endpoint: http://localhost:8101/sse")
        
        config = uvicorn.Config(
            "mcp_gmail.mcp_server:app", 
            host="0.0.0.0", 
            port=8101, 
            reload=False,  # Disable reload for stability
            log_level="info"
        )
        server = uvicorn.Server(config)
        
        try:
            await server.serve()
        except KeyboardInterrupt:
            logger.info("Server shutdown by user")
        except Exception as e:
            logger.error(f"Server error: {e}", exc_info=True)

    asyncio.run(run_server())