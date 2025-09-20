import os
import sys
import asyncio
import json
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.cors import CORSMiddleware
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='[%(asctime)s] [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/calendar.readonly'
]
SCOPES.sort()

TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'meeting_tokens')
CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')

os.makedirs(TOKEN_PATH, exist_ok=True)

calendar_services: Dict[str, Any] = {}
service_last_refresh: Dict[str, datetime] = {}

class CalendarAuth:
    def __init__(self):
        self.client_config = self._load_client_config()
    
    def _load_client_config(self):
        with open(CREDENTIALS_PATH, 'r') as f:
            creds = json.load(f)
            return creds['web'] if 'web' in creds else creds['installed']
    
    def create_flow(self, redirect_uri: str):
        flow = Flow.from_client_config({'web': self.client_config}, scopes=SCOPES)
        flow.redirect_uri = redirect_uri
        return flow
    
    def get_authorization_url(self, redirect_uri: str, state: str):
        flow = self.create_flow(redirect_uri)
        auth_url, _ = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            state=state,
            prompt='consent'
        )
        return auth_url
    
    def exchange_code_for_tokens(self, code: str, redirect_uri: str):
        flow = self.create_flow(redirect_uri)
        flow.fetch_token(code=code)
        return flow.credentials

calendar_auth = CalendarAuth()

def save_user_credentials(user_id: str, credentials: Credentials):
    token_file = os.path.join(TOKEN_PATH, f'token_{user_id}.json')
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

def load_user_credentials(user_id: str) -> Optional[Credentials]:
    token_file = os.path.join(TOKEN_PATH, f'token_{user_id}.json')
    if not os.path.exists(token_file):
        return None
    
    with open(token_file, 'r') as f:
        creds_data = json.load(f)
    
    credentials = Credentials.from_authorized_user_info(creds_data, SCOPES)
    
    now = datetime.now()
    needs_refresh = (
        credentials.expired or
        (credentials.expiry and credentials.expiry <= now + timedelta(minutes=15)) or
        service_last_refresh.get(user_id, datetime.min) < now - timedelta(minutes=45)
    )
    
    if needs_refresh and credentials.refresh_token:
        try:
            credentials.refresh(GoogleRequest())
            save_user_credentials(user_id, credentials)
            service_last_refresh[user_id] = now
        except Exception:
            calendar_services.pop(user_id, None)
            return None
    
    return credentials

def get_calendar_service(user_id: str):
    now = datetime.now()
    last_refresh = service_last_refresh.get(user_id, datetime.min)
    cache_expired = last_refresh < now - timedelta(minutes=30)
    
    if user_id in calendar_services and not cache_expired:
        try:
            service = calendar_services[user_id]
            service.calendarList().list(maxResults=1).execute()
            return service
        except Exception:
            calendar_services.pop(user_id, None)
    
    credentials = load_user_credentials(user_id)
    if credentials and credentials.valid:
        try:
            service = build('calendar', 'v3', credentials=credentials)
            calendar_services[user_id] = service
            service_last_refresh[user_id] = now
            return service
        except Exception:
            return None
    
    return None

def get_user_info(credentials: Credentials):
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
            if email and not name:
                name = email.split('@')[0]
            return {'email': email, 'name': name}
        
        return {'email': '', 'name': ''}
    except Exception:
        return {'email': '', 'name': ''}

def parse_datetime(datetime_str: str) -> Dict:
    try:
        dt = datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
        return {'dateTime': dt.isoformat(), 'timeZone': 'UTC'}
    except:
        try:
            from dateutil import parser
            dt = parser.parse(datetime_str)
            if dt.time() == datetime.min.time():
                return {'date': dt.date().isoformat()}
            else:
                return {'dateTime': dt.isoformat(), 'timeZone': 'UTC'}
        except:
            raise ValueError(f"Invalid datetime format: {datetime_str}")

def format_meeting_response(event: Dict) -> Dict:
    return {
        'id': event['id'],
        'title': event.get('summary', 'No Title'),
        'description': event.get('description', ''),
        'location': event.get('location', ''),
        'start': event.get('start', {}),
        'end': event.get('end', {}),
        'status': event.get('status', 'confirmed'),
        'organizer': event.get('organizer', {}),
        'attendees': [
            {
                'email': attendee.get('email', ''),
                'status': attendee.get('responseStatus', 'needsAction'),
                'optional': attendee.get('optional', False)
            } for attendee in event.get('attendees', [])
        ],
        'htmlLink': event.get('htmlLink', ''),
        'created': event.get('created', ''),
        'updated': event.get('updated', ''),
        'recurringEventId': event.get('recurringEventId'),
        'originalStartTime': event.get('originalStartTime'),
        'transparency': event.get('transparency', 'opaque'),
        'visibility': event.get('visibility', 'default'),
        'iCalUID': event.get('iCalUID', ''),
        'sequence': event.get('sequence', 0)
    }

mcp = FastMCP(name="Meeting-CRUD-Manager", stateless_http=True, port=8102)
transport = SseServerTransport("/messages")

@mcp.tool()
def create_meeting(
    user_id: str,
    title: str,
    start_time: str,
    end_time: str,
    description: str = "",
    location: str = "",
    attendees: List[str] = None,
    calendar_id: str = "primary",
    send_notifications: bool = True,
    timezone: str = "UTC"
) -> Dict:
    """CREATE: Schedule a new meeting in the calendar."""
    
    service = get_calendar_service(user_id)
    if not service:
        return {
            'success': False,
            'meeting': None,
            'error': 'Authentication required. Please visit http://localhost:8102 to authenticate.',
            'operation': 'CREATE'
        }
    
    try:
        start_parsed = parse_datetime(start_time)
        end_parsed = parse_datetime(end_time)
        
        meeting_data = {
            'summary': title,
            'start': start_parsed,
            'end': end_parsed,
            'status': 'confirmed'
        }
        
        if description:
            meeting_data['description'] = description
        if location:
            meeting_data['location'] = location
        if attendees:
            meeting_data['attendees'] = [
                {'email': email.strip(), 'responseStatus': 'needsAction'} 
                for email in attendees if email.strip()
            ]
        
        if timezone != "UTC":
            if 'dateTime' in start_parsed:
                start_parsed['timeZone'] = timezone
            if 'dateTime' in end_parsed:
                end_parsed['timeZone'] = timezone
        
        created_event = service.events().insert(
            calendarId=calendar_id, 
            body=meeting_data,
            sendNotifications=send_notifications
        ).execute()
        
        formatted_meeting = format_meeting_response(created_event)
        
        return {
            'success': True,
            'meeting': formatted_meeting,
            'operation': 'CREATE',
            'message': f'Meeting "{title}" created successfully'
        }
        
    except Exception as e:
        if 'invalid_grant' in str(e) or 'unauthorized' in str(e).lower():
            calendar_services.pop(user_id, None)
            error_msg = 'Authentication expired. Please re-authenticate.'
        else:
            error_msg = f'Failed to create meeting: {str(e)}'
        
        return {
            'success': False,
            'meeting': None,
            'error': error_msg,
            'operation': 'CREATE'
        }

@mcp.tool()
def read_meeting(user_id: str, meeting_id: str, calendar_id: str = "primary") -> Dict:
    """READ: Retrieve a specific meeting by ID."""
    
    service = get_calendar_service(user_id)
    if not service:
        return {
            'success': False,
            'meeting': None,
            'error': 'Authentication required. Please visit http://localhost:8102 to authenticate.',
            'operation': 'READ'
        }
    
    try:
        event = service.events().get(calendarId=calendar_id, eventId=meeting_id).execute()
        formatted_meeting = format_meeting_response(event)
        
        return {
            'success': True,
            'meeting': formatted_meeting,
            'operation': 'READ',
            'message': 'Meeting retrieved successfully'
        }
        
    except HttpError as e:
        if e.resp.status == 404:
            error_msg = f'Meeting not found: {meeting_id}'
        else:
            error_msg = f'Failed to retrieve meeting: {str(e)}'
        
        return {
            'success': False,
            'meeting': None,
            'error': error_msg,
            'operation': 'READ'
        }
    except Exception as e:
        return {
            'success': False,
            'meeting': None,
            'error': f'Unexpected error: {str(e)}',
            'operation': 'READ'
        }

@mcp.tool()
def read_meetings(
    user_id: str,
    calendar_id: str = "primary",
    max_results: int = 50,
    time_min: str = None,
    time_max: str = None,
    show_deleted: bool = False,
    single_events: bool = True,
    order_by: str = "startTime"
) -> Dict:
    """READ: Retrieve all upcoming meetings from current time onwards."""
    
    service = get_calendar_service(user_id)
    if not service:
        return {
            'success': False,
            'meetings': [],
            'total_count': 0,
            'error': 'Authentication required. Please visit http://localhost:8102 to authenticate.',
            'operation': 'READ_MULTIPLE'
        }
    
    try:
        all_meetings = []
        page_token = None
        
        # If no time_min provided, start from current time
        if not time_min:
            time_min = datetime.utcnow().isoformat() + 'Z'
        
        # If no time_max provided, set to far future to get all upcoming events
        if not time_max:
            time_max = (datetime.utcnow() + timedelta(days=3650)).isoformat() + 'Z'  # 10 years ahead
        
        while len(all_meetings) < max_results:
            query_params = {
                'calendarId': calendar_id,
                'maxResults': min(2500, max_results - len(all_meetings)),
                'singleEvents': single_events,
                'showDeleted': show_deleted,
                'timeMin': time_min,
                'timeMax': time_max,
                'orderBy': order_by
            }
            
            if page_token:
                query_params['pageToken'] = page_token
            
            events_result = service.events().list(**query_params).execute()
            events = events_result.get('items', [])
            
            if not events:
                break
            
            all_meetings.extend(events)
            page_token = events_result.get('nextPageToken')
            
            if not page_token:
                break
        
        # Limit to requested max_results
        all_meetings = all_meetings[:max_results]
        formatted_meetings = [format_meeting_response(event) for event in all_meetings]
        
        return {
            'success': True,
            'meetings': formatted_meetings,
            'total_count': len(formatted_meetings),
            'operation': 'READ_MULTIPLE',
            'message': f'Retrieved {len(formatted_meetings)} meetings successfully'
        }
        
    except Exception as e:
        if 'invalid_grant' in str(e) or 'unauthorized' in str(e).lower():
            calendar_services.pop(user_id, None)
            error_msg = 'Authentication expired. Please re-authenticate.'
        else:
            error_msg = f'Failed to retrieve meetings: {str(e)}'
        
        return {
            'success': False,
            'meetings': [],
            'total_count': 0,
            'error': error_msg,
            'operation': 'READ_MULTIPLE'
        }

@mcp.tool()
def update_meeting(
    user_id: str,
    meeting_id: str,
    title: str = None,
    start_time: str = None,
    end_time: str = None,
    description: str = None,
    location: str = None,
    attendees: List[str] = None,
    calendar_id: str = "primary",
    send_notifications: bool = True
) -> Dict:
    """UPDATE: Modify an existing meeting."""
    
    service = get_calendar_service(user_id)
    if not service:
        return {
            'success': False,
            'meeting': None,
            'error': 'Authentication required. Please visit http://localhost:8102 to authenticate.',
            'operation': 'UPDATE'
        }
    
    try:
        existing_event = service.events().get(calendarId=calendar_id, eventId=meeting_id).execute()
        
        updated_fields = []
        
        if title is not None:
            existing_event['summary'] = title
            updated_fields.append('title')
        if description is not None:
            existing_event['description'] = description
            updated_fields.append('description')
        if location is not None:
            existing_event['location'] = location
            updated_fields.append('location')
        if start_time is not None:
            existing_event['start'] = parse_datetime(start_time)
            updated_fields.append('start_time')
        if end_time is not None:
            existing_event['end'] = parse_datetime(end_time)
            updated_fields.append('end_time')
        if attendees is not None:
            existing_event['attendees'] = [
                {'email': email.strip(), 'responseStatus': 'needsAction'} 
                for email in attendees if email.strip()
            ]
            updated_fields.append('attendees')
        
        updated_event = service.events().update(
            calendarId=calendar_id, 
            eventId=meeting_id, 
            body=existing_event,
            sendNotifications=send_notifications
        ).execute()
        
        formatted_meeting = format_meeting_response(updated_event)
        
        return {
            'success': True,
            'meeting': formatted_meeting,
            'updated_fields': updated_fields,
            'operation': 'UPDATE',
            'message': f'Meeting updated successfully. Changed fields: {", ".join(updated_fields) if updated_fields else "none"}'
        }
        
    except HttpError as e:
        if e.resp.status == 404:
            error_msg = f'Meeting not found: {meeting_id}'
        else:
            error_msg = f'Failed to update meeting: {str(e)}'
        
        return {
            'success': False,
            'meeting': None,
            'error': error_msg,
            'operation': 'UPDATE'
        }
    except Exception as e:
        if 'invalid_grant' in str(e) or 'unauthorized' in str(e).lower():
            calendar_services.pop(user_id, None)
            error_msg = 'Authentication expired. Please re-authenticate.'
        else:
            error_msg = f'Unexpected error updating meeting: {str(e)}'
        
        return {
            'success': False,
            'meeting': None,
            'error': error_msg,
            'operation': 'UPDATE'
        }

@mcp.tool()
def delete_meeting(
    user_id: str, 
    meeting_id: str, 
    calendar_id: str = "primary",
    send_notifications: bool = True
) -> Dict:
    """DELETE: Permanently delete a meeting from the calendar."""
    
    service = get_calendar_service(user_id)
    if not service:
        return {
            'success': False,
            'deleted_meeting_id': None,
            'error': 'Authentication required. Please visit http://localhost:8102 to authenticate.',
            'operation': 'DELETE'
        }
    
    try:
        try:
            existing_event = service.events().get(calendarId=calendar_id, eventId=meeting_id).execute()
            meeting_title = existing_event.get('summary', 'Unknown Meeting')
            meeting_start = existing_event.get('start', {})
        except HttpError:
            meeting_title = 'Unknown Meeting'
            meeting_start = {}
        
        service.events().delete(
            calendarId=calendar_id, 
            eventId=meeting_id,
            sendNotifications=send_notifications
        ).execute()
        
        return {
            'success': True,
            'deleted_meeting_id': meeting_id,
            'deleted_meeting_title': meeting_title,
            'deleted_meeting_start': meeting_start,
            'operation': 'DELETE',
            'message': f'Meeting "{meeting_title}" deleted successfully'
        }
        
    except HttpError as e:
        if e.resp.status == 404:
            error_msg = f'Meeting not found or already deleted: {meeting_id}'
        elif e.resp.status == 410:
            error_msg = f'Meeting was already deleted: {meeting_id}'
        else:
            error_msg = f'Failed to delete meeting: {str(e)}'
        
        return {
            'success': False,
            'deleted_meeting_id': None,
            'error': error_msg,
            'operation': 'DELETE'
        }
    except Exception as e:
        if 'invalid_grant' in str(e) or 'unauthorized' in str(e).lower():
            calendar_services.pop(user_id, None)
            error_msg = 'Authentication expired. Please re-authenticate.'
        else:
            error_msg = f'Unexpected error deleting meeting: {str(e)}'
        
        return {
            'success': False,
            'deleted_meeting_id': None,
            'error': error_msg,
            'operation': 'DELETE'
        }

@mcp.tool()
def check_meeting_auth(user_id: str) -> Dict:
    """Check if user is authenticated and can perform meeting operations."""
    
    service = get_calendar_service(user_id)
    if not service:
        return {
            'authenticated': False,
            'user_email': None,
            'message': 'Not authenticated. Please visit http://localhost:8102 to authenticate.',
            'operation': 'AUTH_CHECK'
        }
    
    try:
        calendar_list = service.calendarList().list(maxResults=1).execute()
        primary_calendar = None
        
        for calendar in calendar_list.get('items', []):
            if calendar.get('primary', False):
                primary_calendar = calendar
                break
        
        if primary_calendar:
            return {
                'authenticated': True,
                'user_email': primary_calendar.get('id', ''),
                'primary_calendar': primary_calendar.get('summary', 'Primary Calendar'),
                'calendars_count': len(calendar_list.get('items', [])),
                'message': 'Authenticated and ready for meeting operations',
                'operation': 'AUTH_CHECK'
            }
        else:
            return {
                'authenticated': True,
                'user_email': 'Unknown',
                'message': 'Authenticated but no primary calendar found',
                'calendars_count': len(calendar_list.get('items', [])),
                'operation': 'AUTH_CHECK'
            }
    except Exception as e:
        calendar_services.pop(user_id, None)
        return {
            'authenticated': False,
            'user_email': None,
            'error': f'Authentication check failed: {str(e)}',
            'message': 'Please re-authenticate at http://localhost:8102',
            'operation': 'AUTH_CHECK'
        }

app = FastAPI()

app.add_middleware(
    SessionMiddleware, 
    secret_key=os.getenv('SESSION_SECRET', 'meeting-crud-mcp-key'),
    max_age=86400 * 7,
    same_site='lax',
    https_only=False
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        <h1 class="text-3xl font-bold text-gray-800 mb-4">Meeting CRUD MCP Server</h1>
        <p class="text-gray-600 mb-6">Comprehensive meeting management with full CRUD operations</p>
        <a href="/login" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 px-6 rounded-lg inline-block transition duration-200">
            Connect with Google Calendar
        </a>
    </div>
    {% else %}
    <div class="text-center">
        <h1 class="text-3xl font-bold text-gray-800 mb-4">CRUD Operations Ready</h1>
        <p class="text-gray-600 mb-4">{{ user_info.email }}</p>
        
        <div class="space-y-4">
            <div class="bg-green-50 border border-green-200 rounded-lg p-4">
                <p class="text-green-800 font-semibold">Status: Meeting CRUD operations enabled</p>
                <p class="text-green-600 text-sm">User ID: {{ user_info.email.replace('@', '_').replace('.', '_') }}</p>
            </div>
            
            <button onclick="checkConnection()" class="w-full bg-green-600 hover:bg-green-700 text-white font-bold py-2 px-4 rounded transition duration-200" id="checkBtn">
                Test CRUD Connection
            </button>
            
            <div id="connectionResult" class="hidden p-4 rounded-lg"></div>
            
            <a href="/logout" class="w-full bg-red-600 hover:bg-red-700 text-white font-bold py-2 px-4 rounded inline-block transition duration-200">
                Logout
            </a>
        </div>
    </div>
    {% endif %}
</div>

<script>
async function checkConnection() {
    const btn = document.getElementById('checkBtn');
    const result = document.getElementById('connectionResult');
    
    btn.textContent = 'Testing...';
    btn.disabled = true;
    
    try {
        const response = await fetch('/check-connection');
        const data = await response.json();
        
        result.className = data.authenticated 
            ? 'p-4 rounded-lg bg-green-50 border border-green-200'
            : 'p-4 rounded-lg bg-red-50 border border-red-200';
        
        if (data.authenticated) {
            result.innerHTML = `
                <p class="text-green-800 font-semibold">CRUD Operations Ready</p>
                <p class="text-green-600 text-sm">Email: ${data.user_email}</p>
            `;
        } else {
            result.innerHTML = `<p class="text-red-800">Connection failed</p>`;
        }
        result.classList.remove('hidden');
    } catch (error) {
        result.className = 'p-4 rounded-lg bg-red-50 border border-red-200';
        result.innerHTML = `<p class="text-red-800">Error: ${error.message}</p>`;
        result.classList.remove('hidden');
    }
    
    btn.textContent = 'Test CRUD Connection';
    btn.disabled = false;
}
</script>
"""

def render_template(template: str, **kwargs) -> str:
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
    
    import re
    
    if_not_pattern = r'{%\s*if\s+not\s+(\w+)\s*%}(.*?){%\s*else\s*%}(.*?){%\s*endif\s*%}'
    def replace_if_not(match):
        var_name = match.group(1)
        if_content = match.group(2)
        else_content = match.group(3)
        return else_content if kwargs.get(var_name) else if_content
    
    result = re.sub(if_not_pattern, replace_if_not, result, flags=re.DOTALL)
    
    if_pattern = r'{%\s*if\s+(\w+)\s*%}(.*?){%\s*endif\s*%}'
    def replace_if(match):
        var_name = match.group(1)
        content = match.group(2)
        return content if kwargs.get(var_name) else ''
    
    result = re.sub(if_pattern, replace_if, result, flags=re.DOTALL)
    
    return result

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user_info = request.session.get('user_info')
    content = render_template(HOME_TEMPLATE, user_info=user_info)
    html = render_template(SIMPLE_BASE_TEMPLATE, title="Meeting CRUD MCP Server", content=content)
    return HTMLResponse(content=html)

@app.get("/login")
async def login(request: Request):
    request.session.clear()
    
    state = secrets.token_urlsafe(32)
    request.session['oauth_state'] = state
    
    redirect_uri = str(request.url_for('oauth_callback'))
    auth_url = calendar_auth.get_authorization_url(redirect_uri, state)
    
    return RedirectResponse(url=auth_url)

@app.get("/oauth2callback")
async def oauth_callback(request: Request, code: str = None, state: str = None, error: str = None):
    if error:
        return RedirectResponse(url="/?error=" + error)
    
    if not code:
        return RedirectResponse(url="/?error=no_code")
    
    stored_state = request.session.get('oauth_state')
    if not stored_state or state != stored_state:
        return RedirectResponse(url="/?error=state_mismatch")
    
    try:
        redirect_uri = str(request.url_for('oauth_callback'))
        credentials = calendar_auth.exchange_code_for_tokens(code, redirect_uri)
        
        user_info = get_user_info(credentials)
        
        if not user_info.get('email'):
            return RedirectResponse(url="/?error=no_email")
        
        user_id = user_info['email'].replace('@', '_').replace('.', '_')
        
        save_user_credentials(user_id, credentials)
        
        request.session.clear()
        request.session['user_info'] = user_info
        request.session['user_id'] = user_id
        
        calendar_services.pop(user_id, None)
        service_last_refresh[user_id] = datetime.now()
        
        return RedirectResponse(url="/")
        
    except Exception:
        request.session.clear()
        return RedirectResponse(url="/?error=auth_failed")

@app.get("/check-connection")
async def check_connection_web(request: Request):
    user_id = request.session.get('user_id')
    if not user_id:
        return JSONResponse({"authenticated": False, "message": "Not logged in"})
    
    result = check_meeting_auth(user_id)
    return JSONResponse(result)

@app.get("/logout")
async def logout(request: Request):
    user_id = request.session.get('user_id')
    if user_id:
        calendar_services.pop(user_id, None)
        service_last_refresh.pop(user_id, None)
    
    request.session.clear()
    return RedirectResponse(url="/")

@app.get("/health")
def health_check():
    token_files = len([f for f in os.listdir(TOKEN_PATH) if f.startswith('token_')]) if os.path.exists(TOKEN_PATH) else 0
    
    return {
        "status": "healthy",
        "server": "Meeting CRUD MCP Server",
        "operations": ["CREATE", "READ", "UPDATE", "DELETE"],
        "authenticated_users": token_files,
        "active_services": len(calendar_services),
        "mcp_port": 8102,
        "web_interface": "http://localhost:8102"
    }

@app.get("/sse")
async def handle_sse(request: Request):
    async with transport.connect_sse(request.scope, request.receive, request._send) as (in_stream, out_stream):
        await mcp._mcp_server.run(in_stream, out_stream, mcp._mcp_server.create_initialization_options())

app.mount("/messages", transport.handle_post_message)

if __name__ == "__main__":
    import uvicorn
    
    async def main():
        print("Meeting CRUD MCP Server Starting...")
        print("Web interface: http://localhost:8102")
        print("MCP endpoint: http://localhost:8102/sse")
        
        config = uvicorn.Config(app, host="0.0.0.0", port=8102, reload=False, log_level="error")
        server = uvicorn.Server(config)
        
        try:
            await server.serve()
        except KeyboardInterrupt:
            print("Server shutdown by user")

    asyncio.run(main())